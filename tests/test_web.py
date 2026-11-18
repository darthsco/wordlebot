import asyncio
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_bot import Share, make_bot
from test_golf import ROUND_DONE, score
from test_service import GOOD_SHARE, SOLUTION, TODAY
from test_solutions import FakeSource

from wordlebot.auth import Auth, hash_password
from wordlebot.config import GolfConfig, WebConfig
from wordlebot.service import REROLL_PREFIX
from wordlebot.web import SESSION_COOKIE, Panel

HOST = "panel.test"
PASSWORD = "correct horse battery"


class UtcClock:
    def __init__(self):
        self.now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class Browser:
    """A test client that behaves like a browser behind the HTTPS proxy: it sends the
    proxy's headers and a same-origin Origin, and keeps the session cookie."""

    def __init__(self, client, address="203.0.113.7"):
        self.client = client
        self.address = address
        self.cookie = None
        self.csrf = ""

    def _headers(self, extra):
        headers = {
            "Host": HOST,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": self.address,
            "Origin": f"https://{HOST}",
        }
        if self.cookie:
            headers["Cookie"] = f"{SESSION_COOKIE}={self.cookie}"
        headers.update(extra)
        return {k: v for k, v in headers.items() if v is not None}

    async def _done(self, response):
        text = await response.text()
        if SESSION_COOKIE in response.cookies:
            self.cookie = response.cookies[SESSION_COOKIE].value or None
        if match := re.search(r'name="csrf" value="([^"]*)"', text):
            self.csrf = match[1]
        return response, text

    async def get(self, path, **headers):
        response = await self.client.get(path, headers=self._headers(headers), allow_redirects=False)
        return await self._done(response)

    async def post(self, path, data=None, csrf=True, **headers):
        data = dict(data or {})
        if csrf:
            data.setdefault("csrf", self.csrf)
        response = await self.client.post(
            path, data=data, headers=self._headers(headers), allow_redirects=False
        )
        return await self._done(response)


class Env:
    def __init__(self, tmp_path, **bot_kwargs):
        self.bot, self.clock, self.channel = make_bot(tmp_path, **bot_kwargs)
        self.service = self.bot.service
        self.service.source = FakeSource({TODAY: SOLUTION})
        self.db = self.service.db
        self.utc = UtcClock()
        self.auth = Auth(self.db, WebConfig(login_max_failures=3), clock=self.utc)
        self.panel = Panel(self.service, self.auth, self.bot)
        self.db.add_admin("ada", hash_password(PASSWORD))

    def code(self, username="ada", offset=0):
        secret = self.db.admin(username).totp_secret
        return pyotp.TOTP(secret).at(self.utc.now.timestamp() + offset)

    async def login(self, browser, username="ada"):
        await browser.get("/login")
        response, _ = await browser.post("/login", {"username": username, "password": PASSWORD}, csrf=False)
        assert response.status == 303
        if response.headers["Location"] == "/login/enroll":
            await browser.get("/login/enroll")
            response, _ = await browser.post("/login/enroll", {"code": self.code(username)})
        else:
            await browser.get("/login/code")
            response, _ = await browser.post("/login/code", {"code": self.code(username)})
        assert response.status == 303 and response.headers["Location"] == "/"
        self.utc.advance(seconds=30)  # the next login needs a fresh code
        await browser.get("/")


def run(tmp_path, scenario, **bot_kwargs):
    env = Env(tmp_path, **bot_kwargs)

    async def main():
        async with TestClient(TestServer(env.panel.app())) as client:
            await scenario(env, Browser(client))

    asyncio.run(main())
    return env


