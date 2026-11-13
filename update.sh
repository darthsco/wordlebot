#!/usr/bin/env bash
# Pull the latest code and redeploy the bot container if anything changed.
#
#   ./update.sh          deploy only if HEAD is not the last deployed commit (safe for a timer)
#   ./update.sh --force  rebuild and recreate the container even if nothing changed
#
# The last successfully deployed commit is recorded in .deployed-sha, so a
# deploy that failed is retried on the next run instead of being forgotten.
# .deploy-pending exists while a deploy is unconfirmed; the retry recreates
# the container so the login check is real.
set -euo pipefail

cd "$(dirname "$0")"

force=false
[[ "${1:-}" == "--force" ]] && force=true

# Written only after a successful deploy; missing on a first run.
state_file=.deployed-sha
deployed=$(cat "$state_file" 2>/dev/null || true)

# Left behind by a deploy that failed or was interrupted.
pending_file=.deploy-pending
retry=false
[[ -e "$pending_file" ]] && retry=true

git pull --ff-only --quiet
head=$(git rev-parse HEAD)

if [[ "$deployed" == "$head" && "$force" == false && "$retry" == false ]]; then
    echo "Already up to date ($(git rev-parse --short HEAD)); nothing to do."
    exit 0
fi

if [[ -z "$deployed" ]]; then
    echo "No deployed commit recorded; deploying $(git rev-parse --short HEAD)."
elif [[ "$deployed" != "$head" ]]; then
    # The recorded commit can be unknown to git (rewritten history, bad file).
    if git cat-file -e "$deployed^{commit}" 2>/dev/null; then
        echo "Updating $(git rev-parse --short "$deployed") -> $(git rev-parse --short "$head"):"
        git log --oneline "$deployed..$head"
    else
        echo "Updating to $(git rev-parse --short "$head") (recorded commit '$deployed' not found)."
    fi
fi
[[ "$retry" == true ]] && echo "Previous deploy did not complete; recreating the container."

# Compose leaves the container alone when the image and config are unchanged
# (e.g. a docs-only commit), so there is no new login line to wait for.
running_id() { docker compose ps -q --status running wordlebot 2>/dev/null || true; }

up_args=(-d --build)
# A container left by a failed deploy must not pass as "unchanged".
[[ "$force" == true || "$retry" == true ]] && up_args+=(--force-recreate)

before=$(running_id)
touch "$pending_file"
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker compose up "${up_args[@]}"
docker image prune -f >/dev/null
after=$(running_id)

if [[ -z "$after" ]]; then
    echo "Bot container is not running after 'up'. Recent logs:" >&2
    docker compose logs --tail 30 wordlebot >&2
    exit 1
fi

if [[ "$after" == "$before" ]]; then
    echo "$head" > "$state_file"
    rm -f "$pending_file"
    echo "Container unchanged; bot still running, now at $(git rev-parse --short HEAD)."
    exit 0
fi

echo "Waiting for the bot to log in..."
for _ in $(seq 1 30); do
    # Captured, not piped to grep -q: an early match would SIGPIPE the logs
    # command and pipefail would report that as "not logged in".
    logs=$(docker compose logs --since "$started" wordlebot 2>&1 || true)
    if [[ "$logs" == *"Logged in as"* ]]; then
        echo "$head" > "$state_file"
        rm -f "$pending_file"
        echo "Bot is up at $(git rev-parse --short HEAD)."
        exit 0
    fi
    sleep 2
done

echo "Bot did not log in within 60s. Recent logs:" >&2
docker compose logs --tail 30 wordlebot >&2
exit 1
