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

## Config

`config.example.toml` is the documented config surface: every key maps 1:1 to a dataclass field in `config.py`, and a test asserts the example equals the defaults. A new setting goes in both, plus an entry in `_RULES` if its value is bounded. Types are checked from the dataclass annotations; cross-field checks live in `validate()`, which reports every problem at once. `load_config(for_run=True)` (the `run` command) also requires the file to exist and nonzero `guild_id`/`channel_id`; `sync`/`pick` run on defaults without a file.

## Layering

- Pure logic, no I/O: `game.py` (rules, puzzle numbering), `scores.py` (share parsing, verification), `selection.py` (strategies, exclusions), `leaderboard.py` (standings, formatting, `escape_name`).
- `service.py` (`WordleService`) owns all application behaviour and is Discord-free, including what to post and when; it is handed a `send` callable. `bot.py` does I/O only: the loop, reactions, slash commands. New features go into the service with a thin bot wrapper.
- `db.py` is the only SQL. Dates are ISO strings in the bot's configured timezone, never UTC (`*_at` timestamps are UTC). Migrations are hand-written in `Database.__init__`; a new column on an existing table needs an `ALTER` there.
- Service methods read the clock once (`now()`/`today()`) and pass `today` down, so a run across midnight stays on one date. Tests inject `clock`.
- Tests use fakes (`FakeSource`) and temp SQLite; nothing in `tests/` touches the network or Discord.

## Posting: the reconciler

Posting is a 60s reconciler loop (`bot._reconcile`), not a timer aimed at `post_time`. Each tick, `service.due_posts()` derives what the channel is owed from the database and `post_due()` sends it, so missed or failed posts are made up on a later tick.

- **The database is the record of what reached the channel.** A pick is saved before it is sent, with `starters.message_id` NULL, and marked announced after. A failed or interrupted send re-announces the same word; worst case it appears twice, never as two different words.
- `daily_posts` records the scheduled post per target date (`results_at`, `completed_at`), so a retry never repeats the results and a restart after post time never repeats the post.
- Kinds of owed post: `SCHEDULED` (results, then the starter, or a REMINDER if it was already announced, e.g. by `/reroll`), `UNANNOUNCED` (saved pick whose send failed), `LATE` (a date already in play with no announced starter).
- **LATE gating:** a missing starter is owed only for dates after `db.first_starter_date()`. A new install has never promised anything, so an empty database never posts LATE. A saved-but-unannounced pick is always owed.
- Results in the daily post, and the `/results` default, are for `results_date()` = `min(target - 1 day, today)`: today's puzzle with the default offset, yesterday's with `target_offset_days = 0`.
- Anything that picks or announces a starter holds `bot._post_lock` (the loop and `/reroll`).
- `_reconcile` catches everything, because `tasks.loop` dies on an unhandled exception.

## Spoiler rules (load-bearing)

- The solution cache stores dates **up to today** (bot timezone) only. The NYT publishes about a week ahead; `sync_solutions` deliberately stops at today.
- Future solutions are only **peeked** (`peek_solution`) to keep a puzzle's own answer from being chosen as its starter, then discarded, never persisted.
- Every user-facing solution goes through `WordleService.answer_visible` and inside `||spoiler||` tags. It reveals a puzzle 2 days after its date (`ANSWER_REVEAL_DAYS`): yesterday's puzzle can still be in play in timezones west of the bot.

## Solution source

`https://www.nytimes.com/svc/wordle/v2/YYYY-MM-DD.json`. The puzzle number players see is `days_since_launch` (= days since 2021-06-19), **not** the JSON `id`. Sync fetches only dates missing from the cache, so a normal day is one request; the first run backfills ~2k days in about a minute.

Today's solution is fetched lazily by whichever needs it first: the reconciler tick after local midnight, a share, `/results` or `/leaderboard`, or the daily post. These call `refresh_if_missing`, which never raises, never fetches a date after today, and after an attempt that didn't produce the solution leaves the source alone for 15 minutes (`REFRESH_RETRY`).

## Selection

Candidate pool: `data/words/answers-5.txt` plus every historical solution ("answer-like"); `random_all_valid` adds `allowed-5.txt`. Exclusion windows (own starters, past solutions) use `0` = off and `-1` = all time, and are relaxed if they would empty the pool; `forbidden` words never are. Forbidden: the target's answer and, on a reroll, every word that date has had (the current one plus `retired_starters`, written by `db.save_starter` when it replaces a word).

