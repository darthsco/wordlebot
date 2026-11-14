import asyncio
import sys

import pyotp
import pytest
from test_web import PASSWORD, UtcClock

from wordlebot import __main__ as entry
from wordlebot.auth import (
    STAGE_ENROLL,
    STAGE_FULL,
    STAGE_TOTP,
    Auth,
    hash_password,
    password_problem,
    verify_password,
)
from wordlebot.config import WebConfig
from wordlebot.db import Database


def test_password_hashes_verify_and_are_salted():
    stored = hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert verify_password(PASSWORD, stored)
    assert not verify_password(PASSWORD + "!", stored)
    assert hash_password(PASSWORD) != stored
    assert not verify_password(PASSWORD, "garbage")
    assert not verify_password(PASSWORD, "bcrypt$1$2$3$AA==$AA==")


def test_password_policy():
    assert password_problem("short") is not None
    assert password_problem("x" * 12) is None
    assert password_problem("x" * 2000) is not None


def make_auth(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    db.add_admin("ada", hash_password(PASSWORD))
    clock = UtcClock()
    return Auth(db, WebConfig(), clock=clock), db, clock


def full_login(auth, db, clock):
    step = asyncio.run(auth.login("ada", PASSWORD, "1.2.3.4"))
    current = auth.session(step.token)
    code = pyotp.TOTP(db.admin("ada").totp_secret).at(clock.now.timestamp())
    if step.stage == STAGE_ENROLL:
        return auth.confirm_enrollment(current, code, "1.2.3.4")
    return auth.verify_totp(current, code, "1.2.3.4")


def test_codes_allow_one_step_of_drift_and_are_never_reused(tmp_path):
    auth, db, clock = make_auth(tmp_path)
    assert full_login(auth, db, clock).stage == STAGE_FULL
    totp = pyotp.TOTP(db.admin("ada").totp_secret)

    for offset, accepted in [(-60, False), (60, False), (-30, False), (30, True), (0, False)]:
        # -30 and 0 are steps at or before the one just used, so they're replays.
        step = asyncio.run(auth.login("ada", PASSWORD, "1.2.3.4"))
        assert step.stage == STAGE_TOTP
        code = totp.at(clock.now.timestamp() + offset)
        result = auth.verify_totp(auth.session(step.token), code, "1.2.3.4")
        assert (result.stage == STAGE_FULL) is accepted, offset


def test_partial_sessions_expire_quickly(tmp_path):
    auth, db, clock = make_auth(tmp_path)
    step = asyncio.run(auth.login("ada", PASSWORD, "1.2.3.4"))
    clock.advance(minutes=6)
    assert auth.session(step.token) is None


def test_removing_an_admin_ends_their_sessions(tmp_path):
    auth, db, clock = make_auth(tmp_path)
    token = full_login(auth, db, clock).token
    assert auth.session(token) is not None
    db.remove_admin("ada")
    assert auth.session(token) is None


def cli(monkeypatch, tmp_path, *args, passwords=()):
    config = tmp_path / "config.toml"
    config.write_text(f'[storage]\ndatabase_path = "{tmp_path / "t.sqlite3"}"\n')
    answers = iter(passwords)
    monkeypatch.setattr(entry.getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(sys, "argv", ["wordlebot", "--config", str(config), "admin", *args])
    with pytest.raises(SystemExit) as exc:
        entry.main()
    return exc.value.code


def test_admin_cli_manages_accounts(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("WORDLEBOT_DATABASE_PATH", raising=False)
    # A too-short password is asked again; a mismatched repeat too.
    assert cli(monkeypatch, tmp_path, "add", "ada", passwords=["short", PASSWORD, "nope", PASSWORD, PASSWORD]) == 0
    assert cli(monkeypatch, tmp_path, "add", "ada", passwords=[PASSWORD, PASSWORD]) == 1
    assert cli(monkeypatch, tmp_path, "list") == 0
    assert "ada  (two-factor not set up yet)" in capsys.readouterr().out

    db = Database(tmp_path / "t.sqlite3")
    assert verify_password(PASSWORD, db.admin("ada").password_hash)
    db.set_admin_totp("ada", "SECRET", enabled=True)
    assert cli(monkeypatch, tmp_path, "reset-2fa", "ada") == 0
    assert not db.admin("ada").totp_enabled and db.admin("ada").totp_secret is None
    assert cli(monkeypatch, tmp_path, "passwd", "ada", passwords=["another password", "another password"]) == 0
    assert verify_password("another password", db.admin("ada").password_hash)
    assert cli(monkeypatch, tmp_path, "remove", "bob") == 1
    assert cli(monkeypatch, tmp_path, "remove", "ada") == 0
    assert db.admin_names() == []
    assert [e.action for e in db.audit_entries()] == [
        "admin_removed", "password_reset", "totp_reset", "admin_added",
    ]
    assert cli(monkeypatch, tmp_path, "add") == 2
