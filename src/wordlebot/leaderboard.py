"""Aggregating scores into daily results and standings, and formatting them."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .db import Score
from .scores import VERIFIED, PENDING, effective_score


# Characters that start inline markdown, a mention (<@id>, <@&role>, <#channel>),
# a custom emoji or a masked link wherever they appear.
_INLINE_MARKUP = re.compile(r"([\\*_~`|<>\[\]()#])")
# Markup that only counts at the start of a line: "- item", "+ item", "1. item".
_LINE_START_MARKUP = re.compile(r"^(?:([-+])|(\d+)([.)]))(?=\s)")
_ZERO_WIDTH_SPACE = "\u200b"


def escape_name(name: str) -> str:
    """A member's display name made safe to echo in a Discord message.

    Display names are user-supplied, so one like `<@1234>`, `@everyone` or `**x**`
    would otherwise ping someone or break the formatting around it. Every
    user-facing output that includes a name must pass it through here; names are
    stored raw and escaped only on the way out.
    """
    name = " ".join(name.split())  # a line break would start a new markdown line
    name = _INLINE_MARKUP.sub(r"\\\1", name)
    name = _LINE_START_MARKUP.sub(
        lambda m: f"\\{m[1]}" if m[1] else f"{m[2]}\\{m[3]}", name
    )
    # Backslashes don't defuse @everyone / @here; a zero-width space does.
    return name.replace("@", "@" + _ZERO_WIDTH_SPACE)


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


# How many players below a board's minimum are named before the rest are counted.
BELOW_MINIMUM_NAMED = 5


def standings(scores: list[Score], fail_score: int) -> list[Standing]:
    """One row per player, best first, whatever order `scores` came in.

    Every player is included; `format_standings` applies a board's minimum games.
    """
    by_user: dict[int, list[Score]] = defaultdict(list)
    for s in scores:
        by_user[s.user_id].append(s)

    result = []
    for user_id, games in by_user.items():
        # The name on the most recently submitted score, not the last row read.
        latest = max(games, key=lambda g: (g.submitted_at, g.puzzle_number))
        values = [effective_score(g.guesses, fail_score) for g in games]
        solved = [g.guesses for g in games if g.guesses is not None]
        result.append(
            Standing(
                user_id=user_id,
                user_name=latest.user_name,
                games=len(games),
                wins=len(solved),
                average=sum(values) / len(values),
                best=min(solved, default=None),
            )
        )
    result.sort(key=lambda s: (*_rank_key(s), s.user_name.lower(), s.user_id))
    return result


def _rank_key(s: Standing) -> tuple[float, int]:
    """What a rank is decided on: lower average first, then more games played."""
    return (s.average, -s.games)


def ranked(rows: list[Standing]) -> list[tuple[int, Standing]]:
    """Competition ranking ("1, 2, 2, 4") of rows already in standings order.

    Players tie only on the same average over the same number of games; at an
    equal average the one who played more ranks higher.
    """
    result: list[tuple[int, Standing]] = []
    for position, s in enumerate(rows, 1):
        tied = result and _rank_key(result[-1][1]) == _rank_key(s)
        result.append((result[-1][0] if tied else position, s))
    return result


def _games(count: int) -> str:
    return f"{count} game" if count == 1 else f"{count} games"


def _below_minimum(rows: list[Standing], min_games: int) -> str:
    """One line accounting for the players a board's minimum leaves unranked."""
    rows = sorted(rows, key=lambda s: (-s.games, s.user_name.lower(), s.user_id))
    names = [f"{escape_name(s.user_name)} ({s.games})" for s in rows[:BELOW_MINIMUM_NAMED]]
    if len(rows) > BELOW_MINIMUM_NAMED:
        names.append(f"+{len(rows) - BELOW_MINIMUM_NAMED} more")
    return f"Under {_games(min_games)}, not ranked: {', '.join(names)}"


def format_standings(title: str, rows: list[Standing], min_games: int = 1) -> str:
    """A board of the players with at least `min_games`, and a line for the rest."""
    if min_games > 1:
        title = f"{title} (min {_games(min_games)})"
    qualifying = [s for s in rows if s.games >= min_games]
    below = [s for s in rows if s.games < min_games]
    lines = [f"**{title}**"]
    if not qualifying:
        lines.append("No qualifying results yet.")
    for rank, s in ranked(qualifying):
        # "\\." keeps Discord from rendering an ordered list, which it would
        # renumber 1, 2, 3, ... and so hide tied ranks.
        lines.append(
            f"{rank}\\. {escape_name(s.user_name)} — avg **{s.average:.2f}** "
            f"({s.games} played, {s.wins} solved)"
        )
    if below:
        lines.append(_below_minimum(below, min_games))
    return "\n".join(lines)


def format_daily(puzzle_number: int, starter: str | None, scores: list[Score]) -> str:
    header = f"**Wordle {puzzle_number:,} results**"
    if starter:
        header += f" (starter: `{starter.upper()}`)"
    if not scores:
        return f"{header}\nNo verified results."
    # Equal guesses are listed by name, then id, so the order never depends on the query.
    ordered = sorted(
        scores,
        key=lambda s: (s.guesses is None, s.guesses or 0, s.user_name.lower(), s.user_id),
    )
    lines = [header]
    for s in ordered:
        result = "X" if s.guesses is None else str(s.guesses)
        lines.append(f"{escape_name(s.user_name)}: {result}{'*' if s.hard_mode else ''}")
    return "\n".join(lines)
