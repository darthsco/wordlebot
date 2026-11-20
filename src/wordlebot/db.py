"""SQLite persistence for solutions, our starters, and member scores.

All dates are stored as ISO strings (YYYY-MM-DD) in the bot's configured timezone.
Queries are small and fast, so this uses the synchronous stdlib driver directly.
"""

from __future__ import annotations

import base64
import json
import secrets
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import ScheduleConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS solutions (
    puzzle_date   TEXT PRIMARY KEY,
    puzzle_number INTEGER NOT NULL UNIQUE,
    word          TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS starters (
    target_date   TEXT PRIMARY KEY,
    puzzle_number INTEGER NOT NULL UNIQUE,
    word          TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    posted_at     TEXT NOT NULL,
    message_id    INTEGER           -- NULL means picked but not announced yet
);

-- One row per scheduled daily post, keyed by the date its starter is for.
CREATE TABLE IF NOT EXISTS daily_posts (
    target_date   TEXT PRIMARY KEY,
    results_at    TEXT,             -- when the results part was sent
    completed_at  TEXT              -- when the starter (or reminder) was sent
);

-- Golf leaderboards posted, one per round milestone: holes = 9 (front nine) or 18.
CREATE TABLE IF NOT EXISTS golf_posts (
    first_puzzle  INTEGER NOT NULL, -- the round's hole 1
    holes         INTEGER NOT NULL,
    posted_at     TEXT NOT NULL,
    PRIMARY KEY (first_puzzle, holes)
);

-- The messages of each golf post as sent, so a score edit can update them.
CREATE TABLE IF NOT EXISTS golf_post_messages (
    first_puzzle  INTEGER NOT NULL,
    holes         INTEGER NOT NULL,
    seq           INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    text          TEXT NOT NULL,
    PRIMARY KEY (first_puzzle, holes, seq)
);

-- Words a date's starter was rerolled away from, so a later reroll can't bring
-- one back. Members may already have seen (or opened with) any of them.
CREATE TABLE IF NOT EXISTS retired_starters (
    target_date   TEXT NOT NULL,
    word          TEXT NOT NULL,
    PRIMARY KEY (target_date, word)
);

CREATE TABLE IF NOT EXISTS scores (
    puzzle_number INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    user_name     TEXT NOT NULL,
    guesses       INTEGER,          -- NULL means failed (X)
    hard_mode     INTEGER NOT NULL DEFAULT 0,
    grid          TEXT NOT NULL,    -- rows of marks, e.g. "apaac\\nccccc"
    status        TEXT NOT NULL,    -- pending | verified | mismatch | no_starter
    submitted_at  TEXT NOT NULL,
    channel_id    INTEGER,          -- where the share was pasted; NULL for rows
    message_id    INTEGER,          -- from before these columns existed
    PRIMARY KEY (puzzle_number, user_id)
);
CREATE INDEX IF NOT EXISTS scores_status ON scores(status);

-- Admin panel. Admins sign in with passkeys (see auth.py). `user_handle` is the
-- random WebAuthn user id their passkeys are registered under, never the username.
CREATE TABLE IF NOT EXISTS admins (
    username      TEXT PRIMARY KEY,
    user_handle   TEXT NOT NULL UNIQUE, -- base64url
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS passkeys (
    credential_id TEXT PRIMARY KEY, -- base64url
    username      TEXT NOT NULL,
    public_key    BLOB NOT NULL,    -- COSE key
    sign_count    INTEGER NOT NULL,
    transports    TEXT NOT NULL,    -- JSON list, a hint for the browser
    label         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT
);
CREATE INDEX IF NOT EXISTS passkeys_username ON passkeys(username);

-- One-time links, made by the CLI, that let an admin register a passkey without
-- signing in. Only a hash of the token is stored.
CREATE TABLE IF NOT EXISTS enrollment_tokens (
    token_hash    TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);

-- Only a hash of the session token is stored, so a copy of the database can't log in.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash    TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    stage         TEXT NOT NULL,    -- always "full" (partial stages ended with passwords)
    csrf_token    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

-- Failed logins, keyed "user:<name>" or "ip:<address>", for rate limiting.
CREATE TABLE IF NOT EXISTS login_failures (
    key           TEXT NOT NULL,
    at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS login_failures_key ON login_failures(key, at);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    actor         TEXT NOT NULL,    -- panel username, or "cli"
    action        TEXT NOT NULL,
    detail        TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    address       TEXT NOT NULL DEFAULT ''
);

-- Settings edited in the panel, layered over config.toml (values are JSON).
CREATE TABLE IF NOT EXISTS config_overrides (
    section       TEXT NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    updated_by    TEXT NOT NULL,
    PRIMARY KEY (section, key)
);

-- Scores removed in the panel, kept so they can be restored.
CREATE TABLE IF NOT EXISTS deleted_scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    puzzle_number INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    user_name     TEXT NOT NULL,
    guesses       INTEGER,
    hard_mode     INTEGER NOT NULL,
    grid          TEXT NOT NULL,
    status        TEXT NOT NULL,
    submitted_at  TEXT NOT NULL,
    channel_id    INTEGER,
    message_id    INTEGER,
    deleted_at    TEXT NOT NULL,
    deleted_by    TEXT NOT NULL,
    reason        TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Starter:
    target_date: date
    puzzle_number: int
    word: str
    strategy: str
    message_id: int | None = None

    @property
    def announced(self) -> bool:
        return self.message_id is not None


@dataclass(frozen=True)
class Score:
    puzzle_number: int
    user_id: int
    user_name: str
    guesses: int | None
    hard_mode: bool
    grid: str
    status: str
    # The Discord message the share was pasted in, so its reaction can be updated.
    channel_id: int | None = None
    message_id: int | None = None
    # UTC ISO timestamp, set by the database on insert when left empty.
    submitted_at: str = ""


@dataclass(frozen=True)
class Admin:
    username: str
    user_handle: str  # base64url WebAuthn user id


@dataclass(frozen=True)
class Passkey:
    credential_id: str  # base64url
    username: str
    public_key: bytes
    sign_count: int
    transports: tuple[str, ...]
    label: str
    created_at: str
    last_used_at: str | None


@dataclass(frozen=True)
class Session:
    username: str
    stage: str
    csrf_token: str
    created_at: str
    last_seen_at: str


@dataclass(frozen=True)
class AuditEntry:
    id: int
    at: str
    actor: str
    action: str
    detail: str
    reason: str
    address: str


@dataclass(frozen=True)
class DeletedScore:
    id: int
    score: Score
    deleted_at: str
    deleted_by: str
    reason: str


def new_user_handle() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path, schedule: ScheduleConfig | None = None) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        upgrading = self._has_table("starters") and not self._has_table("daily_posts")
        self.conn.executescript(SCHEMA)
        if upgrading:
            self._backfill_daily_posts(schedule or ScheduleConfig())
        self._add_score_message_columns()
        self._drop_admin_passwords()

    def _has_table(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def _backfill_daily_posts(self, schedule: ScheduleConfig) -> None:
        """Migrate a database from before `daily_posts` existed.

        A starter announced at or after its daily post's scheduled time is taken to
        have had that post, so upgrading after post time doesn't repeat today's
        results and starter. One announced earlier (by a reroll) has not: its
        daily post is still owed, as a reminder.
        """
        rows = self.conn.execute(
            "SELECT target_date, posted_at FROM starters WHERE message_id IS NOT NULL"
        ).fetchall()
        done = []
        for target_iso, posted_at in rows:
            target = date.fromisoformat(target_iso)
            post_day = target - timedelta(days=schedule.target_offset_days)
            due = datetime.combine(post_day, schedule.post_time_local)
            if datetime.fromisoformat(posted_at) >= due:
                done.append((target_iso, posted_at, posted_at))
        with self.conn:
            self.conn.executemany("INSERT OR IGNORE INTO daily_posts VALUES (?, ?, ?)", done)

    def _add_score_message_columns(self) -> None:
        """Migrate a `scores` table from before submissions recorded their message.

        Existing rows keep NULL ids, so their reactions are simply never updated.
        """
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(scores)")}
        with self.conn:
            for column in ("channel_id", "message_id"):
                if column not in columns:
                    self.conn.execute(f"ALTER TABLE scores ADD COLUMN {column} INTEGER")

    def _drop_admin_passwords(self) -> None:
        """Migrate `admins` from password + TOTP sign-in to passkeys.

        Admins are kept with a new user handle and no passkeys, so each needs an
        enrollment link (`admin enroll NAME`). Signed-in sessions stay valid;
        half-finished logins (waiting for a code) are dropped.
        """
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(admins)")}
        if "password_hash" not in columns:
            return
        rows = self.conn.execute("SELECT username, created_at FROM admins").fetchall()
        with self.conn:
            self.conn.execute("ALTER TABLE admins RENAME TO admins_password_era")
            self.conn.execute(
                "CREATE TABLE admins (username TEXT PRIMARY KEY, "
                "user_handle TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)"
            )
            self.conn.executemany(
                "INSERT INTO admins VALUES (?, ?, ?)",
                [(r["username"], new_user_handle(), r["created_at"]) for r in rows],
            )
            self.conn.execute("DROP TABLE admins_password_era")
            self.conn.execute("DELETE FROM sessions WHERE stage != 'full'")

    def close(self) -> None:
        self.conn.close()

    # --- solutions -------------------------------------------------------

    def add_solutions(self, rows: list[tuple[date, int, str]]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO solutions VALUES (?, ?, ?, ?)",
                [(d.isoformat(), n, w.lower(), _now()) for d, n, w in rows],
            )

    def solution_dates(self) -> set[date]:
        rows = self.conn.execute("SELECT puzzle_date FROM solutions")
        return {date.fromisoformat(r[0]) for r in rows}

    def solution_for(self, puzzle_number: int) -> str | None:
        row = self.conn.execute(
            "SELECT word FROM solutions WHERE puzzle_number = ?", (puzzle_number,)
        ).fetchone()
        return row[0] if row else None

    def all_solution_words(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT word FROM solutions")}

    def solutions_between(self, start: date | None, end: date) -> set[str]:
        """Solution words with start <= date <= end (start=None means all time)."""
        if start is None:
            rows = self.conn.execute(
                "SELECT word FROM solutions WHERE puzzle_date <= ?", (end.isoformat(),)
            )
        else:
            rows = self.conn.execute(
                "SELECT word FROM solutions WHERE puzzle_date BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            )
        return {r[0] for r in rows}

    # --- starters --------------------------------------------------------

    def add_starter(self, starter: Starter) -> Starter:
        """Persist a pick, not yet announced, unless the date already has one.

        Returns the stored starter, which is the earlier pick if there was one, so
        a date can never end up with two different words.
        """
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO starters VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    starter.target_date.isoformat(),
                    starter.puzzle_number,
                    starter.word,
                    starter.strategy,
                    _now(),
                ),
            )
        stored = self.starter_for_date(starter.target_date)
        assert stored is not None
        return stored

    def save_starter(self, starter: Starter, message_id: int | None = None) -> None:
        """Insert or replace (a reroll). `message_id=None` means not announced yet.

        A different word being replaced is kept in the `retired_starters` table.
        """
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO retired_starters "
                "SELECT target_date, word FROM starters WHERE target_date = ? AND word != ?",
                (starter.target_date.isoformat(), starter.word),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO starters VALUES (?, ?, ?, ?, ?, ?)",
                (
                    starter.target_date.isoformat(),
                    starter.puzzle_number,
                    starter.word,
                    starter.strategy,
                    _now(),
                    message_id,
                ),
            )

    def mark_announced(self, target: date, message_id: int, daily_post: bool = False) -> None:
        """Record that the starter reached the channel; `daily_post` also completes that."""
        with self.conn:
            self.conn.execute(
                "UPDATE starters SET message_id = ? WHERE target_date = ?",
                (message_id, target.isoformat()),
            )
            if daily_post:
                self._stamp_daily_post(target, "completed_at")

    def retired_words(self, target: date) -> set[str]:
        """Every word the starter for `target` was rerolled away from."""
        rows = self.conn.execute(
            "SELECT word FROM retired_starters WHERE target_date = ?", (target.isoformat(),)
        )
        return {r[0] for r in rows}

    def starter_for_date(self, target: date) -> Starter | None:
        row = self.conn.execute(
            "SELECT * FROM starters WHERE target_date = ?", (target.isoformat(),)
        ).fetchone()
        return self._starter(row) if row else None

    def starter_for_puzzle(self, puzzle_number: int) -> Starter | None:
        row = self.conn.execute(
            "SELECT * FROM starters WHERE puzzle_number = ?", (puzzle_number,)
        ).fetchone()
        return self._starter(row) if row else None

    def first_starter_date(self) -> date | None:
        """The earliest date we ever picked a starter for; None if there is none."""
        row = self.conn.execute("SELECT MIN(target_date) FROM starters").fetchone()
        return date.fromisoformat(row[0]) if row[0] else None

    def starters_between(self, start: date | None, end: date) -> list[Starter]:
        if start is None:
            rows = self.conn.execute(
                "SELECT * FROM starters WHERE target_date <= ? ORDER BY target_date",
                (end.isoformat(),),
            )
        else:
            rows = self.conn.execute(
                "SELECT * FROM starters WHERE target_date BETWEEN ? AND ? ORDER BY target_date",
                (start.isoformat(), end.isoformat()),
            )
        return [self._starter(r) for r in rows]

    @staticmethod
    def _starter(row: sqlite3.Row) -> Starter:
        return Starter(
            target_date=date.fromisoformat(row["target_date"]),
            puzzle_number=row["puzzle_number"],
            word=row["word"],
            strategy=row["strategy"],
            message_id=row["message_id"],
        )

    # --- daily posts -----------------------------------------------------

    def _stamp_daily_post(self, target: date, column: str) -> None:
        self.conn.execute(
            f"INSERT INTO daily_posts (target_date, {column}) VALUES (?, ?) "
            f"ON CONFLICT(target_date) DO UPDATE SET {column} = excluded.{column}",
            (target.isoformat(), _now()),
        )

    def mark_results_posted(self, target: date) -> None:
        with self.conn:
            self._stamp_daily_post(target, "results_at")

    def mark_daily_post_completed(self, target: date) -> None:
        with self.conn:
            self._stamp_daily_post(target, "completed_at")

    def _daily_post_stamp(self, target: date, column: str) -> str | None:
        row = self.conn.execute(
            f"SELECT {column} FROM daily_posts WHERE target_date = ?", (target.isoformat(),)
        ).fetchone()
        return row[0] if row else None

    def results_posted(self, target: date) -> bool:
        return self._daily_post_stamp(target, "results_at") is not None

    def daily_post_completed(self, target: date) -> bool:
        return self._daily_post_stamp(target, "completed_at") is not None

    # --- golf ------------------------------------------------------------

    def first_starter_puzzle(self) -> int | None:
        """Puzzle number of the earliest starter: hole 1 of golf round 1."""
        row = self.conn.execute(
            "SELECT puzzle_number FROM starters ORDER BY target_date LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def mark_golf_posted(
        self, first_puzzle: int, holes: int, messages: list[tuple[int, str]]
    ) -> None:
        """Record a golf post as sent, as `messages` of (message id, text). Called
        again for a correction or an edit, it replaces the stored messages."""
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO golf_posts VALUES (?, ?, ?)", (first_puzzle, holes, _now())
            )
            self.conn.execute(
                "DELETE FROM golf_post_messages WHERE first_puzzle = ? AND holes = ?",
                (first_puzzle, holes),
            )
            self.conn.executemany(
                "INSERT INTO golf_post_messages VALUES (?, ?, ?, ?, ?)",
                [(first_puzzle, holes, i, mid, text) for i, (mid, text) in enumerate(messages)],
            )

    def golf_posted(self, first_puzzle: int, holes: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM golf_posts WHERE first_puzzle = ? AND holes = ?", (first_puzzle, holes)
        ).fetchone()
        return row is not None

    def latest_golf_post(self) -> tuple[int, int] | None:
        """(first puzzle, holes) of the latest milestone posted."""
        row = self.conn.execute(
            "SELECT first_puzzle, holes FROM golf_posts ORDER BY first_puzzle + holes DESC LIMIT 1"
        ).fetchone()
        return (row[0], row[1]) if row else None

    def golf_posts_covering(self, puzzle_number: int) -> list[tuple[int, int]]:
        """(first puzzle, holes) of every golf post that included `puzzle_number`."""
        rows = self.conn.execute(
            "SELECT first_puzzle, holes FROM golf_posts "
            "WHERE first_puzzle <= ? AND ? < first_puzzle + holes ORDER BY holes",
            (puzzle_number, puzzle_number),
        )
        return [(r[0], r[1]) for r in rows]

    def golf_post_messages(self, first_puzzle: int, holes: int) -> list[tuple[int, str]]:
        rows = self.conn.execute(
            "SELECT message_id, text FROM golf_post_messages "
            "WHERE first_puzzle = ? AND holes = ? ORDER BY seq",
            (first_puzzle, holes),
        )
        return [(r[0], r[1]) for r in rows]

    # --- scores ----------------------------------------------------------

    def add_score(self, score: Score) -> bool:
        """Insert a score. Returns False if the user already submitted for that puzzle."""
        with self.conn:
            return self._insert_score(score)

    def set_score_status(self, puzzle_number: int, user_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE scores SET status = ? WHERE puzzle_number = ? AND user_id = ?",
                (status, puzzle_number, user_id),
            )

    def scores_with_status(self, status: str) -> list[Score]:
        rows = self.conn.execute("SELECT * FROM scores WHERE status = ?", (status,))
        return [self._score(r) for r in rows]

    def score_count(self, puzzle_number: int) -> int:
        """How many members have submitted for a puzzle, whatever the status."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM scores WHERE puzzle_number = ?", (puzzle_number,)
        ).fetchone()
        return row[0]

    def scores_between(self, first_puzzle: int | None, last_puzzle: int) -> list[Score]:
        """Scores for a range of puzzles, oldest submission first."""
        lo = first_puzzle if first_puzzle is not None else 0
        rows = self.conn.execute(
            "SELECT * FROM scores WHERE puzzle_number BETWEEN ? AND ? "
            "ORDER BY submitted_at, rowid",
            (lo, last_puzzle),
        )
        return [self._score(r) for r in rows]

    @staticmethod
    def _score(row: sqlite3.Row) -> Score:
        return Score(
            puzzle_number=row["puzzle_number"],
            user_id=row["user_id"],
            user_name=row["user_name"],
            guesses=row["guesses"],
            hard_mode=bool(row["hard_mode"]),
            grid=row["grid"],
            status=row["status"],
            channel_id=row["channel_id"],
            message_id=row["message_id"],
            submitted_at=row["submitted_at"],
        )

    def score_for(self, puzzle_number: int, user_id: int) -> Score | None:
        row = self.conn.execute(
            "SELECT * FROM scores WHERE puzzle_number = ? AND user_id = ?", (puzzle_number, user_id)
        ).fetchone()
        return self._score(row) if row else None

    def scores_for_puzzle(self, puzzle_number: int) -> list[Score]:
        return self.scores_between(puzzle_number, puzzle_number)

    def scores_for_user(self, user_id: int, limit: int = 100) -> list[Score]:
        rows = self.conn.execute(
            "SELECT * FROM scores WHERE user_id = ? ORDER BY puzzle_number DESC LIMIT ?",
            (user_id, limit),
        )
        return [self._score(r) for r in rows]

    def players(self) -> list[tuple[int, str, int]]:
        """(user_id, latest name, number of scores) for everyone who ever submitted."""
        rows = self.conn.execute(
            "SELECT user_id, user_name, COUNT(*) OVER (PARTITION BY user_id) AS n, "
            "ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY submitted_at DESC, rowid DESC) AS r "
            "FROM scores"
        )
        found = [(r["user_id"], r["user_name"], r["n"]) for r in rows if r["r"] == 1]
        return sorted(found, key=lambda p: p[1].lower())

    def delete_score(self, puzzle_number: int, user_id: int, by: str, reason: str) -> Score | None:
        """Move a score to `deleted_scores`; returns it, or None if there was none."""
        score = self.score_for(puzzle_number, user_id)
        if score is None:
            return None
        with self.conn:
            self.conn.execute(
                "INSERT INTO deleted_scores (puzzle_number, user_id, user_name, guesses, hard_mode,"
                " grid, status, submitted_at, channel_id, message_id, deleted_at, deleted_by, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    score.puzzle_number, score.user_id, score.user_name, score.guesses,
                    int(score.hard_mode), score.grid, score.status, score.submitted_at,
                    score.channel_id, score.message_id, _now(), by, reason,
                ),
            )
            self.conn.execute(
                "DELETE FROM scores WHERE puzzle_number = ? AND user_id = ?", (puzzle_number, user_id)
            )
        return score

    def deleted_scores(self, limit: int = 100) -> list[DeletedScore]:
        rows = self.conn.execute("SELECT * FROM deleted_scores ORDER BY id DESC LIMIT ?", (limit,))
        return [self._deleted(r) for r in rows]

    def deleted_score(self, deleted_id: int) -> DeletedScore | None:
        row = self.conn.execute("SELECT * FROM deleted_scores WHERE id = ?", (deleted_id,)).fetchone()
        return self._deleted(row) if row else None

    def restore_score(self, deleted_id: int, status: str) -> bool:
        """Put a deleted score back with `status`. False if the member has a score for
        that puzzle again (they resubmitted), or the deleted row is gone."""
        deleted = self.deleted_score(deleted_id)
        if deleted is None:
            return False
        with self.conn:
            if not self._insert_score(replace(deleted.score, status=status)):
                return False
            self.conn.execute("DELETE FROM deleted_scores WHERE id = ?", (deleted_id,))
        return True

    def _insert_score(self, score: Score) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO scores (puzzle_number, user_id, user_name, guesses,"
            " hard_mode, grid, status, submitted_at, channel_id, message_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                score.puzzle_number, score.user_id, score.user_name, score.guesses,
                int(score.hard_mode), score.grid, score.status, score.submitted_at or _now(),
                score.channel_id, score.message_id,
            ),
        )
        return cur.rowcount == 1

    def _deleted(self, row: sqlite3.Row) -> DeletedScore:
        return DeletedScore(row["id"], self._score(row), row["deleted_at"], row["deleted_by"], row["reason"])

    # --- admin panel: accounts and sessions -------------------------------

    def add_admin(self, username: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO admins VALUES (?, ?, ?)", (username, new_user_handle(), _now())
            )
        return cur.rowcount == 1

    @staticmethod
    def _admin(row: sqlite3.Row | None) -> Admin | None:
        return None if row is None else Admin(row["username"], row["user_handle"])

    def admin(self, username: str) -> Admin | None:
        return self._admin(self.conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone())

    def admin_by_handle(self, user_handle: str) -> Admin | None:
        row = self.conn.execute("SELECT * FROM admins WHERE user_handle = ?", (user_handle,)).fetchone()
        return self._admin(row)

    def admin_names(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT username FROM admins ORDER BY username")]

    def remove_admin(self, username: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM admins WHERE username = ?", (username,))
            for table in ("sessions", "passkeys", "enrollment_tokens"):
                self.conn.execute(f"DELETE FROM {table} WHERE username = ?", (username,))
        return cur.rowcount == 1

    # --- admin panel: passkeys and enrollment links -------------------------

    @staticmethod
    def _passkey(row: sqlite3.Row) -> Passkey:
        return Passkey(
            row["credential_id"], row["username"], bytes(row["public_key"]), row["sign_count"],
            tuple(json.loads(row["transports"])), row["label"], row["created_at"], row["last_used_at"],
        )

    def add_passkey(
        self, credential_id: str, username: str, public_key: bytes, sign_count: int,
        transports: list[str], label: str,
    ) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO passkeys VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (credential_id, username, public_key, sign_count, json.dumps(transports), label, _now()),
            )
        return cur.rowcount == 1

    def passkey(self, credential_id: str) -> Passkey | None:
        row = self.conn.execute("SELECT * FROM passkeys WHERE credential_id = ?", (credential_id,)).fetchone()
        return None if row is None else self._passkey(row)

    def passkeys(self, username: str) -> list[Passkey]:
        rows = self.conn.execute(
            "SELECT * FROM passkeys WHERE username = ? ORDER BY created_at, rowid", (username,)
        )
        return [self._passkey(r) for r in rows]

    def use_passkey(self, credential_id: str, sign_count: int, now: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE credential_id = ?",
                (sign_count, now, credential_id),
            )

    def remove_passkey(self, username: str, credential_id: str) -> bool:
        """Remove one of `username`'s passkeys, unless it is their last."""
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM passkeys WHERE username = ? AND credential_id = ? "
                "AND (SELECT COUNT(*) FROM passkeys WHERE username = ?) > 1",
                (username, credential_id, username),
            )
        return cur.rowcount == 1

    def remove_passkeys(self, username: str) -> int:
        with self.conn:
            return self.conn.execute("DELETE FROM passkeys WHERE username = ?", (username,)).rowcount

    def set_enrollment_token(self, username: str, token_hash: str, expires_at: str) -> None:
        """Make `token_hash` the admin's only enrollment link."""
        with self.conn:
            self.conn.execute("DELETE FROM enrollment_tokens WHERE username = ?", (username,))
            self.conn.execute(
                "INSERT INTO enrollment_tokens VALUES (?, ?, ?)", (token_hash, username, expires_at)
            )

    def enrollment_token(self, token_hash: str, now: str) -> str | None:
        """The admin an unexpired enrollment token belongs to."""
        row = self.conn.execute(
            "SELECT username FROM enrollment_tokens WHERE token_hash = ? AND expires_at > ?",
            (token_hash, now),
        ).fetchone()
        return None if row is None else row[0]

    def delete_enrollment_token(self, token_hash: str) -> bool:
        with self.conn:
            return self.conn.execute(
                "DELETE FROM enrollment_tokens WHERE token_hash = ?", (token_hash,)
            ).rowcount == 1

    def add_session(self, token_hash: str, username: str, stage: str, csrf: str, now: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                (token_hash, username, stage, csrf, now, now),
            )

    def session(self, token_hash: str) -> Session | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
        if row is None:
            return None
        return Session(row["username"], row["stage"], row["csrf_token"], row["created_at"], row["last_seen_at"])

    def touch_session(self, token_hash: str, now: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now, token_hash))

    def delete_session(self, token_hash: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_sessions(self, username: str, keep: str | None = None) -> int:
        """End every session of `username`, except the one with token hash `keep`."""
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM sessions WHERE username = ? AND token_hash IS NOT ?", (username, keep)
            )
        return cur.rowcount

    def delete_sessions_before(self, created_before: str, seen_before: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM sessions WHERE created_at < ? OR last_seen_at < ?",
                (created_before, seen_before),
            )

    def add_login_failure(self, keys: list[str], now: str) -> None:
        with self.conn:
            self.conn.executemany("INSERT INTO login_failures VALUES (?, ?)", [(k, now) for k in keys])

    def login_failures_since(self, key: str, since: str) -> list[str]:
        """Timestamps of failures for `key` at or after `since`, oldest first."""
        rows = self.conn.execute(
            "SELECT at FROM login_failures WHERE key = ? AND at >= ? ORDER BY at", (key, since)
        )
        return [r[0] for r in rows]

    def clear_login_failures(self, keys: list[str], before: str | None = None) -> None:
        """Forget the failures for `keys` (all of them, or only those before `before`)."""
        with self.conn:
            for key in keys:
                if before is None:
                    self.conn.execute("DELETE FROM login_failures WHERE key = ?", (key,))
                else:
                    self.conn.execute(
                        "DELETE FROM login_failures WHERE key = ? AND at < ?", (key, before)
                    )

    def prune_login_failures(self, before: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM login_failures WHERE at < ?", (before,))

    # --- admin panel: audit log and config overrides ------------------------

    def audit(self, actor: str, action: str, detail: str, reason: str = "", address: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO audit_log (at, actor, action, detail, reason, address)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), actor, action, detail, reason, address),
            )

    def audit_entries(self, limit: int = 200, before_id: int | None = None) -> list[AuditEntry]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log WHERE id < ? ORDER BY id DESC LIMIT ?",
            (before_id if before_id is not None else 2**62, limit),
        )
        return [
            AuditEntry(r["id"], r["at"], r["actor"], r["action"], r["detail"], r["reason"], r["address"])
            for r in rows
        ]

    def config_overrides(self) -> dict[tuple[str, str], object]:
        rows = self.conn.execute("SELECT section, key, value FROM config_overrides")
        return {(r["section"], r["key"]): json.loads(r["value"]) for r in rows}

    def config_override_meta(self) -> dict[tuple[str, str], tuple[str, str]]:
        """(section, key) -> (updated_at, updated_by)."""
        rows = self.conn.execute("SELECT section, key, updated_at, updated_by FROM config_overrides")
        return {(r["section"], r["key"]): (r["updated_at"], r["updated_by"]) for r in rows}

    def set_config_override(self, section: str, key: str, value: object, by: str) -> None:
        if isinstance(value, tuple):
            value = list(value)
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO config_overrides VALUES (?, ?, ?, ?, ?)",
                (section, key, json.dumps(value), _now(), by),
            )

    def delete_config_override(self, section: str, key: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM config_overrides WHERE section = ? AND key = ?", (section, key)
            )
        return cur.rowcount == 1
