import asyncio
from datetime import datetime, timedelta, timezone

from test_service import LAUNCH, LISTS, TODAY, make

from wordlebot import golf
from wordlebot.config import Config, GolfConfig, ScheduleConfig, SolutionsConfig
from wordlebot.db import Database, Score, Starter
from wordlebot.game import puzzle_date
from wordlebot.golf import Milestone, card, format_round, latest_milestone, progress
from wordlebot.scores import MISMATCH, VERIFIED
from wordlebot.service import WordleService


def score(number, user_id, guesses, name=None, status=VERIFIED, at=None):
    return Score(
        puzzle_number=number,
        user_id=user_id,
        user_name=name or f"user{user_id}",
        guesses=guesses,
        hard_mode=False,
        grid="",
        status=status,
        submitted_at=at or f"2026-01-01T00:00:{number % 60:02d}",
    )


# --- rounds -------------------------------------------------------------------


def test_rounds_run_back_to_back_from_the_anchor():
    assert progress(100, 99) is None
    assert progress(100, 100) == Milestone(1, 100, 1)
    assert progress(100, 117) == Milestone(1, 100, 18)
    assert progress(100, 118) == Milestone(2, 118, 1)
    assert golf.round_start(140, 100) == 136


def test_milestones_are_each_front_nine_and_each_full_round():
    assert latest_milestone(100, 107) is None
    assert latest_milestone(100, 108) == Milestone(1, 100, 9)
    assert latest_milestone(100, 116) == Milestone(1, 100, 9)
    assert latest_milestone(100, 117) == Milestone(1, 100, 18)
    assert latest_milestone(100, 126) == Milestone(2, 118, 9)
    assert latest_milestone(100, 135) == Milestone(2, 118, 18)


# --- card ---------------------------------------------------------------------


def test_a_missed_hole_scores_the_same_as_a_fail():
    m = Milestone(1, 100, 3)
    ranked = card([score(100, 1, 3), score(101, 1, None)], m, fail_score=7)
    [(rank, player)] = ranked
    assert player.cells == ("3", "X", "-")
    assert player.strokes == (3, 7, 7)
    assert (player.total, player.to_par, player.played) == (17, 5, 2)


def test_card_ignores_scores_outside_the_round_so_far():
    m = Milestone(1, 100, 9)
    ranked = card([score(99, 1, 2), score(109, 1, 2), score(100, 2, 4)], m, 7)
    assert [p.user_id for _, p in ranked] == [2]


def test_ties_share_a_rank():
    m = Milestone(1, 100, 1)
    ranked = card([score(100, 1, 3), score(100, 2, 3), score(100, 3, 5)], m, 7)
    assert [r for r, _ in ranked] == [1, 1, 3]


def test_player_name_is_the_latest_submitted():
    m = Milestone(1, 100, 2)
    ranked = card(
        [score(100, 1, 3, "new", at="2026-01-02"), score(101, 1, 3, "old", at="2026-01-01")], m, 7
    )
    assert ranked[0][1].name == "new"


# --- formatting ---------------------------------------------------------------


def test_front_nine_names_the_leader_and_shows_one_card():
    m = Milestone(2, 118, 9)
    scores = [score(n, 1, 4, "Alice") for n in range(118, 127)] + [score(118, 2, 3, "Bob")]
    messages = format_round(m, card(scores, m, 7))
    assert len(messages) == 2
    assert "Golf round 2, after the front nine" in messages[0]
    assert "par 36" in messages[0]
    assert "Leader: **Alice** (E, 36)" in messages[0]
    assert "OUT" in messages[1] and "IN" not in messages[1]


def test_final_declares_co_champions_and_shows_both_nines():
    m = Milestone(1, 100, 18)
    scores = [score(n, u, 4) for n in range(100, 118) for u in (1, 2)]
    scores += [score(n, 3, 5) for n in range(100, 118)]
    messages = format_round(m, card(scores, m, 7))
    assert len(messages) == 3
    assert "Golf round 1, final" in messages[0] and "par 72" in messages[0]
    assert "🏆 Co-champions: **user1**, **user2** (E, 72)" in messages[0]
    assert "T1" in messages[0]
    assert "IN" in messages[2] and " 18" in messages[2]


def test_single_winner():
    m = Milestone(1, 100, 18)
    scores = [score(n, 1, 3) for n in range(100, 118)] + [score(100, 2, 2)]
    assert "🏆 Winner: **user1** (-18, 54)" in format_round(m, card(scores, m, 7))[0]


def test_names_are_escaped_outside_the_card_and_safe_inside_it():
    m = Milestone(1, 100, 1)
    messages = format_round(m, card([score(100, 1, 3, "**`@everyone`**")], m, 7))
    leader_line, block = messages[0].split("```", 1)
    assert "\\*\\*\\`@​everyone\\`\\*\\*" in leader_line
    assert block.count("`") == 3  # only the closing fence: no name backtick ends it early


def test_long_names_are_truncated_on_the_card():
    m = Milestone(1, 100, 1)
    messages = format_round(m, card([score(100, 1, 3, "A" * 30)], m, 7))
    assert "A" * golf.CARD_NAME_WIDTH not in messages[1]
    assert "A" * (golf.CARD_NAME_WIDTH - 1) + "…" in messages[1]


