"""Admin panel authentication: passwords, TOTP, sessions and login rate limiting.

Discord-free and HTTP-free; `web` turns these results into cookies and pages.

A login is two steps. The password starts a short-lived partial session (stage
`totp`, or `enroll` for an admin who has no authenticator yet); a valid code
replaces it with a full session under a new token, so a token seen before the
second factor is never one that grants access.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pyotp
import segno

from .config import WebConfig
from .db import Admin, Database, Session

ISSUER = "wordlebot"
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024  # scrypt hashes anything, but don't let a request make it work hard
PARTIAL_SESSION = timedelta(minutes=5)  # time allowed between the password and the code
TOTP_PERIOD = 30
TOTP_DRIFT_STEPS = 1  # accept the previous and next code too, for clock drift

STAGE_TOTP = "totp"
STAGE_ENROLL = "enroll"
STAGE_FULL = "full"

# scrypt parameters (n=2^15, r=8 uses 32 MiB). Stored with each hash, so they can be
# raised later without invalidating existing passwords.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=_SCRYPT_MAXMEM
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(expected))


@functools.cache
def _dummy_hash() -> str:
    """Checked against when the username is unknown, so a wrong name takes as long as
    a wrong password and response times don't reveal which admins exist."""
    return hash_password(secrets.token_urlsafe(16))


