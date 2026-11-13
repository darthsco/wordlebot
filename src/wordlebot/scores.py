"""Parsing pasted Wordle share results and verifying the shared starter was used."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .game import Mark, feedback

# Standard and high-contrast palettes.
SQUARES = {
    "🟩": Mark.CORRECT,
    "🟧": Mark.CORRECT,
    "🟨": Mark.PRESENT,
    "🟦": Mark.PRESENT,
    "⬛": Mark.ABSENT,
    "⬜": Mark.ABSENT,
}

HEADER = re.compile(r"Wordle\s+(\d{1,3}(?:[,.]\d{3})*|\d+)\s+([0-9X])/(\d+)(\*?)", re.IGNORECASE)

PENDING = "pending"  # solution not yet cached
VERIFIED = "verified"
MISMATCH = "mismatch"  # first row doesn't match the shared starter
NO_STARTER = "no_starter"  # we didn't post a starter for that puzzle


@dataclass(frozen=True)
class ParsedShare:
    puzzle_number: int
    guesses: int | None  # None = failed
    hard_mode: bool
    rows: tuple[tuple[Mark, ...], ...]

    @property
    def grid_text(self) -> str:
        return "\n".join("".join(m.value for m in row) for row in self.rows)


def _parse_row(line: str) -> tuple[Mark, ...] | None:
    line = line.replace("️", "").strip()
    if not line or any(ch not in SQUARES for ch in line):
        return None
    return tuple(SQUARES[ch] for ch in line)


def parse_share(text: str, word_length: int = 5, max_guesses: int = 6) -> ParsedShare | None:
    """Parse a Wordle share. Returns None if `text` isn't a well-formed share."""
    match = HEADER.search(text)
    if not match:
        return None
    number = int(re.sub(r"\D", "", match.group(1)))
    raw_guesses, total, hard = match.group(2), int(match.group(3)), match.group(4)
    if total != max_guesses:
        return None
    guesses = None if raw_guesses.upper() == "X" else int(raw_guesses)
    if guesses is not None and not 1 <= guesses <= max_guesses:
        return None

    rows: list[tuple[Mark, ...]] = []
    for line in text[match.end() :].splitlines():
        row = _parse_row(line)
        if row is not None:
            rows.append(row)
        elif rows:
            break  # grid ended
    if not rows or any(len(r) != word_length for r in rows):
        return None

    solved_row = (Mark.CORRECT,) * word_length
    expected_rows = guesses if guesses is not None else max_guesses
    if len(rows) != expected_rows:
        return None
    if (rows[-1] == solved_row) != (guesses is not None):
        return None
    if any(r == solved_row for r in rows[:-1]):
        return None

    return ParsedShare(number, guesses, bool(hard), tuple(rows))


def grid_rows(grid_text: str) -> tuple[tuple[Mark, ...], ...]:
    return tuple(tuple(Mark(c) for c in line) for line in grid_text.splitlines())


def verify(first_row: tuple[Mark, ...], starter: str | None, solution: str | None) -> str:
    if starter is None:
        return NO_STARTER
    if solution is None:
        return PENDING
    return VERIFIED if first_row == feedback(starter, solution) else MISMATCH


def effective_score(guesses: int | None, fail_score: int) -> int:
    return fail_score if guesses is None else guesses