`/reroll` (`WordleService.reroll`) is refused when the target puzzle has started (`PuzzleInPlay`; always the case with `target_offset_days = 0`) or any score exists for it (`SubmissionsExist`): shares are verified against the starter on record, so a new word would turn honest openers into mismatches.

## Scores

A share's first grid row must equal `feedback(starter, solution)`. `WordleService._status` is the one place a status is decided.

- Lifecycle: `pending` → `verified` | `mismatch` | `no_starter`. `pending` means the solution isn't cached yet **or the starter isn't picked yet but still can be**. `no_starter` is final: no starter exists and none can come.
- Early shares are normal: members in timezones ahead of the bot play tomorrow's puzzle before the evening post. Puzzle numbers 0..today+1 are accepted; anything else is ignored without a reaction.
- Pending scores are settled by `reverify_pending`, which runs on every refresh and after every `post_due`. Settled scores queue in the service; the bot drains them with `take_resolved()` and swaps the ⏳ reaction for the outcome, using the `channel_id`/`message_id` stored on the score (NULL on rows older than those columns, which are skipped). Call `bot._update_reactions()` after any new code path that can settle scores.
- Only `verified` counts on leaderboards; `count_unverified` adds `pending`, never `mismatch` or `no_starter`. One submission per user per puzzle; first wins. X/6 scores as `fail_score`.
- Parser tolerances (quotes, spoiler tags, trailing text, locale thousands separators, several headers in one message) are pinned by `tests/test_scores.py`.

## Standings

Competition ranking (1, 2, 2, 4), decided on lower average, then more games; a tie is the same average over the same number of games. Each board has a minimum (`min_games_*`); the weekly and monthly ones are capped at the puzzles in the period so far. Players below it are listed unranked under the board. A player's name is the one on their most recently submitted score.

## Configurability scaffolding

`word_length` and `max_guesses` flow through parsing, word-list file names (`answers-{n}.txt`), and feedback, but `_RULES` pins `word_length = 5` because the NYT source is 5-letter only. A different length needs a new `SolutionSource` implementation and word lists.

## Discord gotchas

- The **Message Content** privileged intent must be enabled in the Developer Portal, or score intake silently sees empty messages. Swapping reactions on older messages needs Add Reactions and Read Message History.
- Slash commands sync to `discord.guild_id` only (instant updates; no global commands).
- Every slash command is wrapped in `@self._deferred` as its innermost decorator: it defers ephemerally first (Discord drops an interaction not acknowledged within 3 seconds) and reports failures. Command bodies answer through `interaction.followup` or `_dm` only.
- Slash command output goes by DM (`_dm`), falling back to ephemeral followups when DMs are closed.
- The client is built with `AllowedMentions.none()`; a message meant to ping must pass `allowed_mentions` at its call site. Display names are stored raw, so any output that echoes one passes it through `leaderboard.escape_name`.

## Deployment

`Dockerfile` + `compose.yaml` + `update.sh` (run by a systemd timer, see README).

- Env overrides: `WORDLEBOT_CONFIG` (config path) and `WORDLEBOT_DATABASE_PATH` (beats `[storage].database_path`); the image sets both so state lands in the `/app/state` volume.
- The image installs runtime deps from `requirements.txt` (exact pins including transitives, resolved inside `python:3.13-slim`), then `pip install --no-deps .` and `pip check`, so timer rebuilds never pull new releases. A dependency change goes in `pyproject.toml` (loose range) **and** `requirements.txt`; the regeneration command is in that file's header.
- `tzdata` is a dependency because slim images ship no zoneinfo database.
- Stop signal is SIGINT (`STOPSIGNAL` in the image, repeated in compose): Python as PID 1 ignores SIGTERM. `main()` catches the `KeyboardInterrupt`, logs "Shutting down" and exits 0.
- The `config.toml` bind mount uses `create_host_path: false`, so a missing file is an error instead of a root-owned directory.
- `update.sh` deploys when HEAD differs from `.deployed-sha`, written only once the deploy is confirmed (container replaced and "Logged in as" seen in its logs, or container left unchanged by an image-neutral commit). `.deploy-pending` marks an unconfirmed deploy; the next run retries with `--force-recreate`. Both files are git-ignored. The login check greps for the `Logged in as` log line in `bot.on_ready`; keep the two in step.