def test_plain_http_and_cross_origin_posts_are_refused(tmp_path):
    async def scenario(env, browser):
        response, text = await browser.get("/login", **{"X-Forwarded-Proto": None})
        assert response.status == 403 and "HTTPS required" in text
        response, _ = await browser.get("/login", **{"X-Forwarded-Proto": "http"})
        assert response.status == 403
        response, text = await browser.post(
            "/login", {"username": "ada", "password": PASSWORD}, csrf=False, Origin="https://evil.test"
        )
        assert response.status == 403 and "Cross-origin" in text
        # A browser that withholds the origin is believed only on Sec-Fetch-Site.
        login = {"username": "ada", "password": PASSWORD}
        for origin, fetch_site, allowed in [
            ("null", "same-origin", True),
            (None, "same-origin", True),
            ("null", "cross-site", False),
            ("null", None, False),
            (None, None, False),
        ]:
            response, _ = await browser.post(
                "/login", login, csrf=False, Origin=origin, **{"Sec-Fetch-Site": fetch_site}
            )
            assert (response.status != 403) is allowed, (origin, fetch_site)
        response, _ = await browser.get("/login")
        assert response.status == 200
        # "no-referrer" would make browsers send `Origin: null` on the login form.
        assert response.headers["Referrer-Policy"] == "same-origin"
        assert "max-age" in response.headers["Strict-Transport-Security"]
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]

    run(tmp_path, scenario)


def test_pages_need_a_full_session(tmp_path):
    async def scenario(env, browser):
        for path in ("/", "/scores", "/config", "/audit", "/account", "/login/code"):
            response, _ = await browser.get(path)
            assert response.status == 303 and response.headers["Location"] == "/login", path
        # htmx background requests get a full-page redirect instead.
        response, _ = await browser.get("/status", **{"HX-Request": "true"})
        assert response.headers["HX-Redirect"] == "/login"

    run(tmp_path, scenario)


def test_first_login_enrolls_two_factor_then_requires_it(tmp_path):
    async def scenario(env, browser):
        response, _ = await browser.post("/login", {"username": "ada", "password": PASSWORD}, csrf=False)
        assert response.headers["Location"] == "/login/enroll"
        set_cookie = response.headers["Set-Cookie"]
        assert "Secure" in set_cookie and "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie
        partial = browser.cookie

        # The partial session can't reach the panel.
        response, _ = await browser.get("/")
        assert response.headers["Location"] == "/login/enroll"
        response, text = await browser.get("/login/enroll")
        assert "<svg" in text and env.db.admin("ada").totp_secret in text

        response, text = await browser.post("/login/enroll", {"code": "000000"})
        assert response.status == 401 and "That code didn&#39;t work" in text
        response, _ = await browser.post("/login/enroll", {"code": env.code()})
        assert response.status == 303
        assert browser.cookie != partial  # a new token once both factors passed
        assert env.db.admin("ada").totp_enabled
        response, text = await browser.get("/")
        assert response.status == 200 and "Dashboard" in text

        # The old partial token is dead.
        browser.cookie = partial
        response, _ = await browser.get("/")
        assert response.headers["Location"] == "/login"

        # Second login: password, then a code; a reused code is refused.
        used = env.code()
        response, _ = await browser.post("/login", {"username": "ada", "password": PASSWORD}, csrf=False)
        assert response.headers["Location"] == "/login/code"
        await browser.get("/login/code")
        response, _ = await browser.post("/login/code", {"code": used})
        assert response.status == 401
        env.utc.advance(seconds=30)
        response, _ = await browser.post("/login/code", {"code": env.code()})
        assert response.status == 303 and response.headers["Location"] == "/"

    env = run(tmp_path, scenario)
    actions = [e.action for e in env.db.audit_entries()]
    assert actions.count("login") == 2 and "totp_enrolled" in actions and "totp_failed" in actions


def test_wrong_passwords_lock_out_the_user_and_address(tmp_path):
    async def scenario(env, browser):
        for _ in range(3):
            response, text = await browser.post("/login", {"username": "ada", "password": "nope"}, csrf=False)
            assert response.status == 401 and "Wrong username or password" in text
        # Even the right password is refused while locked out, from any address.
        other = Browser(browser.client, address="198.51.100.9")
        response, text = await other.post("/login", {"username": "ada", "password": PASSWORD}, csrf=False)
        assert response.status == 401 and "Too many" in text
        # The address is locked out for other usernames too.
        response, text = await browser.post("/login", {"username": "bob", "password": "x"}, csrf=False)
        assert "Too many" in text
        env.utc.advance(minutes=16)
        await env.login(other)

    env = run(tmp_path, scenario)
    actions = [e.action for e in env.db.audit_entries()]
    assert actions.count("login_failed") == 3 and "login_locked" in actions and "login" in actions


