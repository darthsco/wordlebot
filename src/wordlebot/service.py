"""Application logic, independent of Discord. The bot layer only does I/O."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import leaderboard
from .config import Config
from .db import Database, Score, Starter
from .game import puzzle_date, puzzle_number
from .scores import MISMATCH, PENDING, ParsedShare, grid_rows, parse_share, verify
from .selection import excluded_words, pick_starter
from .solutions import SolutionSource, peek_solution, sync_solutions
from .words import WordLists

log = logging.getLogger(__name__)


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
    ) -> None:
        self.config = config
        self.db = db
        self.source = source
        self.lists = lists
        self.rng = rng or random.Random()

    # --- time ------------------------------------------------------------

    @property
    def launch(self) -> date:
        return self.config.solutions.launch

    def now(self) -> datetime:
        return datetime.now(self.config.schedule.tz)

    def today(self) -> date:
        return self.now().date()

    def target_date(self) -> date:
        return self.today() + timedelta(days=self.config.schedule.target_offset_days)

    def post_is_due(self) -> bool:
        """True if today's post time has passed and the upcoming starter wasn't posted."""
        post_at = datetime.combine(self.today(), self.config.schedule.post_time_local)
        return self.now() >= post_at and self.db.starter_for_date(self.target_date()) is None

    # --- solutions -------------------------------------------------------

    async def refresh(self) -> None:
        """Bring the solution cache up to today and verify any scores that were waiting."""
        await sync_solutions(
            self.db,
            self.source,
            self.launch,
            self.today(),
            self.config.solutions.fetch_concurrency,
        )
        self.reverify_pending()

    def reverify_pending(self) -> None:
        for score in self.db.scores_with_status(PENDING):
            starter = self.db.starter_for_puzzle(score.puzzle_number)
            status = verify(
                grid_rows(score.grid)[0],
                starter.word if starter else None,
                self.db.solution_for(score.puzzle_number),
            )
            if status != PENDING:
                self.db.set_score_status(score.puzzle_number, score.user_id, status)

    # --- starters --------------------------------------------------------

    async def choose_starter(self, target: date, also_exclude: set[str] = frozenset()) -> Starter:  # type: ignore[assignment]
        cfg = self.config.selection
        forbidden: set[str] = set(also_exclude)
        if cfg.avoid_target_solution:
            if answer := await peek_solution(self.source, target):
                forbidden.add(answer)
        word = pick_starter(
            cfg,
            self.lists,
            self.db.all_solution_words(),
            excluded_words(self.db, cfg, target, self.today()),
            self.rng,
            forbidden,
        )
        return Starter(target, puzzle_number(target, self.launch), word, cfg.strategy)

    def upcoming_starter(self) -> Starter | None:
        return self.db.starter_for_date(self.target_date())

    def current_starter(self) -> Starter | None:
        return self.db.starter_for_date(self.today())

    # --- scores ----------------------------------------------------------

    def submit(self, user_id: int, user_name: str, text: str) -> Submission | None:
        game = self.config.game
        share = parse_share(text, game.word_length, game.max_guesses)
        if share is None:
            return None
        # Reject puzzles that can't have been played yet anywhere on Earth.
        if puzzle_date(share.puzzle_number, self.launch) > self.today() + timedelta(days=1):
            return None

        starter = self.db.starter_for_puzzle(share.puzzle_number)
        status = verify(
            share.rows[0],
            starter.word if starter else None,
            self.db.solution_for(share.puzzle_number),
        )
        inserted = self.db.add_score(
            Score(
                share.puzzle_number,
                user_id,
                user_name,
                share.guesses,
                share.hard_mode,
                share.grid_text,
                status,
            )
        )
        if inserted and status == MISMATCH:
            log.info("Starter mismatch: %s on puzzle %d", user_name, share.puzzle_number)
        return Submission(share, status, inserted)

    # --- reports ---------------------------------------------------------

    def _counted(self, first: int | None, last: int) -> list[Score]:
        return leaderboard.counted(
            self.db.scores_between(first, last), self.config.scores.count_unverified
        )

    def daily_results(self, number: int | None = None) -> str:
        if number is None:
            number = puzzle_number(self.today(), self.launch)
        starter = self.db.starter_for_puzzle(number)
        return leaderboard.format_daily(
            number, starter.word if starter else None, self._counted(number, number)
        )

    def weekly(self) -> str:
        last = puzzle_number(self.today(), self.launch)
        days = self.config.scores.weekly_window_days
        rows = leaderboard.standings(
            self._counted(last - days + 1, last), self.config.game.fail_score
        )
        return leaderboard.format_standings(f"Last {days} days", rows)

    def monthly(self) -> str:
        today = self.today()
        first = puzzle_number(today.replace(day=1), self.launch)
        last = puzzle_number(today, self.launch)
        rows = leaderboard.standings(self._counted(first, last), self.config.game.fail_score)
        return leaderboard.format_standings(today.strftime("%B %Y"), rows)

    def all_time(self) -> str:
        last = puzzle_number(self.today(), self.launch)
        rows = leaderboard.standings(
            self._counted(None, last),
            self.config.game.fail_score,
            self.config.scores.min_games_all_time,
        )
        return leaderboard.format_standings(
            f"All time (min {self.config.scores.min_games_all_time} games)", rows
        )

    def history(self, count: int) -> str:
        """Our recent starters. Solutions are shown only for puzzles before today."""
        today = self.today()
        starters = self.db.starters_between(None, self.target_date())[-count:]
        if not starters:
            return "No starters posted yet."
        lines = ["**Recent starters**"]
        for s in reversed(starters):
            line = f"#{s.puzzle_number:,} ({s.target_date:%a %b %d}): `{s.word.upper()}`"
            if s.target_date < today and (answer := self.db.solution_for(s.puzzle_number)):
                line += f" → answer ||{answer.upper()}||"
            lines.append(line)
        return "\n".join(lines)
