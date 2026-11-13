# wordlebot

Discord bot that posts a shared Wordle **starter** (tomorrow's opening word) to one channel at a configured time, then collects members' pasted share results and ranks them. The competition only works if everyone opens with the same word, so scores are **verified** against the starter.

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/python -m wordlebot sync   # refresh solution cache, exit
.venv/bin/python -m wordlebot pick   # dry-run: print the starter that would be chosen
.venv/bin/python -m wordlebot        # run the bot (needs DISCORD_TOKEN in .env, config.toml)
```

`config.example.toml` is the documented config surface; every key maps 1:1 to a dataclass in `config.py`, and unknown keys fail loudly. Add new settings in both places.

## Layering

- Pure logic, no I/O: `game.py` (rules, puzzle numbering), `scores.py` (share parsing, verification), `selection.py` (strategies, exclusions), `leaderboard.py`.
- `service.py` (`WordleService`) owns all application behaviour and is Discord-free. `bot.py` does I/O only: scheduling, posting, reactions, slash commands. New features go into the service with a thin bot wrapper.
- `db.py` is the only SQL. Dates are ISO strings in the bot's configured timezone, never UTC.
- Tests use fakes (`FakeSource`) and temp SQLite; nothing in `tests/` touches the network or Discord.

## Spoiler rules (load-bearing)

- The solution cache stores dates **up to today** (bot timezone) only. The NYT publishes about a week ahead; `sync_solutions` deliberately stops at today.
- Future solutions are only **peeked** (`peek_solution`) to keep a puzzle's own answer from being chosen as its starter, then discarded, never persisted.
- User-facing output shows a solution only for puzzles before today, and in `||spoiler||` tags.

## Solution source

`https://www.nytimes.com/svc/wordle/v2/YYYY-MM-DD.json`. The puzzle number players see is `days_since_launch` (= days since 2021-06-19), **not** the JSON `id`. Sync fetches only dates missing from the cache, so a normal day is one request; the first run backfills ~2k days in about a minute.

## Selection

Candidate pool: `data/words/answers-5.txt` plus every historical solution ("answer-like"); `random_all_valid` adds `allowed-5.txt`. Exclusion windows (own starters, past solutions) use `0` = off and `-1` = all time, and are relaxed if they would empty the pool; `forbidden` words (the target's answer, the word being rerolled) never are.

## Scores

A share's first grid row must equal `feedback(starter, solution)`. Status lifecycle: `pending` (solution not cached yet; re-checked on every `refresh()`) → `verified` | `mismatch`; `no_starter` if we posted nothing for that puzzle. Only `verified` counts on leaderboards unless `count_unverified`. One submission per user per puzzle; first wins. X/6 scores as `fail_score`.

## Configurability scaffolding

`word_length` and `max_guesses` flow through parsing, word-list file names (`answers-{n}.txt`), and feedback, but `validate()` pins `word_length = 5` because the NYT source is 5-letter only. A different length needs a new `SolutionSource` implementation and word lists.

## Discord setup gotchas

- The **Message Content** privileged intent must be enabled in the Developer Portal, or score intake silently sees empty messages.
- Slash commands sync to `discord.guild_id` only (instant updates; no global commands).
- Slash command output goes by DM, falling back to an ephemeral reply when DMs are closed. The daily channel post carries the previous puzzle's results plus the new starter.
- If the starter for the target date already exists at `post_time` (e.g. from `/reroll`), the daily post sends a REMINDER of it instead of picking a new one. If the bot starts after `post_time` with no starter saved, `on_ready` posts it immediately. `tasks.loop` dies on an unhandled exception, so `_daily_post` catches everything.

## Deployment

`Dockerfile` + `compose.yaml`. Env overrides: `WORDLEBOT_CONFIG` (config path) and `WORDLEBOT_DATABASE_PATH` (beats `[storage].database_path`); the image sets both so state lands in the `/app/state` volume. `tzdata` is a dependency because slim images ship no zoneinfo database.
