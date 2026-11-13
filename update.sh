#!/usr/bin/env bash
# Pull the latest code and redeploy the bot container if anything changed.
#
#   ./update.sh          update only if new commits were pulled (safe for a timer)
#   ./update.sh --force  rebuild and restart even if nothing changed
set -euo pipefail

cd "$(dirname "$0")"

force=false
[[ "${1:-}" == "--force" ]] && force=true

before=$(git rev-parse HEAD)
git pull --ff-only --quiet
after=$(git rev-parse HEAD)

if [[ "$before" == "$after" && "$force" == false ]]; then
    echo "Already up to date ($(git rev-parse --short HEAD)); nothing to do."
    exit 0
fi

if [[ "$before" != "$after" ]]; then
    echo "Updating $(git rev-parse --short "$before") -> $(git rev-parse --short "$after"):"
    git log --oneline "$before..$after"
fi

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker compose up -d --build
docker image prune -f >/dev/null

echo "Waiting for the bot to log in..."
for _ in $(seq 1 30); do
    if docker compose logs --since "$started" wordlebot 2>&1 | grep -q "Logged in as"; then
        echo "Bot is up at $(git rev-parse --short HEAD)."
        exit 0
    fi
    sleep 2
done

echo "Bot did not log in within 60s. Recent logs:" >&2
docker compose logs --tail 30 wordlebot >&2
exit 1