def test_unknown_user_looks_like_a_wrong_password(tmp_path):
    async def scenario(env, browser):
        response, text = await browser.post("/login", {"username": "mallory", "password": PASSWORD}, csrf=False)
        assert response.status == 401 and "Wrong username or password" in text

    run(tmp_path, scenario)


def test_sessions_expire_when_idle_and_after_their_lifetime(tmp_path):
    async def scenario(env, browser):
        await env.login(browser)
        env.utc.advance(minutes=59)
        response, _ = await browser.get("/")
        assert response.status == 200
        # Background polling doesn't count as activity.
        env.utc.advance(minutes=30)
        await browser.get("/status", **{"HX-Request": "true"})
        env.utc.advance(minutes=31)
        response, _ = await browser.get("/")
        assert response.headers["Location"] == "/login"

        await env.login(browser)
        for _ in range(14):  # 12h50m of activity, never idle for an hour
            env.utc.advance(minutes=55)
            response, _ = await browser.get("/")
        assert response.headers["Location"] == "/login"  # 12 hours at most

    run(tmp_path, scenario)


def test_posts_need_the_csrf_token(tmp_path):
    async def scenario(env, browser):
        await env.login(browser)
        response, text = await browser.post("/actions/tick", {"csrf": "forged"})
        assert response.status == 403 and "form token" in text
        response, _ = await browser.post("/actions/tick", csrf=False)
        assert response.status == 403

    run(tmp_path, scenario)


def test_reroll_from_the_panel_is_audited_and_can_be_announced(tmp_path):
    async def scenario(env, browser):
        await env.login(browser)
        env.service.config = env.service.config  # announce.reroll is off by default
        response, _ = await browser.post("/actions/reroll")
        assert response.status == 303
        _, text = await browser.get("/")
        assert "Rerolled Wordle" in text
        assert env.channel.sent[-1].startswith(REROLL_PREFIX)
        assert "admin panel" not in env.channel.sent[-1]

        await browser.get("/config")
        await browser.post("/config", {"section": "announce", "key": "reroll", "value": "true"})
        await browser.get("/")
        await browser.post("/actions/reroll")
        assert "Rerolled from the admin panel by ada" in env.channel.sent[-1]

    env = run(tmp_path, scenario, hour=10)
    assert [e.action for e in env.db.audit_entries()][:3] == ["reroll", "config_set", "reroll"]


def test_score_delete_needs_a_reason_and_confirmation_and_can_be_restored(tmp_path):
    async def scenario(env, browser):
        channel = env.channel
        share = Share(channel, 501, 7, GOOD_SHARE)
        await env.bot.on_message(share)
        assert share.reactions == ["✅"]
        await env.login(browser)

        await browser.get("/scores?puzzle=12")
        edit = {"action": "delete", "puzzle": "12", "user_id": "7"}
        response, text = await browser.post("/scores/edit", edit)
        assert response.status == 200 and "Confirm: Delete" in text and "not on the board" in text
        assert env.db.score_for(12, 7) is not None  # nothing happens before confirmation

        response, text = await browser.post("/scores/edit", {**edit, "confirm": "yes", "reason": "  "})
        assert "A reason is required" in text and env.db.score_for(12, 7) is not None

        response, _ = await browser.post("/scores/edit", {**edit, "confirm": "yes", "reason": "duplicate account"})
        assert response.status == 303
        assert env.db.score_for(12, 7) is None
        assert share.reactions == []  # the ✅ came off

        _, text = await browser.get("/scores/deleted")
        assert "duplicate account" in text
        deleted_id = env.db.deleted_scores()[0].id
        restore = {"action": "restore", "deleted_id": str(deleted_id), "puzzle": "12"}
        _, text = await browser.post("/scores/edit", restore)
        assert "Confirm: Restore" in text
        await browser.post("/scores/edit", {**restore, "confirm": "yes", "reason": "my mistake"})
        assert env.db.score_for(12, 7).status == "verified"
        assert share.reactions == ["✅"]

    env = run(tmp_path, scenario, hour=10)
    entries = env.db.audit_entries()
    assert [(e.action, e.reason) for e in entries[:2]] == [
        ("score_restore", "my mistake"),
        ("score_delete", "duplicate account"),
    ]


