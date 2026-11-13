import random
from datetime import date

from wordlebot.config import SelectionConfig
from wordlebot.db import Database, Starter
from wordlebot.selection import excluded_words, pick_starter, quality_scores
from wordlebot.words import WordLists

LISTS = WordLists(
    answers=frozenset({"crane", "slate", "fuzzy", "jazzy", "audio"}),
    allowed=frozenset({"xylyl"}),
)


def test_random_answer_like_respects_exclusions_and_forbidden():
    cfg = SelectionConfig()
    rng = random.Random(1)
    for _ in range(50):
        word = pick_starter(cfg, LISTS, set(), {"crane", "slate"}, rng, {"audio"})
        assert word in {"fuzzy", "jazzy"}


def test_history_solutions_join_the_pool():
    cfg = SelectionConfig()
    word = pick_starter(cfg, LISTS, {"karat"}, set(LISTS.answers), random.Random(0))
    assert word == "karat"


def test_all_valid_includes_allowed_guesses():
    cfg = SelectionConfig(strategy="random_all_valid")
    word = pick_starter(cfg, LISTS, set(), set(LISTS.answers), random.Random(0))
    assert word == "xylyl"


def test_exclusions_relaxed_when_pool_empty_but_forbidden_is_not():
    cfg = SelectionConfig()
    word = pick_starter(cfg, LISTS, set(), set(LISTS.answers), random.Random(0), {"crane"})
    assert word in LISTS.answers - {"crane"}


def test_quality_prefers_common_distinct_letters():
    scores = quality_scores(["slate", "jazzy"], ["slate", "crane", "audio", "stare"])
    assert scores["slate"] > scores["jazzy"]


def test_varied_and_weighted_return_pool_words():
    for strategy in ("weighted_quality", "varied"):
        cfg = SelectionConfig(strategy=strategy)
        assert pick_starter(cfg, LISTS, set(), set(), random.Random(3)) in LISTS.answers


def test_excluded_words_windows(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    db.save_starter(Starter(date(2026, 11, 1), 1961, "crane", "x"))
    db.save_starter(Starter(date(2026, 5, 1), 1777, "slate", "x"))
    db.add_solutions([(date(2026, 11, 10), 1970, "fuzzy"), (date(2025, 1, 1), 1292, "jazzy")])
    target, today = date(2026, 11, 14), date(2026, 11, 13)

    cfg = SelectionConfig(exclude_own_starters_days=30, exclude_solutions_days=30)
    assert excluded_words(db, cfg, target, today) == {"crane", "fuzzy"}

    cfg = SelectionConfig(exclude_own_starters_days=-1, exclude_solutions_days=0)
    assert excluded_words(db, cfg, target, today) == {"crane", "slate"}
