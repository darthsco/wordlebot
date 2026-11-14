"""The admin panel: an aiohttp app served from the bot's event loop.

HTTP only, like `bot.py` is Discord only: behaviour lives in `WordleService` and
`Auth`, and anything touching Discord goes through the bot's `panel_*` hooks.

The app speaks plain HTTP and expects to sit behind a TLS-terminating reverse
proxy (Caddy in compose.yaml) that sets `X-Forwarded-Proto` and `X-Forwarded-For`.
A request without `X-Forwarded-Proto: https` didn't come through the proxy and is
refused, so the panel is never usable over plain HTTP. The listening port must
only be reachable by the proxy, which is what makes those headers trustworthy.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import jinja2
from aiohttp import web

from ..auth import STAGE_ENROLL, STAGE_FULL, STAGE_TOTP, Auth, CurrentSession
from ..db import Starter
from ..game import puzzle_number
from ..service import (
    ADD,
    DELETE,
    OVERRIDE,
    RESTORE,
    STATUSES,
    Actor,
    AdminError,
    PuzzleInPlay,
    ScoreEdit,
    SubmissionsExist,
    WordleService,
    describe_score,
)
from ..version import version_info

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
# __Host- cookies must be Secure, Path=/ and have no Domain, so no other site or
# subdomain can set or shadow them.
SESSION_COOKIE = "__Host-wordlebot"
MAX_BODY = 64 * 1024
REASON_MAX = 500
GRID_SQUARES = str.maketrans({"a": "⬛", "p": "🟨", "c": "🟩"})

CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
    ]
)
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cache-Control": "no-store",
}


class Host(Protocol):
    """What the panel needs from the bot (see `WordleBot.panel_*`)."""

    started_at: datetime

    async def panel_send(self, text: str) -> int: ...
    async def panel_reroll(self, actor: Actor) -> Starter: ...
    async def panel_tick(self) -> None: ...
    async def panel_settle(self, actor: Actor) -> int: ...
    async def panel_update_reactions(self) -> None: ...
    def panel_gateway(self) -> tuple[bool, str | None, float | None]: ...


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

SESSION_KEY = web.RequestKey("session", CurrentSession)


@dataclass
class Panel:
    service: WordleService
    auth: Auth
    host: Host

    def __post_init__(self) -> None:
        self.templates = jinja2.Environment(
            loader=jinja2.FileSystemLoader(HERE / "templates"),
            autoescape=True,
            undefined=jinja2.StrictUndefined,
        )
        self.templates.filters["score"] = lambda s: describe_score(s, self.service.config.game.max_guesses)
        self.templates.filters["local"] = self._local_time
        self.templates.filters["squares"] = lambda grid: grid.translate(GRID_SQUARES)
        # One-shot messages shown on the next page, keyed by session token.
        self._flash: dict[str, str] = {}

    # --- plumbing -----------------------------------------------------------

    def app(self) -> web.Application:
        app = web.Application(
            middlewares=[self._security, self._authenticate], client_max_size=MAX_BODY
        )
        r = app.router
        r.add_get("/login", self.login_page)
        r.add_post("/login", self.login)
        r.add_get("/login/code", self.code_page)
        r.add_post("/login/code", self.code)
        r.add_get("/login/enroll", self.enroll_page)
        r.add_post("/login/enroll", self.enroll)
        r.add_post("/logout", self.logout)
        r.add_get("/", self.dashboard)
        r.add_get("/status", self.status_fragment)
        r.add_post("/actions/{name}", self.action)
        r.add_get("/scores", self.scores)
        r.add_get("/scores/deleted", self.deleted_scores)
        r.add_get("/scores/add", self.add_score_page)
        r.add_post("/scores/edit", self.edit_score)
        r.add_get("/config", self.config_page)
        r.add_post("/config", self.config_change)
        r.add_get("/audit", self.audit)
        r.add_get("/account", self.account)
        r.add_post("/account/password", self.change_password)
        r.add_post("/account/logout-all", self.logout_all)
        r.add_static("/static", HERE / "static", name="static", append_version=True)
        return app

    @staticmethod
    def address(request: web.Request) -> str:
        """The client's address. The proxy appends it as the last X-Forwarded-For entry;
        earlier entries came from the client and can't be trusted."""
        forwarded = request.headers.get("X-Forwarded-For", "")
        return forwarded.rsplit(",", 1)[-1].strip() or request.remote or "?"

    @web.middleware
    async def _security(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.headers.get("X-Forwarded-Proto") != "https":
            return web.Response(status=403, text="HTTPS required (use the panel's https:// address).")
        if request.method == "POST":
            # Same-origin POSTs only. SameSite=Strict cookies and CSRF tokens cover
            # signed-in forms; this also covers the login form itself.
            origin = request.headers.get("Origin")
            if origin != f"https://{request.host}":
                return web.Response(status=403, text="Cross-origin request refused.")
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if isinstance(response, web.HTTPException):
            raise response
        return response

    @web.middleware
    async def _authenticate(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        path = request.path
        # The dashboard's background refresh mustn't keep an idle session alive.
        current = self.auth.session(request.cookies.get(SESSION_COOKIE), touch=path != "/status")
        if current is not None:
            request[SESSION_KEY] = current
        if path.startswith("/static/") or path == "/login":
            return await handler(request)
        if current is None:
            raise self._redirect(request, "/login")
        if current.stage == STAGE_TOTP and path != "/login/code":
            raise self._redirect(request, "/login/code")
        if current.stage == STAGE_ENROLL and path != "/login/enroll":
            raise self._redirect(request, "/login/enroll")
        if current.stage == STAGE_FULL and path.startswith("/login/"):
            raise self._redirect(request, "/")
        if request.method == "POST":
            form = await request.post()
            sent = str(form.get("csrf", ""))
            if not hmac.compare_digest(sent.encode(), current.csrf_token.encode()):
                return web.Response(status=403, text="Invalid form token; reload the page and try again.")
        return await handler(request)

    @staticmethod
    def _redirect(request: web.Request, location: str) -> web.HTTPException:
        # htmx follows a 303 inside its request, which would put the login page into
        # a fragment; HX-Redirect makes it load the page instead.
        if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
            return web.HTTPOk(headers={"HX-Redirect": location})
        return web.HTTPSeeOther(location)

    def _session(self, request: web.Request) -> CurrentSession:
        return request[SESSION_KEY]

    def _actor(self, request: web.Request) -> Actor:
        return Actor(self._session(request).username, self.address(request))

    @staticmethod
    def _see_other(location: str, token: str | None = None) -> web.Response:
        """A redirect that sets the session cookie to `token`, or clears it ("")."""
        response = web.Response(status=303, headers={"Location": location})
        if token:
            response.set_cookie(
                SESSION_COOKIE, token, path="/", secure=True, httponly=True, samesite="Strict"
            )
        elif token == "":
            response.del_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="Strict")
        return response

    def _flash_next(self, request: web.Request, message: str) -> None:
        self._flash[self._session(request).token] = message

    def _local_time(self, value: datetime | str | None) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(self.service.config.schedule.tz).strftime("%Y-%m-%d %H:%M")

    def render(self, request: web.Request, template: str, status: int = 200, **context: Any) -> web.Response:
        current = request.get(SESSION_KEY)
        full = current is not None and current.stage == STAGE_FULL
        # Fragments (template names starting "_") leave the message for the next page.
        flash = self._flash.pop(current.token, None) if full and not template.startswith("_") else None
        html = self.templates.get_template(template).render(
            user=current.username if full else None,
            csrf=current.csrf_token if current else "",
            flash=flash,
            path=request.path,
            static=lambda name: str(request.app.router["static"].url_for(filename=name)),
            **context,
        )
        return web.Response(text=html, content_type="text/html", status=status)

    # --- login ----------------------------------------------------------------

    async def login_page(self, request: web.Request) -> web.StreamResponse:
        current = request.get(SESSION_KEY)
        if current is not None and current.stage == STAGE_FULL:
            raise web.HTTPSeeOther("/")
        return self.render(request, "login.html", error=None, username="")

    async def login(self, request: web.Request) -> web.StreamResponse:
        form = await request.post()
        username = str(form.get("username", ""))
        step = await self.auth.login(username, str(form.get("password", "")), self.address(request))
        if step.token is None:
            return self.render(request, "login.html", status=401, error=step.error, username=username)
        if current := request.get(SESSION_KEY):
            self.auth.logout(current.token)
        return self._see_other(
            "/login/enroll" if step.stage == STAGE_ENROLL else "/login/code", step.token
        )

    async def code_page(self, request: web.Request) -> web.StreamResponse:
        return self.render(request, "code.html", error=None)

    async def code(self, request: web.Request) -> web.StreamResponse:
        form = await request.post()
        step = self.auth.verify_totp(self._session(request), str(form.get("code", "")), self.address(request))
        if step.token is None:
            return self.render(request, "code.html", status=401, error=step.error)
        return self._see_other("/", step.token)

    async def enroll_page(self, request: web.Request, error: str | None = None) -> web.StreamResponse:
        enrollment = self.auth.enrollment(self._session(request))
        if enrollment is None:
            raise web.HTTPSeeOther("/login")
        return self.render(
            request, "enroll.html", status=401 if error else 200, error=error, enrollment=enrollment
        )

    async def enroll(self, request: web.Request) -> web.StreamResponse:
        form = await request.post()
        step = self.auth.confirm_enrollment(
            self._session(request), str(form.get("code", "")), self.address(request)
        )
        if step.token is None:
            return await self.enroll_page(request, step.error)
        return self._see_other("/", step.token)

    async def logout(self, request: web.Request) -> web.StreamResponse:
        current = self._session(request)
        self.auth.logout(current.token)
        self.auth.db.audit(current.username, "logout", "", address=self.address(request))
        return self._see_other("/login", "")

    # --- dashboard and actions -------------------------------------------------

    def _status_context(self) -> dict[str, Any]:
        connected, bot_user, latency = self.host.panel_gateway()
        now = datetime.now(timezone.utc)
        uptime = now - self.host.started_at
        return {
            "overview": self.service.overview(),
            "connected": connected,
            "bot_user": bot_user,
            "latency": latency,
            "uptime": str(uptime).split(".")[0],
            "version": version_info(markdown=False),
            "overrides_error": self.service.overrides_error,
        }

    async def dashboard(self, request: web.Request) -> web.StreamResponse:
        return self.render(request, "dashboard.html", **self._status_context())

    async def status_fragment(self, request: web.Request) -> web.StreamResponse:
        return self.render(request, "_status.html", **self._status_context())

    async def action(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        actor = self._actor(request)
        if name == "reroll":
            try:
                starter = await self.host.panel_reroll(actor)
            except PuzzleInPlay:
                message = "Not rerolled: the upcoming starter's puzzle has already started."
            except SubmissionsExist as refused:
                message = (
                    f"Not rerolled: {refused.count} submission(s) already exist for "
                    f"Wordle {refused.puzzle_number:,}."
                )
            except Exception:
                log.exception("Panel reroll failed")
                message = "Reroll failed; see the bot's log. A saved pick is announced by the next tick."
            else:
                message = f"Rerolled Wordle {starter.puzzle_number:,} to {starter.word.upper()}."
        elif name == "tick":
            await self.host.panel_tick()
            self.service.db.audit(actor.name, "reconcile", "ran a posting tick", address=actor.address)
            message = "Ran a posting tick; anything owed has been sent."
        elif name == "settle":
            try:
                settled = await self.host.panel_settle(actor)
            except Exception:
                log.exception("Panel solution sync failed")
                message = "Solution sync failed; see the bot's log."
            else:
                message = f"Solutions synced; {settled} pending score(s) settled."
        else:
            raise web.HTTPNotFound()
        self._flash_next(request, message)
        raise web.HTTPSeeOther("/")

    # --- scores ---------------------------------------------------------------

    @staticmethod
    def _int(value: Any, default: int | None = None) -> int | None:
        try:
            return int(str(value).strip().replace(",", ""))
        except (TypeError, ValueError):
            return default

    async def scores(self, request: web.Request) -> web.StreamResponse:
        service = self.service
        today_number = puzzle_number(service.today(), service.launch)
        user_id = self._int(request.query.get("user"))
        puzzle = self._int(request.query.get("puzzle"))
        if user_id is not None:
            rows = service.db.scores_for_user(user_id)
            title = f"Scores from user {user_id}"
        else:
            if puzzle is None:
                puzzle = today_number
            rows = service.db.scores_for_puzzle(puzzle)
            title = f"Wordle {puzzle:,}"
        starter = service.db.starter_for_puzzle(puzzle) if puzzle is not None and user_id is None else None
        answer = service.answer_for_panel(puzzle) if puzzle is not None and user_id is None else None
        return self.render(
            request,
            "scores.html",
            title=title,
            rows=rows,
            puzzle=puzzle,
            user_id=user_id,
            today_number=today_number,
            starter=starter,
            answer=answer,
            players=service.db.players(),
            statuses=STATUSES,
        )

    async def deleted_scores(self, request: web.Request) -> web.StreamResponse:
        return self.render(request, "deleted.html", rows=self.service.db.deleted_scores())

    async def add_score_page(self, request: web.Request) -> web.StreamResponse:
        return self.render(request, "add_score.html", error=None, form={})

    async def _plan(self, form: Any) -> ScoreEdit:
        action = str(form.get("action", ""))
        number = self._int(form.get("puzzle"))
        user_id = self._int(form.get("user_id"))
        if action == "add":
            return await self.service.plan_add(
                user_id or 0, str(form.get("user_name", "")), str(form.get("share", ""))
            )
        if action == "restore":
            deleted_id = self._int(form.get("deleted_id"))
            if deleted_id is None:
                raise AdminError("Missing deleted score.")
            return self.service.plan_restore(deleted_id)
        if number is None or user_id is None:
            raise AdminError("Missing puzzle or user.")
        if action == "delete":
            return self.service.plan_delete(number, user_id)
        if action == "override":
            return self.service.plan_override(number, user_id, str(form.get("status", "")))
        if action == "reverify":
            return self.service.plan_reverify(number, user_id)
        raise AdminError(f"Unknown action {action!r}.")

    async def edit_score(self, request: web.Request) -> web.StreamResponse:
        """Every score change: a first POST previews it, the confirm POST applies it."""
        form = await request.post()
        fields = {k: str(form.get(k, "")) for k in ("action", "puzzle", "user_id", "status",
                                                      "deleted_id", "user_name", "share", "reason")}
        reason = fields["reason"].strip()
        try:
            if len(reason) > REASON_MAX:
                raise AdminError(f"Keep the reason under {REASON_MAX} characters.")
            edit = await self._plan(form)
        except AdminError as exc:
            if fields["action"] == "add":
                return self.render(request, "add_score.html", status=400, error=str(exc), form=fields)
            self._flash_next(request, str(exc))
            raise web.HTTPSeeOther(self._back(fields)) from None
        if form.get("confirm") != "yes" or not reason:
            return self.render(
                request,
                "confirm.html",
                preview=self.service.preview(edit),
                fields=fields,
                labels={DELETE: "Delete", RESTORE: "Restore", OVERRIDE: "Change status", ADD: "Add"},
                error="A reason is required." if form.get("confirm") == "yes" else None,
            )
        try:
            await self.service.apply_edit(edit, self._actor(request), reason, self.host.panel_send)
        except AdminError as exc:
            self._flash_next(request, str(exc))
        else:
            await self.host.panel_update_reactions()
            done = {DELETE: "Deleted", RESTORE: "Restored", OVERRIDE: "Changed the status of", ADD: "Added"}
            self._flash_next(
                request,
                f"{done[edit.action]} {edit.score.user_name}'s score for Wordle {edit.score.puzzle_number:,}.",
            )
        raise web.HTTPSeeOther(
            f"/scores?{urlencode({'puzzle': edit.score.puzzle_number})}"
        )

    @staticmethod
    def _back(fields: dict[str, str]) -> str:
        if fields["action"] == "restore":
            return "/scores/deleted"
        if fields["puzzle"]:
            return f"/scores?{urlencode({'puzzle': fields['puzzle']})}"
        return "/scores"

    # --- config ----------------------------------------------------------------

    async def config_page(self, request: web.Request) -> web.StreamResponse:
        rows = self.service.config_rows()
        sections: dict[str, list[Any]] = {}
        for row in rows:
            sections.setdefault(row.section, []).append(row)
        return self.render(
            request, "config.html", sections=sections, overrides_error=self.service.overrides_error
        )

    async def config_change(self, request: web.Request) -> web.StreamResponse:
        form = await request.post()
        section, key = str(form.get("section", "")), str(form.get("key", ""))
        actor = self._actor(request)
        try:
            if form.get("reset") == "yes":
                await self.service.reset_config(section, key, actor, self.host.panel_send)
                message = f"Reset {section}.{key} to the file's value."
            else:
                await self.service.set_config(section, key, str(form.get("value", "")), actor, self.host.panel_send)
                message = f"Saved {section}.{key}."
        except AdminError as exc:
            message = f"Not saved: {exc}"
        await self.host.panel_update_reactions()
        self._flash_next(request, message)
        raise web.HTTPSeeOther(f"/config#{section}-{key}")

    # --- audit and account -----------------------------------------------------

    async def audit(self, request: web.Request) -> web.StreamResponse:
        before = self._int(request.query.get("before"))
        entries = self.service.db.audit_entries(100, before)
        return self.render(request, "audit.html", entries=entries)

    async def account(self, request: web.Request, error: str | None = None) -> web.StreamResponse:
        return self.render(request, "account.html", status=400 if error else 200, error=error)

    async def change_password(self, request: web.Request) -> web.StreamResponse:
        form = await request.post()
        new, repeat = str(form.get("new", "")), str(form.get("repeat", ""))
        if new != repeat:
            return await self.account(request, "The new passwords don't match.")
        error = await self.auth.change_password(
            self._session(request), str(form.get("old", "")), new, self.address(request)
        )
        if error:
            return await self.account(request, error)
        self._flash_next(request, "Password changed; your other sessions were signed out.")
        raise web.HTTPSeeOther("/account")

    async def logout_all(self, request: web.Request) -> web.StreamResponse:
        current = self._session(request)
        count = self.auth.logout_everywhere(current.username)
        self.auth.db.audit(current.username, "logout_all", f"{count} session(s) ended", address=self.address(request))
        return self._see_other("/login", "")


async def start(panel: Panel, host: str, port: int) -> web.AppRunner:
    """Serve the panel; the caller cleans up the returned runner on shutdown."""
    runner = web.AppRunner(panel.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Admin panel listening on %s:%d (behind the HTTPS proxy)", host, port)
    return runner
