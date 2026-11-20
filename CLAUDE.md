# wordlebot

Discord bot that posts a shared Wordle **starter** (tomorrow's opening word) to one channel at a configured time, then collects members' pasted share results and ranks them. The competition only works if everyone opens with the same word, so scores are **verified** against the starter.

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/python -m wordlebot sync   # refresh solution cache, exit
.venv/bin/python -m wordlebot pick   # dry-run: print the starter that would be chosen
.venv/bin/python -m wordlebot        # run the bot (needs DISCORD_TOKEN in .env, config.toml)
.venv/bin/python -m wordlebot admin add NAME   # panel accounts: list | add | enroll | reset-passkeys | remove
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
- Kinds of owed post: `SCHEDULED` (results, then the starter, or a REMINDER if it was already announced, e.g. by `/wordlebot reroll`), `UNANNOUNCED` (saved pick whose send failed), `LATE` (a date already in play with no announced starter).
- **LATE gating:** a missing starter is owed only for dates after `db.first_starter_date()`. A new install has never promised anything, so an empty database never posts LATE. A saved-but-unannounced pick is always owed.
- Results in the daily post, and the `/wordlebot results` default, are for `results_date()` = `min(target - 1 day, today)`: today's puzzle with the default offset, yesterday's with `target_offset_days = 0`.
- Anything that picks or announces a starter holds `bot._post_lock` (the loop and `reroll`).
- `_reconcile` catches everything, because `tasks.loop` dies on an unhandled exception.

## Spoiler rules (load-bearing)

- The solution cache stores dates **up to today** (bot timezone) only. The NYT publishes about a week ahead; `sync_solutions` deliberately stops at today.
- Future solutions are only **peeked** (`peek_solution`) to keep a puzzle's own answer from being chosen as its starter, then discarded, never persisted.
- Every user-facing solution goes through `WordleService.answer_visible` and inside `||spoiler||` tags. It reveals a puzzle 2 days after its date (`ANSWER_REVEAL_DAYS`): yesterday's puzzle can still be in play in timezones west of the bot.

## Solution source

`https://www.nytimes.com/svc/wordle/v2/YYYY-MM-DD.json`. The puzzle number players see is `days_since_launch` (= days since 2021-06-19), **not** the JSON `id`. Sync fetches only dates missing from the cache, so a normal day is one request; the first run backfills ~2k days in about a minute.

Today's solution is fetched lazily by whichever needs it first: the reconciler tick after local midnight, a share, the `results` or `leaderboard` subcommand, or the daily post. These call `refresh_if_missing`, which never raises, never fetches a date after today, and after an attempt that didn't produce the solution leaves the source alone for 15 minutes (`REFRESH_RETRY`).

## Selection

Candidate pool: `data/words/answers-5.txt` plus every historical solution ("answer-like"); `random_all_valid` adds `allowed-5.txt`. Exclusion windows (own starters, past solutions) use `0` = off and `-1` = all time, and are relaxed if they would empty the pool; `forbidden` words never are. Forbidden: the target's answer and, on a reroll, every word that date has had (the current one plus `retired_starters`, written by `db.save_starter` when it replaces a word).

`/wordlebot reroll` (`WordleService.reroll`) is refused when the target puzzle has started (`PuzzleInPlay`; always the case with `target_offset_days = 0`) or any score exists for it (`SubmissionsExist`): shares are verified against the starter on record, so a new word would turn honest openers into mismatches.

## Scores

A share's first grid row must equal `feedback(starter, solution)`. `WordleService._status` is the one place a status is decided.

- Lifecycle: `pending` → `verified` | `mismatch` | `no_starter`. `pending` means the solution isn't cached yet **or the starter isn't picked yet but still can be**. `no_starter` is final: no starter exists and none can come.
- Early shares are normal: members in timezones ahead of the bot play tomorrow's puzzle before the evening post. Puzzle numbers 0..today+1 are accepted; anything else is ignored without a reaction.
- Pending scores are settled by `reverify_pending`, which runs on every refresh and after every `post_due`. Settled scores queue in the service as `ReactionUpdate`s (so do panel edits); the bot drains them with `take_reaction_updates()` and swaps the old reaction (⏳ when settling) for the outcome, using the `channel_id`/`message_id` stored on the score (NULL on rows older than those columns, which are skipped). Call `bot._update_reactions()` after any new code path that can settle scores.
- Only `verified` counts on leaderboards; `count_unverified` adds `pending`, never `mismatch` or `no_starter`. One submission per user per puzzle; first wins. X/6 scores as `fail_score`.
- Parser tolerances (quotes, spoiler tags, trailing text, locale thousands separators, several headers in one message) are pinned by `tests/test_scores.py`.

## Standings

Competition ranking (1, 2, 2, 4), decided on lower average, then more games; a tie is the same average over the same number of games. Each board has a minimum (`min_games_*`); the weekly and monthly ones are capped at the puzzles in the period so far. Players below it are listed unranked under the board. A player's name is the one on their most recently submitted score.

## Golf

`golf.py` (pure) plus `WordleService.due_golf`/`_post_golf`/`golf_standings`. Each puzzle is a par-4 hole. Rounds of 18 run back to back from `db.first_starter_puzzle()` (the anchor). A hole's strokes are its guesses; X and a missing (or non-counting) score are both `fail_score`. The players on a card are everyone with a counted score in the round so far, ranked by score to par (ties share a rank; tied leaders of a final are co-champions). A **live** card (`/wordlebot golf`, the panel's current round) runs through today's hole: a player yet to share it shows `golf.YET` (`·`) there, which adds nothing to total or par, so mid-hole players are compared fairly. Milestone posts are never live: by then a missing hole is `MISSED` and scores `fail_score`.

