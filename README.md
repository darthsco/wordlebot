# wordlebot

A Discord bot for friendly Wordle competition. Every evening it posts **tomorrow's starting word** to a channel. Everyone opens with that word, pastes their result, and the bot keeps score. With the first guess shared, the number of guesses is a fair comparison.

## Features

- **Daily starter post** at a time and timezone you choose (default 20:00 America/New_York). It includes the results for the puzzle before the one the starter is for: today's with the default settings, yesterday's if you set `target_offset_days = 0`.
- **Catches up by itself:** if the bot is offline at post time, or a post fails, it posts within a minute of coming back. If the outage runs past midnight, the missed starter is posted marked ⚠️ **LATE**. A new install never posts LATE; it starts with its first scheduled post.
- **Starter selection** with four strategies:
  - `random_answer_like` (default): any word that is a plausible answer.
  - `random_all_valid`: any valid guess, obscure words included.
  - `weighted_quality`: biased toward strong openers.
  - `varied`: some days great openers, some days terrible ones.
- **No repeats:** skips words we suggested recently and words that were recent solutions. Both lookback windows are configurable.
- **Full solution history** from the NYT, cached locally in SQLite. After the first run it fetches only the days that are missing, so a normal day is one request. Future answers are never stored or shown, and past answers are shown only once the puzzle is over in every timezone (2 days later).
- **Score tracking:** paste your Wordle share text in the channel. The bot checks that your first row matches the shared starter and reacts:

  | Reaction | Meaning |
  |---|---|
  | ✅ | Verified: your first row matches the shared starter |
  | ⏳ | Pending: the bot can't check it yet. Either the answer couldn't be fetched, or it's tomorrow's puzzle (you're in a timezone ahead of the bot) |
  | ❌ | Mismatch: your first row doesn't match the starter, so the score isn't counted |
  | ➖ | No starter was posted for that puzzle |
  | 🔁 | You already submitted for this puzzle; the first submission stands |

  Shares are normally checked on the spot. A ⏳ is swapped for the outcome automatically once the bot can check it. Shares for puzzles that can't have been played yet get no reaction.

- **Leaderboards** for daily, rolling weekly, monthly and all-time. A failed puzzle (X/6) counts as 7. Each board has a configurable minimum number of games to be ranked (defaults: 3 weekly, 7 monthly, 5 all-time; early in a week or month the minimum is capped at the puzzles so far). Players below it are listed under the board, unranked. Ties share a rank.
- **No pings:** the bot never mentions anyone, and display names are escaped so they can't ping or break formatting.

## Slash commands

The bot sends replies by DM. If your DMs are closed, the reply appears only to you.

| Command | What it does |
|---|---|
| `/starter` | Today's and the upcoming starting word |
| `/results [puzzle]` | Results for one puzzle. Defaults to the puzzle the daily post reports on: today's, or yesterday's if `target_offset_days = 0` |
| `/leaderboard <weekly\|monthly\|all-time>` | Standings |
| `/history [count]` | Our recent starters (default 10, up to 100). Answers appear behind spoiler tags 2 days after each puzzle |
| `/config` | The bot's current settings |
| `/reroll` | **Admin only:** pick and post a new upcoming starter |

`/reroll` is for members with **Manage Server** or a role listed in `admin_role_ids`. It is refused once the starter's puzzle has started or anyone has submitted a result for it, so it never works with `target_offset_days = 0`, where the starter is for a puzzle that is already live. It never picks a word that date has already had. Its confirmation is shown only to you instead of by DM.

## Setup

### 1. Create the Discord bot

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application and add a **Bot**. Copy its token.
2. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**. The bot needs it to read pasted results.
3. Under **OAuth2 → URL Generator**:
   1. Select the scopes `bot` and `applications.commands`.
   2. Select the permissions **View Channel**, **Send Messages**, **Add Reactions** and **Read Message History**.
   3. Open the generated URL to invite the bot to your server.
4. In Discord, turn on **Developer Mode** (User Settings → Advanced). Then right-click your server and your channel to copy their IDs.

### 2. Install and configure

Requires Python 3.12+.

```sh
git clone https://github.com/darthsco/wordlebot.git
cd wordlebot
python3 -m venv .venv
.venv/bin/pip install -e .

cp .env.example .env                 # set DISCORD_TOKEN
cp config.example.toml config.toml   # set guild_id and channel_id at minimum
```

Every setting is documented in [`config.example.toml`](config.example.toml). The config is checked at startup: if `config.toml` is missing, `guild_id` or `channel_id` is still `0`, or any value is invalid, the bot exits immediately and lists every problem. Keep `post_time` and `launch_date` quoted, as in the example.

### 3. Run

```sh
.venv/bin/python -m wordlebot sync   # optional: backfill solution history (~1 minute, first time only)
.venv/bin/python -m wordlebot pick   # optional: dry run, prints the starter it would choose
.venv/bin/python -m wordlebot        # start the bot
```

The bot has to keep running to post on schedule. Run it under a process manager such as systemd or launchd, or with Docker (below). If it's offline at post time, or a post fails, it catches up within a minute of coming back. Stop it with Ctrl-C (SIGINT) for a clean shutdown.

### Running with Docker

Requires Docker Compose v2 (the `docker compose` command). Create `.env` and `config.toml` as in step 2 **before the first `up`**, then:

```sh
docker compose up -d --build
docker compose logs -f                      # follow logs
docker compose run --rm wordlebot pick      # dry run inside the container
```

- `config.toml` must exist before `docker compose up`. If it doesn't, Compose refuses to start with a "bind source path does not exist" error. If an older version of this setup left a root-owned `config.toml` *directory* behind, remove it with `sudo rm -r config.toml` and create the file.
- `config.toml` is mounted read-only, so after editing it run `docker compose restart`.
- The SQLite database (solution cache, starters, scores) lives in the `wordlebot-state` named volume. It survives rebuilds and `docker compose down`, but `down -v` deletes it.
- Inside the container, `WORDLEBOT_DATABASE_PATH` overrides `database_path` in `config.toml`, so you don't need to change it for Docker.
- The container stops with SIGINT, which lets the bot close its Discord session cleanly. `docker compose stop`, `restart` and `down` all do this for you.
- The image installs exact dependency versions from `requirements.txt`, so a rebuild never pulls in a new release by surprise. `pyproject.toml` keeps the loose ranges. To upgrade dependencies, run the command in the header of `requirements.txt`, replace the pins with its output and rebuild. A new dependency goes in both files; the build fails at `pip check` if they disagree.

#### Updating

```sh
./update.sh          # pull; if HEAD isn't the last deployed commit, rebuild and confirm the bot is up
./update.sh --force  # rebuild and recreate the container even with no new commits
```

`update.sh` records the last successfully deployed commit in `.deployed-sha` (git-ignored):

- If a commit doesn't change the image (docs, tests, `update.sh` itself), Compose leaves the container alone and `update.sh` reports success without restarting the bot.
- If the container was replaced, it waits up to 60 seconds for the bot to log in.
- If the build fails, the container isn't running, or the bot doesn't log in, it exits non-zero and leaves `.deployed-sha` alone. A `.deploy-pending` marker stays behind, so the next run tries again and recreates the container.
- The first run has no `.deployed-sha`, so it always does one build-and-up to record the commit. That's expected.
- Delete `.deployed-sha` to make the next run redeploy.

If you only edited `config.toml`, run `docker compose restart`. If you changed `.env`, run `docker compose up -d --force-recreate`.

#### Automatic updates (systemd timer)

`update.sh` does nothing when the current commit is already deployed, so it's safe to run on a schedule. A deploy that fails is retried on the next run, so a broken build shows up as a failure in `journalctl` every 15 minutes until it's fixed. On a systemd-based Linux host such as Debian, run these from the wordlebot folder **as your normal user**. They fill in your username and the folder path automatically:

```sh
sudo tee /etc/systemd/system/wordlebot-update.service >/dev/null <<EOF
[Unit]
Description=Pull and redeploy wordlebot
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$PWD
ExecStart=$PWD/update.sh
EOF

sudo tee /etc/systemd/system/wordlebot-update.timer >/dev/null <<EOF
[Unit]
Description=Check for wordlebot updates every 15 minutes

[Timer]
OnCalendar=*:7/15
RandomizedDelaySec=60
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now wordlebot-update.timer
```

Check it:

```sh
systemctl list-timers wordlebot-update.timer    # next run
sudo systemctl start wordlebot-update.service   # run once now
journalctl -u wordlebot-update.service -n 20    # what it did
```

Before relying on it:

- **`git pull` must work without a prompt.** For a private repo, set up an SSH key without a passphrase or a saved credential. Test it by running `git pull` by hand.
- **Your user must be in the `docker` group**, because the timer runs `update.sh` as you, without `sudo`.

The timer runs at 7, 22, 37 and 52 minutes past each hour, so it doesn't fire at the same moment as a daily post scheduled on the hour or a quarter hour. A restart during the daily post is safe anyway: the pick is saved before it is sent, so the worst case is the same word announced twice.

To change the schedule, edit the `OnCalendar` line in `/etc/systemd/system/wordlebot-update.timer` (for example, `hourly`), then run `sudo systemctl daemon-reload && sudo systemctl restart wordlebot-update.timer`. To turn it off, run `sudo systemctl disable --now wordlebot-update.timer`.

## Upgrading an existing install

- **Database:** nothing to do. The bot migrates its SQLite database when it starts.
- **Config:** nothing to add; new settings have defaults. Startup is stricter, though: the bot now refuses to run with an invalid value or with `guild_id`/`channel_id` unset. Weekly and monthly leaderboards now have minimum games (3 and 7); change `min_games_weekly` and `min_games_monthly` if you prefer otherwise.
- **Update timer:** if you installed the systemd timer with `OnCalendar=*:0/15`, it fires at the same moment as the default 20:00 post. Change that line in `/etc/systemd/system/wordlebot-update.timer` to `OnCalendar=*:7/15`, then run `sudo systemctl daemon-reload && sudo systemctl restart wordlebot-update.timer`.
- **`update.sh`:** its first run after upgrading does one build-and-up to record the deployed commit, even with no new commits.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

[`CLAUDE.md`](CLAUDE.md) describes the architecture and the rules that keep answers from leaking.

## Credits

The word lists in `data/words/` are the original Wordle answer and allowed-guess lists. Daily solutions come from the New York Times Wordle endpoint. This project is not affiliated with the NYT.
