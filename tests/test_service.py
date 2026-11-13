import asyncio
import random
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from test_solutions import FakeSource

from wordlebot.config import Config, ScheduleConfig, ScoresConfig, SolutionsConfig
from wordlebot.db import Database, Score, Starter
from wordlebot.scores import MISMATCH, NO_STARTER, PENDING, VERIFIED
from wordlebot.service import (
    LATE,
    LATE_PREFIX,
    REMINDER_PREFIX,
    REROLL_PREFIX,
    SCHEDULED,
    UNANNOUNCED,
    DuePost,
    PuzzleInPlay,
    SubmissionsExist,
    WordleService,
)
from wordlebot.words import WordLists

LISTS = WordLists(
    answers=frozenset({"crane", "slate", "fuzzy", "jazzy", "audio"}), allowed=frozenset()
)
LAUNCH = date(2026, 11, 1)
TODAY = date(2026, 11, 13)
TOMORROW = TODAY + timedelta(days=1)
RESULTS = "Wordle 12"  # how the results post for TODAY starts (puzzle 12 from LAUNCH)


class Clock:
    def __init__(self, config, day=TODAY, hour=20, minute=0):
        self.tz = config.schedule.tz
        self.set(day, hour, minute)

    def set(self, day, hour, minute=0):
        self.now = datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tz)

    def __call__(self):
        return self.now


class Channel:
    """Records what was sent; `fail` makes the next sends raise."""

    def __init__(self):
        self.sent = []
        self.fail = 0

    async def send(self, text):
        await asyncio.sleep(0)  # yield, like a real send, so overlapping callers interleave
        if self.fail:
            self.fail -= 1
            raise ConnectionError("network blip")
        self.sent.append(text)
        return len(self.sent)

    def starters(self):
        return [t for t in self.sent if "Starting word" in t]


def word_in(text):
    return text.split("`")[1].lower()


def make(tmp_path, hour=20, seed=True, **schedule):
    config = Config(
        schedule=ScheduleConfig(**schedule),
        solutions=SolutionsConfig(launch_date=LAUNCH.isoformat()),
    )
    clock = Clock(config, hour=hour)
    db = Database(tmp_path / "t.sqlite3")
    if seed:
        # Yesterday's post happened, so nothing is owed for today unless a test says so.
        db.save_starter(Starter(TODAY, 12, "crane", "x"), message_id=1)
    service = WordleService(config, db, FakeSource({}), LISTS, random.Random(0), clock)
    return service, clock, Channel()


def test_nothing_is_owed_before_post_time(tmp_path):
    service, _, channel = make(tmp_path, hour=19)
    assert asyncio.run(service.post_due(channel.send)) == []
    assert channel.sent == []


def test_scheduled_post_sends_results_then_starter_once(tmp_path):
    service, clock, channel = make(tmp_path)
    assert service.due_posts() == [DuePost(TOMORROW, SCHEDULED)]
    asyncio.run(service.post_due(channel.send))

    assert len(channel.sent) == 2
    assert RESULTS in channel.sent[0]
    assert channel.sent[1].startswith("🟩")
    starter = service.db.starter_for_date(TOMORROW)
    assert starter.announced and starter.word == word_in(channel.sent[1])

    # Later ticks, and a restart (a new service on the same database), add nothing.
    clock.set(TODAY, 20, 1)
    asyncio.run(service.post_due(channel.send))
    restarted = WordleService(service.config, service.db, FakeSource({}), LISTS, clock=clock)
    asyncio.run(restarted.post_due(channel.send))
    assert len(channel.sent) == 2


def test_failed_starter_send_is_retried_with_the_same_word(tmp_path):
    service, _, channel = make(tmp_path, post_daily_results=False)
    channel.fail = 1
    with pytest.raises(ConnectionError):
        asyncio.run(service.post_due(channel.send))
    picked = service.db.starter_for_date(TOMORROW)
    assert picked is not None and not picked.announced

    service.rng = random.Random(99)  # a fresh pick would differ; the saved one must win
    asyncio.run(service.post_due(channel.send))
    assert [word_in(t) for t in channel.sent] == [picked.word]
    assert service.db.starter_for_date(TOMORROW).announced


