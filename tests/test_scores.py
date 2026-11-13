from wordlebot.game import Mark
from wordlebot.scores import MISMATCH, NO_STARTER, PENDING, VERIFIED, parse_share, verify

SHARE = """Wordle 1,972 3/6*

⬛🟨🟩⬛⬛
🟩⬛🟩🟨⬛
🟩🟩🟩🟩🟩"""


def test_parse_share():
    s = parse_share(SHARE)
    assert s is not None
    assert (s.puzzle_number, s.guesses, s.hard_mode) == (1972, 3, True)
    assert s.grid_text.splitlines()[0] == "apcaa"


def test_parse_fail_and_high_contrast():
    rows = "\n".join(["⬜🟦⬜⬜⬜"] * 6)
    s = parse_share(f"Wordle 1972 X/6\n\n{rows}")
    assert s is not None and s.guesses is None and len(s.rows) == 6


def test_parse_rejects_inconsistent_grid():
    assert parse_share("Wordle 1,972 2/6\n\n🟩🟩🟩🟩🟩") is None  # too few rows
    assert parse_share("Wordle 1,972 1/6\n\n⬛🟩🟩🟩🟩") is None  # not solved
    assert parse_share("hello there") is None


def test_parse_ignores_surrounding_chatter():
    s = parse_share("ugh, rough one\n" + SHARE + "\nwhat a day")
    assert s is not None and s.guesses == 3


def test_verify():
    row = (Mark.ABSENT, Mark.PRESENT, Mark.PRESENT, Mark.ABSENT, Mark.ABSENT)
    assert verify(row, "crane", "karat") == VERIFIED
    assert verify(row, "slate", "karat") == MISMATCH
    assert verify(row, "crane", None) == PENDING
    assert verify(row, None, "karat") == NO_STARTER
