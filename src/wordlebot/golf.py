"""Golf: every puzzle is a par-4 hole, and 18 in a row are a par-72 round.

Pure logic, no I/O. Rounds run back to back from an anchor puzzle (the first one
the bot ever picked a starter for): holes 1-9 are the front nine, 10-18 the back
nine. A hole's strokes are its guesses; a failed hole (X) and a hole the player
didn't play (or that didn't count, e.g. a mismatched starter) both score
`fail_score`, so skipping never beats playing.

A round can be shown live, with its last hole being today's puzzle: a player who
hasn't shared it yet is still to play it, so it adds nothing to their total until
they do. Players are ranked by score to par, which is the same order as total
strokes once everyone has the same holes counted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .leaderboard import escape_name
from .scores import effective_score

if TYPE_CHECKING:
    from .db import Score

PAR = 4
NINE = 9
HOLES = 2 * NINE

FAILED = "X"
MISSED = "-"
YET = "·"  # the live hole, not played yet

# Longest name shown on the leaderboard, which is monospace and should fit a phone.
CARD_NAME_WIDTH = 12

# Each message of a golf post stays under this, below Discord's 2,000 characters
# with room for the note or prefix added to it (see `format_round`).
MESSAGE_LIMIT = 1900


@dataclass(frozen=True)
class Milestone:
    """A point a round is reported at: after its front nine, or when it's over."""

    round_no: int  # 1-based
    first: int  # puzzle number of the round's hole 1
    holes: int  # holes played so far, 1..HOLES

    @property
    def last(self) -> int:
        return self.first + self.holes - 1

    @property
    def final(self) -> bool:
        return self.holes == HOLES


def round_start(number: int, anchor: int) -> int:
    """Puzzle number of hole 1 of the round puzzle `number` belongs to."""
    return anchor + (number - anchor) // HOLES * HOLES


def round_no(number: int, anchor: int) -> int:
    return (number - anchor) // HOLES + 1


def progress(anchor: int, last_played: int) -> Milestone | None:
    """The round in progress once puzzle `last_played` is over: its first puzzle and
    how many holes are done. None before the first hole is over."""
    if last_played < anchor:
        return None
    first = round_start(last_played, anchor)
    return Milestone(round_no(last_played, anchor), first, last_played - first + 1)


