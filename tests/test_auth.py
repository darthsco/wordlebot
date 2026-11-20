import sqlite3
import sys

import pytest
from passkeys import HOST, Authenticator
from test_web import UtcClock

from wordlebot import __main__ as entry
from wordlebot.auth import BAD_LINK, BAD_PASSKEY, LOCKED_OUT, NOT_REGISTERED, STAGE_FULL, Auth
from wordlebot.config import WebConfig
from wordlebot.db import Database

ADDRESS = "1.2.3.4"


def make_auth(tmp_path, **web):
    db = Database(tmp_path / "t.sqlite3")
    db.add_admin("ada")
    clock = UtcClock()
    config = WebConfig(enabled=True, domain=HOST, **web)
    return Auth(db, config, clock=clock), db, clock


def enroll(auth, device, username="ada", label="Laptop", **create):
    token = auth.issue_enrollment(username)
    options = auth.enroll_options(token, ADDRESS)
    return auth.enroll(token, device.create(options, **create), label, ADDRESS)


def login(auth, device, address=ADDRESS, **get):
    options = auth.login_options(address)
    return auth.login(device.get(options, **get), address)


def test_an_enrollment_link_registers_a_passkey_and_signs_in_once(tmp_path):
    auth, db, clock = make_auth(tmp_path)
    device = Authenticator()
    token = auth.issue_enrollment("ada")
    assert auth.enrollment_user(token) == "ada"
    step = auth.enroll(token, device.create(auth.enroll_options(token, ADDRESS)), " My   laptop ", ADDRESS)
    assert step.token and auth.session(step.token).stage == STAGE_FULL
    [passkey] = db.passkeys("ada")
    assert passkey.label == "My laptop" and passkey.transports == ("internal", "hybrid")

    # The link is spent.
    assert auth.enrollment_user(token) is None and auth.enroll_options(token, ADDRESS) is None
    assert auth.enroll(token, {}, "", ADDRESS).error == BAD_LINK

    # The passkey signs in, with no username.
    step = login(auth, device)
    assert step.token and auth.session(step.token) is not None
    assert db.passkeys("ada")[0].last_used_at is not None
    actions = [e.action for e in db.audit_entries()]
    assert actions.count("login") == 2 and "passkey_registered" in actions


def test_enrollment_links_expire_and_a_new_one_replaces_the_old(tmp_path):
    auth, db, clock = make_auth(tmp_path)
    first = auth.issue_enrollment("ada")
    second = auth.issue_enrollment("ada")
    assert auth.enrollment_user(first) is None and auth.enrollment_user(second) == "ada"
    clock.advance(hours=24)
    assert auth.enrollment_user(second) is None


def test_registration_and_sign_in_require_user_verification(tmp_path):
    auth, db, clock = make_auth(tmp_path)
    device = Authenticator()
    assert enroll(auth, device, uv=False).error == NOT_REGISTERED
    assert db.passkeys("ada") == []
    assert enroll(auth, device).token
    assert login(auth, device, uv=False).error == BAD_PASSKEY


@pytest.mark.parametrize("where", ["origin", "rp_id"])
def test_responses_for_another_site_are_refused(tmp_path, where):
    auth, db, clock = make_auth(tmp_path)
    device = Authenticator()
    if where == "rp_id":
        assert enroll(auth, device, rp_id="evil.test").error == NOT_REGISTERED
        return
    assert enroll(auth, device, origin="https://evil.test").error == NOT_REGISTERED
    assert enroll(auth, device).token
    assert login(auth, device, origin="https://evil.test").error == BAD_PASSKEY


def test_challenges_are_single_use_short_lived_and_for_one_purpose(tmp_path):
    auth, db, clock = make_auth(tmp_path)
    device = Authenticator()
    enroll(auth, device)

    options = auth.login_options(ADDRESS)
    response = device.get(options)
    assert auth.login(response, ADDRESS).token
    assert auth.login(device.get(options), ADDRESS).error == BAD_PASSKEY  # replayed challenge

    options = auth.login_options(ADDRESS)
    clock.advance(minutes=5)
    assert auth.login(device.get(options), ADDRESS).error == BAD_PASSKEY  # expired

    # A challenge issued to re-confirm on the Account page can't sign in.
    current = auth.session(auth.login(device.get(auth.login_options(ADDRESS)), ADDRESS).token)
    reauth = auth.add_options(current, ADDRESS)["reauth"]
    assert auth.login(device.get(reauth), ADDRESS).error == BAD_PASSKEY