def test_results_are_not_repeated_when_the_starter_send_fails(tmp_path):
    service, _, channel = make(tmp_path)

    async def flaky(text):
        if "Starting word" in text and not channel.starters() and channel.fail:
            channel.fail = 0
            raise ConnectionError("network blip")
        channel.sent.append(text)
        return len(channel.sent)

    channel.fail = 1
    with pytest.raises(ConnectionError):
        asyncio.run(service.post_due(flaky))
    asyncio.run(service.post_due(flaky))
    assert len([t for t in channel.sent if RESULTS in t]) == 1
    assert len(channel.starters()) == 1


def test_reminder_when_starter_was_announced_before_post_time(tmp_path):
    service, clock, channel = make(tmp_path, hour=15)
    rerolled = asyncio.run(service.reroll(channel.send))
    assert channel.sent[-1].startswith(REROLL_PREFIX)
    assert service.due_posts() == []

    clock.set(TODAY, 20)
    asyncio.run(service.post_due(channel.send))
    asyncio.run(service.post_due(channel.send))
    assert len(channel.sent) == 3
    assert RESULTS in channel.sent[1]
    assert channel.sent[2].startswith(REMINDER_PREFIX)
    assert word_in(channel.sent[2]) == rerolled.word


def test_missed_post_is_made_late_after_midnight(tmp_path):
    service, clock, channel = make(tmp_path, hour=18)
    # Down from before post time until 00:30 the next day.
    clock.set(TOMORROW, 0, 30)
    assert service.due_posts() == [DuePost(TOMORROW, LATE)]
    asyncio.run(service.post_due(channel.send))
    asyncio.run(service.post_due(channel.send))

    assert len(channel.sent) == 1
    assert channel.sent[0].startswith(LATE_PREFIX)
    assert f"{TOMORROW:%B %d}" in channel.sent[0]
    assert service.current_starter().word == word_in(channel.sent[0])

    # That evening's post is a normal one for the day after.
    clock.set(TOMORROW, 20)
    asyncio.run(service.post_due(channel.send))
    assert channel.sent[-1].startswith("🟩")
    assert len(channel.starters()) == 2


def test_new_install_posts_nothing_late(tmp_path):
    service, clock, channel = make(tmp_path, hour=10, seed=False)
    # Today's puzzle has no starter, but the bot wasn't running when it was due.
    assert service.due_posts() == []
    asyncio.run(service.post_due(channel.send))
    assert channel.sent == []

    clock.set(TODAY, 20)
    assert service.due_posts() == [DuePost(TOMORROW, SCHEDULED)]
    asyncio.run(service.post_due(channel.send))
    asyncio.run(service.post_due(channel.send))
    assert len(channel.starters()) == 1 and channel.sent[-1].startswith("🟩")
    assert service.db.starter_for_date(TODAY) is None

    # The day after, today's starter is the one posted last night: still nothing late.
    clock.set(TOMORROW, 0, 30)
    assert service.due_posts() == []


def test_no_late_post_for_dates_up_to_the_first_starter(tmp_path):
    # Posting two days ahead: the first post skips over a day we never picked for.
    service, clock, channel = make(tmp_path, seed=False, target_offset_days=2)
    first = TODAY + timedelta(days=2)
    asyncio.run(service.post_due(channel.send))
    assert service.db.first_starter_date() == first

    clock.set(TOMORROW, 0, 30)
    assert service.due_posts() == []  # TOMORROW is before the first starter
    clock.set(TOMORROW, 20)
    assert service.due_posts() == [DuePost(first + timedelta(days=1), SCHEDULED)]
    asyncio.run(service.post_due(channel.send))
    assert len(channel.starters()) == 2
    assert not any(t.startswith(LATE_PREFIX) for t in channel.sent)
    assert service.db.starter_for_date(TOMORROW) is None


def test_outage_after_the_first_post_is_recovered_late_once(tmp_path):
    service, clock, channel = make(tmp_path, seed=False, post_daily_results=False)
    asyncio.run(service.post_due(channel.send))  # the first ever post, for TOMORROW
    # Down from then until the morning of the day after TOMORROW.
    missed = TOMORROW + timedelta(days=1)
    clock.set(missed, 9)
    assert service.due_posts() == [DuePost(missed, LATE)]
    asyncio.run(service.post_due(channel.send))
    asyncio.run(service.post_due(channel.send))
    assert len(channel.sent) == 2
    assert channel.sent[1].startswith(LATE_PREFIX) and f"{missed:%B %d}" in channel.sent[1]