def test_override_changes_the_reaction_and_reverify_undoes_it(tmp_path):
    async def scenario(env, browser):
        share = Share(env.channel, 501, 7, GOOD_SHARE)
        await env.bot.on_message(share)
        await env.login(browser)
        await browser.get("/scores")
        edit = {"action": "override", "puzzle": "12", "user_id": "7", "status": "mismatch"}
        await browser.post("/scores/edit", edit)
        await browser.post("/scores/edit", {**edit, "confirm": "yes", "reason": "opened with SLATE, admitted it"})
        assert env.db.score_for(12, 7).status == "mismatch"
        assert share.reactions == ["❌"]

        edit = {"action": "reverify", "puzzle": "12", "user_id": "7"}
        _, text = await browser.post("/scores/edit", edit)
        assert "Confirm: Change status" in text
        await browser.post("/scores/edit", {**edit, "confirm": "yes", "reason": "undo"})
        assert env.db.score_for(12, 7).status == "verified"
        assert share.reactions == ["✅"]

    run(tmp_path, scenario, hour=10)


def test_confirm_is_refused_if_the_score_changed_meanwhile(tmp_path):
    async def scenario(env, browser):
        await env.bot.on_message(Share(env.channel, 501, 7, GOOD_SHARE))
        await env.login(browser)
        await browser.get("/scores")
        edit = {"action": "override", "puzzle": "12", "user_id": "7", "status": "mismatch"}
        await browser.post("/scores/edit", edit)
        # The score goes away before the admin confirms: the edit is planned again
        # from the form on confirm, so it fails instead of acting on stale data.
        env.db.delete_score(12, 7, "x", "y")
        response, _ = await browser.post("/scores/edit", {**edit, "confirm": "yes", "reason": "r"})
        _, text = await browser.get(response.headers["Location"])
        assert "No score from user 7" in text

    run(tmp_path, scenario, hour=10)


def test_manual_add_is_verified_like_a_share(tmp_path):
    async def scenario(env, browser):
        await env.login(browser)
        await browser.get("/scores/add")
        form = {"action": "add", "user_id": "42", "user_name": "late <b>Larry</b>", "share": GOOD_SHARE}
        _, text = await browser.post("/scores/edit", form)
        assert "Confirm: Add" in text and "2/6 (verified)" in text
        assert "<b>Larry" not in text  # escaped
        await browser.post("/scores/edit", {**form, "confirm": "yes", "reason": "bot was down"})
        score = env.db.score_for(12, 42)
        assert score.status == "verified" and score.message_id is None

        response, text = await browser.post("/scores/edit", {**form, "share": "hello"})
        assert response.status == 400 and "isn&#39;t a Wordle share" in text

    run(tmp_path, scenario, hour=10)


def test_config_edits_apply_at_once_and_reset_to_the_file(tmp_path):
    async def scenario(env, browser):
        await env.login(browser)
        _, text = await browser.get("/config")
        assert 'name="key" value="strategy"' in text
        assert 'name="key" value="guild_id"' not in text  # read-only

        await browser.post("/config", {"section": "scores", "key": "min_games_weekly", "value": "2"})
        assert env.service.config.scores.min_games_weekly == 2
        assert env.bot.config.scores.min_games_weekly == 2

        response, _ = await browser.post("/config", {"section": "scores", "key": "min_games_weekly", "value": "0"})
        _, text = await browser.get(response.headers["Location"])
        assert "Not saved" in text and "must be &gt;= 1" in text
        assert env.service.config.scores.min_games_weekly == 2

        response, _ = await browser.post("/config", {"section": "discord", "key": "guild_id", "value": "5"})
        _, text = await browser.get(response.headers["Location"])
        assert "can&#39;t be edited" in text

        await browser.post("/config", {"section": "scores", "key": "min_games_weekly", "reset": "yes"})
        assert env.service.config.scores.min_games_weekly == 3
        assert env.db.config_overrides() == {}

    env = run(tmp_path, scenario)
    assert [e.action for e in env.db.audit_entries()][:2] == ["config_reset", "config_set"]