def test_a_sign_count_that_goes_backwards_is_refused_but_zero_is_fine(tmp_path):
    auth, db, clock = make_auth(tmp_path)
    device = Authenticator()
    enroll(auth, device)
    assert login(auth, device, count=5).token
    assert login(auth, device, count=5).error == BAD_PASSKEY  # a cloned authenticator
    assert login(auth, device, count=6).token

    synced = Authenticator(counter=False)
    db.add_admin("bob")
    enroll(auth, synced, username="bob")
    assert login(auth, synced).token and login(auth, synced).token


def test_unknown_or_garbage_credentials_count_towards_a_lockout(tmp_path):
    auth, db, clock = make_auth(tmp_path, login_max_failures=3)
    device = Authenticator()
    enroll(auth, device)
    stranger = Authenticator()
    stranger.create({"rp": {"id": HOST}, "user": {"id": "AAAA"}, "challenge": "AAAA"})
    assert login(auth, stranger).error == BAD_PASSKEY
    assert auth.login({"id": "x"}, ADDRESS).error == BAD_PASSKEY
    assert auth.login(None, ADDRESS).error == BAD_PASSKEY
    # The address is locked out, even for the right passkey; another address isn't.
    assert auth.login_options(ADDRESS) is None
    assert auth.login(device.get(auth.login_options("5.6.7.8")), ADDRESS).error == LOCKED_OUT
    assert login(auth, device, address="5.6.7.8").token
    clock.advance(minutes=16)
    assert login(auth, device).token
    actions = [e.action for e in db.audit_entries()]
    assert actions.count("login_failed") == 3 and "login_locked" in actions


def test_failed_passkey_checks_lock_out_the_admin_from_any_address(tmp_path):
    auth, db, clock = make_auth(tmp_path, login_max_failures=2)
    device = Authenticator()
    enroll(auth, device)
    for address in ("10.0.0.1", "10.0.0.2"):
        assert login(auth, device, address=address, uv=False).error == BAD_PASSKEY
    assert login(auth, device, address="10.0.0.3").error == LOCKED_OUT


def test_adding_a_passkey_needs_an_existing_one_first(tmp_path):
    auth, db, clock = make_auth(tmp_path)
    laptop, phone = Authenticator(), Authenticator()
    current = auth.session(enroll(auth, laptop).token)

    step = auth.add_options(current, ADDRESS)
    assert set(step) == {"reauth"}
    assert len(step["reauth"]["allowCredentials"]) == 1
    # The registration challenge only comes after confirming with an existing passkey.
    assert auth.reauth(current, {}, ADDRESS) == BAD_PASSKEY
    step = auth.add_options(current, ADDRESS)
    created = auth.reauth(current, laptop.get(step["reauth"]), ADDRESS)
    options = created["create"]
    assert len(options["excludeCredentials"]) == 1
    assert auth.add_passkey(current, phone.create(options), "", ADDRESS) is None
    assert [p.label for p in db.passkeys("ada")] == ["Laptop", "Passkey 2"]
    assert login(auth, phone).token

    # Another admin's passkey can't confirm for ada.
    db.add_admin("bob")
    bobs = Authenticator()
    enroll(auth, bobs, username="bob")
    step = auth.add_options(current, ADDRESS)
    assert auth.reauth(current, bobs.get(step["reauth"], credential=bobs.credentials[0]), ADDRESS) == BAD_PASSKEY


def test_an_admin_without_a_passkey_adds_one_straight_away(tmp_path):
    """Signed in from before passkeys: there's nothing to confirm with yet."""
    auth, db, clock = make_auth(tmp_path)
    current = auth.session(auth._new_session("ada"))
    options = auth.add_options(current, ADDRESS)["create"]
    assert auth.add_passkey(current, Authenticator().create(options), "Phone", ADDRESS) is None
    assert "reauth" in auth.add_options(current, ADDRESS)


def test_the_last_passkey_cant_be_removed(tmp_path):
    auth, db, clock = make_auth(tmp_path)
    laptop, phone = Authenticator(), Authenticator()
    current = auth.session(enroll(auth, laptop).token)
    only = db.passkeys("ada")[0].credential_id
    assert auth.remove_passkey(current, only, ADDRESS) == "You can't remove your only passkey."
    created = auth.reauth(current, laptop.get(auth.add_options(current, ADDRESS)["reauth"]), ADDRESS)
    auth.add_passkey(current, phone.create(created["create"]), "Phone", ADDRESS)
    assert auth.remove_passkey(current, "nope", ADDRESS) == "No such passkey."
    assert auth.remove_passkey(current, only, ADDRESS) is None
    assert [p.label for p in db.passkeys("ada")] == ["Phone"]
    assert login(auth, phone).token