def test_unsent_first_pick_is_still_announced_late(tmp_path):
    # The bot picked it, so it was running; the word must not stay unseen.
    service, clock, channel = make(tmp_path, seed=False, post_daily_results=False)
    channel.fail = 1
    with pytest.raises(ConnectionError):
        asyncio.run(service.post_due(channel.send))
    picked = service.db.starter_for_date(TOMORROW).word

    clock.set(TOMORROW, 0, 5)
    asyncio.run(service.post_due(channel.send))
    asyncio.run(service.post_due(channel.send))
    assert [word_in(t) for t in channel.sent] == [picked]
    assert channel.sent[0].startswith(LATE_PREFIX)


def add_score(service, number, user_id):
    service.db.add_score(Score(number, user_id, f"user{user_id}", 3, False, "ccccc", VERIFIED))


def test_same_day_post_reports_yesterdays_puzzle(tmp_path):
    service, _, channel = make(
        tmp_path, hour=7, seed=False, target_offset_days=0, post_time="07:00"
    )
    add_score(service, 11, 1)  # yesterday's puzzle
    add_score(service, 12, 2)  # today's, pasted early
    assert service.results_date() == TODAY - timedelta(days=1)

    asyncio.run(service.post_due(channel.send))
    assert len(channel.sent) == 2
    assert "Wordle 11" in channel.sent[0] and "user1" in channel.sent[0]
    assert "user2" not in channel.sent[0]
    assert f"{TODAY:%B %d}" in channel.sent[1]
    # The command with no argument agrees with the post.
    assert "Wordle 11" in service.daily_results()
    assert "user2" in service.daily_results(12)


def test_results_default_never_names_a_puzzle_that_has_not_started(tmp_path):
    service, _, channel = make(tmp_path)  # offset 1: today's puzzle, as before
    assert service.results_date() == TODAY
    (tmp_path / "ahead").mkdir()
    ahead, _, _ = make(tmp_path / "ahead", seed=False, target_offset_days=2)
    assert ahead.results_date() == TODAY
    asyncio.run(ahead.post_due(channel.send))
    assert RESULTS in channel.sent[0]


def test_pick_saved_before_midnight_is_announced_late_with_the_same_word(tmp_path):
    service, clock, channel = make(tmp_path, post_daily_results=False)
    channel.fail = 1
    with pytest.raises(ConnectionError):
        asyncio.run(service.post_due(channel.send))
    picked = service.db.starter_for_date(TOMORROW).word

    clock.set(TOMORROW, 0, 5)
    asyncio.run(service.post_due(channel.send))
    assert channel.sent == [channel.sent[0]] and channel.sent[0].startswith(LATE_PREFIX)
    assert word_in(channel.sent[0]) == picked


def test_dates_come_from_one_clock_reading(tmp_path):
    service, clock, channel = make(tmp_path)
    clock.set(TODAY, 23, 59)

    async def send_past_midnight(text):
        clock.set(TOMORROW, 0, 0)
        return await channel.send(text)

    asyncio.run(service.post_due(send_past_midnight))
    assert RESULTS in channel.sent[0]
    assert service.db.starter_for_date(TOMORROW).announced
    assert service.db.starter_for_date(TOMORROW + timedelta(days=1)) is None


def test_reroll_replaces_the_word_and_respects_the_in_play_guard(tmp_path):
    service, clock, channel = make(tmp_path)
    asyncio.run(service.post_due(channel.send))
    before = service.db.starter_for_date(TOMORROW).word

    rerolled = asyncio.run(service.reroll(channel.send))
    after = service.db.starter_for_date(TOMORROW)
    assert rerolled.word == after.word != before
    assert after.announced
    assert service.due_posts() == []  # the daily post is not repeated

    (tmp_path / "same_day").mkdir()
    same_day = make(tmp_path / "same_day", target_offset_days=0)[0]
    with pytest.raises(PuzzleInPlay):
        asyncio.run(same_day.reroll(channel.send))