def test_change_password_ends_other_sessions(tmp_path):
    async def scenario(env, browser):
        other = Browser(browser.client)
        await env.login(other)
        await env.login(browser)
        await browser.get("/account")
        response, text = await browser.post("/account/password", {"old": "wrong", "new": "x" * 12, "repeat": "x" * 12})
        assert "current password is wrong" in text
        await browser.get("/account")
        response, _ = await browser.post(
            "/account/password", {"old": PASSWORD, "new": "new password 123", "repeat": "new password 123"}
        )
        assert response.status == 303
        response, _ = await browser.get("/")
        assert response.status == 200
        response, _ = await other.get("/")
        assert response.headers["Location"] == "/login"

    run(tmp_path, scenario)


def test_dashboard_hides_answers_until_they_may_be_shown(tmp_path):
    async def scenario(env, browser):
        await env.bot.on_message(Share(env.channel, 501, 7, GOOD_SHARE))  # caches today's solution
        await env.login(browser)
        _, text = await browser.get("/")
        assert SOLUTION.upper() not in text
        _, text = await browser.get("/scores?puzzle=12")
        assert SOLUTION.upper() not in text
        env.clock.set(TODAY + timedelta(days=2), 10)
        _, text = await browser.get("/scores?puzzle=12")
        assert f'class="spoiler" tabindex="0">{SOLUTION.upper()}' in text

    run(tmp_path, scenario, hour=10)


def test_golf_tab_shows_the_current_round_then_earlier_ones(tmp_path):
    async def scenario(env, browser):
        env.db.add_score(score(12, 1, 3, name="<Ada>"))
        env.db.add_score(score(30, 2, 2, name="Bob"))
        env.clock.set(ROUND_DONE + timedelta(days=5), 10)
        await env.login(browser)
        _, text = await browser.get("/golf")
        nav = text[text.index("<nav>"):text.index("</nav>")]
        assert nav.index('"/scores"') < nav.index('"/golf"') < nav.index('"/config"')
        assert text.index("Round 2 <span") < text.index("Round 1 <span")
        assert "&lt;Ada&gt;" in text and "<Ada>" not in text
        current, earlier = text.split("Round 1 <span")
        # Every card has all 18 holes; the current one shades the holes still to come.
        assert current.count('<th scope="col">') == earlier.count('<th scope="col">') == 18
        assert current.count('<td class="future">') == 13 and "future" not in earlier
        assert '<details open>' in current and '<details open>' not in earlier
        assert '<span class="mark eagle" title="eagle">2</span>' in current  # Bob's hole 1
        assert current.count('<span class="mark missed"') == 4
        assert '<td class="sum">30</td>' in current  # Out: 2 + 4 missed at 7
        assert '<span class="mark birdie" title="birdie">3</span>' in earlier  # Ada's hole 1


        env.service.config = replace(env.service.config, golf=GolfConfig(enabled=False))
        _, text = await browser.get("/")
        assert '"/golf"' not in text
        response, _ = await browser.get("/golf")
        assert response.status == 404

    run(tmp_path, scenario, hour=10)


@pytest.mark.parametrize("path", ["/static/htmx.min.js", "/static/panel.css"])
def test_static_files_are_served_without_a_session(tmp_path, path):
    async def scenario(env, browser):
        response, _ = await browser.get(path)
        assert response.status == 200

    run(tmp_path, scenario)
