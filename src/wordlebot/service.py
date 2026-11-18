"""Application logic, independent of Discord. The bot layer only does I/O."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timedelta

from . import golf, leaderboard
from .config import Config, Overrides, is_editable, parse_value, with_overrides
from .db import Database, DeletedScore, Score, Starter
from .game import Mark, puzzle_date, puzzle_number
from .scores import (
    MISMATCH,
    NO_STARTER,
    PENDING,
    VERIFIED,
    ParsedShare,
    grid_rows,
    parse_share,
    verify,
)
from .selection import excluded_words, pick_starter
from .solutions import SolutionSource, peek_solution, sync_solutions
from .words import WordLists

log = logging.getLogger(__name__)

# Sends text to the channel and returns the id of the message it created.
Send = Callable[[str], Awaitable[int]]
# Replaces the text of one of the bot's messages in the channel.
EditMessage = Callable[[int, str], Awaitable[None]]

# Kinds of owed post, see `WordleService.due_posts`.
LATE = "late"  # the post for a date already in play was missed
SCHEDULED = "scheduled"  # the daily post: results, then the starter (or a reminder)
UNANNOUNCED = "unannounced"  # picked outside the daily post, but the send failed

LATE_PREFIX = "⚠️ **LATE** "
REMINDER_PREFIX = "⏰ **REMINDER** "
REROLL_PREFIX = "🔁 **Rerolled!** "
GOLF_CORRECTED_PREFIX = "📝 **Corrected** "

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
class ReactionUpdate:
    """A share whose status reaction should change: `previous` comes off, `current`
    goes on (None for either means nothing to remove or add)."""

    score: Score
    previous: str | None
    current: str | None


@dataclass(frozen=True)
class Actor:
    """Who did an admin action, for the audit log."""

    name: str
    address: str = ""


class AdminError(Exception):
    """An admin action that can't be done; the message is shown to the admin."""


STATUSES = (PENDING, VERIFIED, MISMATCH, NO_STARTER)

# Score edits, see `WordleService.plan_*`.
DELETE = "delete"
RESTORE = "restore"
OVERRIDE = "override"
ADD = "add"


@dataclass(frozen=True)
class ScoreEdit:
    """A planned change to one score, to preview and then apply."""

    action: str  # DELETE | RESTORE | OVERRIDE | ADD
    before: Score | None  # the stored row now (None for ADD and RESTORE)
    after: Score | None  # the row afterwards (None for DELETE)
    deleted_id: int | None = None  # RESTORE: the deleted_scores row

    @property
    def score(self) -> Score:
        result = self.after or self.before
        assert result is not None
        return result


@dataclass(frozen=True)
class PlayerStanding:
    rank: int | None  # None: below the all-time minimum, unranked
    average: float
    games: int


@dataclass(frozen=True)
class EditPreview:
    edit: ScoreEdit
    before: PlayerStanding | None  # the player's all-time standing now
    after: PlayerStanding | None  # ...and after the edit


@dataclass(frozen=True)
class ConfigRow:
    section: str
    key: str
    value: object  # effective value
    file_value: object  # value from config.toml (or the default)
    editable: bool
    overridden: bool
    updated: tuple[str, str] | None  # (when, by) for an override