- Milestones are each front nine and each full round, due once past `[golf] post_time` on the day after their last hole. `golf_posts` records each one sent. `missed_posts = "latest"` (default) owes only the newest, so a missed one is superseded. `"all"` owes every one, oldest first. Neither goes back before the latest posted.
- The `SCHEDULED` post (starter or reminder) ends with `golf_nudge`: today's hole and `<@id>` mentions for `golf_unplayed`, the players with a counted score earlier in the round and no share (any status) for today's puzzle. Those ids go to `send(..., ping=ids)`, which is the only way a post pings anyone (`bot._send` turns it into `allowed_mentions`). Off with `remind_unplayed = false` and when `target_offset_days = 0`.
- No backfill: golf shipped while the only install had played 3 holes, so existing history is the start of round 1.
- `golf.format_round` keeps every message under `MESSAGE_LIMIT` (a long board or card continues in a new message with its header rows repeated), so `bot.chunks` never splits a code block. `card_max_players` caps the cards (0 = everyone); the board always lists everyone.
- `golf_post_messages` stores each post's (message id, text). After a panel score edit, `apply_edit` calls `_golf_score_edited`, which regenerates every post covering that puzzle and, if it differs: `on_score_edit = "edit"` rewrites the messages (`bot._edit` via `panel_edit`), and `"correct"` posts a `GOLF_CORRECTED_PREFIX` copy. `"edit"` falls back to a correction when the message count changed. A changed `card_max_players` also counts as a difference.
- The panel's Golf tab (`/golf`, 404 and hidden from the nav while `[golf] enabled` is off) renders `golf_rounds`: the current round live, as `/wordlebot golf` sees it, then every earlier round complete, newest first.
- Card names go through `golf._card_name` (no backticks, truncated); names outside the code block go through `escape_name`.

## Admin panel

Off unless `[web] enabled`. `__main__._run` starts it (`web.start`) next to the bot, in the same event loop.

