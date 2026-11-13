"""Choosing the shared starting word.

Pure functions; the caller supplies word lists, history, and an RNG so this is
deterministic under test.
"""

from __future__ import annotations

import logging
import random
from collections import Counter
from collections.abc import Iterable
from datetime import date, timedelta

from .config import ALL_TIME, STRATEGIES, SelectionConfig  # noqa: F401 (re-exported)
from .db import Database
from .words import WordLists

log = logging.getLogger(__name__)


def quality_scores(pool: Iterable[str], reference: Iterable[str]) -> dict[str, float]:
    """Opener strength from letter frequencies in `reference`, normalized to 0..1.

    Each distinct letter scores how often it appears in reference words, plus a bonus
    for how often it appears in that exact position. Repeated letters add nothing.
    """
    reference = list(reference)
    present: Counter[str] = Counter()
    positional: Counter[tuple[int, str]] = Counter()
    for word in reference:
        present.update(set(word))
        positional.update(enumerate(word))
    n = len(reference) or 1

    raw: dict[str, float] = {}
    for word in pool:
        seen: set[str] = set()
        score = 0.0
        for i, ch in enumerate(word):
            if ch in seen:
                continue
            seen.add(ch)
            score += present[ch] / n + 0.5 * positional[(i, ch)] / n
        raw[word] = score

    hi = max(raw.values(), default=1.0) or 1.0
    return {w: s / hi for w, s in raw.items()}


def exclusion_window(days: int, end: date) -> tuple[date | None, date] | None:
    """(start, end) inclusive for a lookback of `days` ending at `end`; None if disabled."""
    if days == 0:
        return None
    if days == ALL_TIME:
        return (None, end)
    return (end - timedelta(days=days - 1), end)


def excluded_words(db: Database, cfg: SelectionConfig, target: date, today: date) -> set[str]:
    excluded: set[str] = set()
    if window := exclusion_window(cfg.exclude_own_starters_days, target - timedelta(days=1)):
        excluded |= {s.word for s in db.starters_between(*window)}
    if window := exclusion_window(cfg.exclude_solutions_days, today):
        excluded |= db.solutions_between(*window)
    return excluded


def candidate_pool(strategy: str, lists: WordLists, solution_history: set[str]) -> set[str]:
    answer_like = set(lists.answers) | solution_history
    if strategy == "random_all_valid":
        return answer_like | set(lists.valid)
    return answer_like


def pick_starter(
    cfg: SelectionConfig,
    lists: WordLists,
    solution_history: set[str],
    excluded: set[str],
    rng: random.Random,
    forbidden: set[str] = frozenset(),  # type: ignore[assignment]
) -> str:
    """Pick a starter. `excluded` is relaxed if it empties the pool; `forbidden` never is."""
    pool = candidate_pool(cfg.strategy, lists, solution_history) - forbidden
    if not pool:
        raise ValueError("No candidate words available")
    eligible = pool - excluded
    if not eligible:
        log.warning("Exclusions removed every candidate; ignoring exclusions")
        eligible = pool

    ordered = sorted(eligible)  # stable order so a seeded RNG is reproducible
    if cfg.strategy in ("random_answer_like", "random_all_valid"):
        return rng.choice(ordered)

    scores = quality_scores(ordered, set(lists.answers) | solution_history)
    if cfg.strategy == "weighted_quality":
        weights = [scores[w] ** cfg.quality_weight_exponent for w in ordered]
        return rng.choices(ordered, weights=weights, k=1)[0]

    # varied: a random pick from either the strongest or the weakest slice
    by_score = sorted(ordered, key=lambda w: (scores[w], w))
    size = max(1, int(len(by_score) * cfg.varied_pool_fraction))
    slice_ = by_score[:size] if rng.random() < cfg.varied_bad_chance else by_score[-size:]
    return rng.choice(slice_)
