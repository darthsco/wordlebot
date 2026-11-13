"""Configuration loaded from a TOML file, with secrets from the environment."""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import date, time
from pathlib import Path
from typing import Any, Callable, get_args, get_origin, get_type_hints
from zoneinfo import ZoneInfo

ALL_TIME = -1

STRATEGIES = ("random_answer_like", "random_all_valid", "weighted_quality", "varied")


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
    min_games_weekly: int = 3
    min_games_monthly: int = 7
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


def _build(name: str, cls: type, raw: Any) -> Any:
    if not isinstance(raw, dict):
        raise ValueError(f"Config section [{name}] must be a table, got {_describe(raw)}")
    known = {f.name: f for f in fields(cls)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(f"Unknown config keys in [{name}]: {sorted(unknown)}")
    kwargs = {}
    for key, value in raw.items():
        if isinstance(value, list):
            value = tuple(value)
        kwargs[key] = value
    return cls(**kwargs)


def load_config(path: str | Path = "config.toml", *, for_run: bool = False) -> Config:
    """Load and validate the config file.

    A missing file yields all defaults, which is fine for the offline `sync` and `pick`
    commands. `for_run=True` (starting the bot) requires the file to exist and the
    Discord guild and channel to be set.
    """
    raw: dict[str, Any] = {}
    path = Path(path)
    if path.exists():
        raw = tomllib.loads(path.read_text())
    elif for_run:
        raise ValueError(
            f"Config file not found: {path} (copy config.example.toml to config.toml, "
            "or point --config / WORDLEBOT_CONFIG at it)"
        )

    sections = {}
    for f in fields(Config):
        if f.name == "token":
            continue
        section_cls = f.default_factory  # type: ignore[misc]
        assert is_dataclass(section_cls)
        sections[f.name] = _build(f.name, section_cls, raw.pop(f.name, {}))
    if raw:
        raise ValueError(f"Unknown config sections: {sorted(raw)}")

    # Deployment overrides (e.g. a container volume) without editing config.toml.
    if db_path := os.environ.get("WORDLEBOT_DATABASE_PATH"):
        sections["storage"] = replace(sections["storage"], database_path=db_path)

    config = Config(**sections, token=os.environ.get("DISCORD_TOKEN", ""))
    validate(config, for_run=for_run)
    return config


# --- validation -------------------------------------------------------------------------
#
# Types are checked generically against the dataclass annotations, so a new key needs no
# type rule. Value constraints live in _RULES; add an entry there for a new bounded key.

Rule = tuple[Callable[[Any], bool], str]  # (is_valid, "must be ..." message)


def _at_least(minimum: int) -> Rule:
    return (lambda v: v >= minimum, f"must be >= {minimum}")


def _is_post_time(value: str) -> bool:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    return bool(match) and int(match[1]) <= 23 and int(match[2]) <= 59


def _is_timezone(value: str) -> bool:
    try:
        ZoneInfo(value)
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        return False
    return True


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


_NON_EMPTY: Rule = (lambda v: bool(v.strip()), "must not be empty")
_EXCLUSION_DAYS: Rule = (lambda v: v >= ALL_TIME, "must be >= -1 (0 = off, -1 = all time)")

# section name -> key -> rule. Rules only run on values that already passed the type check.
_RULES: dict[str, dict[str, Rule]] = {
    "discord": {
        "guild_id": _at_least(0),
        "channel_id": _at_least(0),
    },
    "schedule": {
        "timezone": (_is_timezone, "must be an IANA timezone name such as \"America/New_York\""),
        "post_time": (_is_post_time, "must be HH:MM in 24-hour time, e.g. \"20:00\""),
        "target_offset_days": _at_least(0),
    },
    "game": {
        "word_length": (lambda v: v == 5, "must be 5 (the NYT solution source is 5-letter only)"),
        "max_guesses": _at_least(1),
    },
    "selection": {
        "strategy": (lambda v: v in STRATEGIES, f"must be one of {', '.join(STRATEGIES)}"),
        "exclude_own_starters_days": _EXCLUSION_DAYS,
        "exclude_solutions_days": _EXCLUSION_DAYS,
        "quality_weight_exponent": (lambda v: math.isfinite(v) and v >= 0, "must be >= 0"),
        "varied_bad_chance": (lambda v: 0 <= v <= 1, "must be between 0 and 1"),
        "varied_pool_fraction": (lambda v: 0 < v <= 1, "must be greater than 0 and at most 1"),
    },
    "scores": {
        "weekly_window_days": _at_least(1),
        "min_games_weekly": _at_least(1),
        "min_games_monthly": _at_least(1),
        "min_games_all_time": _at_least(1),
    },
    "storage": {
        "database_path": _NON_EMPTY,
        "words_dir": _NON_EMPTY,
    },
    "solutions": {
        "base_url": _NON_EMPTY,
        "launch_date": (_is_iso_date, "must be a YYYY-MM-DD date"),
        "fetch_concurrency": _at_least(1),
    },
}

_TYPE_NAMES = {
    bool: "a boolean",
    int: "an integer",
    float: "a number",
    str: "a string",
    tuple: "an array",
    list: "an array",
    dict: "a table",
}


def _describe(value: Any) -> str:
    return _TYPE_NAMES.get(type(value), type(value).__name__)


def _expected(hint: Any) -> str:
    if get_origin(hint) is tuple:
        return f"an array of {_expected(get_args(hint)[0]).split()[-1]}s"
    return _TYPE_NAMES.get(hint, str(hint))


def _type_ok(value: Any, hint: Any) -> bool:
    if get_origin(hint) is tuple:  # tuple[X, ...]: a TOML array of X
        item = get_args(hint)[0]
        return isinstance(value, tuple) and all(_type_ok(v, item) for v in value)
    if hint is bool:
        return isinstance(value, bool)
    if isinstance(value, bool):  # bool is an int subclass, but never a valid number here
        return False
    if hint is float:
        return isinstance(value, (int, float))
    return isinstance(value, hint)


def _section_errors(name: str, section: Any) -> tuple[list[str], set[str]]:
    """Problems in one section, plus the keys whose values have the right type."""
    errors: list[str] = []
    typed: set[str] = set()
    rules = _RULES.get(name, {})
    for key, hint in get_type_hints(type(section)).items():
        value = getattr(section, key)
        if not _type_ok(value, hint):
            shown = list(value) if isinstance(value, tuple) else value  # as written in TOML
            errors.append(f"[{name}] {key} must be {_expected(hint)}, got {_describe(value)} ({shown!r})")
            continue
        typed.add(key)
        if key in rules:
            is_valid, message = rules[key]
            if not is_valid(value):
                errors.append(f"[{name}] {key} {message} (got {value!r})")
    return errors, typed


def validate(config: Config, *, for_run: bool = False) -> None:
    """Raise ValueError listing every problem found. `for_run` adds what the bot itself needs."""
    errors: list[str] = []
    typed: dict[str, set[str]] = {}
    for f in fields(Config):
        if f.name == "token":
            continue
        section_errors, typed[f.name] = _section_errors(f.name, getattr(config, f.name))
        errors += section_errors

    # Cross-field rules.
    game = config.game
    if {"max_guesses", "fail_score"} <= typed["game"] and game.fail_score <= game.max_guesses:
        errors.append(
            f"[game] fail_score must be greater than max_guesses ({game.max_guesses}), got {game.fail_score}"
        )

    if for_run:
        for key in ("guild_id", "channel_id"):
            if key in typed["discord"] and getattr(config.discord, key) == 0:
                errors.append(f"[discord] {key} must be set to run the bot (it is 0)")

    if len(errors) == 1:
        raise ValueError(f"Invalid config: {errors[0]}")
    if errors:
        raise ValueError("Invalid config:\n" + "\n".join(f"  - {e}" for e in errors))
