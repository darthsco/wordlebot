import asyncio
import io
import sqlite3
from dataclasses import replace
from datetime import timedelta

from PIL import Image
from test_service import LAUNCH, LISTS, TODAY, make

from wordlebot import golf, golf_card
from wordlebot.config import GolfConfig
from wordlebot.db import Database, Score
from wordlebot.game import puzzle_date
from wordlebot.golf import Milestone, card, format_round, latest_milestone, progress
from wordlebot.scores import MISMATCH, NO_STARTER, VERIFIED
from wordlebot.service import GOLF_CORRECTED_PREFIX, Actor, WordleService


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


def test_a_live_hole_not_yet_played_counts_nothing():
    m = Milestone(1, 100, 3)
    ranked = card([score(100, 1, 3), score(102, 2, 4)], m, fail_score=7, live=True)
    player = {p.user_id: p for _, p in ranked}
    assert player[1].cells == ("3", "-", golf.YET)
    assert player[1].strokes == (3, 7, None)
    assert (player[1].total, player[1].to_par, player[1].played) == (10, 2, 1)
    assert player[2].cells == ("-", "-", "4")
    assert (player[2].total, player[2].to_par) == (18, 6)


def test_live_card_ranks_by_score_to_par():
    """Mid-hole, fewer strokes over fewer holes isn't a better round."""
    m = Milestone(1, 100, 2)
    scores = [score(100, 1, 3), score(101, 1, 4), score(100, 2, 4)]
    ranked = card(scores, m, 7, live=True)
    assert [(r, p.user_id, p.total) for r, p in ranked] == [(1, 1, 7), (2, 2, 4)]
    tied = card(scores + [score(100, 3, 3)], m, 7, live=True)
    assert [(r, p.user_id) for r, p in tied] == [(1, 1), (1, 3), (3, 2)]


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


def test_front_nine_names_the_leader_over_the_board():
    m = Milestone(2, 118, 9)
    scores = [score(n, 1, 4, "Alice") for n in range(118, 127)] + [score(118, 2, 3, "Bob")]
    [message] = format_round(m, card(scores, m, 7))
    assert "Golf round 2, after the front nine" in message
    assert "par 36" in message
    assert "Leader: **Alice** (E, 36)" in message
    assert message.count("```") == 2 and "Bob" in message


def test_final_declares_co_champions():
    m = Milestone(1, 100, 18)
    scores = [score(n, u, 4) for n in range(100, 118) for u in (1, 2)]
    scores += [score(n, 3, 5) for n in range(100, 118)]
    [message] = format_round(m, card(scores, m, 7))
    assert "Golf round 1, final" in message and "par 72" in message
    assert "🏆 Co-champions: **user1**, **user2** (E, 72)" in message
    assert "T1" in message


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


def test_long_names_are_truncated_on_the_board():
    m = Milestone(1, 100, 1)
    [message] = format_round(m, card([score(100, 1, 3, "A" * 30)], m, 7))
    board = message.split("```")[1]
    assert "A" * golf.CARD_NAME_WIDTH not in board
    assert "A" * (golf.CARD_NAME_WIDTH - 1) + "…" in board


def test_hole_marks_follow_scorecard_names():
    cells = ["1", "2", "3", "4", "5", "6", "7", golf.FAILED, golf.MISSED, golf.YET]
    marks = [golf.hole_mark(c) for c in cells]
    assert marks == [
        "ace", "eagle", "birdie", "par", "bogey", "double", "double", "failed", "missed", "yet"
    ]


def test_a_live_round_is_never_final():
    m = Milestone(1, 100, 18)
    scores = [score(n, 1, 4) for n in range(100, 117)]
    [message] = format_round(m, card(scores, m, 7, live=True), live=True)
    assert "Golf round 1, hole 18 in play" in message
    assert "Leader: **user1** (E, 68)" in message and "🏆" not in message
    assert "17/18" in message


def test_empty_round():
    assert "No scores this round." in format_round(Milestone(1, 100, 9), [])[0]


def test_long_boards_split_into_complete_blocks():
    m = Milestone(1, 100, 18)
    scores = [score(n, u, 4, f"player{u:03d}") for n in range(100, 118) for u in range(80)]
    messages = format_round(m, card(scores, m, 7), max_players=0)
    assert len(messages) > 1
    for message in messages:
        assert len(message) <= golf.MESSAGE_LIMIT
        assert message.count("```") == 2  # one whole code block per message
    text = "".join(messages)
    assert all(f"player{u:03d}" in text for u in range(80))
    assert text.count("Pos ") == len(messages)  # each continuation repeats the header row


