"""Application logic, independent of Discord. The bot layer only does I/O."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from . import leaderboard
from .config import Config
from .db import Database, Score, Starter
from .game import Mark, puzzle_date, puzzle_number
from .scores import MISMATCH, PENDING, ParsedShare, grid_rows, parse_share, verify
from .selection import excluded_words, pick_starter
from .solutions import SolutionSource, peek_solution, sync_solutions
from .words import WordLists

log = logging.getLogger(__name__)

# Sends text to the channel and returns the id of the message it created.
Send = Callable[[str], Awaitable[int]]

# Kinds of owed post, see `WordleService.due_posts`.
LATE = "late"  # the post for a date already in play was missed
SCHEDULED = "scheduled"  # the daily post: results, then the starter (or a reminder)
UNANNOUNCED = "unannounced"  # picked outside the daily post, but the send failed

LATE_PREFIX = "⚠️ **LATE** "
REMINDER_PREFIX = "⏰ **REMINDER** "
REROLL_PREFIX = "🔁 **Rerolled!** "

# How long `refresh_if_missing` waits before asking the source again for a
# solution it failed to get, so a failing endpoint isn't hit on every tick.
REFRESH_RETRY = timedelta(minutes=15)

# How many days old a puzzle must be, in the bot's timezone, before its answer
# may appear in user-facing output. A Wordle day runs on each player's local
# midnight and the furthest-behind timezone (UTC-12) is at most about a day
# behind any bot timezone, so yesterday's puzzle can still be in play somewhere;
# the day before yesterday's cannot. See `WordleService.answer_visible`.
ANSWER_REVEAL_DAYS = 2


def starter_announcement(starter: Starter) -> str:
    return (
        f"🟩 **Starting word for Wordle {starter.puzzle_number:,}** "
        f"({starter.target_date:%A, %B %d}):\n"
        f"# `{starter.word.upper()}`\n"
        "Open with this word, then paste your share result here to be counted."
    )


class RerollRefused(Exception):
    """`WordleService.reroll` declined to replace the starter."""


class PuzzleInPlay(RerollRefused):
    """The starter's puzzle has already started."""


class SubmissionsExist(RerollRefused):
    """`count` members have already submitted for the starter's puzzle."""

    def __init__(self, puzzle_number: int, count: int) -> None:
        super().__init__(f"{count} submission(s) already exist for puzzle {puzzle_number}")
        self.puzzle_number = puzzle_number
        self.count = count


@dataclass(frozen=True)
class DuePost:
    target: date  # the puzzle date the starter is for
    kind: str  # LATE | SCHEDULED | UNANNOUNCED


@dataclass(frozen=True)
class Submission:
    share: ParsedShare
    status: str
    inserted: bool  # False if the user had already submitted for this puzzle


