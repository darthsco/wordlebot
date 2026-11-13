"""SQLite persistence for solutions, our starters, and member scores.

All dates are stored as ISO strings (YYYY-MM-DD) in the bot's configured timezone.
Queries are small and fast, so this uses the synchronous stdlib driver directly.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        upgrading = self._has_table("starters") and not self._has_table("daily_posts")
        self.conn.executescript(SCHEMA)
        if upgrading:
            self._backfill_daily_posts()
        self._add_score_message_columns()

    def _has_table(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def _backfill_daily_posts(self) -> None:
        """Migrate a database from before `daily_posts` existed.

        Every starter that reached the channel is taken to have had its daily post,
        so upgrading after post time doesn't repeat today's results and starter.
        """
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO daily_posts "
                "SELECT target_date, posted_at, posted_at FROM starters "
                "WHERE message_id IS NOT NULL"
            )

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
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO scores (puzzle_number, user_id, user_name, guesses,"
                " hard_mode, grid, status, submitted_at, channel_id, message_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    score.puzzle_number,
                    score.user_id,
                    score.user_name,
                    score.guesses,
                    int(score.hard_mode),
                    score.grid,
                    score.status,
                    score.submitted_at or _now(),
                    score.channel_id,
                    score.message_id,
                ),
            )
        return cur.rowcount == 1

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
