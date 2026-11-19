# Changelog

Notable changes to wordlebot. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

What counts as a breaking change: anything that makes an operator or player change what they do. That means renamed or removed `config.toml` keys or `WORDLEBOT_*` variables, a database that can't be migrated forward automatically, a changed CLI command, or a removed slash command or behaviour players rely on. Before 1.0, a breaking change bumps the minor version.

## [Unreleased]

### Added

- Golf: the daily starter post now ends with a line @mentioning the players in the current round who haven't shared a result for today's hole yet (they are pinged; nobody else is). A player joins a round's list with their first counted hole in it, so hole 1 has no reminder. Turn it off with `[golf] remind_unplayed = false`. Not sent with `target_offset_days = 0`.
- Admin panel: a Golf tab (shown when `[golf] enabled`, next to Scores) with the full leaderboard of the current round, through yesterday's puzzle, then every earlier round, newest first. Each round has an 18-hole scorecard, marked as golf cards are: circles for birdies and eagles, squares for bogeys, and a gold badge for a hole in one.

## [0.3.0] - 2026-11-17

### Added

- Golf: each puzzle is a par-4 hole and 18 in a row are a par-72 round, counted from the bot's first starter. The morning after each front nine and each full round (`[golf] post_time`, default 08:00), the bot posts the round's leaderboard and hole-by-hole card, and the final names the winner. A missed or failed hole scores `fail_score`. `/golf` shows the round in progress. Settings: `enabled`, `post_time`, `missed_posts` (`latest` or `all`), `card_max_players` (default 10; 0 shows everyone across several messages) and `on_score_edit` (`none`, `edit` the posted messages, or post a `correct`ion after a panel score edit).

## [0.2.0] - 2026-11-16

The first tagged release. It covers everything since the project began; `0.1.0` was never tagged.

### Added

- A daily shared starter, posted to one channel at a configured time and picked by configurable strategies with exclusion windows for recent starters and past solutions.
- A solution cache synced from the NYT. It stores answers only up to today, so future answers are never saved.
- Pasted share results are checked against the starter and ranked. Results stay pending until they can be checked, and reactions show the outcome.
- Leaderboards (all-time, weekly, monthly) with competition ranking and minimum-game thresholds (`min_games_weekly`, `min_games_monthly`).
- Slash commands that answer by DM, including `/reroll`, `/results`, `/leaderboard`, `/history` and `/version`.
- An optional admin panel (`[web] enabled`) with password and mandatory TOTP login. It covers score moderation with previews and required reasons, config overrides, an audit log and admin actions. Optional channel announcements (`[announce]`) are off by default.
- `python -m wordlebot admin list|add|passwd|reset-2fa|remove` to manage panel accounts from the host.
- Docker deployment: `Dockerfile`, `compose.yaml`, pinned `requirements.txt`, Caddy for the panel's HTTPS (compose profile `web`), and `update.sh` with a systemd timer for automatic redeploys.

### Changed

- Posting is a 60-second reconciler, so a missed or failed post is made up later, never as a different word.
- Config is validated at startup, with every problem reported together. `run` requires the config file and nonzero `guild_id`/`channel_id`.

### Fixed

- The panel's login form was refused as cross-origin in real browsers, because of `Referrer-Policy: no-referrer`.
- The panel showed hidden form fields and Discord markdown on the dashboard.
- A starter rerolled before the upgrade to `daily_posts` lost its 20:00 reminder.

### Upgrading

See [Upgrading an existing install](README.md#upgrading-an-existing-install). The database migrates itself, and new settings have defaults.

[Unreleased]: https://github.com/darthsco/wordlebot/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/darthsco/wordlebot/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/darthsco/wordlebot/releases/tag/v0.2.0
