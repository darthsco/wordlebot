"""Aggregating scores into daily results and standings, and formatting them."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .db import Score
from .scores import VERIFIED, PENDING, effective_score


@dataclass(frozen=True)
class Standing:
    user_id: int
    user_name: str
    games: int
    wins: int
    average: float
    best: int | None


def counted(scores: list[Score], count_unverified: bool) -> list[Score]:
    allowed = {VERIFIED, PENDING} if count_unverified else {VERIFIED}
    return [s for s in scores if s.status in allowed]


def standings(scores: list[Score], fail_score: int, min_games: int = 1) -> list[Standing]:
    by_user: dict[int, list[Score]] = defaultdict(list)
    for s in scores:
        by_user[s.user_id].append(s)

    result = []
    for user_id, games in by_user.items():
        if len(games) < min_games:
            continue
        values = [effective_score(g.guesses, fail_score) for g in games]
        solved = [g.guesses for g in games if g.guesses is not None]
        result.append(
            Standing(
                user_id=user_id,
                user_name=games[-1].user_name,
                games=len(games),
                wins=len(solved),
                average=sum(values) / len(values),
                best=min(solved, default=None),
            )
        )
    result.sort(key=lambda s: (s.average, -s.games, s.user_name.lower()))
    return result


def format_standings(title: str, rows: list[Standing]) -> str:
    if not rows:
        return f"**{title}**\nNo qualifying results yet."
    lines = [f"**{title}**"]
    for rank, s in enumerate(rows, 1):
        lines.append(
            f"{rank}. {s.user_name} — avg **{s.average:.2f}** "
            f"({s.games} played, {s.wins} solved)"
        )
    return "\n".join(lines)


def format_daily(puzzle_number: int, starter: str | None, scores: list[Score]) -> str:
    header = f"**Wordle {puzzle_number:,} results**"
    if starter:
        header += f" (starter: `{starter.upper()}`)"
    if not scores:
        return f"{header}\nNo verified results."
    ordered = sorted(scores, key=lambda s: (s.guesses is None, s.guesses or 0, s.user_name.lower()))
    lines = [header]
    for s in ordered:
        result = "X" if s.guesses is None else str(s.guesses)
        lines.append(f"{s.user_name}: {result}{'*' if s.hard_mode else ''}")
    return "\n".join(lines)
