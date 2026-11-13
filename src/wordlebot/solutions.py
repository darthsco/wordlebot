"""Historical Wordle solutions: fetching from the NYT and keeping the local cache current.

The NYT serves one JSON document per day at {base_url}/YYYY-MM-DD.json and publishes
some days in advance. To avoid spoilers, the cache only ever stores dates up to and
including "today" in the bot's timezone; future solutions are only peeked at (never
persisted) to avoid suggesting a puzzle's own answer as its starter.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Protocol

import aiohttp

from .db import Database
from .game import puzzle_number

log = logging.getLogger(__name__)


class SolutionSource(Protocol):
    async def fetch(self, day: date) -> str | None:
        """The solution word for `day`, or None if unavailable."""
        ...


class NytSolutionSource:
    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")

    async def fetch(self, day: date) -> str | None:
        url = f"{self.base_url}/{day.isoformat()}.json"
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            data = await resp.json()
        word = data.get("solution")
        return word.lower() if isinstance(word, str) else None


def missing_dates(cached: set[date], launch: date, today: date) -> list[date]:
    """Every date from launch through today that isn't cached, oldest first."""
    days = (today - launch).days + 1
    return [d for d in (launch + timedelta(n) for n in range(days)) if d not in cached]


async def sync_solutions(
    db: Database,
    source: SolutionSource,
    launch: date,
    today: date,
    concurrency: int = 8,
) -> int:
    """Fetch any solutions missing from the cache up to `today`. Returns count added.

    Normally this is a single request for today's puzzle; the first run backfills
    the full history, and gaps from downtime are filled automatically.
    """
    todo = missing_dates(db.solution_dates(), launch, today)
    if not todo:
        return 0

    sem = asyncio.Semaphore(concurrency)

    async def one(day: date) -> tuple[date, int, str] | None:
        async with sem:
            try:
                word = await source.fetch(day)
            except Exception:
                log.warning("Failed to fetch solution for %s", day, exc_info=True)
                return None
        if word is None:
            log.warning("No solution published for %s", day)
            return None
        return (day, puzzle_number(day, launch), word)

    results = await asyncio.gather(*(one(d) for d in todo))
    rows = [r for r in results if r is not None]
    db.add_solutions(rows)
    log.info("Cached %d new solution(s) (%d requested)", len(rows), len(todo))
    return len(rows)


async def peek_solution(source: SolutionSource, day: date) -> str | None:
    """Fetch a (possibly future) solution without storing it. Never raises."""
    try:
        return await source.fetch(day)
    except Exception:
        log.warning("Could not peek solution for %s", day, exc_info=True)
        return None
