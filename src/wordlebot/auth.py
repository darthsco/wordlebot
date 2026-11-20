"""Admin panel authentication: passkeys, sessions, enrollment links and rate limiting.

Discord-free and HTTP-free; `web` turns these results into cookies and JSON.

Admins sign in with a passkey (WebAuthn) and nothing else. Every ceremony requires
user verification (a fingerprint, face or device PIN), so a passkey is two factors on
its own: the device, and the person unlocking it. Without that flag it would be one.

An admin's first passkey comes from a one-time enrollment link made on the host
(`python -m wordlebot admin add|enroll NAME`), because accounts are CLI-only and
registering a passkey needs a browser. Further passkeys are added from the Account
page, after confirming with an existing one, so a stolen session can't quietly add
an attacker's passkey.

Challenges are kept in memory, single-use and short-lived. The browser sends each
back inside the signed client data, which is how a response is matched to its
challenge; a restart only means pressing the button again.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, parse_authentication_credential_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .config import WebConfig
from .db import Database, Passkey, Session

RP_NAME = "wordlebot"
CHALLENGE_LIFETIME = timedelta(minutes=5)
ENROLLMENT_LIFETIME = timedelta(hours=24)
MAX_PENDING = 1000  # outstanding challenges kept; the login button is unauthenticated
LABEL_MAX = 64

STAGE_FULL = "full"

LOGIN = "login"
REAUTH = "reauth"
REGISTER = "register"

LOCKED_OUT = "Too many failed attempts. Try again later."
BAD_PASSKEY = "That passkey wasn't accepted. Try again, or use another one."
BAD_LINK = "This enrollment link has expired or was already used. Ask for a new one."
NOT_REGISTERED = "The passkey couldn't be registered. Try again."

# Malformed or forged responses: the library's own errors, and bad base64 or JSON.
_REJECTED = (WebAuthnException, ValueError, KeyError, TypeError, AttributeError)


def valid_username(username: str) -> bool:
    return 1 <= len(username) <= 64 and username.isprintable() and username == username.strip()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _client_challenge(credential: dict[str, Any]) -> bytes:
    """The challenge a browser response answers, from its (not yet verified) client data."""
    raw = base64.urlsafe_b64decode(credential["response"]["clientDataJSON"] + "==")
    return base64url_to_bytes(json.loads(raw)["challenge"])


def clean_label(label: str, fallback: str) -> str:
    label = " ".join(label.split())[:LABEL_MAX]
    return label if label and label.isprintable() else fallback


@dataclass(frozen=True)
class LoginStep:
    """The outcome of a sign-in or enrollment. `token` is set when a session was created."""

    token: str | None = None
    error: str | None = None


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


@dataclass(frozen=True)
class _Pending:
    purpose: str
    username: str | None
    expires: datetime


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
        self._pending: dict[bytes, _Pending] = {}

    @property
    def rp_id(self) -> str:
        return self.config.domain

    @property
    def origin(self) -> str:
        return f"https://{self.config.domain}"

    # --- rate limiting -----------------------------------------------------

    def _keys(self, username: str, address: str) -> list[str]:
        keys = [f"ip:{address}"]
        if username:
            keys.append(f"user:{username.casefold()}")
        return keys

    def locked_out(self, username: str, address: str) -> bool:
        since = _iso(self.clock() - timedelta(minutes=self.config.login_lockout_minutes))
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

    def _new_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _iso(self.clock())
        self.db.add_session(_token_hash(token), username, STAGE_FULL, secrets.token_urlsafe(32), now)
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
        if (
            session.stage != STAGE_FULL
            or self.db.admin(session.username) is None
            or now >= created + timedelta(hours=self.config.session_hours)
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

    # --- challenges --------------------------------------------------------

    def _challenge(self, purpose: str, username: str | None) -> bytes:
        now = self.clock()
        for key in [k for k, p in self._pending.items() if p.expires <= now]:
            del self._pending[key]
        while len(self._pending) >= MAX_PENDING:
            del self._pending[next(iter(self._pending))]  # oldest first
        challenge = secrets.token_bytes(32)
        self._pending[challenge] = _Pending(purpose, username, now + CHALLENGE_LIFETIME)
        return challenge

    def _take(self, credential: dict[str, Any], purpose: str, username: str | None) -> bytes | None:
        """Spend the challenge `credential` answers, if it is live and was issued for
        this purpose (and admin). A challenge is spent even when that check fails."""
        challenge = _client_challenge(credential)
        pending = self._pending.pop(challenge, None)
        if pending is None or pending.expires <= self.clock():
            return None
        if pending.purpose != purpose or pending.username != username:
            return None
        return challenge

    @property
    def _timeout_ms(self) -> int:
        return int(CHALLENGE_LIFETIME.total_seconds() * 1000)

    @staticmethod
    def _descriptor(passkey: Passkey) -> PublicKeyCredentialDescriptor:
        transports = []
        for name in passkey.transports:
            try:
                transports.append(AuthenticatorTransport(name))
            except ValueError:
                pass  # a transport this library doesn't know; it's only a hint
        return PublicKeyCredentialDescriptor(id=base64url_to_bytes(passkey.credential_id), transports=transports)

    def _request_options(self, purpose: str, username: str | None) -> dict[str, Any]:
        allowed = [self._descriptor(p) for p in self.db.passkeys(username)] if username else None
        options = generate_authentication_options(
            rp_id=self.rp_id,
            challenge=self._challenge(purpose, username),
            timeout=self._timeout_ms,
            allow_credentials=allowed,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return json.loads(options_to_json(options))

    def _creation_options(self, username: str) -> dict[str, Any]:
        admin = self.db.admin(username)
        assert admin is not None
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name=RP_NAME,
            user_name=username,
            user_id=base64url_to_bytes(admin.user_handle),
            challenge=self._challenge(REGISTER, username),
            timeout=self._timeout_ms,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[self._descriptor(p) for p in self.db.passkeys(username)],
        )
        return json.loads(options_to_json(options))

    def _assert(
        self, credential: Any, purpose: str, username: str | None
    ) -> tuple[Passkey | None, str]:
        """Check a sign-in response. Returns the passkey used (None if refused) and the
        admin it claimed to be, as far as known, for the failure count."""
        claimed = username or ""
        try:
            challenge = self._take(credential, purpose, username)
            parsed = parse_authentication_credential_json(credential)
            passkey = self.db.passkey(bytes_to_base64url(parsed.raw_id))
            if passkey is not None:
                claimed = passkey.username
            if challenge is None or passkey is None:
                return None, claimed
            admin = self.db.admin(passkey.username)
            if admin is None or (username is not None and passkey.username != username):
                return None, claimed
            handle = parsed.response.user_handle
            if handle is not None and bytes_to_base64url(handle) != admin.user_handle:
                return None, claimed
            verified = verify_authentication_response(
                credential=parsed,
                expected_challenge=challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.origin,
                credential_public_key=passkey.public_key,
                credential_current_sign_count=passkey.sign_count,
                require_user_verification=True,
            )
        except _REJECTED:
            return None, claimed
        self.db.use_passkey(passkey.credential_id, verified.new_sign_count, _iso(self.clock()))
        return passkey, passkey.username

    def _register(self, username: str, credential: Any, label: str) -> Passkey | None:
        try:
            challenge = self._take(credential, REGISTER, username)
            if challenge is None:
                return None
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.origin,
                require_user_verification=True,
            )
            transports = [t for t in credential["response"].get("transports") or [] if isinstance(t, str)]
        except _REJECTED:
            return None
        credential_id = bytes_to_base64url(verified.credential_id)
        label = clean_label(label, f"Passkey {len(self.db.passkeys(username)) + 1}")
        if not self.db.add_passkey(
            credential_id, username, verified.credential_public_key, verified.sign_count,
            transports[:8], label,
        ):
            return None  # already registered (to anyone)
        return self.db.passkey(credential_id)

    # --- sign-in -----------------------------------------------------------

    def login_options(self, address: str) -> dict[str, Any] | None:
        """Options for `navigator.credentials.get`, or None while the address is locked out.

        No username: the browser offers whichever passkeys it has for this site.
        """
        if self.locked_out("", address):
            return None
        return self._request_options(LOGIN, None)

    def login(self, credential: Any, address: str) -> LoginStep:
        self.prune_sessions()
        if self.locked_out("", address):
            self.db.audit("", "login_refused", "locked out", address=address)
            return LoginStep(error=LOCKED_OUT)
        passkey, claimed = self._assert(credential, LOGIN, None)
        if passkey is None:
            self._fail(claimed, address, "login_failed")
            return LoginStep(error=BAD_PASSKEY)
        if self.locked_out(passkey.username, address):
            self.db.audit(passkey.username, "login_refused", "locked out", address=address)
            return LoginStep(error=LOCKED_OUT)
        self.db.clear_login_failures(self._keys(passkey.username, address))
        self.db.audit(passkey.username, "login", f"passkey {passkey.label}", address=address)
        return LoginStep(self._new_session(passkey.username))

    # --- enrollment links ----------------------------------------------------

    def issue_enrollment(self, username: str) -> str:
        """A new one-time link token for `username`; it replaces any earlier one."""
        token = secrets.token_urlsafe(32)
        expires = _iso(self.clock() + ENROLLMENT_LIFETIME)
        self.db.set_enrollment_token(username, _token_hash(token), expires)
        return token

    def enrollment_user(self, token: str) -> str | None:
        """The admin a live enrollment token belongs to."""
        return self.db.enrollment_token(_token_hash(token), _iso(self.clock()))

    def enroll_options(self, token: str, address: str) -> dict[str, Any] | None:
        if self.locked_out("", address):
            return None
        username = self.enrollment_user(token)
        return None if username is None else self._creation_options(username)

    def enroll(self, token: str, credential: Any, label: str, address: str) -> LoginStep:
        """Register a passkey through an enrollment link, spend the link, and sign in."""
        if self.locked_out("", address):
            return LoginStep(error=LOCKED_OUT)
        username = self.enrollment_user(token)
        if username is None:
            self._fail("", address, "enroll_failed")
            return LoginStep(error=BAD_LINK)
        passkey = self._register(username, credential, label)
        if passkey is None:
            self._fail(username, address, "enroll_failed")
            return LoginStep(error=NOT_REGISTERED)
        self.db.delete_enrollment_token(_token_hash(token))
        self.db.audit(username, "passkey_registered", f"{passkey.label} (enrollment link)", address=address)
        self.db.clear_login_failures(self._keys(username, address))
        self.db.audit(username, "login", f"passkey {passkey.label}", address=address)
        return LoginStep(self._new_session(username))

    # --- account: managing your passkeys ---------------------------------------

    def passkeys(self, current: CurrentSession) -> list[Passkey]:
        return self.db.passkeys(current.username)

    def add_options(self, current: CurrentSession, address: str) -> dict[str, Any] | None:
        """The first step of adding a passkey: `{"reauth": ...}` to confirm with an
        existing passkey, or `{"create": ...}` straight away for an admin who has none
        (signed in before passkeys, or since reset). None while locked out."""
        if self.locked_out(current.username, address):
            return None
        if self.db.passkeys(current.username):
            return {"reauth": self._request_options(REAUTH, current.username)}
        return {"create": self._creation_options(current.username)}

    def reauth(self, current: CurrentSession, credential: Any, address: str) -> dict[str, Any] | str:
        """Confirm with an existing passkey; returns `{"create": ...}` or an error."""
        if self.locked_out(current.username, address):
            return LOCKED_OUT
        passkey, _ = self._assert(credential, REAUTH, current.username)
        if passkey is None:
            self._fail(current.username, address, "reauth_failed")
            return BAD_PASSKEY
        return {"create": self._creation_options(current.username)}

    def add_passkey(self, current: CurrentSession, credential: Any, label: str, address: str) -> str | None:
        """Register a passkey for the signed-in admin; returns an error, or None."""
        passkey = self._register(current.username, credential, label)
        if passkey is None:
            return NOT_REGISTERED
        self.db.audit(current.username, "passkey_registered", passkey.label, address=address)
        return None

    def remove_passkey(self, current: CurrentSession, credential_id: str, address: str) -> str | None:
        passkey = self.db.passkey(credential_id)
        if passkey is None or passkey.username != current.username:
            return "No such passkey."
        if not self.db.remove_passkey(current.username, credential_id):
            return "You can't remove your only passkey."
        self.db.audit(current.username, "passkey_removed", passkey.label, address=address)
        return None
