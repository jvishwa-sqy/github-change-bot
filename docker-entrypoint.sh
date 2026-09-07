#!/bin/sh
set -eu

# Compose mounts the host key read-only here. Copy it into the container at
# startup so the unprivileged application user can use a host-owned key
# without weakening that key's permissions on the host.
host_ssh_dir=/run/host-ssh
bot_ssh_dir=/home/gitbot/.ssh

mkdir -p /var/lib/git-change-bot "$bot_ssh_dir"
chown gitbot:gitbot /var/lib/git-change-bot "$bot_ssh_dir"
chmod 700 "$bot_ssh_dir"

if [ -d "$host_ssh_dir" ]; then
    for file in id_ed25519 id_ed25519.pub known_hosts config; do
        if [ -f "$host_ssh_dir/$file" ]; then
            cp "$host_ssh_dir/$file" "$bot_ssh_dir/$file"
            chown gitbot:gitbot "$bot_ssh_dir/$file"
            chmod 600 "$bot_ssh_dir/$file"
        fi
    done
fi

exec gosu gitbot "$@"
