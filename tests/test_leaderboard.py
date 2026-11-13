import pytest

from wordlebot.db import Score
from wordlebot.leaderboard import (
    BELOW_MINIMUM_NAMED,
    escape_name,
    format_daily,
    format_standings,
    ranked,
    standings,
)
from wordlebot.scores import VERIFIED

ZWSP = "​"


@pytest.mark.parametrize(
    ("name", "escaped"),
    [
        ("plain name", "plain name"),
        ("j.smith-2", "j.smith-2"),
        ("<@123456789012345678>", rf"\<@{ZWSP}123456789012345678\>"),
        ("<@&42>", rf"\<@{ZWSP}&42\>"),
        ("<#42>", r"\<\#42\>"),
        ("@everyone", f"@{ZWSP}everyone"),
        ("@here", f"@{ZWSP}here"),
        ("**bold** _i_ ~~s~~", r"\*\*bold\*\* \_i\_ \~\~s\~\~"),
        ("||spoiler|| `code`", r"\|\|spoiler\|\| \`code\`"),
        ("# header", r"\# header"),
        ("> quote", r"\> quote"),
        ("- item", r"\- item"),
        ("1. item", r"1\. item"),
        ("[x](http://a.b)", r"\[x\]\(http://a.b\)"),
        ("back\\slash", "back\\\\slash"),
        ("two\nlines", "two lines"),
    ],
)
def test_escape_name(name, escaped):
    assert escape_name(name) == escaped


def score(user_id, name, guesses=3, puzzle=1, at=""):
    return Score(
        puzzle_number=puzzle,
        user_id=user_id,
        user_name=name,
        guesses=guesses,
        hard_mode=False,
        grid="",
        status=VERIFIED,
        submitted_at=at,
    )


NAMES = ["<@111>", "@everyone", "**loud**", "# big"]


def assert_defused(text):
    assert "<@111>" not in text
    assert rf"\<@{ZWSP}111\>" in text
    assert "@everyone" not in text
    assert r"\*\*loud\*\*" in text
    assert r"\# big" in text


def test_daily_results_escape_display_names():
    text = format_daily(1, "crane", [score(i, name) for i, name in enumerate(NAMES)])
    assert_defused(text)
    assert text.startswith("**Wordle 1 results** (starter: `CRANE`)")  # our own markup stays


def test_standings_escape_display_names():
    rows = standings([score(i, name) for i, name in enumerate(NAMES)], fail_score=7)
    assert rows[0].user_name in NAMES  # raw until formatted
    assert_defused(format_standings("Last 7 days", rows))


def games(user_id, name, *guesses):
    """One score per puzzle, numbered from 1, for a player."""
    return [score(user_id, name, g, puzzle=n) for n, g in enumerate(guesses, 1)]


def board(scores, min_games=1):
    return format_standings("Board", standings(scores, fail_score=7), min_games).split("\n")


def test_standings_name_is_from_the_latest_submission_whatever_the_row_order():
    # Puzzle 9 was pasted first, puzzle 2 last (a late catch-up under a new name).
    scores = [
        score(1, "old name", puzzle=9, at="2026-11-01T10:00:00+00:00"),
        score(1, "new name", puzzle=2, at="2026-11-05T10:00:00+00:00"),
        score(1, "older name", puzzle=5, at="2026-10-30T10:00:00+00:00"),
    ]
    for rows in (scores, scores[::-1], sorted(scores, key=lambda s: s.puzzle_number)):
        assert [s.user_name for s in standings(rows, fail_score=7)] == ["new name"]


def test_standings_name_falls_back_to_the_highest_puzzle_when_times_are_equal():
    scores = [score(1, "later", puzzle=4), score(1, "earlier", puzzle=3)]
    assert standings(scores, fail_score=7)[0].user_name == "later"
    assert standings(scores[::-1], fail_score=7)[0].user_name == "later"


def test_tied_players_share_a_rank_and_the_next_rank_is_skipped():
    scores = (
        games(1, "ann", 2, 2)
        + games(2, "cat", 3, 4)
        + games(3, "bob", 4, 3)
        + games(4, "dan", 5, 5)
    )
    assert board(scores)[1:] == [
        "1\\. ann — avg **2.00** (2 played, 2 solved)",
        "2\\. bob — avg **3.50** (2 played, 2 solved)",
        "2\\. cat — avg **3.50** (2 played, 2 solved)",
        "4\\. dan — avg **5.00** (2 played, 2 solved)",
    ]


def test_equal_average_with_more_games_ranks_higher_and_is_not_a_tie():
    rows = standings(games(1, "few", 3, 4) + games(2, "many", 3, 4, 3, 4), fail_score=7)
    assert [(rank, s.user_name) for rank, s in ranked(rows)] == [(1, "many"), (2, "few")]


def test_tied_standings_are_ordered_by_name_then_id_whatever_the_row_order():
    scores = [score(3, "Zed"), score(2, "amy"), score(9, "Bo"), score(1, "bo")]
    expected = ["amy", "bo", "Bo", "Zed"]  # ids 1 and 9 share a name, ignoring case
    for rows in (scores, scores[::-1]):
        result = ranked(standings(rows, fail_score=7))
        assert [s.user_name for _, s in result] == expected
        assert [rank for rank, _ in result] == [1, 1, 1, 1]


def test_minimum_games_keeps_a_single_lucky_game_off_the_top():
    scores = games(1, "regular", 3, 4, 3, 4, 3, 4, 3) + games(2, "lucky", 2)
    assert board(scores, min_games=3) == [
        "**Board (min 3 games)**",
        "1\\. regular — avg **3.43** (7 played, 7 solved)",
        "Under 3 games, not ranked: lucky (1)",
    ]


def test_players_below_the_minimum_are_listed_escaped_most_games_first():
    scores = games(1, "@everyone", 3) + games(2, "**b**", 3, 3) + games(3, "ann", 3)
    lines = board(scores, min_games=3)
    assert lines == [
        "**Board (min 3 games)**",
        "No qualifying results yet.",
        f"Under 3 games, not ranked: \\*\\*b\\*\\* (2), @{ZWSP}everyone (1), ann (1)",
    ]


def test_long_below_minimum_list_is_cut_to_a_count():
    scores = [score(i, f"user{i:02}") for i in range(BELOW_MINIMUM_NAMED + 3)]
    line = board(scores, min_games=2)[-1]
    assert line.count("user") == BELOW_MINIMUM_NAMED
    assert line.endswith("user04 (1), +3 more")


def test_minimum_of_one_adds_nothing_to_the_board():
    assert board(games(1, "ann", 3)) == ["**Board**", "1\\. ann — avg **3.00** (1 played, 1 solved)"]
    assert board([]) == ["**Board**", "No qualifying results yet."]


def test_daily_results_list_equal_guesses_in_a_stable_order():
    scores = [
        score(4, "Zed", 3),
        score(9, "bo", 3),
        score(1, "Bo", 3),
        score(2, "amy", None),
        score(3, "cy", 2),
    ]
    expected = ["cy: 2", "Bo: 3", "bo: 3", "Zed: 3", "amy: X"]
    for rows in (scores, scores[::-1]):
        assert format_daily(1, None, rows).split("\n")[1:] == expected