def test_removing_an_admin_ends_their_sessions_and_passkeys(tmp_path):
    auth, db, clock = make_auth(tmp_path)
    token = enroll(auth, Authenticator()).token
    assert auth.session(token) is not None
    db.remove_admin("ada")
    assert auth.session(token) is None and db.passkeys("ada") == []


def test_password_era_admins_are_migrated_without_their_passwords(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE admins (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, totp_secret TEXT,
            totp_enabled INTEGER NOT NULL DEFAULT 0, totp_last_step INTEGER, created_at TEXT NOT NULL);
        CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, stage TEXT NOT NULL,
            csrf_token TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
        INSERT INTO admins VALUES ('ada', 'scrypt$x', 'SECRET', 1, 5, '2026-11-01T00:00:00+00:00');
        INSERT INTO admins VALUES ('bob', 'scrypt$y', NULL, 0, NULL, '2026-11-02T00:00:00+00:00');
        INSERT INTO sessions VALUES ('a', 'ada', 'full', 'c', '2026-11-13T11:00:00+00:00', '2026-11-13T11:00:00+00:00');
        INSERT INTO sessions VALUES ('b', 'bob', 'totp', 'c', '2026-11-13T11:00:00+00:00', '2026-11-13T11:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.admin_names() == ["ada", "bob"]
    ada, bob = db.admin("ada"), db.admin("bob")
    assert ada.user_handle and ada.user_handle != bob.user_handle
    columns = {r["name"] for r in db.conn.execute("PRAGMA table_info(admins)")}
    assert columns == {"username", "user_handle", "created_at"}
    assert db.session("a") is not None and db.session("b") is None
    db.close()
    assert Database(path).admin("ada") == ada  # migrates once


def cli(monkeypatch, tmp_path, *args, domain=HOST):
    config = tmp_path / "config.toml"
    config.write_text(
        f'[storage]\ndatabase_path = "{tmp_path / "t.sqlite3"}"\n[web]\ndomain = "{domain}"\n'
    )
    monkeypatch.setattr(sys, "argv", ["wordlebot", "--config", str(config), "admin", *args])
    with pytest.raises(SystemExit) as exc:
        entry.main()
    return exc.value.code


def test_admin_cli_manages_accounts_with_enrollment_links(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("WORDLEBOT_DATABASE_PATH", raising=False)
    monkeypatch.delenv("WORDLEBOT_WEB_DOMAIN", raising=False)
    assert cli(monkeypatch, tmp_path, "add", "ada") == 0
    out = capsys.readouterr().out
    first = out.split(f"https://{HOST}/enroll/")[1].split()[0]
    assert cli(monkeypatch, tmp_path, "add", "ada") == 1
    assert cli(monkeypatch, tmp_path, "list") == 0
    assert "ada  (no passkey yet)" in capsys.readouterr().out

    db = Database(tmp_path / "t.sqlite3")
    auth = Auth(db, WebConfig(enabled=True, domain=HOST))
    assert auth.enrollment_user(first) == "ada"
    assert auth.enroll(first, Authenticator().create(auth.enroll_options(first, ADDRESS)), "", ADDRESS).token
    assert cli(monkeypatch, tmp_path, "list") == 0
    assert "ada  (1 passkey)" in capsys.readouterr().out

    assert cli(monkeypatch, tmp_path, "enroll", "ada") == 0
    second = capsys.readouterr().out.split("/enroll/")[1].split()[0]
    assert auth.enrollment_user(second) == "ada" and len(db.passkeys("ada")) == 1

    assert cli(monkeypatch, tmp_path, "reset-passkeys", "ada", domain="") == 0
    out = capsys.readouterr().out
    assert "Removed 1 passkey(s)" in out and "[web] domain is not set" in out
    third = out.split("/enroll/")[1].split()[0]
    assert db.passkeys("ada") == [] and auth.enrollment_user(second) is None
    assert auth.enrollment_user(third) == "ada"

    assert cli(monkeypatch, tmp_path, "remove", "bob") == 1
    assert cli(monkeypatch, tmp_path, "remove", "ada") == 0
    assert db.admin_names() == [] and auth.enrollment_user(third) is None
    assert [e.action for e in db.audit_entries()][:4] == [
        "admin_removed", "passkeys_reset", "enrollment_link", "login",
    ]
    assert cli(monkeypatch, tmp_path, "add") == 2