class WordleService:
    def __init__(
        self,
        config: Config,
        db: Database,
        source: SolutionSource,
        lists: WordLists,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.source = source
        self.lists = lists
        self.rng = rng or random.Random()
        self.clock = clock
        self._refresh_lock: asyncio.Lock | None = None
        self._refresh_loop: asyncio.AbstractEventLoop | None = None
        self._refresh_retry_at: datetime | None = None
        self._resolved: list[Score] = []

    # --- time ------------------------------------------------------------

    @property
    def launch(self) -> date:
        return self.config.solutions.launch

    def now(self) -> datetime:
        """The current time in the bot's timezone (from `clock` under test)."""
        return self.clock() if self.clock else datetime.now(self.config.schedule.tz)

    def today(self) -> date:
        return self.now().date()

    def target_date(self, today: date | None = None) -> date:
        """The date the next starter is for. Pass `today` to reuse one clock reading."""
        return (today or self.today()) + timedelta(
            days=self.config.schedule.target_offset_days
        )

    def results_date(self, today: date | None = None) -> date:
        """The puzzle the daily post reports on, and `/results` defaults to.

        It is the one before the target, the last puzzle played with a starter
        before the one being announced: today's with the default offset of 1,
        yesterday's when the starter is posted on the day (offset 0). Never later
        than today, as a puzzle that hasn't started has no results.
        """
        today = today or self.today()
        return min(self.target_date(today) - timedelta(days=1), today)

    # --- solutions -------------------------------------------------------

    def _refresh_guard(self) -> asyncio.Lock:
        """One refresh at a time. Rebuilt per event loop, as a lock is tied to one."""
        loop = asyncio.get_running_loop()
        if self._refresh_lock is None or self._refresh_loop is not loop:
            self._refresh_lock, self._refresh_loop = asyncio.Lock(), loop
        return self._refresh_lock

    def _solution_cached(self, day: date) -> bool:
        return self.db.solution_for(puzzle_number(day, self.launch)) is not None

    async def refresh(self, today: date | None = None) -> None:
        """Bring the solution cache up to today and verify any scores that were waiting."""
        async with self._refresh_guard():
            await self._refresh(self.now(), today)

    async def _refresh(self, now: datetime, today: date | None = None) -> None:
        today = today or now.date()
        try:
            await sync_solutions(
                self.db,
                self.source,
                self.launch,
                today,
                self.config.solutions.fetch_concurrency,
            )
        finally:
            # Per-day fetch failures don't raise, so "failed" means still missing.
            self._refresh_retry_at = None if self._solution_cached(today) else now + REFRESH_RETRY
        self.reverify_pending(today)

    async def refresh_if_missing(self, day: date | None = None) -> None:
        """Refresh if the solution for `day` (default today) should be cached but isn't.

        Cheap enough to call on every tick, share and report: it is a single lookup
        when the solution is there, concurrent callers share one refresh, and after
        an attempt that didn't produce the solution the source is left alone for
        `REFRESH_RETRY`. Never fetches a date after today, and never raises.
        """
        now = self.now()
        today = now.date()
        day = day or today
        if not self.launch <= day <= today or self._solution_cached(day):
            return
        try:
            async with self._refresh_guard():
                if self._solution_cached(day):
                    return  # a concurrent caller fetched it while we waited
                if self._refresh_retry_at and now < self._refresh_retry_at:
                    return
                await self._refresh(now, today)
                if not self._solution_cached(day):
                    self._refresh_retry_at = now + REFRESH_RETRY
        except Exception:
            log.exception("Solution refresh failed; continuing with cached data")

    def _starter_may_come(self, day: date, today: date) -> bool:
        """Whether a starter that `day` lacks can still be picked (see `due_posts`)."""
        if day < today:
            return False
        if day >= self.target_date(today):
            return True  # its daily post is yet to be made, today or on a later day
        # In play or about to be, and skipped by its daily post: owed as a late
        # post, but only for dates after the first one we ever picked for.
        first = self.db.first_starter_date()
        return first is not None and day > first

    def _status(self, number: int, first_row: tuple[Mark, ...], today: date) -> str:
        """Where a share for puzzle `number` stands, given its first grid row.

        A share can arrive before its starter: members in timezones ahead of the
        bot's play tomorrow's puzzle before the evening post. It waits as pending
        and is `no_starter` only once no starter can come any more.
        """
        starter = self.db.starter_for_puzzle(number)
        if starter is None and self._starter_may_come(puzzle_date(number, self.launch), today):
            return PENDING
        return verify(first_row, starter.word if starter else None, self.db.solution_for(number))

    def reverify_pending(self, today: date | None = None) -> list[Score]:
        """Settle pending scores whose starter and solution are both known, or
        whose starter is never coming; returns them, updated.

        They are also queued for `take_resolved`, whichever code path got here.
        """
        today = today or self.today()
        resolved = []
        for score in self.db.scores_with_status(PENDING):
            status = self._status(score.puzzle_number, grid_rows(score.grid)[0], today)
            if status != PENDING:
                self.db.set_score_status(score.puzzle_number, score.user_id, status)
                resolved.append(replace(score, status=status))
        self._resolved += resolved
        return resolved

    def take_resolved(self) -> list[Score]:
        """Scores that left `pending` since the last call, for the caller to act on
        (the bot updates the reaction on each one's message)."""
        resolved, self._resolved = self._resolved, []
        return resolved

    # --- starters --------------------------------------------------------

    async def choose_starter(
        self,
        target: date,
        also_exclude: set[str] = frozenset(),  # type: ignore[assignment]
        today: date | None = None,
    ) -> Starter:
        """Pick a word for `target` without saving it."""
        cfg = self.config.selection
        forbidden: set[str] = set(also_exclude)
        if cfg.avoid_target_solution:
            if answer := await peek_solution(self.source, target):
                forbidden.add(answer)
        word = pick_starter(
            cfg,
            self.lists,
            self.db.all_solution_words(),
            excluded_words(self.db, cfg, target, today or self.today()),
            self.rng,
            forbidden,
        )
        return Starter(target, puzzle_number(target, self.launch), word, cfg.strategy)

    def upcoming_starter(self, today: date | None = None) -> Starter | None:
        return self.db.starter_for_date(self.target_date(today))

    def current_starter(self, today: date | None = None) -> Starter | None:
        return self.db.starter_for_date(today or self.today())

    # --- posting ---------------------------------------------------------
    #
    # The database is the source of truth for what has reached the channel. A pick
    # is saved before it is sent and marked announced after, so a failed or
    # interrupted send is retried with the same word. Callers must not overlap
    # `post_due` and `reroll` (the bot holds a lock around both).

    def due_posts(self, now: datetime | None = None) -> list[DuePost]:
        """What the channel is owed at `now`, oldest date first."""
        now = now or self.now()
        today = now.date()
        schedule = self.config.schedule
        due = []
        # Dates already in play should have been announced by an earlier daily post,
        # but only those the bot was around for. A date up to the first one we ever
        # picked for (any date, on a new install) was never ours to announce, and
        # its puzzle may already have been played with another word. A pick that
        # was saved but never sent is ours whatever its date.
        first = self.db.first_starter_date()
        for offset in range(schedule.target_offset_days):
            day = today + timedelta(days=offset)
            starter = self.db.starter_for_date(day)
            if starter is None:
                if first is not None and day > first:
                    due.append(DuePost(day, LATE))
            elif not starter.announced:
                due.append(DuePost(day, LATE))

        target = today + timedelta(days=schedule.target_offset_days)
        starter = self.db.starter_for_date(target)
        post_at = datetime.combine(today, schedule.post_time_local)
        if now >= post_at and not self.db.daily_post_completed(target):
            due.append(DuePost(target, SCHEDULED))
        elif starter is not None and not starter.announced:
            due.append(DuePost(target, UNANNOUNCED))
        return due

    async def post_due(self, send: Send) -> list[DuePost]:
        """Send whatever is owed and record it. Safe to call again after a failure."""
        now = self.now()  # read once, so a run near midnight can't change dates halfway
        due = self.due_posts(now)
        if not due:
            return []
        today = now.date()
        try:
            await self.refresh(today)
        except Exception:
            log.exception("Solution refresh failed; continuing with cached data")
        try:
            for post in due:
                await self._post(post, today, send)
        finally:
            # A starter picked just now may be the one a share was waiting for, and
            # with its solution already cached no refresh would come to settle it.
            self.reverify_pending(today)
        return due

    async def _post(self, post: DuePost, today: date, send: Send) -> None:
        target = post.target
        scheduled = post.kind == SCHEDULED
        if (
            scheduled
            and self.config.schedule.post_daily_results
            and not self.db.results_posted(target)
        ):
            await send(self.daily_results(today=today))
            self.db.mark_results_posted(target)

        starter = self.db.starter_for_date(target)
        if starter is None:
            starter = self.db.add_starter(await self.choose_starter(target, today=today))
        if starter.announced:
            if scheduled:
                # Already announced earlier (e.g. /reroll), so remind instead.
                await send(REMINDER_PREFIX + starter_announcement(starter))
                self.db.mark_daily_post_completed(target)
                log.info("Starter for %s already announced; sent reminder", target)
            return
        prefix = LATE_PREFIX if post.kind == LATE else ""
        message_id = await send(prefix + starter_announcement(starter))
        self.db.mark_announced(target, message_id, daily_post=scheduled)
        log.info("Posted starter %s for %s (%s)", starter.word, target, post.kind)

    async def reroll(self, send: Send) -> Starter:
        """Replace the upcoming starter and announce it.

        Raises `PuzzleInPlay` if that puzzle has started, which with
        `target_offset_days = 0` is always: the starter is for today's puzzle,
        live since midnight. Raises `SubmissionsExist` if anyone has submitted
        for it (members in timezones ahead of the bot's play it early): a share
        is checked against the starter on record when it is settled, so a new
        word would turn those who opened with the announced one into mismatches.
        """
        today = self.today()  # read once; nothing below may re-read the clock
        target = self.target_date(today)
        if target <= today:
            raise PuzzleInPlay
        number = puzzle_number(target, self.launch)
        if count := self.db.score_count(number):
            raise SubmissionsExist(number, count)
        # Never a word this date already had, however many rerolls ago.
        exclude = self.db.retired_words(target)
        if previous := self.db.starter_for_date(target):
            exclude.add(previous.word)
        starter = await self.choose_starter(target, exclude, today)
        self.db.save_starter(starter)
        message_id = await send(REROLL_PREFIX + starter_announcement(starter))
        self.db.mark_announced(target, message_id)
        return starter

    # --- scores ----------------------------------------------------------

    async def submit(
        self,
        user_id: int,
        user_name: str,
        text: str,
        channel_id: int | None = None,
        message_id: int | None = None,
    ) -> Submission | None:
        """Record a pasted share; `channel_id`/`message_id` say where it was pasted."""
        game = self.config.game
        share = parse_share(text, game.word_length, game.max_guesses)
        if share is None:
            return None
        # Reject puzzles that can't have been played yet anywhere on Earth. The
        # number is whatever the member typed, so it is range-checked as a number:
        # converting an absurd one to a date (or handing it to SQLite) would raise.
        today = self.today()
        if not self._playable(share.puzzle_number, today):
            return None
        played = puzzle_date(share.puzzle_number, self.launch)
        # Today's solution is normally not cached until someone needs it. If this
        # fails the score is stored as pending and settled by a later refresh.
        await self.refresh_if_missing(played)

        status = self._status(share.puzzle_number, share.rows[0], today)
        inserted = self.db.add_score(
            Score(
                share.puzzle_number,
                user_id,
                user_name,
                share.guesses,
                share.hard_mode,
                share.grid_text,
                status,
                channel_id,
                message_id,
            )
        )
        if inserted and status == MISMATCH:
            log.info("Starter mismatch: %s on puzzle %d", user_name, share.puzzle_number)
        return Submission(share, status, inserted)

    def _playable(self, number: int, today: date | None = None) -> bool:
        """Whether puzzle `number` exists and can have been played somewhere on Earth:
        from the launch puzzle (0) to tomorrow's, the furthest timezone ahead of ours."""
        return 0 <= number <= puzzle_number(today or self.today(), self.launch) + 1

    # --- reports ---------------------------------------------------------

    def _counted(self, first: int | None, last: int) -> list[Score]:
        return leaderboard.counted(
            self.db.scores_between(first, last), self.config.scores.count_unverified
        )

    def daily_results(self, number: int | None = None, today: date | None = None) -> str:
        """Results for puzzle `number`; by default the one for `results_date`.

        `number` is user input (`/results puzzle:`), so one outside the playable
        range gets a plain message instead of reaching the database.
        """
        if number is None:
            number = puzzle_number(self.results_date(today), self.launch)
        elif number < 0:
            return f"There is no Wordle {number:,}; puzzle numbers start at 0."
        elif not self._playable(number, today):
            return f"Wordle {number:,} hasn't been played yet."
        starter = self.db.starter_for_puzzle(number)
        return leaderboard.format_daily(
            number, starter.word if starter else None, self._counted(number, number)
        )

    def _board(self, title: str, first: int | None, last: int, min_games: int) -> str:
        """A standings board for puzzles `first`..`last` (`first=None`: all time).

        A bounded period can't ask for more games than it has had puzzles so far,
        or the monthly board would be empty for the first days of every month.
        """
        if first is not None:
            min_games = max(1, min(min_games, last - first + 1))
        rows = leaderboard.standings(self._counted(first, last), self.config.game.fail_score)
        return leaderboard.format_standings(title, rows, min_games)

    def weekly(self) -> str:
        last = puzzle_number(self.today(), self.launch)
        days = self.config.scores.weekly_window_days
        return self._board(
            f"Last {days} days", last - days + 1, last, self.config.scores.min_games_weekly
        )

    def monthly(self) -> str:
        today = self.today()
        first = puzzle_number(today.replace(day=1), self.launch)
        last = puzzle_number(today, self.launch)
        return self._board(
            today.strftime("%B %Y"), first, last, self.config.scores.min_games_monthly
        )

    def all_time(self) -> str:
        last = puzzle_number(self.today(), self.launch)
        return self._board("All time", None, last, self.config.scores.min_games_all_time)

    def answer_visible(self, played: date, today: date | None = None) -> bool:
        """Whether the answer of the puzzle for `played` may be shown to members.

        The one spoiler rule for output: every place that shows a solution must
        ask here (and keep it inside `||spoiler||` tags).
        """
        return played <= (today or self.today()) - timedelta(days=ANSWER_REVEAL_DAYS)

    def history(self, count: int) -> str:
        """Our recent starters, with the answers of puzzles that are over everywhere
        (see `answer_visible`); yesterday's and today's stay hidden."""
        today = self.today()
        starters = self.db.starters_between(None, self.target_date(today))[-count:]
        if not starters:
            return "No starters posted yet."
        lines = ["**Recent starters**"]
        for s in reversed(starters):
            line = f"#{s.puzzle_number:,} ({s.target_date:%a %b %d}): `{s.word.upper()}`"
            if self.answer_visible(s.target_date, today) and (
                answer := self.db.solution_for(s.puzzle_number)
            ):
                line += f" → answer ||{answer.upper()}||"
            lines.append(line)
        lines.append(f"-# Answers appear {ANSWER_REVEAL_DAYS} days after their puzzle.")
        return "\n".join(lines)
