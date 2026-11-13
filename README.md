# wordlebot

A Discord bot for friendly Wordle competition. Every evening it posts **tomorrow's starting word** to a channel. Everyone opens with that word, pastes their result, and the bot keeps score. With the first guess shared, the number of guesses is a fair comparison.

## Features

- **Daily starter post** at a time and timezone you choose (default 20:00 America/New_York). It includes the results for the puzzle that just finished.
- **Starter selection** with four strategies:
  - `random_answer_like` (default): any word that is a plausible answer.
  - `random_all_valid`: any valid guess, obscure words included.
  - `weighted_quality`: biased toward strong openers.
  - `varied`: some days great openers, some days terrible ones.
- **No repeats:** skips words we suggested recently and words that were recent solutions. Both lookback windows are configurable.
- **Full solution history** from the NYT, cached locally in SQLite. After the first run it fetches only the days that are missing, so a normal day is one request. Future answers are never stored or shown.
- **Score tracking:** paste your Wordle share text in the channel. The bot checks that your first row matches the shared starter and reacts:

  | Reaction | Meaning |
  |---|---|
  | ✅ | Verified: your first row matches the shared starter |
  | ⏳ | Pending: the answer isn't cached yet, so it's checked automatically later |
  | ❌ | Mismatch: your first row doesn't match the starter, so the score isn't counted |
  | ➖ | No starter was posted for that puzzle |
  | 🔁 | You already submitted for this puzzle |

- **Leaderboards** for daily, rolling weekly, monthly and all-time. A failed puzzle (X/6) counts as 7.

## Slash commands

The bot sends replies by DM. If your DMs are closed, the reply appears only to you.

| Command | What it does |
|---|---|
| `/starter` | Today's and the upcoming starting word |
| `/results [puzzle]` | Results for one puzzle (defaults to today's) |
| `/leaderboard <weekly\|monthly\|all-time>` | Standings |
| `/history [count]` | Our recent starters, with past answers hidden behind spoiler tags |
| `/config` | The bot's current settings |
| `/reroll` | **Admin only:** pick and post a new upcoming starter |

`/reroll` is for members with **Manage Server** or a role listed in `admin_role_ids`.

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

Every setting is documented in [`config.example.toml`](config.example.toml).

### 3. Run

```sh
.venv/bin/python -m wordlebot sync   # optional: backfill solution history (~1 minute, first time only)
.venv/bin/python -m wordlebot pick   # optional: dry run, prints the starter it would choose
.venv/bin/python -m wordlebot        # start the bot
```

The bot has to keep running to post on schedule. Run it under a process manager such as systemd or launchd, or with Docker (below). If it's offline at post time, it posts as soon as it comes back.

### Running with Docker

Create `.env` and `config.toml` as in step 2, then:

```sh
docker compose up -d --build
docker compose logs -f                      # follow logs
docker compose run --rm wordlebot pick      # dry run inside the container
```

- `config.toml` is mounted read-only, so after editing it run `docker compose restart`.
- The SQLite database (solution cache, starters, scores) lives in the `wordlebot-state` named volume. It survives rebuilds and `docker compose down`, but `down -v` deletes it.
- Inside the container, `WORDLEBOT_DATABASE_PATH` overrides `database_path` in `config.toml`, so you don't need to change it for Docker.

#### Updating

```sh
./update.sh          # pull; if there are new commits, rebuild, restart and confirm the bot logged in
./update.sh --force  # rebuild and restart even with no new commits
```

If you only edited `config.toml`, run `docker compose restart`. If you changed `.env`, run `docker compose up -d --force-recreate`.

#### Automatic updates (systemd timer)

`update.sh` does nothing when there are no new commits, so it's safe to run on a schedule. On a systemd-based Linux host such as Debian, run these from the wordlebot folder **as your normal user**. They fill in your username and the folder path automatically:

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
OnCalendar=*:0/15
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
- **Avoid pushing right around the daily post time.** A restart in the moment between posting the starter and saving it can cause a duplicate post.

To check less often, change `OnCalendar` (for example, `hourly`) and run `sudo systemctl daemon-reload`. To turn it off, run `sudo systemctl disable --now wordlebot-update.timer`.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

[`CLAUDE.md`](CLAUDE.md) describes the architecture and the rules that keep answers from leaking.

## Credits

The word lists in `data/words/` are the original Wordle answer and allowed-guess lists. Daily solutions come from the New York Times Wordle endpoint. This project is not affiliated with the NYT.
