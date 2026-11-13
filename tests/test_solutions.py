import asyncio
from datetime import date

from wordlebot.db import Database
from wordlebot.solutions import missing_dates, sync_solutions


class FakeSource:
    def __init__(self, words):
        self.words = words
        self.requested = []

    async def fetch(self, day):
        self.requested.append(day)
        return self.words.get(day)


def test_sync_only_fetches_missing_days(tmp_path):
    launch = date(2026, 1, 1)
    db = Database(tmp_path / "t.sqlite3")
    db.add_solutions([(date(2026, 1, 2), 1, "slate")])
    source = FakeSource({date(2026, 1, 1): "crane", date(2026, 1, 3): "audio"})

    added = asyncio.run(sync_solutions(db, source, launch, date(2026, 1, 3)))
    assert added == 2
    assert sorted(source.requested) == [date(2026, 1, 1), date(2026, 1, 3)]

    source.requested.clear()
    assert asyncio.run(sync_solutions(db, source, launch, date(2026, 1, 3))) == 0
    assert source.requested == []


def test_missing_dates_never_includes_future():
    launch = date(2026, 1, 1)
    assert missing_dates(set(), launch, date(2026, 1, 2)) == [date(2026, 1, 1), date(2026, 1, 2)]
