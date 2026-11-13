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


def test_parse_space_grouped_puzzle_number():
    for sep in (" ", "\u00a0", "\u202f", ",", "."):
        s = parse_share(SHARE.replace("1,972", f"1{sep}972"))
        assert s is not None, repr(sep)
        assert (s.puzzle_number, s.guesses) == (1972, 3)


def test_parse_ungrouped_puzzle_number_keeps_guess_count():
    grid = "\n".join(["⬛🟨🟩⬛⬛"] * 3 + ["🟩🟩🟩🟩🟩"])
    for header, number in (("972", 972), ("1972", 1972), ("12 345", 12345)):
        s = parse_share(f"Wordle {header} 4/6\n\n{grid}")
        assert s is not None, header
        assert (s.puzzle_number, s.guesses) == (number, 4)
    # A space is only a separator when exactly three digits follow it.
    assert parse_share(f"Wordle 1 97 4/6\n\n{grid}") is None


def test_parse_spoiler_tagged_grid():
    expected = parse_share(SHARE)
    head, grid = SHARE.split("\n\n")
    per_row = "\n".join(f"||{line}||" for line in grid.splitlines())
    assert parse_share(f"{head}\n\n{per_row}") == expected
    assert parse_share(f"{head}\n\n||{grid}||") == expected
    assert parse_share(f"{head}\n||\n{grid}\n||") == expected
    assert parse_share(f"||{SHARE}||") == expected


def test_parse_quoted_share():
    expected = parse_share(SHARE)
    assert parse_share("\n".join(f"> {line}" for line in SHARE.splitlines())) == expected
    assert parse_share("\n".join(f">{line}" for line in SHARE.splitlines())) == expected


def test_parse_trailing_text_after_grid_row():
    expected = parse_share(SHARE)
    assert parse_share(SHARE + " nice!") == expected
    assert parse_share(SHARE.replace("🟩🟩🟩🟩🟩", "||🟩🟩🟩🟩🟩|| nice!")) == expected
    # Chatter that merely starts with a square is not a grid row.
    assert parse_share(SHARE + "\n🟩 finally") == expected
    assert parse_share(SHARE.replace("\n\n", "\n🟩 = right spot\n", 1)) == expected


def test_parse_lenient_rows_keep_consistency_checks():
    assert parse_share("> Wordle 1 972 2/6\n>\n> ||🟩🟩🟩🟩🟩|| nice!") is None  # too few rows
    assert parse_share("Wordle 1 972 1/6\n\n||⬛🟩🟩🟩🟩|| so close") is None  # not solved
    early = "Wordle 1 972 3/6\n\n🟩🟩🟩🟩🟩 oops\n⬛🟨🟩⬛⬛\n🟩🟩🟩🟩🟩"
    assert parse_share(early) is None  # solved row before the last
    assert parse_share("Wordle 1 972 1/6\n\n🟩🟩🟩🟩🟩🟩 nice!") is None  # wrong width
    assert parse_share("Wordle 1 972 1/6\n\n🟩🟩🟩🟩🟩nice!") is None  # text glued to the row


def test_parse_skips_header_without_grid():
    expected = parse_share(SHARE)
    # An earlier mention of another puzzle must not cause rejection...
    assert parse_share("yesterday Wordle 1,971 5/6 lol\n" + SHARE) == expected
    # ...nor steal the grid when its guess count happens to match.
    assert parse_share("yesterday Wordle 1,971 3/6 lol\n" + SHARE) == expected
    assert parse_share("Wordle 1,971 3/6 Wordle 1,972 3/6*\n\n" + SHARE.split("\n\n")[1]) == expected
    # Headers that are malformed for this game are skipped too.
    assert parse_share("Wordle 1,971 5/7\n" + SHARE) == expected
    assert parse_share("Wordle 1,971 9/6\n" + SHARE) == expected


def test_parse_skips_header_with_invalid_grid():
    expected = parse_share(SHARE)
    assert parse_share("Wordle 1,971 2/6\n\n🟩🟩🟩🟩🟩\n\n" + SHARE) == expected  # too few rows
    assert parse_share("Wordle 1,971 1/6\n\n⬛🟩🟩🟩🟩\n\n" + SHARE) == expected  # not solved


def test_parse_grid_must_precede_next_header():
    # The only grid belongs to 1,972; a trailing header has no grid of its own.
    assert parse_share(SHARE + "\nbetter than Wordle 1,971 5/6") == parse_share(SHARE)
    # A header never borrows a grid from across another header.
    grid = SHARE.split("\n\n")[1]
    s = parse_share("Wordle 1,970 3/6\nWordle 1,971 4/6\n\n" + grid)
    assert s is None
    assert parse_share("Wordle 1,971 5/6 lol\nWordle 1,970 2/6") is None


def test_parse_two_complete_shares_takes_first():
    other = "Wordle 1,971 2/6\n\n⬛⬛🟨⬛⬛\n🟩🟩🟩🟩🟩"
    s = parse_share(other + "\n\n" + SHARE)
    assert s is not None and (s.puzzle_number, s.guesses, s.hard_mode) == (1971, 2, False)
    assert len(s.rows) == 2
    s = parse_share(SHARE + "\n\n" + other)
    assert s is not None and (s.puzzle_number, s.guesses) == (1972, 3)
    assert len(s.rows) == 3


def test_verify():
    row = (Mark.ABSENT, Mark.PRESENT, Mark.PRESENT, Mark.ABSENT, Mark.ABSENT)
    assert verify(row, "crane", "karat") == VERIFIED
    assert verify(row, "slate", "karat") == MISMATCH
    assert verify(row, "crane", None) == PENDING
    assert verify(row, None, "karat") == NO_STARTER


def test_absurdly_long_puzzle_number_is_not_a_share():
    grid = "\n" + "🟩" * 5
    assert parse_share(f"Wordle 999,999,999 1/6{grid}").puzzle_number == 999_999_999
    assert parse_share(f"Wordle 1,999,999,999 1/6{grid}") is None
    assert parse_share(f"Wordle {'9' * 5000} 1/6{grid}") is None