def test_reroll_never_returns_to_a_word_the_date_already_had(tmp_path):
    service, _, channel = make(tmp_path)
    asyncio.run(service.post_due(channel.send))
    seen = [service.db.starter_for_date(TOMORROW).word]
    # CRANE is today's starter (excluded as a recent one), which leaves four words.
    for _ in range(3):
        seen.append(asyncio.run(service.reroll(channel.send)).word)
    assert len(set(seen)) == 4
    assert service.db.retired_words(TOMORROW) == set(seen[:-1])
    assert service.db.retired_words(TODAY) == set()

    # The record survives a restart, and saving the same word again retires nothing.
    service.db.save_starter(service.db.starter_for_date(TOMORROW), message_id=9)
    assert Database(tmp_path / "t.sqlite3").retired_words(TOMORROW) == set(seen[:-1])


def test_reroll_is_refused_once_anyone_has_submitted_for_the_puzzle(tmp_path):
    service, _, channel = make(tmp_path)
    asyncio.run(service.post_due(channel.send))
    before = service.db.starter_for_date(TOMORROW)
    sent = len(channel.sent)
    add_score(service, 12, 1)  # today's puzzle: not the one being rerolled
    service.db.add_score(Score(13, 1, "user1", 3, False, "ccccc", PENDING))
    service.db.add_score(Score(13, 2, "user2", 3, False, "ccccc", MISMATCH))

    with pytest.raises(SubmissionsExist) as refused:
        asyncio.run(service.reroll(channel.send))
    assert (refused.value.puzzle_number, refused.value.count) == (13, 2)
    assert service.db.starter_for_date(TOMORROW) == before
    assert len(channel.sent) == sent


def test_failed_reroll_send_is_announced_by_the_next_tick(tmp_path):
    service, _, channel = make(tmp_path, hour=15)
    channel.fail = 1
    with pytest.raises(ConnectionError):
        asyncio.run(service.reroll(channel.send))
    picked = service.db.starter_for_date(TOMORROW)
    assert not picked.announced
    assert service.due_posts() == [DuePost(TOMORROW, UNANNOUNCED)]

    asyncio.run(service.post_due(channel.send))
    assert [word_in(t) for t in channel.sent] == [picked.word]
    assert service.due_posts() == []


