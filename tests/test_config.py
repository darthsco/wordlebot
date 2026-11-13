import sys
from dataclasses import replace

import pytest

from wordlebot.config import (
    Config,
    DiscordConfig,
    GameConfig,
    ScheduleConfig,
    ScoresConfig,
    SelectionConfig,
    SolutionsConfig,
    load_config,
    validate,
)

RUNNABLE = "[discord]\nguild_id = 1\nchannel_id = 2\n"


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_defaults_are_valid():
    validate(Config())


def test_example_config_loads():
    config = load_config("config.example.toml")
    assert replace(config, token="", storage=Config().storage) == Config()


def test_missing_file_gives_defaults_for_offline_commands(tmp_path):
    config = load_config(tmp_path / "nope.toml")
    assert config.discord.guild_id == 0
    assert config.solutions.fetch_concurrency == 8


def test_missing_file_fails_in_run_mode(tmp_path):
    with pytest.raises(ValueError, match="Config file not found: .*nope.toml"):
        load_config(tmp_path / "nope.toml", for_run=True)


def test_run_mode_requires_guild_and_channel(tmp_path):
    with pytest.raises(ValueError) as exc:
        load_config(write(tmp_path, "[discord]\nguild_id = 1\n"), for_run=True)
    assert "[discord] channel_id must be set" in str(exc.value)
    assert "guild_id" not in str(exc.value)

    with pytest.raises(ValueError, match=r"\[discord\] guild_id must be set"):
        load_config(write(tmp_path, "[schedule]\npost_time = \"09:30\"\n"), for_run=True)


def test_run_mode_accepts_a_complete_file(tmp_path):
    config = load_config(write(tmp_path, RUNNABLE + "admin_role_ids = [3, 4]\n"), for_run=True)
    assert config.discord == DiscordConfig(guild_id=1, channel_id=2, admin_role_ids=(3, 4))


def test_unknown_keys_and_sections_fail(tmp_path):
    with pytest.raises(ValueError, match=r"Unknown config keys in \[scores\]: \['bogus'\]"):
        load_config(write(tmp_path, "[scores]\nbogus = 1\n"))
    with pytest.raises(ValueError, match="Unknown config sections"):
        load_config(write(tmp_path, "[nonsense]\na = 1\n"))
    with pytest.raises(ValueError, match=r"\[scores\] must be a table"):
        load_config(write(tmp_path, "scores = 5\n"))


@pytest.mark.parametrize(
    "config, message",
    [
        (Config(solutions=SolutionsConfig(fetch_concurrency=0)), "[solutions] fetch_concurrency must be >= 1"),
        (Config(solutions=SolutionsConfig(launch_date="19/06/2021")), "[solutions] launch_date must be a YYYY-MM-DD"),
        (Config(solutions=SolutionsConfig(base_url=" ")), "[solutions] base_url must not be empty"),
        (Config(selection=SelectionConfig(exclude_own_starters_days=-5)), "[selection] exclude_own_starters_days must be >= -1"),
        (Config(selection=SelectionConfig(exclude_solutions_days=-2)), "[selection] exclude_solutions_days must be >= -1"),
        (Config(selection=SelectionConfig(strategy="best")), "[selection] strategy must be one of"),
        (Config(selection=SelectionConfig(quality_weight_exponent=-0.1)), "[selection] quality_weight_exponent must be >= 0"),
        (Config(selection=SelectionConfig(varied_bad_chance=1.5)), "[selection] varied_bad_chance must be between 0 and 1"),
        (Config(selection=SelectionConfig(varied_pool_fraction=0)), "[selection] varied_pool_fraction must be greater than 0"),
        (Config(scores=ScoresConfig(weekly_window_days=0)), "[scores] weekly_window_days must be >= 1"),
        (Config(scores=ScoresConfig(min_games_all_time=0)), "[scores] min_games_all_time must be >= 1"),
        (Config(scores=ScoresConfig(min_games_weekly=0)), "[scores] min_games_weekly must be >= 1"),
        (Config(scores=ScoresConfig(min_games_monthly=0)), "[scores] min_games_monthly must be >= 1"),
        (Config(schedule=ScheduleConfig(target_offset_days=-1)), "[schedule] target_offset_days must be >= 0"),
        (Config(schedule=ScheduleConfig(post_time="24:00")), "[schedule] post_time must be HH:MM"),
        (Config(schedule=ScheduleConfig(post_time="20:60")), "[schedule] post_time must be HH:MM"),
        (Config(schedule=ScheduleConfig(post_time="8pm")), "[schedule] post_time must be HH:MM"),
        (Config(schedule=ScheduleConfig(post_time="20:00:00")), "[schedule] post_time must be HH:MM"),
        (Config(schedule=ScheduleConfig(timezone="Mars/Olympus")), "[schedule] timezone must be an IANA timezone"),
        (Config(game=GameConfig(word_length=6)), "[game] word_length must be 5"),
        (Config(game=GameConfig(max_guesses=0)), "[game] max_guesses must be >= 1"),
        (Config(game=GameConfig(fail_score=6)), "[game] fail_score must be greater than max_guesses (6), got 6"),
        (Config(discord=DiscordConfig(guild_id=-1)), "[discord] guild_id must be >= 0"),
    ],
)
def test_out_of_range_values_are_rejected(config, message):
    with pytest.raises(ValueError) as exc:
        validate(config)
    assert message in str(exc.value)