@dataclass(frozen=True)
class Overview:
    """What the panel's dashboard shows; see `WordleService.overview`."""

    now: datetime
    current: Starter | None
    upcoming: Starter | None
    next_post_at: datetime
    due: list[DuePost]
    solutions_cached: int
    latest_solution: date | None
    today_solution_cached: bool
    pending_scores: int
    submissions_current: int
    submissions_upcoming: int
    visible_answers: list[tuple[Starter, str]]  # recent puzzles whose answer may be shown


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
        self._reactions: list[ReactionUpdate] = []
        # The config as loaded from the file, before panel overrides.
        self.file_config = config
        self.overrides_error: str | None = None

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
        self._reactions += [ReactionUpdate(s, PENDING, s.status) for s in resolved]
        return resolved

    def take_reaction_updates(self) -> list[ReactionUpdate]:
        """Shares whose status changed since the last call (settled, or edited in the
        panel), for the bot to update the reaction on each one's message."""
        updates, self._reactions = self._reactions, []
        return updates

    def take_resolved(self) -> list[Score]:
        """The scores of `take_reaction_updates`, as they are now."""
        return [u.score for u in self.take_reaction_updates()]

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
        """Send whatever is owed and record it. Safe to call again after a failure.

        Returns the daily posts that were owed; a golf post isn't one.
        """
        now = self.now()  # read once, so a run near midnight can't change dates halfway
        due = self.due_posts(now)
        golf_due = self.due_golf(now)
        if not due and not golf_due:
            return []
        today = now.date()
        try:
            await self.refresh(today)
        except Exception:
            log.exception("Solution refresh failed; continuing with cached data")
        try:
            if golf_due:
                # Settle the last hole's shares first, so the card counts them.
                self.reverify_pending(today)
            for milestone in golf_due:
                await self._post_golf(milestone, send)
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

    # --- golf ------------------------------------------------------------
    #
    # Each puzzle is a hole, counted in rounds of 18 from the first starter's puzzle.
    # The morning after a round's front nine, and after its 18th hole, the
    # reconciler posts the round's card. With `missed_posts = "latest"` only the
    # latest milestone is ever owed: one the bot missed entirely is superseded by
    # the next. With "all", every one after the last posted is owed, oldest first.

    def golf_anchor(self) -> int | None:
        """Hole 1 of round 1; None until the bot has picked a starter."""
        return self.db.first_starter_puzzle()

    def _golf_post_at(self, milestone: golf.Milestone) -> datetime:
        post_day = puzzle_date(milestone.last, self.launch) + timedelta(days=1)
        return datetime.combine(post_day, self.config.golf.post_time_local(self.config.schedule.tz))

    def due_golf(self, now: datetime | None = None) -> list[golf.Milestone]:
        """The golf milestones owed at `now`, oldest first."""
        now = now or self.now()
        anchor = self.golf_anchor()
        if not self.config.golf.enabled or anchor is None:
            return []
        yesterday = puzzle_number(now.date() - timedelta(days=1), self.launch)
        done = [m for m in golf.milestones(anchor, yesterday) if now >= self._golf_post_at(m)]
        if self.config.golf.missed_posts == "latest":
            done = done[-1:]
        if latest := self.db.latest_golf_post():
            first, holes = latest
            done = [m for m in done if m.last > first + holes - 1]  # never go back
        return done

    def golf_report(self, milestone: golf.Milestone) -> list[str]:
        """The round's leaderboard and cards as of `milestone`, one message each."""
        scores = self._counted(milestone.first, milestone.last)
        ranked = golf.card(scores, milestone, self.config.game.fail_score)
        return golf.format_round(milestone, ranked, self.config.golf.card_max_players)

    async def _post_golf(self, milestone: golf.Milestone, send: Send, prefix: str = "") -> None:
        report = self.golf_report(milestone)
        sent = []
        for i, message in enumerate(report):
            sent.append((await send((prefix if i == 0 else "") + message), message))
        self.db.mark_golf_posted(milestone.first, milestone.holes, sent)
        log.info("Posted golf round %d after %d holes", milestone.round_no, milestone.holes)

    async def _golf_score_edited(
        self, number: int, send: Send | None, edit_message: EditMessage | None
    ) -> None:
        """Bring the golf posts that included puzzle `number` up to date, as
        `on_score_edit` says. A failure is logged: the score edit itself is done."""
        mode = self.config.golf.on_score_edit
        anchor = self.golf_anchor()
        if mode == "none" or send is None or anchor is None:
            return
        for first, holes in self.db.golf_posts_covering(number):
            milestone = golf.Milestone(golf.round_no(first, anchor), first, holes)
            posted = self.db.golf_post_messages(first, holes)
            report = self.golf_report(milestone)
            if report == [text for _, text in posted]:
                continue  # the edit didn't change this post
            try:
                if mode == "edit" and edit_message is not None and len(report) == len(posted):
                    for (message_id, old), new in zip(posted, report):
                        if old != new:
                            await edit_message(message_id, new)
                    self.db.mark_golf_posted(
                        first, holes, [(mid, new) for (mid, _), new in zip(posted, report)]
                    )
                else:
                    # A correction; also "edit" when the post now needs a different
                    # number of messages than it was sent as.
                    await self._post_golf(milestone, send, GOLF_CORRECTED_PREFIX)
            except Exception:
                log.exception("Could not update the golf post for round %d", milestone.round_no)

    def golf_standings(self, today: date | None = None) -> str:
        """`/golf`: the current round through the last puzzle that is over everywhere
        (yesterday's), or the round that just ended."""
        today = today or self.today()
        anchor = self.golf_anchor()
        if anchor is None:
            return "Golf starts with the first starter the bot posts."
        yesterday = puzzle_number(today - timedelta(days=1), self.launch)
        milestone = golf.progress(anchor, yesterday)
        if milestone is None:
            return f"Golf round 1 tees off with Wordle {anchor:,}; check back the day after."
        return "\n".join(self.golf_report(milestone))

    async def reroll(self, send: Send, actor: Actor | None = None) -> Starter:
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
        if actor is not None:
            self.db.audit(
                actor.name,
                "reroll",
                f"Wordle {number:,}: {previous.word.upper() if previous else '(none)'}"
                f" -> {starter.word.upper()}",
                address=actor.address,
            )
        text = REROLL_PREFIX + starter_announcement(starter)
        if actor is not None and self.config.announce.reroll:
            text += f"\n-# Rerolled from the admin panel by {leaderboard.escape_name(actor.name)}."
        message_id = await send(text)
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

    # --- admin panel -------------------------------------------------------
    #
    # Everything the panel changes is audited. Score edits are planned first, so the
    # panel can show their effect before the admin confirms, and re-planned from the
    # same input on confirm: `apply_edit` refuses one whose row changed in between.

    def overview(self) -> Overview:
        now = self.now()
        today = now.date()
        schedule = self.config.schedule
        post_at = datetime.combine(today, schedule.post_time_local)
        if self.db.daily_post_completed(self.target_date(today)):
            post_at = datetime.combine(today + timedelta(days=1), schedule.post_time_local)
        dates = self.db.solution_dates()
        current = self.current_starter(today)
        upcoming = self.upcoming_starter(today)
        visible = []
        for starter in reversed(self.db.starters_between(None, today)[-7:]):
            if self.answer_visible(starter.target_date, today) and (
                answer := self.db.solution_for(starter.puzzle_number)
            ):
                visible.append((starter, answer))
        return Overview(
            now=now,
            current=current,
            upcoming=upcoming,
            next_post_at=post_at,
            due=self.due_posts(now),
            solutions_cached=len(dates),
            latest_solution=max(dates, default=None),
            today_solution_cached=today in dates,
            pending_scores=len(self.db.scores_with_status(PENDING)),
            submissions_current=self.db.score_count(puzzle_number(today, self.launch)),
            submissions_upcoming=self.db.score_count(puzzle_number(self.target_date(today), self.launch)),
            visible_answers=visible,
        )

    def answer_for_panel(self, number: int, today: date | None = None) -> str | None:
        """A puzzle's answer if `answer_visible` allows showing it, else None."""
        if number < 0 or not self.answer_visible(puzzle_date(number, self.launch), today):
            return None
        return self.db.solution_for(number)

    # Score edits.

    def _stored(self, number: int, user_id: int) -> Score:
        score = self.db.score_for(number, user_id)
        if score is None:
            raise AdminError(f"No score from user {user_id} for Wordle {number:,}.")
        return score

    def plan_delete(self, number: int, user_id: int) -> ScoreEdit:
        return ScoreEdit(DELETE, self._stored(number, user_id), None)

    def plan_override(self, number: int, user_id: int, status: str) -> ScoreEdit:
        if status not in STATUSES:
            raise AdminError(f"Unknown status {status!r}.")
        score = self._stored(number, user_id)
        if score.status == status:
            raise AdminError(f"That score is already {status}.")
        return ScoreEdit(OVERRIDE, score, replace(score, status=status))

    def plan_reverify(self, number: int, user_id: int) -> ScoreEdit:
        """Put a score back to the status the rules give it (undoes an override)."""
        score = self._stored(number, user_id)
        status = self._status(number, grid_rows(score.grid)[0], self.today())
        if score.status == status:
            raise AdminError(f"That score is already {status}, as the rules have it.")
        return ScoreEdit(OVERRIDE, score, replace(score, status=status))

    def plan_restore(self, deleted_id: int) -> ScoreEdit:
        deleted = self.db.deleted_score(deleted_id)
        if deleted is None:
            raise AdminError("That deleted score no longer exists.")
        score = deleted.score
        if self.db.score_for(score.puzzle_number, score.user_id) is not None:
            raise AdminError(
                f"{score.user_name} has a score for Wordle {score.puzzle_number:,} again; "
                "delete that one first."
            )
        # Its status is decided afresh: the starter or solution may be known by now.
        status = self._status(score.puzzle_number, grid_rows(score.grid)[0], self.today())
        return ScoreEdit(RESTORE, None, replace(score, status=status), deleted_id)

    async def plan_add(self, user_id: int, user_name: str, text: str) -> ScoreEdit:
        """A score entered by an admin from a member's share text, verified as usual."""
        game = self.config.game
        if user_id <= 0:
            raise AdminError("Enter the member's Discord user ID (a positive number).")
        user_name = " ".join(user_name.split())
        if not user_name:
            raise AdminError("Enter the member's display name.")
        share = parse_share(text, game.word_length, game.max_guesses)
        if share is None:
            raise AdminError("That isn't a Wordle share (paste the whole thing, header and grid).")
        today = self.today()
        if not self._playable(share.puzzle_number, today):
            raise AdminError(f"Wordle {share.puzzle_number:,} can't have been played yet.")
        if self.db.score_for(share.puzzle_number, user_id) is not None:
            raise AdminError(f"That member already has a score for Wordle {share.puzzle_number:,}.")
        await self.refresh_if_missing(puzzle_date(share.puzzle_number, self.launch))
        status = self._status(share.puzzle_number, share.rows[0], today)
        score = Score(
            share.puzzle_number, user_id, user_name, share.guesses, share.hard_mode,
            share.grid_text, status,
        )
        return ScoreEdit(ADD, None, score)

    def _all_time_standing(self, scores: list[Score], user_id: int) -> PlayerStanding | None:
        rows = leaderboard.standings(
            leaderboard.counted(scores, self.config.scores.count_unverified), self.config.game.fail_score
        )
        minimum = self.config.scores.min_games_all_time
        qualifying = [r for r in rows if r.games >= minimum]
        for rank, row in leaderboard.ranked(qualifying):
            if row.user_id == user_id:
                return PlayerStanding(rank, row.average, row.games)
        for row in rows:
            if row.user_id == user_id:
                return PlayerStanding(None, row.average, row.games)
        return None

    def preview(self, edit: ScoreEdit) -> EditPreview:
        """The player's all-time standing before and after `edit`."""
        target = edit.score
        last = max(puzzle_number(self.today(), self.launch), target.puzzle_number)
        scores = self.db.scores_between(None, last)
        key = (target.puzzle_number, target.user_id)
        after = [s for s in scores if (s.puzzle_number, s.user_id) != key]
        if edit.after is not None:
            after.append(replace(edit.after, submitted_at=edit.after.submitted_at or "9999"))
        return EditPreview(
            edit,
            self._all_time_standing(scores, target.user_id),
            self._all_time_standing(after, target.user_id),
        )

    async def apply_edit(
        self,
        edit: ScoreEdit,
        actor: Actor,
        reason: str,
        send: Send | None = None,
        edit_message: EditMessage | None = None,
    ) -> None:
        """Make a planned edit, audit it, queue its reaction change, announce it if
        configured, and update golf posts it changed (`on_score_edit`). Raises
        AdminError if the score changed since it was planned."""
        reason = " ".join(reason.split())
        if not reason:
            raise AdminError("A reason is required.")
        score = edit.score
        current = self.db.score_for(score.puzzle_number, score.user_id)
        if current != edit.before:
            raise AdminError("That score changed since you looked at it; start again.")
        where = f"Wordle {score.puzzle_number:,}, {score.user_name} ({score.user_id})"
        if edit.action == DELETE:
            self.db.delete_score(score.puzzle_number, score.user_id, actor.name, reason)
            detail = f"{where}: deleted {describe_score(score, self.config.game.max_guesses)}"
            self._reactions.append(ReactionUpdate(score, score.status, None))
        elif edit.action == RESTORE:
            assert edit.deleted_id is not None and edit.after is not None
            if not self.db.restore_score(edit.deleted_id, edit.after.status):
                raise AdminError("That score could not be restored; start again.")
            detail = f"{where}: restored {describe_score(edit.after, self.config.game.max_guesses)}"
            self._reactions.append(ReactionUpdate(edit.after, None, edit.after.status))
        elif edit.action == OVERRIDE:
            assert edit.before is not None and edit.after is not None
            self.db.set_score_status(score.puzzle_number, score.user_id, edit.after.status)
            detail = f"{where}: status {edit.before.status} -> {edit.after.status}"
            self._reactions.append(ReactionUpdate(edit.after, edit.before.status, edit.after.status))
        elif edit.action == ADD:
            assert edit.after is not None
            if not self.db.add_score(edit.after):
                raise AdminError("That member has a score for this puzzle now; start again.")
            detail = f"{where}: added {describe_score(edit.after, self.config.game.max_guesses)}"
        else:
            raise AdminError(f"Unknown edit {edit.action!r}.")
        self.db.audit(actor.name, f"score_{edit.action}", detail, reason, actor.address)
        log.info("Admin %s: %s (%s)", actor.name, detail, reason)
        if send is not None and getattr(self.config.announce, f"score_{edit.action}"):
            await self._announce(send, actor, _announcement(edit))
        await self._golf_score_edited(score.puzzle_number, send, edit_message)

    async def _announce(self, send: Send, actor: Actor, text: str) -> None:
        """Post an admin-action note; a failure is logged, as the action itself is done."""
        try:
            await send(f"🛠️ {text} (by {leaderboard.escape_name(actor.name)}, via the admin panel)")
        except Exception:
            log.exception("Could not announce an admin action")

    async def settle_pending(self, actor: Actor) -> int:
        """Fetch any missing solution and settle the pending scores that can be."""
        before = len(self.db.scores_with_status(PENDING))
        await self.refresh(self.today())
        settled = before - len(self.db.scores_with_status(PENDING))
        self.db.audit(actor.name, "settle_pending", f"{settled} score(s) settled", address=actor.address)
        return settled

    # Config overrides.

    def load_overrides(self) -> None:
        """Apply the overrides stored in the database (at startup).

        If they no longer validate against the file (it was edited since), the bot
        runs on the file alone and the panel shows why.
        """
        try:
            self.config = with_overrides(self.file_config, self.db.config_overrides())
            self.overrides_error = None
        except ValueError as exc:
            self.config = self.file_config
            self.overrides_error = str(exc)
            log.error("Ignoring the panel's config overrides: %s", exc)

    def config_rows(self) -> list[ConfigRow]:
        overrides = self.db.config_overrides()
        meta = self.db.config_override_meta()
        rows = []
        for section_field in fields(Config):
            section = section_field.name
            if section == "token":
                continue
            for key_field in fields(getattr(self.config, section)):
                key = key_field.name
                rows.append(
                    ConfigRow(
                        section,
                        key,
                        getattr(getattr(self.config, section), key),
                        getattr(getattr(self.file_config, section), key),
                        is_editable(section, key),
                        (section, key) in overrides,
                        meta.get((section, key)),
                    )
                )
        return rows

    def _apply_overrides(self, overrides: Overrides) -> Config:
        try:
            return with_overrides(self.file_config, overrides)
        except ValueError as exc:
            raise AdminError(str(exc).removeprefix("Invalid config: ")) from None

    async def set_config(
        self, section: str, key: str, text: str, actor: Actor, send: Send | None = None
    ) -> None:
        try:
            value = parse_value(section, key, text)
        except ValueError as exc:
            raise AdminError(str(exc)) from None
        old = getattr(getattr(self.config, section), key)
        overrides = self.db.config_overrides()
        overrides[(section, key)] = value
        self.config = self._apply_overrides(overrides)
        self.overrides_error = None
        self.db.set_config_override(section, key, value, actor.name)
        await self._config_changed(section, key, old, value, actor, send)

    async def reset_config(self, section: str, key: str, actor: Actor, send: Send | None = None) -> None:
        overrides = self.db.config_overrides()
        if overrides.pop((section, key), None) is None:
            raise AdminError(f"[{section}] {key} isn't overridden.")
        old = getattr(getattr(self.config, section), key)
        self.config = self._apply_overrides(overrides)
        self.db.delete_config_override(section, key)
        new = getattr(getattr(self.config, section), key)
        await self._config_changed(section, key, old, new, actor, send, reset=True)

    async def _config_changed(
        self, section: str, key: str, old: object, new: object, actor: Actor,
        send: Send | None, reset: bool = False,
    ) -> None:
        detail = f"[{section}] {key}: {old!r} -> {new!r}" + (" (reset to file)" if reset else "")
        self.db.audit(actor.name, "config_reset" if reset else "config_set", detail, address=actor.address)
        log.info("Admin %s: %s", actor.name, detail)
        # Settings that decide a status (a starter's availability, what counts) may
        # settle pending scores.
        self.reverify_pending()
        if send is not None and self.config.announce.config_change and section != "announce":
            await self._announce(send, actor, f"Setting `{section}.{key}` changed to `{new!r}`")


def describe_score(score: Score, max_guesses: int) -> str:
    """A score as its share header shows it, plus its status: "4/6* (verified)"."""
    result = "X" if score.guesses is None else str(score.guesses)
    return f"{result}/{max_guesses}{'*' if score.hard_mode else ''} ({score.status})"


def _announcement(edit: ScoreEdit) -> str:
    score = edit.score
    who = leaderboard.escape_name(score.user_name)
    where = f"Wordle {score.puzzle_number:,}"
    if edit.action == DELETE:
        return f"Removed {who}'s score for {where}"
    if edit.action == RESTORE:
        return f"Restored {who}'s score for {where}"
    if edit.action == OVERRIDE:
        return f"Changed {who}'s score for {where} to {score.status}"
    return f"Added {who}'s score for {where}"