def test_add_starter_keeps_the_first_pick(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    first = db.add_starter(Starter(TOMORROW, 13, "slate", "x"))
    second = db.add_starter(Starter(TOMORROW, 13, "fuzzy", "x"))
    assert first.word == second.word == "slate" and not second.announced


def test_existing_database_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE starters (target_date TEXT PRIMARY KEY, puzzle_number INTEGER NOT NULL"
        " UNIQUE, word TEXT NOT NULL, strategy TEXT NOT NULL, posted_at TEXT NOT NULL,"
        " message_id INTEGER)"
    )
    old.execute(
        "INSERT INTO starters VALUES (?, 13, 'slate', 'x', '2026-11-14T01:00:00+00:00', 555)",
        (TOMORROW.isoformat(),),
    )
    old.commit()
    old.close()

    db = Database(path)
    assert db.starter_for_date(TOMORROW).announced
    # Upgrading after post time must not repeat today's results and starter.
    assert db.daily_post_completed(TOMORROW) and db.results_posted(TOMORROW)
    # Tables added since are created empty, and a reroll starts filling them.
    assert db.retired_words(TOMORROW) == set()
    db.save_starter(Starter(TOMORROW, 13, "fuzzy", "x"))
    assert db.retired_words(TOMORROW) == {"slate"}
    db.close()
    assert Database(path).daily_post_completed(TOMORROW)  # reopening is a no-op


# --- scores: verifying against a solution that isn't cached yet ---------------

SOLUTION = "slate"
GOOD_SHARE = "Wordle 12 2/6\n\n⬛⬛🟩⬛🟩\n🟩🟩🟩🟩🟩"  # opened with CRANE
BAD_SHARE = "Wordle 12 2/6\n\n⬛⬛⬛⬛⬛\n🟩🟩🟩🟩🟩"  # opened with something else
TOMORROW_SHARE = "Wordle 13 2/6\n\n⬛🟩⬛⬛⬛\n🟩🟩🟩🟩🟩"  # FUZZY against AUDIO


def make_morning(tmp_path, words=None, hour=8):
    """A service on the morning of TODAY: the last sync was yesterday evening."""
    service, clock, channel = make(tmp_path, hour=hour)
    service.source = FakeSource({TODAY: SOLUTION} if words is None else words)
    return service, clock, channel


def submit(service, user_id, text, message_id=None):
    return asyncio.run(service.submit(user_id, f"user{user_id}", text, 77, message_id))


def test_share_before_post_time_is_verified_at_once(tmp_path):
    service, _, _ = make_morning(tmp_path)
    assert submit(service, 1, GOOD_SHARE).status == VERIFIED
    assert submit(service, 2, BAD_SHARE).status == MISMATCH
    assert "user1: 2" in service.daily_results()
    assert "user1" in service.weekly()
    # Spoiler rule: nothing after today was asked for or stored.
    assert max(service.source.requested) == TODAY
    assert max(service.db.solution_dates()) == TODAY


def test_concurrent_shares_trigger_one_refresh(tmp_path):
    service, _, _ = make_morning(tmp_path)

    async def run():
        return await asyncio.gather(
            *(service.submit(user, f"user{user}", GOOD_SHARE) for user in range(5))
        )

    assert [s.status for s in asyncio.run(run())] == [VERIFIED] * 5
    assert service.source.requested.count(TODAY) == 1


def test_share_survives_a_failed_refresh_and_is_settled_later(tmp_path):
    service, clock, _ = make_morning(tmp_path, words={})
    submission = submit(service, 1, GOOD_SHARE, message_id=501)
    assert submission.inserted and submission.status == PENDING
    assert "No verified results" in service.daily_results()

    # The source is not asked again on every share or tick while it is failing...
    service.source.words[TODAY] = SOLUTION
    asked = len(service.source.requested)
    assert submit(service, 2, BAD_SHARE, message_id=502).status == PENDING
    clock.set(TODAY, 8, 14)
    asyncio.run(service.refresh_if_missing())
    assert len(service.source.requested) == asked
    assert service.take_resolved() == []

    # ...but it is once the retry delay has passed, and both shares are settled.
    clock.set(TODAY, 8, 15)
    asyncio.run(service.refresh_if_missing())
    resolved = {s.user_id: s for s in service.take_resolved()}
    assert {u: s.status for u, s in resolved.items()} == {1: VERIFIED, 2: MISMATCH}
    assert (resolved[1].channel_id, resolved[1].message_id) == (77, 501)
    assert service.take_resolved() == []  # each change is handed out once
    assert "user1: 2" in service.daily_results()


def test_refresh_that_raises_does_not_drop_the_share(tmp_path):
    service, _, _ = make_morning(tmp_path)

    def broken():
        raise sqlite3.OperationalError("database is locked")

    service.db.solution_dates = broken
    submission = submit(service, 1, GOOD_SHARE)
    assert submission.inserted and submission.status == PENDING


def test_tomorrows_puzzle_is_not_fetched_until_local_midnight(tmp_path):
    words = {TODAY: SOLUTION, TOMORROW: "audio"}
    service, clock, _ = make_morning(tmp_path, words, hour=23)
    service.db.save_starter(Starter(TOMORROW, 13, "fuzzy", "x"), message_id=2)
    # Someone in a timezone ahead of the bot's has already played tomorrow's puzzle.
    assert submit(service, 1, TOMORROW_SHARE, message_id=501).status == PENDING
    asyncio.run(service.refresh_if_missing())
    assert TOMORROW not in service.source.requested
    assert max(service.db.solution_dates()) == TODAY

    clock.set(TOMORROW, 0, 1)
    asyncio.run(service.refresh_if_missing())
    assert [(s.message_id, s.status) for s in service.take_resolved()] == [(501, VERIFIED)]
    assert max(service.db.solution_dates()) == TOMORROW
    # Once cached, further checks cost nothing.
    asked = len(service.source.requested)
    asyncio.run(service.refresh_if_missing())
    assert len(service.source.requested) == asked


def test_scheduled_post_queues_the_scores_it_settles(tmp_path):
    service, _, channel = make_morning(tmp_path, words={}, hour=20)
    submit(service, 1, GOOD_SHARE, message_id=501)
    service.source.words[TODAY] = SOLUTION
    asyncio.run(service.post_due(channel.send))  # refreshes regardless of the retry delay
    assert "user1: 2" in channel.sent[0]
    assert [(s.message_id, s.status) for s in service.take_resolved()] == [(501, VERIFIED)]


def test_share_before_its_starter_waits_for_it(tmp_path):
    words = {TODAY: SOLUTION, TOMORROW: "audio"}
    service, clock, channel = make_morning(tmp_path, words, hour=15)
    # Played in a timezone ahead of the bot's, hours before the evening post.
    assert submit(service, 1, TOMORROW_SHARE, message_id=501).status == PENDING
    asyncio.run(service.refresh())
    assert service.take_resolved() == []

    # The post picks a starter, but the puzzle's answer is still off limits.
    clock.set(TODAY, 20)
    asyncio.run(service.post_due(channel.send))
    assert service.take_resolved() == []
    assert max(service.db.solution_dates()) == TODAY

    service.db.save_starter(Starter(TOMORROW, 13, "fuzzy", "x"), message_id=2)
    clock.set(TOMORROW, 0, 1)
    asyncio.run(service.refresh_if_missing())
    assert [(s.message_id, s.status) for s in service.take_resolved()] == [(501, VERIFIED)]


def test_share_waiting_for_a_late_starter_is_settled_by_its_post(tmp_path):
    # The bot was down at yesterday's post time, so today's starter is still owed.
    service, _, channel = make_morning(tmp_path)
    service.db.conn.execute(
        "UPDATE starters SET target_date = ?, puzzle_number = 11",
        ((TODAY - timedelta(days=1)).isoformat(),),
    )
    service.db.conn.commit()
    assert service.due_posts() == [DuePost(TODAY, LATE)]

    # The solution is cached by the share itself, so only the post can settle it.
    assert submit(service, 1, GOOD_SHARE, message_id=501).status == PENDING
    assert service.db.solution_for(12) == SOLUTION
    asyncio.run(service.post_due(channel.send))
    word = service.db.starter_for_date(TODAY).word
    expected = VERIFIED if word == "crane" else MISMATCH
    assert [(s.message_id, s.status) for s in service.take_resolved()] == [(501, expected)]


def test_no_starter_is_final_only_when_none_can_come(tmp_path):
    # A new install: nothing has been picked, so today's puzzle will get no starter.
    service, clock, _ = make(tmp_path, hour=8, seed=False)
    service.source = FakeSource({TODAY: SOLUTION})
    assert submit(service, 1, GOOD_SHARE).status == NO_STARTER
    # A past puzzle never will either.
    assert submit(service, 1, GOOD_SHARE.replace("12", "11")).status == NO_STARTER
    # Tomorrow's still can...
    assert submit(service, 1, TOMORROW_SHARE, message_id=501).status == PENDING
    asyncio.run(service.refresh())
    assert service.take_resolved() == []
    # ...but if its day arrives without one, the share stops waiting.
    clock.set(TOMORROW, 0, 1)
    asyncio.run(service.refresh())
    assert [(s.message_id, s.status) for s in service.take_resolved()] == [(501, NO_STARTER)]


def test_existing_scores_table_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE scores (puzzle_number INTEGER NOT NULL, user_id INTEGER NOT NULL,"
        " user_name TEXT NOT NULL, guesses INTEGER, hard_mode INTEGER NOT NULL DEFAULT 0,"
        " grid TEXT NOT NULL, status TEXT NOT NULL, submitted_at TEXT NOT NULL,"
        " PRIMARY KEY (puzzle_number, user_id))"
    )
    old.execute(
        "INSERT INTO scores VALUES (12, 1, 'alice', 2, 0, 'aacac\nccccc', 'pending',"
        " '2026-11-13T08:00:00+00:00')"
    )
    old.commit()
    old.close()

    db = Database(path)
    (before,) = db.scores_with_status(PENDING)
    assert (before.user_name, before.channel_id, before.message_id) == ("alice", None, None)
    assert db.add_score(Score(12, 2, "bob", 3, False, "aaaaa\nccccc", PENDING, 77, 502))
    after = {s.user_id: s for s in db.scores_with_status(PENDING)}
    assert (after[2].channel_id, after[2].message_id) == (77, 502)
    db.close()
    assert len(Database(path).scores_with_status(PENDING)) == 2  # reopening is a no-op


GRID_1 = "\n" + "🟩" * 5


@pytest.mark.parametrize(
    "number",
    ["14", "999,999,999", "999999999", "9" * 30, "9" * 1900],
    ids=["day-after-tomorrow", "huge-grouped", "huge", "beyond-int64", "very-long"],
)
def test_share_for_an_unplayable_puzzle_is_ignored_quietly(tmp_path, number):
    service, _, _ = make_morning(tmp_path)
    assert submit(service, 1, f"Wordle {number} 1/6{GRID_1}") is None
    assert service.db.scores_between(None, 10**12) == []
    assert service.source.requested == []


def test_share_for_the_launch_puzzle_is_accepted(tmp_path):
    service, _, _ = make_morning(tmp_path)
    submission = submit(service, 1, f"Wordle 0 1/6{GRID_1}")
    assert submission.share.puzzle_number == 0 and submission.inserted


@pytest.mark.parametrize("number", [14, 999_999_999, 2**53, 10**30])
def test_results_for_a_puzzle_not_yet_played_is_a_friendly_message(tmp_path, number):
    service, _, _ = make(tmp_path)
    assert service.daily_results(number) == f"Wordle {number:,} hasn't been played yet."


@pytest.mark.parametrize("number", [-1, -(2**53), -(10**30)])
def test_results_for_a_negative_puzzle_is_a_friendly_message(tmp_path, number):
    service, _, _ = make(tmp_path)
    assert "There is no Wordle" in service.daily_results(number)


def test_results_for_the_edges_of_the_playable_range(tmp_path):
    service, _, _ = make(tmp_path)
    assert "Wordle 0 results" in service.daily_results(0)
    assert "Wordle 13 results" in service.daily_results(13)  # tomorrow's, live elsewhere


def history_service(tmp_path):
    """Starters and cached solutions for the four days up to TODAY (puzzles 9-12)."""
    service, clock, _ = make(tmp_path, seed=False)
    words = ["slate", "fuzzy", "jazzy", "crane"]
    answers = ["audio", "crane", "slate", "fuzzy"]
    for back, (word, answer) in enumerate(zip(reversed(words), reversed(answers))):
        day = TODAY - timedelta(days=back)
        service.db.save_starter(Starter(day, 12 - back, word, "x"), message_id=1)
        service.db.add_solutions([(day, 12 - back, answer)])
    return service, clock


def history_lines(service, count=10):
    return {line.split(" ")[0]: line for line in service.history(count).splitlines()}


def test_history_hides_answers_until_two_days_after_the_puzzle(tmp_path):
    service, _ = history_service(tmp_path)
    lines = history_lines(service)
    # Today's and yesterday's puzzles may still be in play further west.
    assert lines["#12"].endswith("`CRANE`") and "answer" not in lines["#12"]
    assert lines["#11"].endswith("`JAZZY`") and "answer" not in lines["#11"]
    # The day before yesterday's is over everywhere; it stays in spoiler tags.
    assert lines["#10"].endswith("answer ||CRANE||")
    assert lines["#9"].endswith("answer ||AUDIO||")
    assert service.history(10).count("||") == 4
    assert "Answers appear 2 days after" in service.history(10)


def test_history_answer_appears_at_the_second_local_midnight(tmp_path):
    service, clock = history_service(tmp_path)
    clock.set(TODAY, 23, 59)
    assert "answer" not in history_lines(service)["#11"]
    clock.set(TOMORROW, 0, 0)
    lines = history_lines(service)
    assert lines["#11"].endswith("answer ||SLATE||")
    assert "answer" not in lines["#12"]


def test_answer_visible_is_the_one_rule(tmp_path):
    service, _, _ = make(tmp_path)
    assert not service.answer_visible(TOMORROW)
    assert not service.answer_visible(TODAY)
    assert not service.answer_visible(TODAY - timedelta(days=1))
    assert service.answer_visible(TODAY - timedelta(days=2))
    assert service.answer_visible(LAUNCH)
    assert service.answer_visible(TODAY, today=TODAY + timedelta(days=2))


def test_no_other_report_shows_a_solution(tmp_path):
    service, _ = history_service(tmp_path)
    reports = [
        service.daily_results(),
        service.daily_results(9),
        service.weekly(),
        service.monthly(),
        service.all_time(),
    ]
    # AUDIO is only ever a solution here, never a starter.
    assert not any("audio" in report.lower() for report in reports)


def test_history_with_no_starters(tmp_path):
    service, _, _ = make(tmp_path, seed=False)
    assert service.history(10) == "No starters posted yet."


# --- standings boards ---------------------------------------------------------------
# TODAY is puzzle 12 on the 13th: the week is puzzles 6-12, the month 0-12.


def add_games(service, user_id, numbers, guesses=3, name=None, at=""):
    for number in numbers:
        service.db.add_score(
            Score(number, user_id, name or f"user{user_id}", guesses, False, "c", VERIFIED,
                  submitted_at=at)
        )


def test_weekly_and_monthly_boards_apply_their_minimum_games(tmp_path):
    service, _, _ = make(tmp_path)
    add_games(service, 1, range(1, 13), guesses=4)  # 7 this week, 12 this month
    add_games(service, 2, [10, 11, 12], guesses=3)  # 3 and 3
    add_games(service, 3, [12], guesses=2)  # one lucky game

    assert service.weekly().split("\n") == [
        "**Last 7 days (min 3 games)**",
        "1\\. user2 — avg **3.00** (3 played, 3 solved)",
        "2\\. user1 — avg **4.00** (7 played, 7 solved)",
        "Under 3 games, not ranked: user3 (1)",
    ]
    assert service.monthly().split("\n") == [
        "**November 2026 (min 7 games)**",
        "1\\. user1 — avg **4.00** (12 played, 12 solved)",
        "Under 7 games, not ranked: user2 (3), user3 (1)",
    ]
    assert service.all_time().split("\n") == [
        "**All time (min 5 games)**",
        "1\\. user1 — avg **4.00** (12 played, 12 solved)",
        "Under 5 games, not ranked: user2 (3), user3 (1)",
    ]


def test_board_minimums_come_from_config(tmp_path):
    service, _, _ = make(tmp_path)
    service.config = replace(
        service.config, scores=ScoresConfig(min_games_weekly=1, min_games_monthly=2)
    )
    add_games(service, 1, [11, 12])
    add_games(service, 2, [12], guesses=2)
    assert service.weekly().split("\n")[:2] == ["**Last 7 days**", "1\\. user2 — avg **2.00** (1 played, 1 solved)"]
    assert service.monthly().split("\n") == [
        "**November 2026 (min 2 games)**",
        "1\\. user1 — avg **3.00** (2 played, 2 solved)",
        "Under 2 games, not ranked: user2 (1)",
    ]


def test_monthly_minimum_is_capped_at_the_puzzles_played_so_far_this_month(tmp_path):
    service, clock, _ = make(tmp_path)
    clock.set(date(2026, 12, 2), 9)  # two December puzzles so far: 30 and 31
    add_games(service, 1, [30, 31])
    add_games(service, 2, [31], guesses=2)
    assert service.monthly().split("\n") == [
        "**December 2026 (min 2 games)**",
        "1\\. user1 — avg **3.00** (2 played, 2 solved)",
        "Under 2 games, not ranked: user2 (1)",
    ]


def test_weekly_minimum_is_capped_at_the_window(tmp_path):
    service, _, _ = make(tmp_path)
    service.config = replace(
        service.config, scores=ScoresConfig(weekly_window_days=2, min_games_weekly=3)
    )
    add_games(service, 1, [11, 12])
    assert service.weekly().split("\n") == [
        "**Last 2 days (min 2 games)**",
        "1\\. user1 — avg **3.00** (2 played, 2 solved)",
    ]


def test_board_shows_the_name_on_the_latest_submission_not_the_highest_puzzle(tmp_path):
    service, _, _ = make(tmp_path)
    add_games(service, 1, [11, 12], name="old name", at="2026-11-12T09:00:00+00:00")
    add_games(service, 1, [10], name="new name", at="2026-11-13T09:00:00+00:00")  # late catch-up
    assert "new name" in service.weekly() and "old name" not in service.weekly()


def test_scores_come_back_oldest_submission_first_and_keep_their_time(tmp_path):
    service, _, _ = make(tmp_path)
    add_games(service, 1, [12], at="2026-11-12T09:00:00+00:00")
    add_games(service, 2, [11], at="2026-11-13T09:00:00+00:00")
    add_games(service, 3, [10], at="2026-11-12T09:00:00+00:00")  # same second: insert order
    add_games(service, 4, [9])  # no time given: stamped now, which is after the others
    rows = service.db.scores_between(None, 12)
    assert [s.user_id for s in rows] == [1, 3, 2, 4]
    assert rows[0].submitted_at == "2026-11-12T09:00:00+00:00"
    assert rows[3].submitted_at > "2026-11-13T09:00:00+00:00"
