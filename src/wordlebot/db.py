"""SQLite persistence for solutions, our starters, and member scores.

All dates are stored as ISO strings (YYYY-MM-DD) in the bot's configured timezone.
Queries are small and fast, so this uses the synchronous stdlib driver directly.
"""

from __future__ import annotations

import json
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

-- Admin panel. Passwords are scrypt hashes (see auth.py); the TOTP secret is
-- set at first login and confirmed by a code before it is enabled.
CREATE TABLE IF NOT EXISTS admins (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    totp_secret   TEXT,
    totp_enabled  INTEGER NOT NULL DEFAULT 0,
    totp_last_step INTEGER,         -- the last accepted code's time step, against replay
    created_at    TEXT NOT NULL
);

-- Only a hash of the session token is stored, so a copy of the database can't log in.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash    TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    stage         TEXT NOT NULL,    -- totp | enroll | full
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
    password_hash: str
    totp_secret: str | None
    totp_enabled: bool
    totp_last_step: int | None


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

    def _has_table(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def _backfill_daily_posts(self, schedule: ScheduleConfig) -> None:
        """Migrate a database from before `daily_posts` existed.

        A starter announced at or after its daily post's scheduled time is taken to
        have had that post, so upgrading after post time doesn't repeat today's
        results and starter. One announced earlier (by `/reroll`) has not: its
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

    def add_admin(self, username: str, password_hash: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO admins (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, _now()),
            )
        return cur.rowcount == 1

    def admin(self, username: str) -> Admin | None:
        row = self.conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
        if row is None:
            return None
        return Admin(
            row["username"], row["password_hash"], row["totp_secret"],
            bool(row["totp_enabled"]), row["totp_last_step"],
        )

    def admin_names(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT username FROM admins ORDER BY username")]

    def set_admin_password(self, username: str, password_hash: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE admins SET password_hash = ? WHERE username = ?", (password_hash, username)
            )
        return cur.rowcount == 1

    def set_admin_totp(self, username: str, secret: str | None, enabled: bool) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE admins SET totp_secret = ?, totp_enabled = ?, totp_last_step = NULL "
                "WHERE username = ?",
                (secret, int(enabled), username),
            )
        return cur.rowcount == 1

    def use_totp_step(self, username: str, step: int) -> bool:
        """Record `step` as used; False if it (or a later one) was used already."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE admins SET totp_last_step = ? WHERE username = ? "
                "AND (totp_last_step IS NULL OR totp_last_step < ?)",
                (step, username, step),
            )
        return cur.rowcount == 1

    def remove_admin(self, username: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM admins WHERE username = ?", (username,))
            self.conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
        return cur.rowcount == 1

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