@pytest.mark.parametrize(
    "config",
    [
        Config(selection=SelectionConfig(exclude_own_starters_days=-1, exclude_solutions_days=0)),
        Config(selection=SelectionConfig(varied_bad_chance=0, varied_pool_fraction=1, quality_weight_exponent=0)),
        Config(schedule=ScheduleConfig(target_offset_days=0, post_time="8:05")),
        Config(schedule=ScheduleConfig(post_time="23:59")),
        Config(game=GameConfig(max_guesses=8, fail_score=9)),
        Config(solutions=SolutionsConfig(fetch_concurrency=1)),
        Config(scores=ScoresConfig(min_games_weekly=1, min_games_monthly=1, min_games_all_time=1)),
    ],
)
def test_boundary_values_are_accepted(config):
    validate(config)


@pytest.mark.parametrize(
    "toml, message",
    [
        ('[scores]\nweekly_window_days = "7"\n', "[scores] weekly_window_days must be an integer, got a string ('7')"),
        ("[scores]\nweekly_window_days = 7.5\n", "[scores] weekly_window_days must be an integer, got a number"),
        ("[scores]\nweekly_window_days = true\n", "[scores] weekly_window_days must be an integer, got a boolean"),
        ("[scores]\ncount_unverified = 1\n", "[scores] count_unverified must be a boolean, got an integer"),
        ("[selection]\nvaried_bad_chance = true\n", "[selection] varied_bad_chance must be a number, got a boolean"),
        ('[selection]\nvaried_bad_chance = "0.3"\n', "[selection] varied_bad_chance must be a number, got a string"),
        ("[selection]\nstrategy = 3\n", "[selection] strategy must be a string, got an integer"),
        ("[schedule]\npost_time = 20:00:00\n", "[schedule] post_time must be a string"),
        ("[solutions]\nlaunch_date = 2021-06-19\n", "[solutions] launch_date must be a string"),
        ("[discord]\nadmin_role_ids = 5\n", "[discord] admin_role_ids must be an array of integers, got an integer"),
        ('[discord]\nadmin_role_ids = [1, "2"]\n', "[discord] admin_role_ids must be an array of integers, got an array"),
        ("[discord]\nguild_id = [1]\n", "[discord] guild_id must be an integer, got an array"),
    ],
)
def test_wrong_types_are_rejected(tmp_path, toml, message):
    with pytest.raises(ValueError) as exc:
        load_config(write(tmp_path, toml))
    assert message in str(exc.value)


def test_integers_are_accepted_for_floats(tmp_path):
    config = load_config(write(tmp_path, "[selection]\nquality_weight_exponent = 3\nvaried_bad_chance = 1\n"))
    assert config.selection.quality_weight_exponent == 3


def test_all_problems_are_reported_together(tmp_path):
    text = '[solutions]\nfetch_concurrency = 0\n[scores]\nweekly_window_days = "x"\n[game]\nfail_score = 2\n'
    with pytest.raises(ValueError) as exc:
        load_config(write(tmp_path, text), for_run=True)
    message = str(exc.value)
    for expected in (
        "[solutions] fetch_concurrency must be >= 1",
        "[scores] weekly_window_days must be an integer",
        "[game] fail_score must be greater than max_guesses",
        "[discord] guild_id must be set",
        "[discord] channel_id must be set",
    ):
        assert expected in message


def test_database_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("WORDLEBOT_DATABASE_PATH", "/state/db.sqlite3")
    assert load_config(tmp_path / "nope.toml").storage.database_path == "/state/db.sqlite3"


def test_run_command_exits_clearly_without_a_config_file(tmp_path, monkeypatch):
    from wordlebot import __main__ as entry

    missing = tmp_path / "nope.toml"
    monkeypatch.setattr(entry, "load_dotenv", lambda: None)
    monkeypatch.setattr(sys, "argv", ["wordlebot", "--config", str(missing)])
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert "Config file not found" in str(exc.value)


def test_run_command_reports_invalid_values(tmp_path, monkeypatch):
    from wordlebot import __main__ as entry

    path = write(tmp_path, RUNNABLE + "[solutions]\nfetch_concurrency = 0\n")
    monkeypatch.setattr(entry, "load_dotenv", lambda: None)
    monkeypatch.setattr(sys, "argv", ["wordlebot", "--config", str(path), "run"])
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert "fetch_concurrency must be >= 1" in str(exc.value)


def test_minimum_games_defaults():
    scores = Config().scores
    assert (scores.min_games_weekly, scores.min_games_monthly, scores.min_games_all_time) == (3, 7, 5)