- **HTTPS is the proxy's job.** The app serves plain HTTP and refuses any request without `X-Forwarded-Proto: https`. It trusts `X-Forwarded-For` (last entry) as the client address for rate limiting and the audit log. Both are safe only because the port is reachable by Caddy alone (unpublished in compose; Caddy replaces client-sent `X-Forwarded-*`). Never publish the port.
- **Login** (`auth.Auth`) is passkeys only (`webauthn` library), with no username or password. Every ceremony sets `userVerification: required` and the server checks the UV flag, which is what makes a passkey two factors; never relax it. Passkeys are tied to `[web] domain` (RP ID, origin `https://<domain>`). Challenges live in memory (`Auth._pending`), single-use, 5 minutes, keyed by the challenge the browser echoes in its client data, and bound to a purpose (`login`, `reauth`, `register`) and admin. A sign-in creates a full session under a new token; `sessions.stage` is always `full` now. Only `sha256(token)` is stored, for sessions and enrollment links alike. Refused passkeys are counted per `ip:` and (once the credential names an admin) `user:` key in `login_failures`; either reaching `login_max_failures` within `login_lockout_minutes` locks out. A sign count that doesn't increase is refused unless both are 0 (synced passkeys).
- **Enrollment links**: accounts are CLI-only but registering a passkey needs a browser, so `admin add|enroll|reset-passkeys` print `https://<domain>/enroll/<token>` (one per admin, 24 hours, spent on use; signs the admin in). Adding a passkey from the Account page needs a fresh assertion from an existing one first (`reauth`), except for an admin with none (a session from before passkeys). The last passkey can't be removed. The ceremonies run in `web/static/passkey.js` (CSP forbids inline script); its JSON posts carry the CSRF token in `X-CSRF-Token`.
- **Requests**: `__Host-` cookie (Secure, HttpOnly, SameSite=Strict), a CSRF token per session on every signed-in POST, an `Origin` check on every POST (including `/login`; a missing or `null` Origin passes only with `Sec-Fetch-Site: same-origin`), `Referrer-Policy: same-origin` (`no-referrer` makes browsers send `Origin: null` on plain form posts, which locked everyone out of the login form), and strict CSP (no inline script or style; htmx is vendored in `web/static` with its inline indicator styles off). The `/status` poll doesn't refresh the idle timer.
- **Accounts are CLI-only** (`python -m wordlebot admin ...`). The panel can't create admins.
- **Score edits** are `plan_*` → `preview` → `apply_edit`. The confirm POST plans again from the same form fields, and `apply_edit` refuses if the stored row differs from the plan's `before`. Every edit needs a reason and is audited. Deletes move the row to `deleted_scores`, and a restore re-decides the status with `_status`. A status change queues a `ReactionUpdate` (previous → current emoji); call `bot._update_reactions()` after one, as for settled scores.
- **Config overrides** live in `config_overrides` (JSON values) and are layered over the file by `config.with_overrides`, which validates the result. `service.file_config` is the file and `service.config` the effective config, which `bot.config` reads through. Only keys in `config.EDITABLE` are editable; anything else is read-only in the panel, and a stored override for a key no longer editable is ignored. If the stored overrides stop validating (the file changed), `load_overrides` runs on the file alone and the panel shows `overrides_error`.
- **Audit**: everything the panel changes, every sign-in event and every CLI account change goes through `db.audit`. Announcements (`[announce]`, per action, off by default) never include the reason, and escape the admin's name.
- **Spoilers**: the panel follows the same rule as Discord. Solutions are shown only via `answer_for_panel`/`overview().visible_answers`, which use `answer_visible`.

## Configurability scaffolding

`word_length` and `max_guesses` flow through parsing, word-list file names (`answers-{n}.txt`), and feedback, but `_RULES` pins `word_length = 5` because the NYT source is 5-letter only. A different length needs a new `SolutionSource` implementation and word lists.

## Discord gotchas

- The **Message Content** privileged intent must be enabled in the Developer Portal, or score intake silently sees empty messages. Swapping reactions on older messages needs Add Reactions and Read Message History.
- Slash commands sync to `discord.guild_id` only (instant updates; no global commands).
- The bot has one top-level slash command, `/wordlebot` (`bot.COMMAND`, an `app_commands.Group`); every operation is a subcommand of it, so a new command is `@group.command` in `_register_commands`, never `@tree.command`. Guild sync replaces the whole set, so a removed or renamed command needs no cleanup.
- Every slash command is wrapped in `@self._deferred` as its innermost decorator: it defers ephemerally first (Discord drops an interaction not acknowledged within 3 seconds) and reports failures. Command bodies answer through `interaction.followup` or `_dm` only.
- Slash command output goes by DM (`_dm`), falling back to ephemeral followups when DMs are closed.
- The client is built with `AllowedMentions.none()`; a message meant to ping must pass `allowed_mentions` at its call site. Display names are stored raw, so any output that echoes one passes it through `leaderboard.escape_name`.

