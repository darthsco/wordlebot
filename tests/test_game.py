from datetime import date

from wordlebot.game import Mark, feedback, puzzle_date, puzzle_number

A, P, C = Mark.ABSENT, Mark.PRESENT, Mark.CORRECT
LAUNCH = date(2021, 6, 19)


def test_puzzle_numbering_matches_nyt():
    assert puzzle_number(date(2026, 11, 12), LAUNCH) == 1972
    assert puzzle_date(0, LAUNCH) == LAUNCH


def test_feedback_basic():
    assert feedback("crane", "karat") == (A, P, P, A, A)


def test_feedback_duplicate_guess_letter_only_marks_available_count():
    # one E in solution: the green E consumes it, the other E is absent
    assert feedback("geese", "those") == (A, A, A, C, C)
    assert feedback("speed", "abide") == (A, A, P, A, P)


def test_feedback_present_before_correct_duplicate():
    assert feedback("allee", "apple") == (C, P, A, A, C)