def test_the_board_lists_everyone_and_notes_a_capped_card():
    m = Milestone(1, 100, 9)
    scores = [score(100, u, 3 if u < 3 else 5) for u in range(15)]
    [board] = format_round(m, card(scores, m, 7), max_players=10)
    assert all(f"user{u} " in board for u in range(15))
    assert board.endswith("The card shows the top 10 of 15; everyone is on the leaderboard.")
    [uncapped] = format_round(m, card(scores, m, 7), max_players=20)
    assert "The card shows" not in uncapped


# --- card image ---------------------------------------------------------------


def image_size(png):
    with Image.open(io.BytesIO(png)) as image:
        assert image.format == "PNG"
        return image.size


def test_card_image_stacks_the_back_nine_under_the_front():
    every_mark = [1, 2, 3, 4, 5, 6, None]  # None = X
    scores = [score(100 + h, 1, every_mark[h % 7]) for h in range(18)] + [score(100, 2, 4)]
    front, final = Milestone(1, 100, 9), Milestone(1, 100, 18)
    front_png = golf_card.render(front, card(scores, front, 7), 7)
    final_png = golf_card.render(final, card(scores, final, 7), 7)
    (front_w, front_h), (final_w, final_h) = image_size(front_png), image_size(final_png)
    assert final_h > 1.5 * front_h  # a second section
    assert final_w < 1.5 * front_w  # stacked, not side by side
    assert final_w < 1400  # narrow enough for a phone to show without much zoom


def test_card_image_shows_only_the_capped_players():
    m = Milestone(1, 100, 3)
    scores = [score(100, u, 4) for u in range(12)]
    ranked = card(scores, m, 7, live=True)
    capped = image_size(golf_card.render(m, ranked, 7, max_players=5, live=True))
    everyone = image_size(golf_card.render(m, ranked, 7, max_players=0, live=True))
    rows = (everyone[1] - capped[1]) / (golf_card.ROW_H * golf_card.SCALE)
    assert rows == 7


def test_card_image_drops_what_the_font_cannot_draw():
    assert golf_card._drawable("Zoë 🏌️‍♀️ Ünal") == "Zoë Ünal"
    assert golf_card._drawable("🐸🐸") == "?"
    m = Milestone(1, 100, 1)
    png = golf_card.render(m, card([score(100, 1, 3, "🐸 " + "W" * 40)], m, 7), 7)
    assert image_size(png)[0] > 0


# --- service ------------------------------------------------------------------
#
# `make` seeds a starter for TODAY (puzzle 12), so golf round 1 starts at 12 and its
# front nine (12-20) is posted the morning after puzzle 20.

ANCHOR = 12
FRONT_DONE = puzzle_date(ANCHOR + 8, LAUNCH) + timedelta(days=1)  # the morning after hole 9
ROUND_DONE = puzzle_date(ANCHOR + 17, LAUNCH) + timedelta(days=1)
FRONT = Milestone(1, ANCHOR, 9)
FINAL = Milestone(1, ANCHOR, 18)


def golf_messages(channel):
    """The leaderboard message of each golf post."""
    return [t for t in channel.sent if "Golf round" in t]


def golf_posts(channel):
    """Every message of every golf post (the clock jumps, so starters are owed too)."""
    return [t for t in channel.sent if t not in channel.starters()]


def with_golf(service, **golf_settings):
    service.config = replace(service.config, golf=GolfConfig(**golf_settings))


def test_front_nine_posts_once_at_post_time(tmp_path):
    service, clock, channel = make(tmp_path)
    for n in range(ANCHOR, ANCHOR + 9):
        service.db.add_score(score(n, 1, 4))

    clock.set(FRONT_DONE, 7, 59)
    assert service.due_golf() == []
    clock.set(FRONT_DONE, 8, 0)
    assert service.due_golf() == [FRONT]
    asyncio.run(service.post_due(channel.send))
    [header] = golf_messages(channel)
    assert "after the front nine" in header and "Leader: **user1** (E, 36)" in header
    assert len(golf_posts(channel)) == 1  # the leaderboard, with the card attached
    [image] = channel.images.values()
    assert image.filename == golf_card.FILENAME and image.data.startswith(b"\x89PNG")

    clock.set(FRONT_DONE, 9, 0)
    asyncio.run(service.post_due(channel.send))
    restarted = WordleService(service.config, service.db, service.source, LISTS, clock=clock)
    asyncio.run(restarted.post_due(channel.send))
    assert len(golf_posts(channel)) == 1


