"""Pure Wordle rules: puzzle numbering and guess feedback."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from enum import StrEnum


class Mark(StrEnum):
    ABSENT = "a"
    PRESENT = "p"
    CORRECT = "c"


def puzzle_number(day: date, launch: date) -> int:
    return (day - launch).days


def puzzle_date(number: int, launch: date) -> date:
    return launch + timedelta(days=number)


def feedback(guess: str, solution: str) -> tuple[Mark, ...]:
    """Wordle's coloring, including duplicate-letter handling."""
    guess, solution = guess.lower(), solution.lower()
    if len(guess) != len(solution):
        raise ValueError("guess and solution lengths differ")

    marks = [Mark.ABSENT] * len(guess)
    remaining: Counter[str] = Counter()
    for i, (g, s) in enumerate(zip(guess, solution)):
        if g == s:
            marks[i] = Mark.CORRECT
        else:
            remaining[s] += 1
    for i, g in enumerate(guess):
        if marks[i] is not Mark.CORRECT and remaining[g] > 0:
            marks[i] = Mark.PRESENT
            remaining[g] -= 1
    return tuple(marks)