def password_problem(password: str) -> str | None:
    """Why `password` can't be used, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Passwords must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Passwords must be at most {MAX_PASSWORD_LENGTH} characters."
    return None


def valid_username(username: str) -> bool:
    return 1 <= len(username) <= 64 and username.isprintable() and username == username.strip()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class LoginStep:
    """The outcome of one login step. `token` is set when a session was created."""

    token: str | None = None
    stage: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Enrollment:
    secret: str
    uri: str
    qr_svg: str  # inline <svg>, so the page needs no image source beyond itself


@dataclass(frozen=True)
class CurrentSession:
    token: str
    session: Session

    @property
    def username(self) -> str:
        return self.session.username

    @property
    def stage(self) -> str:
        return self.session.stage

    @property
    def csrf_token(self) -> str:
        return self.session.csrf_token


LOCKED_OUT = "Too many failed attempts. Try again later."
BAD_LOGIN = "Wrong username or password."
BAD_CODE = "That code didn't work. Codes can't be reused; wait for the next one."


class Auth:
    def __init__(
        self,
        db: Database,
        config: WebConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # --- rate limiting -----------------------------------------------------

    def _keys(self, username: str, address: str) -> list[str]:
        return [f"user:{username.casefold()}", f"ip:{address}"]

    def _window_start(self, now: datetime) -> str:
        return _iso(now - timedelta(minutes=self.config.login_lockout_minutes))

    def locked_out(self, username: str, address: str) -> bool:
        since = self._window_start(self.clock())
        return any(
            len(self.db.login_failures_since(key, since)) >= self.config.login_max_failures
            for key in self._keys(username, address)
        )

    def _fail(self, username: str, address: str, action: str) -> None:
        now = self.clock()
        self.db.prune_login_failures(_iso(now - timedelta(days=1)))
        self.db.add_login_failure(self._keys(username, address), _iso(now))
        self.db.audit(username, action, "", address=address)
        if self.locked_out(username, address):
            self.db.audit(username, "login_locked", "too many failed attempts", address=address)

    # --- sessions ----------------------------------------------------------

    def _new_session(self, username: str, stage: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _iso(self.clock())
        self.db.add_session(_token_hash(token), username, stage, secrets.token_urlsafe(32), now)
        return token

    def session(self, token: str | None, touch: bool = True) -> CurrentSession | None:
        """The live session for a cookie token, or None. `touch` refreshes its idle timer."""
        if not token:
            return None
        key = _token_hash(token)
        session = self.db.session(key)
        if session is None:
            return None
        now = self.clock()
        created = datetime.fromisoformat(session.created_at)
        seen = datetime.fromisoformat(session.last_seen_at)
        lifetime = (
            timedelta(hours=self.config.session_hours) if session.stage == STAGE_FULL else PARTIAL_SESSION
        )
        admin = self.db.admin(session.username)
        if (
            admin is None
            or now >= created + lifetime
            or now >= seen + timedelta(minutes=self.config.session_idle_minutes)
        ):
            self.db.delete_session(key)
            return None
        if touch:
            self.db.touch_session(key, _iso(now))
        return CurrentSession(token, session)

    def logout(self, token: str) -> None:
        self.db.delete_session(_token_hash(token))

    def logout_everywhere(self, username: str) -> int:
        return self.db.delete_sessions(username)

    def prune_sessions(self) -> None:
        now = self.clock()
        self.db.delete_sessions_before(
            _iso(now - timedelta(hours=self.config.session_hours)),
            _iso(now - timedelta(minutes=self.config.session_idle_minutes)),
        )

    # --- login -------------------------------------------------------------

    async def login(self, username: str, password: str, address: str) -> LoginStep:
        """Check a password; on success start a partial session (stage totp or enroll)."""
        username = username.strip()
        self.prune_sessions()
        if self.locked_out(username, address):
            self.db.audit(username, "login_refused", "locked out", address=address)
            return LoginStep(error=LOCKED_OUT)
        admin = self.db.admin(username) if valid_username(username) else None
        password = password[:MAX_PASSWORD_LENGTH]
        # scrypt takes tens of milliseconds; keep the event loop (and Discord) running.
        ok = await asyncio.to_thread(
            verify_password, password, admin.password_hash if admin else _dummy_hash()
        )
        if admin is None or not ok:
            self._fail(username, address, "login_failed")
            return LoginStep(error=BAD_LOGIN)
        if not admin.totp_enabled:
            if admin.totp_secret is None:
                self.db.set_admin_totp(admin.username, pyotp.random_base32(), enabled=False)
            return LoginStep(self._new_session(admin.username, STAGE_ENROLL), STAGE_ENROLL)
        return LoginStep(self._new_session(admin.username, STAGE_TOTP), STAGE_TOTP)

    def _check_code(self, admin: Admin, code: str) -> bool:
        """Whether `code` is valid now and unused; marks its time step used."""
        code = "".join(code.split())
        if admin.totp_secret is None or not (len(code) == 6 and code.isdigit()):
            return False
        totp = pyotp.TOTP(admin.totp_secret, interval=TOTP_PERIOD)
        now = self.clock().timestamp()
        for drift in range(-TOTP_DRIFT_STEPS, TOTP_DRIFT_STEPS + 1):
            moment = now + drift * TOTP_PERIOD
            if hmac.compare_digest(totp.at(moment), code):
                return self.db.use_totp_step(admin.username, int(moment // TOTP_PERIOD))
        return False

    def _second_factor(self, current: CurrentSession, code: str, address: str, stage: str) -> LoginStep:
        if current.stage != stage:
            return LoginStep(error="Start again from the login page.")
        username = current.username
        if self.locked_out(username, address):
            return LoginStep(error=LOCKED_OUT)
        admin = self.db.admin(username)
        if admin is None or not self._check_code(admin, code):
            self._fail(username, address, "totp_failed")
            return LoginStep(error=BAD_CODE)
        if stage == STAGE_ENROLL:
            self.db.set_admin_totp(username, admin.totp_secret, enabled=True)
            # set_admin_totp clears the replay guard; this code stays spent.
            self._check_code(self.db.admin(username), code)  # type: ignore[arg-type]
            self.db.audit(username, "totp_enrolled", "", address=address)
        self.logout(current.token)
        self.db.clear_login_failures(self._keys(username, address))
        self.db.audit(username, "login", "", address=address)
        return LoginStep(self._new_session(username, STAGE_FULL), STAGE_FULL)

    def verify_totp(self, current: CurrentSession, code: str, address: str) -> LoginStep:
        return self._second_factor(current, code, address, STAGE_TOTP)

    def enrollment(self, current: CurrentSession) -> Enrollment | None:
        """The authenticator setup for a session in the enroll stage."""
        if current.stage != STAGE_ENROLL:
            return None
        admin = self.db.admin(current.username)
        if admin is None or admin.totp_secret is None:
            return None
        uri = pyotp.TOTP(admin.totp_secret, interval=TOTP_PERIOD).provisioning_uri(
            name=admin.username, issuer_name=ISSUER
        )
        svg = segno.make(uri, error="m").svg_inline(scale=5, dark="#000", light="#fff")
        return Enrollment(admin.totp_secret, uri, svg)

    def confirm_enrollment(self, current: CurrentSession, code: str, address: str) -> LoginStep:
        return self._second_factor(current, code, address, STAGE_ENROLL)

    async def change_password(self, current: CurrentSession, old: str, new: str, address: str) -> str | None:
        """Change the session user's password; returns an error, or None on success.

        Every other session of that admin is ended.
        """
        admin = self.db.admin(current.username)
        if admin is None:
            return "Unknown admin."
        if self.locked_out(admin.username, address):
            return LOCKED_OUT
        ok = await asyncio.to_thread(verify_password, old[:MAX_PASSWORD_LENGTH], admin.password_hash)
        if not ok:
            self._fail(admin.username, address, "password_change_failed")
            return "The current password is wrong."
        if problem := password_problem(new):
            return problem
        self.db.set_admin_password(admin.username, await asyncio.to_thread(hash_password, new))
        self.db.delete_sessions(admin.username, keep=_token_hash(current.token))
        self.db.audit(admin.username, "password_changed", "other sessions ended", address=address)
        return None