def test_empty_round():
    assert "No scores this round." in format_round(Milestone(1, 100, 9), [])[0]


# --- service ------------------------------------------------------------------
#
# `make` seeds a starter for TODAY (puzzle 12), so golf round 1 starts at 12 and its
# front nine (12-20) is posted the morning after puzzle 20.

ANCHOR = 12
FRONT_DONE = puzzle_date(ANCHOR + 8, LAUNCH) + timedelta(days=1)  # the morning after hole 9
ROUND_DONE = puzzle_date(ANCHOR + 17, LAUNCH) + timedelta(days=1)


def golf_messages(channel):
    """The leaderboard message of each golf post."""
    return [t for t in channel.sent if "Golf round" in t]


def golf_posts(channel):
    """Every message of every golf post (the clock jumps, so starters are owed too)."""
    return [t for t in channel.sent if t not in channel.starters()]


def test_front_nine_posts_once_at_post_time(tmp_path):
    service, clock, channel = make(tmp_path)
    for n in range(ANCHOR, ANCHOR + 9):
        service.db.add_score(score(n, 1, 4))

    clock.set(FRONT_DONE, 7, 59)
    assert service.due_golf() is None
    clock.set(FRONT_DONE, 8, 0)
    assert service.due_golf() == Milestone(1, ANCHOR, 9)
    asyncio.run(service.post_due(channel.send))
    [header] = golf_messages(channel)
    assert "after the front nine" in header and "Leader: **user1** (E, 36)" in header
    assert len(golf_posts(channel)) == 2  # leaderboard, then the front nine card

    clock.set(FRONT_DONE, 9, 0)
    asyncio.run(service.post_due(channel.send))
    restarted = WordleService(service.config, service.db, service.source, LISTS, clock=clock)
    asyncio.run(restarted.post_due(channel.send))
    assert len(golf_posts(channel)) == 2


def test_full_round_posts_the_final(tmp_path):
    service, clock, channel = make(tmp_path)
    service.db.add_score(score(ANCHOR, 1, 3))
    service.db.add_score(score(ANCHOR + 17, 2, 2, status=MISMATCH))  # doesn't count
    service.db.mark_golf_posted(ANCHOR, 9)
    clock.set(ROUND_DONE, 8, 0)
    asyncio.run(service.post_due(channel.send))
    [header] = golf_messages(channel)
    assert "Golf round 1, final" in header and "🏆 Winner: **user1**" in header
    assert "user2" not in "".join(channel.sent)


def test_a_missed_milestone_is_superseded_by_the_next(tmp_path):
    service, clock, channel = make(tmp_path)
    clock.set(ROUND_DONE + timedelta(days=3), 12, 0)
    assert service.due_golf() == Milestone(1, ANCHOR, 18)
    asyncio.run(service.post_due(channel.send))
    assert not service.db.golf_posted(ANCHOR, 9)
    assert service.due_golf() is None


def test_golf_can_be_turned_off(tmp_path):
    service, clock, _ = make(tmp_path)
    service.config = Config(
        schedule=service.config.schedule,
        solutions=service.config.solutions,
        golf=GolfConfig(enabled=False),
    )
    clock.set(FRONT_DONE, 8, 0)
    assert service.due_golf() is None


def test_nothing_is_owed_without_a_starter(tmp_path):
    service, clock, _ = make(tmp_path, seed=False)
    clock.set(FRONT_DONE, 8, 0)
    assert service.due_golf() is None
    assert "first starter" in service.golf_standings()


def test_golf_command_shows_the_round_through_yesterday(tmp_path):
    service, clock, _ = make(tmp_path)
    assert "tees off with Wordle 12" in service.golf_standings(TODAY)
    service.db.add_score(score(ANCHOR, 1, 3))
    service.db.add_score(score(ANCHOR + 1, 1, 4))  # today's hole is still in play
    text = service.golf_standings(puzzle_date(ANCHOR + 1, LAUNCH))
    assert "through 1 hole**" in text and "par 4)" in text
    text = service.golf_standings(ROUND_DONE)
    assert "Golf round 1, final" in text


def test_upgrading_does_not_post_a_stale_round(tmp_path):
    """A database from before golf marks a milestone posted before today as done,
    but leaves one due this morning."""
    path = tmp_path / "t.sqlite3"
    today = datetime.now(ScheduleConfig().tz).date()

    def legacy(first_starter):
        db = Database(path)
        db.conn.execute("DROP TABLE golf_posts")
        db.conn.execute("DELETE FROM starters")
        db.save_starter(Starter(first_starter, 500, "crane", "x"), message_id=1)
        db.conn.commit()
        db.close()
        return Database(path)

    # The round's front nine ended three days ago: posted two days ago, so skipped.
    db = legacy(today - timedelta(days=11))
    assert db.golf_posted(500, 9)
    db.close()

    # It ended yesterday: this morning's post is still owed.
    db = legacy(today - timedelta(days=9))
    assert not db.golf_posted(500, 9)
    db.close()