def test_the_first_holes_count_toward_round_one(tmp_path):
    """History from before golf existed is the start of round 1, whatever day it
    ships: nothing is skipped or backfilled."""
    service, clock, channel = make(tmp_path)
    service.db.add_score(score(ANCHOR, 1, 2))  # hole 1, played before the feature
    clock.set(FRONT_DONE, 8, 0)
    asyncio.run(service.post_due(channel.send))
    [header] = golf_messages(channel)
    assert "Golf round 1, after the front nine" in header and "(+22, 58)" in header  # 2 + 8 missed at 7


def test_full_round_posts_the_final(tmp_path):
    service, clock, channel = make(tmp_path)
    service.db.add_score(score(ANCHOR, 1, 3))
    service.db.add_score(score(ANCHOR + 17, 2, 2, status=MISMATCH))  # doesn't count
    service.db.mark_golf_posted(ANCHOR, 9, [])
    clock.set(ROUND_DONE, 8, 0)
    asyncio.run(service.post_due(channel.send))
    [header] = golf_messages(channel)
    assert "Golf round 1, final" in header and "🏆 Winner: **user1**" in header
    assert "user2" not in "".join(channel.sent)


def test_a_missed_milestone_is_superseded_by_default(tmp_path):
    service, clock, channel = make(tmp_path)
    clock.set(ROUND_DONE + timedelta(days=3), 12, 0)
    assert service.due_golf() == [FINAL]
    asyncio.run(service.post_due(channel.send))
    assert not service.db.golf_posted(ANCHOR, 9)
    assert service.due_golf() == []


def test_missed_posts_all_posts_each_oldest_first(tmp_path):
    service, clock, channel = make(tmp_path)
    with_golf(service, missed_posts="all")
    clock.set(ROUND_DONE + timedelta(days=3), 12, 0)
    assert service.due_golf() == [FRONT, FINAL]
    asyncio.run(service.post_due(channel.send))
    assert ["front nine" in m for m in golf_messages(channel)] == [True, False]
    assert service.due_golf() == []


def test_missed_posts_all_never_goes_back_before_the_last_posted(tmp_path):
    service, clock, _ = make(tmp_path)
    with_golf(service, missed_posts="all")
    service.db.mark_golf_posted(ANCHOR, 18, [])  # e.g. posted under "latest"
    clock.set(ROUND_DONE + timedelta(days=12), 12, 0)
    assert service.due_golf() == [Milestone(2, ANCHOR + 18, 9)]


def test_golf_can_be_turned_off(tmp_path):
    service, clock, _ = make(tmp_path)
    with_golf(service, enabled=False)
    clock.set(FRONT_DONE, 8, 0)
    assert service.due_golf() == []


def test_nothing_is_owed_without_a_starter(tmp_path):
    service, clock, _ = make(tmp_path, seed=False)
    clock.set(FRONT_DONE, 8, 0)
    assert service.due_golf() == []
    [message] = service.golf_standings()
    assert "first starter" in message.text and message.image is None


def test_golf_command_shows_the_round_live_through_today(tmp_path):
    service, clock, _ = make(tmp_path)

    def standings(day):
        return "\n".join(m.text for m in service.golf_standings(day))

    assert "tees off with Wordle 12" in standings(TODAY - timedelta(days=1))
    [empty] = service.golf_standings(TODAY)
    assert "No scores this round." in empty.text and empty.image is None
    service.db.add_score(score(ANCHOR, 1, 3))
    service.db.add_score(score(ANCHOR, 2, 5))
    service.db.add_score(score(ANCHOR + 1, 2, 4))  # today's hole: user2 has played it
    [message] = service.golf_standings(puzzle_date(ANCHOR + 1, LAUNCH))
    assert "Golf round 1, hole 2 in play** (Wordle 12–13, par 8)" in message.text
    assert "Leader: **user1** (-1, 3)" in message.text  # yet to play hole 2, so not +2 behind
    assert message.image is not None
    assert "Golf round 2, hole 1 in play" in standings(ROUND_DONE)


