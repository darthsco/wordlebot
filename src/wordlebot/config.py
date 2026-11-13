"""Configuration loaded from a TOML file, with secrets from the environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ALL_TIME = -1


@dataclass(frozen=True)
class DiscordConfig:
    guild_id: int = 0
    channel_id: int = 0
    admin_role_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ScheduleConfig:
    timezone: str = "America/New_York"
    post_time: str = "20:00"
    target_offset_days: int = 1
    post_daily_results: bool = True

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def post_time_local(self) -> time:
        hour, minute = (int(p) for p in self.post_time.split(":"))
        return time(hour, minute, tzinfo=self.tz)


@dataclass(frozen=True)
class GameConfig:
    word_length: int = 5
    max_guesses: int = 6
    fail_score: int = 7


@dataclass(frozen=True)
class SelectionConfig:
    strategy: str = "random_answer_like"
    exclude_own_starters_days: int = 180
    exclude_solutions_days: int = 365
    avoid_target_solution: bool = True
    quality_weight_exponent: float = 2.0
    varied_bad_chance: float = 0.3
    varied_pool_fraction: float = 0.2


@dataclass(frozen=True)
class ScoresConfig:
    count_unverified: bool = False
    weekly_window_days: int = 7
    min_games_all_time: int = 5


@dataclass(frozen=True)
class StorageConfig:
    database_path: str = "wordlebot.sqlite3"
    words_dir: str = "data/words"


@dataclass(frozen=True)
class SolutionsConfig:
    base_url: str = "https://www.nytimes.com/svc/wordle/v2"
    launch_date: str = "2021-06-19"
    fetch_concurrency: int = 8

    @property
    def launch(self) -> date:
        return date.fromisoformat(self.launch_date)


@dataclass(frozen=True)
class Config:
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    game: GameConfig = field(default_factory=GameConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    scores: ScoresConfig = field(default_factory=ScoresConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    solutions: SolutionsConfig = field(default_factory=SolutionsConfig)
    token: str = field(default="", repr=False)


def _build(cls: type, raw: dict[str, Any]) -> Any:
    known = {f.name: f for f in fields(cls)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(f"Unknown config keys in [{cls.__name__}]: {sorted(unknown)}")
    kwargs = {}
    for name, value in raw.items():
        if isinstance(value, list):
            value = tuple(value)
        kwargs[name] = value
    return cls(**kwargs)


def load_config(path: str | Path = "config.toml") -> Config:
    raw: dict[str, Any] = {}
    path = Path(path)
    if path.exists():
        raw = tomllib.loads(path.read_text())

    sections = {}
    for f in fields(Config):
        if f.name == "token":
            continue
        section_cls = f.default_factory  # type: ignore[misc]
        assert is_dataclass(section_cls)
        sections[f.name] = _build(section_cls, raw.pop(f.name, {}))
    if raw:
        raise ValueError(f"Unknown config sections: {sorted(raw)}")

    config = Config(**sections, token=os.environ.get("DISCORD_TOKEN", ""))
    validate(config)
    return config


def validate(config: Config) -> None:
    if config.game.word_length != 5:
        raise ValueError("Only word_length = 5 is supported by the NYT solution source")
    if config.selection.strategy not in {
        "random_answer_like",
        "random_all_valid",
        "weighted_quality",
        "varied",
    }:
        raise ValueError(f"Unknown selection strategy: {config.selection.strategy}")
    config.schedule.post_time_local  # raises on malformed time / timezone
