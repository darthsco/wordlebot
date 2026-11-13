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

# Thousands separators seen in the wild: "," "." and, in locales such as French
# or Swedish, a space, NBSP or narrow NBSP. A separator must be followed by
# exactly three digits, so it can never swallow the single-digit guess count.
HEADER = re.compile(
    r"Wordle\s+(\d{1,3}(?:[,. \u00a0\u202f]\d{3})*|\d+)\s+([0-9X])/(\d+)(\*?)", re.IGNORECASE
)
# Longest puzzle number accepted. Far beyond any real one, yet short enough that
# the number always converts (`int()` refuses very long digit strings) and fits
# a SQLite INTEGER.
MAX_PUZZLE_DIGITS = 9
QUOTE_PREFIX = re.compile(r"^\s*(?:>\s*)+")  # Discord "> " quoting

PENDING = "pending"  # solution not yet cached, or the starter not picked yet
VERIFIED = "verified"
MISMATCH = "mismatch"  # first row doesn't match the shared starter
NO_STARTER = "no_starter"  # we posted no starter for that puzzle, and won't


@dataclass(frozen=True)
class ParsedShare:
    puzzle_number: int
    guesses: int | None  # None = failed
    hard_mode: bool
    rows: tuple[tuple[Mark, ...], ...]

    @property
    def grid_text(self) -> str:
        return "\n".join("".join(m.value for m in row) for row in self.rows)


def _parse_row(line: str) -> tuple[tuple[Mark, ...], bool] | None:
    """Return (marks, has_trailing_text) for a grid line, or None if it isn't one.

    Discord quote prefixes and ``||spoiler||`` tags are ignored. Text after the
    squares is tolerated only when separated from them by whitespace.
    """
    line = QUOTE_PREFIX.sub("", line.replace("\ufe0f", "")).replace("||", "").strip()
    end = 0
    while end < len(line) and line[end] in SQUARES:
        end += 1
    if end == 0 or (end < len(line) and not line[end].isspace()):
        return None
    return tuple(SQUARES[ch] for ch in line[:end]), end < len(line)


def _parse_grid(
    text: str, guesses: int | None, word_length: int, max_guesses: int
) -> tuple[tuple[Mark, ...], ...] | None:
    """Return the first grid in `text` if it is consistent with `guesses`, else None."""
    rows: list[tuple[Mark, ...]] = []
    for line in text.splitlines():
        parsed = _parse_row(line)
        # A line with trailing text only counts as a row if it is a full-width
        # one; otherwise it is chatter that happens to start with a square.
        if parsed is not None and (not parsed[1] or len(parsed[0]) == word_length):
            rows.append(parsed[0])
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
    return tuple(rows)


def parse_share(text: str, word_length: int = 5, max_guesses: int = 6) -> ParsedShare | None:
    """Parse a Wordle share. Returns None if `text` isn't a well-formed share.

    A message may mention several headers ("yesterday Wordle 1,971 5/6 lol"
    above the real share). The first header followed by a valid grid wins; a
    grid belongs to a header only if it sits before the next header.
    """
    headers = list(HEADER.finditer(text))
    for i, match in enumerate(headers):
        raw_guesses, total, hard = match.group(2), int(match.group(3)), match.group(4)
        if total != max_guesses:
            continue
        guesses = None if raw_guesses.upper() == "X" else int(raw_guesses)
        if guesses is not None and not 1 <= guesses <= max_guesses:
            continue
        grid_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        rows = _parse_grid(text[match.end() : grid_end], guesses, word_length, max_guesses)
        if rows is None:
            continue
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) > MAX_PUZZLE_DIGITS:
            continue
        return ParsedShare(int(digits), guesses, bool(hard), rows)
    return None


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
