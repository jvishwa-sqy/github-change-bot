#!/usr/bin/env bash
# Install or upgrade git-change-bot on any Ubuntu/Debian VM.
#
# By default this script uses .env and id_ed25519 from the project root.

set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
env_file=""
ssh_key=""
start_services=true

usage() {
    cat <<'EOF'
Usage: sudo ./scripts/install_systemd.sh [options]

Options:
  --source DIR       Project source directory (default: repository containing this script)
  --env-file FILE    Runtime environment file (default: SOURCE/.env)
  --ssh-key FILE     Existing private key (default: SOURCE/id_ed25519)
  --no-start         Install files without starting systemd services
  -h, --help         Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --source) source_dir="$2"; shift 2 ;;
        --env-file) env_file="$2"; shift 2 ;;
        --ssh-key) ssh_key="$2"; shift 2 ;;
        --no-start) start_services=false; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "${EUID}" -ne 0 ]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi
env_file="${env_file:-$source_dir/.env}"
ssh_key="${ssh_key:-$source_dir/id_ed25519}"

source_dir="$(realpath -e "$source_dir")"
env_file="$(realpath -e "$env_file")"
ssh_key="$(realpath -e "$ssh_key")"
for required in "$source_dir/requirements.txt" "$source_dir/systemd/git-change-bot-web.service" "$env_file" "$ssh_key"; do
    [ -f "$required" ] || { echo "Missing required file: $required" >&2; exit 1; }
done
command -v rsync >/dev/null || { echo "rsync is required." >&2; exit 1; }
command -v python3.12 >/dev/null || { echo "python3.12 is required." >&2; exit 1; }

if ! id -u gitbot >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/git-change-bot --shell /usr/sbin/nologin gitbot
fi

install -d -m 0750 -o gitbot -g gitbot /var/lib/git-change-bot/{repos,indexes,locks}
install -d -m 0700 -o gitbot -g gitbot /var/lib/git-change-bot/.ssh
install -m 0600 -o gitbot -g gitbot "$ssh_key" /var/lib/git-change-bot/.ssh/id_ed25519
if [ -f "${ssh_key}.pub" ]; then
    install -m 0644 -o gitbot -g gitbot "${ssh_key}.pub" /var/lib/git-change-bot/.ssh/id_ed25519.pub
fi
if [ -f "$(dirname "$ssh_key")/known_hosts" ]; then
    install -m 0644 -o gitbot -g gitbot "$(dirname "$ssh_key")/known_hosts" /var/lib/git-change-bot/.ssh/known_hosts
fi

install -d -m 0755 -o root -g root /opt/git-change-bot
rsync -a --delete \
    --exclude .git --exclude .env --exclude .venv --exclude .data \
    --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
    "$source_dir/" /opt/git-change-bot/
chown -R root:root /opt/git-change-bot
find /opt/git-change-bot -type d -exec chmod 0755 {} +
find /opt/git-change-bot -type f -exec chmod 0644 {} +
chmod 0755 /opt/git-change-bot/scripts/*.sh /opt/git-change-bot/scripts/*.py

python3.12 -m venv --upgrade /opt/git-change-bot/.venv
find /opt/git-change-bot/.venv/bin -type f -exec chmod 0755 {} +
/opt/git-change-bot/.venv/bin/python -m pip install --upgrade pip
/opt/git-change-bot/.venv/bin/python -m pip install -r /opt/git-change-bot/requirements.txt

install -m 0640 -o root -g gitbot "$env_file" /etc/git-change-bot.env
sed -i 's|^BOT_DATA_DIR=.*|BOT_DATA_DIR=/var/lib/git-change-bot|' /etc/git-change-bot.env
sed -i 's|^GIT_SSH_COMMAND=.*|GIT_SSH_COMMAND=ssh -i /var/lib/git-change-bot/.ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new|' /etc/git-change-bot.env

install -m 0644 "$source_dir/systemd/git-change-bot-web.service" /etc/systemd/system/git-change-bot-web.service
install -m 0644 "$source_dir/systemd/git-change-bot-worker.service" /etc/systemd/system/git-change-bot-worker.service
systemctl daemon-reload
systemctl enable git-change-bot-web git-change-bot-worker

if [ "$start_services" = true ]; then
    systemctl restart git-change-bot-web git-change-bot-worker
    curl --fail --silent --show-error http://127.0.0.1:8088/health
    echo
fi

echo "Installed git-change-bot. Configure a public HTTPS endpoint separately."