def test_golf_rounds_lists_the_current_round_then_earlier_ones_newest_first(tmp_path):
    service, _, _ = make(tmp_path)
    assert service.golf_rounds(TODAY - timedelta(days=1)) == []  # before hole 1
    assert service.golf_rounds(TODAY) == [(Milestone(1, ANCHOR, 1), [])]
    service.db.add_score(score(ANCHOR, 1, 3))
    service.db.add_score(score(ANCHOR + 18, 2, 2))
    service.db.add_score(score(ANCHOR + 37, 3, 2))  # round 3, after today
    rounds = service.golf_rounds(ROUND_DONE + timedelta(days=5))
    assert [m for m, _ in rounds] == [Milestone(2, ANCHOR + 18, 6), FINAL]
    assert rounds[0][1][0][1].cells[-1] == golf.YET
    assert [p.user_id for _, p in rounds[0][1]] == [2]
    assert [p.user_id for _, p in rounds[1][1]] == [1]


# --- score edits after a post -----------------------------------------------------

ADMIN = Actor("admin", "127.0.0.1")


class Edits:
    """Records message edits; messages are numbered as the fake channel sends them."""

    def __init__(self, channel):
        self.channel = channel
        self.made = []

    async def __call__(self, message_id, text, image=None):
        self.channel.sent[message_id - 1] = text
        if image is not None:
            self.channel.images[message_id] = image
        self.made.append(message_id)


def posted_front_nine(tmp_path, **golf_settings):
    service, clock, channel = make(tmp_path)
    with_golf(service, **golf_settings)
    service.db.add_score(score(ANCHOR, 1, 3))
    service.db.add_score(score(ANCHOR, 2, 4))
    clock.set(FRONT_DONE, 8, 0)
    asyncio.run(service.post_due(channel.send))
    channel.sent = golf_posts(channel)  # drop the LATE starters, keep ids == positions
    edits = Edits(channel)

    def delete_user1():
        edit = service.plan_delete(ANCHOR, 1)
        asyncio.run(service.apply_edit(edit, ADMIN, "test", channel.send, edits))

    return service, channel, edits, delete_user1


def test_score_edits_leave_golf_posts_alone_by_default(tmp_path):
    service, channel, edits, delete_user1 = posted_front_nine(tmp_path)
    before = list(channel.sent)
    delete_user1()
    assert channel.sent == before and edits.made == []


def test_score_edit_can_rewrite_the_posted_messages(tmp_path):
    service, channel, edits, delete_user1 = posted_front_nine(tmp_path, on_score_edit="edit")
    before = channel.images[1]
    delete_user1()
    assert edits.made == [1] and len(channel.sent) == 1
    assert "Leader: **user2**" in channel.sent[0] and "user1" not in channel.sent[0]
    assert channel.images[1] != before  # the card was replaced too
    assert service.db.golf_post_messages(ANCHOR, 9) == [(1, channel.sent[0], channel.images[1].sha)]


def test_score_edit_rewrites_a_post_whose_card_alone_changed(tmp_path):
    service, channel, edits, _ = posted_front_nine(tmp_path, on_score_edit="edit")
    service.db.add_score(score(ANCHOR, 3, 2, status=MISMATCH))

    def override_user3(status):
        edit = service.plan_override(ANCHOR, 3, status)  # counts neither before nor after
        asyncio.run(service.apply_edit(edit, ADMIN, "test", channel.send, edits))

    override_user3(NO_STARTER)
    assert edits.made == []  # nothing on the post changed
    [(mid, text, _)] = service.db.golf_post_messages(ANCHOR, 9)
    service.db.mark_golf_posted(ANCHOR, 9, [(mid, text, "a different image")])
    override_user3(MISMATCH)
    assert edits.made == [1]
    assert service.db.golf_post_messages(ANCHOR, 9) == [(1, text, channel.images[1].sha)]


def test_score_edit_can_post_a_correction(tmp_path):
    service, channel, edits, delete_user1 = posted_front_nine(tmp_path, on_score_edit="correct")
    delete_user1()
    assert edits.made == [] and len(channel.sent) == 2
    assert channel.sent[1].startswith(GOLF_CORRECTED_PREFIX)
    assert "Leader: **user2**" in channel.sent[1] and 2 in channel.images
    assert [mid for mid, _, _ in service.db.golf_post_messages(ANCHOR, 9)] == [2]


def test_an_edit_outside_posted_rounds_changes_nothing(tmp_path):
    service, channel, edits, _ = posted_front_nine(tmp_path, on_score_edit="correct")
    service.db.add_score(score(ANCHOR + 9, 1, 3))  # back nine, not posted yet
    edit = service.plan_delete(ANCHOR + 9, 1)
    asyncio.run(service.apply_edit(edit, ADMIN, "test", channel.send, edits))
    assert len(channel.sent) == 1


