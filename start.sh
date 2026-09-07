#!/usr/bin/env bash
# Start the complete GitHub Change Bot stack from this project directory.
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "'$1' is required. Install Docker Engine and the Docker Compose plugin."
}

env_value() {
  awk -F= -v key="$1" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' .env
}

require_env_value() {
  local key value
  key="$1"
  value="$(env_value "$key")"
  case "$value" in
    ''|change-me*|replace-with-*|https://hooks.slack.com/services/XXXX/*)
      fail "Set $key to a real value in .env."
      ;;
  esac
}

require_command docker
docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin is required."

[ -f .env ] || fail "Missing .env. Create it with: cp .env.example .env"
[ -f config.py ] || fail "Missing config.py. Create it with: cp config.py.example config.py"
[ -f id_ed25519 ] || fail "Missing id_ed25519. Add the SSH private key that can read your GitHub repositories."

chmod 600 id_ed25519
require_env_value GITHUB_WEBHOOK_SECRET
require_env_value GOOGLE_API_KEY
require_env_value SLACK_WEBHOOK_URL

export BOT_SSH_DIR="$project_dir"
docker compose config -q
docker compose up --build --detach --remove-orphans

printf 'Waiting for the webhook API to become healthy'
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error http://127.0.0.1:8088/health >/dev/null; then
    printf '\nGitHub Change Bot is running.\n'
    docker compose ps
    printf '\nCloudflare tunnel URL (may take a few seconds to appear):\n'
    docker compose logs --tail 30 tunnel | grep -Eo 'https://[-a-z0-9]+\.trycloudflare\.com' | tail -n 1 || true
    printf 'Webhook endpoint: <tunnel URL>/webhooks/github\n'
    exit 0
  fi
  printf '.'
  sleep 1
done

printf '\n' >&2
docker compose ps >&2 || true
docker compose logs --tail 100 web >&2 || true
fail "The webhook API did not become healthy. See the logs above."
