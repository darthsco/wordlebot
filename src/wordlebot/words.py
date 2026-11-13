"""Word lists, keyed by word length.

Files live in `words_dir`:
  answers-{n}.txt  curated answer-like words (the original Wordle answer list)
  allowed-{n}.txt  additional valid guesses that were never intended as answers
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def _read(path: Path, length: int) -> frozenset[str]:
    if not path.exists():
        return frozenset()
    words = (line.strip().lower() for line in path.read_text().splitlines())
    return frozenset(w for w in words if len(w) == length and w.isalpha())


@dataclass(frozen=True)
class WordLists:
    answers: frozenset[str]
    allowed: frozenset[str]

    @property
    def valid(self) -> frozenset[str]:
        return self.answers | self.allowed

    @classmethod
    def load(cls, words_dir: str | Path, length: int) -> WordLists:
        words_dir = Path(words_dir)
        answers = _read(words_dir / f"answers-{length}.txt", length)
        if not answers:
            raise FileNotFoundError(f"No answer list for length {length} in {words_dir}")
        return cls(answers=answers, allowed=_read(words_dir / f"allowed-{length}.txt", length))