# --- unplayed-hole reminder ---------------------------------------------------

HOLE_4 = TODAY + timedelta(days=3)  # puzzle 15, hole 4 of round 1


def test_daily_post_pings_round_players_who_havent_played_today(tmp_path):
    service, clock, channel = make(tmp_path)
    service.db.add_score(score(ANCHOR, 1, 4))
    service.db.add_score(score(ANCHOR + 1, 2, 3))
    service.db.add_score(score(ANCHOR + 2, 3, 5))
    service.db.add_score(score(ANCHOR + 3, 3, 4, status=MISMATCH))  # played today, if not well
    service.db.add_score(score(ANCHOR + 1, 4, 4, status=MISMATCH))  # never counted: not on the card
    service.db.add_score(score(ANCHOR + 3, 5, 4))  # first hole today: already played

    clock.set(HOLE_4, 20, 0)
    asyncio.run(service.post_due(channel.send))
    [post] = [t for t in channel.starters() if "LATE" not in t]
    assert post.startswith("🟩")
    head, nudge = post.split("\n\n⛳ ")
    assert "Starting word" in head and "<@" not in head
    assert nudge == "**Golf round 1, hole 4** (Wordle 15) is still to play today:\n<@1> <@2>"
    assert channel.pinged == {channel.sent.index(post) + 1: [1, 2]}


def test_reminder_post_carries_the_golf_line_too(tmp_path):
    service, clock, channel = make(tmp_path)
    service.db.add_score(score(ANCHOR, 1, 4))
    clock.set(TODAY + timedelta(days=1), 12, 0)
    asyncio.run(service.reroll(channel.send))  # announces before the daily post
    clock.set(TODAY + timedelta(days=1), 20, 0)
    asyncio.run(service.post_due(channel.send))
    assert channel.sent[-1].startswith("⏰") and channel.sent[-1].endswith("\n<@1>")
    assert channel.pinged[len(channel.sent)] == [1]


def test_no_golf_line_when_nobody_is_owed_a_reminder(tmp_path):
    service, clock, channel = make(tmp_path)
    # Hole 1: nobody is on the round's card yet.
    service.db.add_score(score(ANCHOR - 1, 1, 4))  # before the round: not active
    assert service.golf_nudge(TODAY) == ("", [])
    asyncio.run(service.post_due(channel.send))
    assert "⛳" not in channel.sent[-1] and channel.pinged == {}

    # A new round starts from scratch: round 1's players aren't on round 2's card.
    service.db.add_score(score(ANCHOR, 1, 4))
    round_2 = puzzle_date(ANCHOR + 18, LAUNCH)
    assert service.golf_unplayed(round_2) == (Milestone(2, ANCHOR + 18, 1), [])
    assert service.golf_nudge(round_2 + timedelta(days=1))[1] == []

    # Off, or golf off, or the starter is for today's own puzzle.
    assert service.golf_nudge(TODAY + timedelta(days=1))[1] == [1]
    with_golf(service, remind_unplayed=False)
    assert service.golf_nudge(TODAY + timedelta(days=1)) == ("", [])
    with_golf(service, enabled=False)
    assert service.golf_nudge(TODAY + timedelta(days=1)) == ("", [])
    with_golf(service)
    service.config = replace(
        service.config, schedule=replace(service.config.schedule, target_offset_days=0)
    )
    assert service.golf_nudge(TODAY + timedelta(days=1)) == ("", [])


def test_golf_post_messages_from_before_card_images_are_migrated(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE golf_post_messages (first_puzzle INTEGER NOT NULL, holes INTEGER NOT NULL,"
        " seq INTEGER NOT NULL, message_id INTEGER NOT NULL, text TEXT NOT NULL,"
        " PRIMARY KEY (first_puzzle, holes, seq))"
    )
    old.execute("INSERT INTO golf_post_messages VALUES (12, 9, 0, 501, 'board')")
    old.commit()
    old.close()

    db = Database(path)
    assert db.golf_post_messages(12, 9) == [(501, "board", None)]
    db.mark_golf_posted(12, 9, [(502, "board", "abc")])
    db.close()
    assert Database(path).golf_post_messages(12, 9) == [(502, "board", "abc")]  # reopening is a no-op