def milestones(anchor: int, last_played: int) -> list[Milestone]:
    """Every nine finished by puzzle `last_played` (front nines and whole rounds),
    oldest first."""
    result = []
    for nines in range(1, (last_played - anchor + 1) // NINE + 1):
        last = anchor + nines * NINE - 1
        first = round_start(last, anchor)
        result.append(Milestone(round_no(last, anchor), first, last - first + 1))
    return result


def latest_milestone(anchor: int, last_played: int) -> Milestone | None:
    """The most recent nine to finish by puzzle `last_played`; None before the
    first front nine is over."""
    done = milestones(anchor, last_played)
    return done[-1] if done else None


@dataclass(frozen=True)
class Player:
    user_id: int
    name: str  # raw; escape on the way out
    cells: tuple[str, ...]  # per hole: guesses, FAILED, MISSED or YET
    strokes: tuple[int | None, ...]  # per hole; None = YET, counts nothing so far

    def strokes_in(self, start: int, stop: int) -> int:
        """Strokes on holes `start`..`stop - 1` (0-based), as a nine's subtotal."""
        return sum(s for s in self.strokes[start:stop] if s is not None)

    @property
    def total(self) -> int:
        return self.strokes_in(0, len(self.strokes))

    @property
    def to_par(self) -> int:
        return self.total - PAR * sum(s is not None for s in self.strokes)

    @property
    def played(self) -> int:
        return sum(cell not in (MISSED, YET) for cell in self.cells)


def card(
    scores: list[Score], milestone: Milestone, fail_score: int, live: bool = False
) -> list[tuple[int, Player]]:
    """Everyone with a counted score in the round's holes so far, ranked by score to
    par ("1, 2, 2, 4"). `scores` must already be filtered to the ones that count.

    `live`: the milestone's last hole is still in play, so a player without a score
    for it is YET rather than MISSED."""
    by_user: dict[int, dict[int, Score]] = defaultdict(dict)
    for s in scores:
        if milestone.first <= s.puzzle_number <= milestone.last:
            by_user[s.user_id][s.puzzle_number] = s

    players = []
    for user_id, holes in by_user.items():
        latest = max(holes.values(), key=lambda g: (g.submitted_at, g.puzzle_number))
        cells, strokes = [], []
        for number in range(milestone.first, milestone.last + 1):
            score = holes.get(number)
            if score is None and live and number == milestone.last:
                cells.append(YET)
                strokes.append(None)
            elif score is None:
                cells.append(MISSED)
                strokes.append(fail_score)
            else:
                cells.append(FAILED if score.guesses is None else str(score.guesses))
                strokes.append(effective_score(score.guesses, fail_score))
        players.append(Player(user_id, latest.user_name, tuple(cells), tuple(strokes)))

    players.sort(key=lambda p: (p.to_par, p.name.lower(), p.user_id))
    result: list[tuple[int, Player]] = []
    for position, p in enumerate(players, 1):
        tied = result and result[-1][1].to_par == p.to_par
        result.append((result[-1][0] if tied else position, p))
    return result


def format_to_par(value: int) -> str:
    return "E" if value == 0 else f"{value:+d}"


def _card_name(name: str) -> str:
    """A name made safe and short for the leaderboard's code block (no markdown
    applies inside one, but a backtick would end it)."""
    name = " ".join(name.split()).replace("`", "'")
    if len(name) > CARD_NAME_WIDTH:
        name = name[: CARD_NAME_WIDTH - 1] + "…"
    return name


def _block(lines: list[str]) -> str:
    return "```\n" + "\n".join(lines) + "\n```"


def hole_mark(cell: str) -> str:
    """A card cell's score name, as scorecards mark it: "ace" (a hole in one),
    "eagle" or better, "birdie", "par", "bogey", "double" (bogey or worse),
    "failed", "missed" or "yet" (the live hole, not played yet)."""
    if cell == YET:
        return "yet"
    if cell == FAILED:
        return "failed"
    if cell == MISSED:
        return "missed"
    guesses = int(cell)
    if guesses == 1:
        return "ace"
    diff = guesses - PAR
    if diff <= -2:
        return "eagle"
    return {-1: "birdie", 0: "par", 1: "bogey"}.get(diff, "double")


def position(rank: int, ranked: list[tuple[int, Player]]) -> str:
    """A rank as shown: "T2" when shared."""
    tied = sum(r == rank for r, _ in ranked) > 1
    return f"T{rank}" if tied else str(rank)


def _blocks(head: list[str], rows: list[str], first_prefix: str = "") -> list[str]:
    """`rows` under `head` in code blocks, as many messages as it takes to keep
    each under MESSAGE_LIMIT. Each message repeats `head`; the first starts with
    `first_prefix`."""
    messages: list[str] = []
    prefix, chunk = first_prefix, list(head)
    for row in rows:
        if len(chunk) > len(head) and len(prefix) + len(_block(chunk + [row])) > MESSAGE_LIMIT:
            messages.append(prefix + _block(chunk))
            prefix, chunk = "", list(head)
        chunk.append(row)
    messages.append(prefix + _block(chunk))
    return messages


def stage(milestone: Milestone, live: bool = False) -> str:
    """How far the round has got: "final", "after the front nine", "through N holes",
    or, `live`, "hole N in play"."""
    if live:
        return f"hole {milestone.holes} in play"
    if milestone.final:
        return "final"
    if milestone.holes == NINE:
        return "after the front nine"
    return f"through {milestone.holes} hole{'s' if milestone.holes != 1 else ''}"


def format_round(
    milestone: Milestone, ranked: list[tuple[int, Player]], max_players: int = 0, live: bool = False
) -> list[str]:
    """The round's leaderboard as text, the part of a golf post that isn't the
    scorecard image (`golf_card.render`).

    Every message is under MESSAGE_LIMIT, so `bot.chunks` never splits a code
    block: a long board continues in another message with its header row
    repeated. `max_players` (0 = everyone) is the number of players on the card
    image; everyone is on the leaderboard. `live` as for `card`.
    """
    m = milestone
    par = PAR * m.holes
    span = f"Wordle {m.first:,}" if m.holes == 1 else f"Wordle {m.first:,}–{m.last:,}"
    header = f"⛳ **Golf round {m.round_no}, {stage(m, live)}** ({span}, par {par})"
    if not ranked:
        return [f"{header}\nNo scores this round."]

    leaders = [p for rank, p in ranked if rank == 1]
    names = ", ".join(f"**{escape_name(p.name)}**" for p in leaders)
    result = f"({format_to_par(leaders[0].to_par)}, {leaders[0].total})"
    if m.final and not live:
        label = "🏆 Winner:" if len(leaders) == 1 else "🏆 Co-champions:"
    else:
        label = "Leader:" if len(leaders) == 1 else "Leaders:"

    width = max(len(_card_name(p.name)) for _, p in ranked)
    board = [
        f"{position(rank, ranked):<4}{_card_name(p.name):<{width}}  "
        f"{p.total:>3}  {format_to_par(p.to_par):>3}  {f'{p.played}/{m.holes}':>5}"
        for rank, p in ranked
    ]
    messages = _blocks(
        [f"{'Pos':<4}{'Name':<{width}}  {'TOT':>3}  {'PAR':>3}  {'PLD':>5}"],
        board,
        f"{header}\n{label} {names} {result}\n",
    )
    if 0 < max_players < len(ranked):
        messages[-1] += (
            f"\nThe card shows the top {max_players} of {len(ranked)}; "
            "everyone is on the leaderboard."
        )
    return messages