## Deployment

`Dockerfile` + `compose.yaml` + `update.sh` (run by a systemd timer, see README).

- Env overrides: `WORDLEBOT_CONFIG` (config path), `WORDLEBOT_DATABASE_PATH` (beats `[storage].database_path`), `WORDLEBOT_WEB_HOST` (beats `[web].host`) and `WORDLEBOT_WEB_DOMAIN` (beats `[web].domain`). The image sets the first three, so state lands in the `/app/state` volume and the panel listens on the container's interfaces; compose sets the domain from `PANEL_DOMAIN`, so Caddy's certificate and the passkeys' domain can't disagree.
- The `caddy` service (profile `web`, enabled by `COMPOSE_PROFILES=web` in `.env`) terminates TLS for `PANEL_DOMAIN` using `Caddyfile`, and proxies to `wordlebot:8080`.
- The image installs runtime deps from `requirements.txt` (exact pins including transitives, resolved inside `python:3.13-slim`), then `pip install --no-deps .` and `pip check`, so timer rebuilds never pull new releases. A dependency change goes in `pyproject.toml` (loose range) **and** `requirements.txt`; the regeneration command is in that file's header.
- `/wordlebot version` reports `WORDLEBOT_COMMIT`, baked in by `update.sh` → compose build arg `GIT_COMMIT` → `ENV` in the image's last layer (the image has no `.git`). Since the SHA is in the image, every commit changes it, so `update.sh` recreates the container even for docs-only commits.
- `tzdata` is a dependency because slim images ship no zoneinfo database.
- Stop signal is SIGINT (`STOPSIGNAL` in the image, repeated in compose): Python as PID 1 ignores SIGTERM. `main()` catches the `KeyboardInterrupt`, logs "Shutting down" and exits 0.
- The `config.toml` bind mount uses `create_host_path: false`, so a missing file is an error instead of a root-owned directory.
- `update.sh` deploys when HEAD differs from `.deployed-sha`, written only once the deploy is confirmed (container replaced and "Logged in as" seen in its logs, or container left unchanged by an image-neutral commit). `.deploy-pending` marks an unconfirmed deploy; the next run retries with `--force-recreate`. Both files are git-ignored. The login check greps for the `Logged in as` log line in `bot.on_ready`; keep the two in step.

## Releasing

SemVer; `CHANGELOG.md` (Keep a Changelog) defines what counts as breaking: config keys, `WORDLEBOT_*` variables, a DB that can't migrate forward, CLI commands, slash commands or behaviour players rely on. Before 1.0, breaking bumps minor; fixes bump patch.

- Every user- or operator-visible change adds a line under `## [Unreleased]` in the same commit (Added / Changed / Fixed / Removed). Internal refactors and tests don't.
- To release: rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, add a fresh empty `[Unreleased]`, update the compare links at the bottom, bump `version` in `pyproject.toml`, commit "Release X.Y.Z", then `git tag -a vX.Y.Z -m "wordlebot X.Y.Z"`. Push the commit and tag only when asked.
- After pushing a tag, create its GitHub release: `gh release create vX.Y.Z --verify-tag --title "wordlebot X.Y.Z" --notes-file NOTES --latest`, where NOTES is that version's `CHANGELOG.md` section (the lines after its heading, without the link references), with relative links like `(README.md#...)` rewritten to `https://github.com/darthsco/wordlebot/blob/vX.Y.Z/...` because they don't resolve on the release page.
- The version lives only in `pyproject.toml`; `/wordlebot version` reads it from package metadata (no setuptools-scm: the image has no `.git`).
- Tags are labels, not deploy gates: `update.sh` deploys every commit on `main`.
