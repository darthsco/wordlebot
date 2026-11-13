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
    message_id    INTEGER
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


@dataclass(frozen=True)
class Score:
    puzzle_number: int
    user_id: int
    user_name: str
    guesses: int | None
    hard_mode: bool
    grid: str
    status: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

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

    def save_starter(self, starter: Starter, message_id: int | None = None) -> None:
        with self.conn:
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
        )

    # --- scores ----------------------------------------------------------

    def add_score(self, score: Score) -> bool:
        """Insert a score. Returns False if the user already submitted for that puzzle."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO scores VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    score.puzzle_number,
                    score.user_id,
                    score.user_name,
                    score.guesses,
                    int(score.hard_mode),
                    score.grid,
                    score.status,
                    _now(),
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

    def scores_between(self, first_puzzle: int | None, last_puzzle: int) -> list[Score]:
        lo = first_puzzle if first_puzzle is not None else 0
        rows = self.conn.execute(
            "SELECT * FROM scores WHERE puzzle_number BETWEEN ? AND ?", (lo, last_puzzle)
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
        )
