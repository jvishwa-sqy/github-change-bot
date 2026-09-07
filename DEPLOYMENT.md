# Portable deployment

The application does not depend on this VM. All runtime state lives outside
the source tree, and either deployment method can be repeated on another
Linux VM using the same repository and a new environment file.

## Option 1: systemd on Ubuntu or Debian

Install Python 3.12, Git, rsync, and curl. Clone the project, create a private
environment file from `.env.example`, and use an existing SSH key that has
read access to the repositories to analyse.

```bash
git clone git@github.com:jvishwa-sqy/github-change-bot.git
cd github-change-bot
cp .env.example /secure/git-change-bot.env
# Fill the required values in /secure/git-change-bot.env.

sudo ./scripts/install_systemd.sh \
  --env-file /secure/git-change-bot.env \
  --ssh-key /secure/id_ed25519
```

The installer creates these standard locations on the new VM:

| Purpose | Location |
|---|---|
| Application release | `/opt/git-change-bot` |
| Environment and secrets | `/etc/git-change-bot.env` |
| Queue, mirrors, indexes, and service SSH key | `/var/lib/git-change-bot` |

The service SSH key is a protected copy of the supplied existing key. The
original key remains untouched. Verify the new VM before adding webhooks:

```bash
curl -sS http://127.0.0.1:8088/health
sudo systemctl status git-change-bot-web git-change-bot-worker --no-pager
```

## Option 2: Docker Compose

This method needs Docker Engine and the Compose plugin. It works without
systemd and keeps all application state in the `bot-data` Docker volume.

```bash
git clone git@github.com:jvishwa-sqy/github-change-bot.git
cd github-change-bot
cp .env.example .env
# Fill the required values in .env.

export BOT_SSH_DIR=/secure/directory-containing-id_ed25519
docker compose up --build -d
curl -sS http://127.0.0.1:8088/health
```

The container copies the mounted SSH key into an ephemeral, private container
path at startup. The host key stays read-only and is never placed in the image
or Docker volume.

## Free temporary webhook URL

For testing without a domain, launch the optional Cloudflare Quick Tunnel:

```bash
docker compose -f compose.yaml -f compose.tunnel.yaml up --build -d
docker compose -f compose.yaml -f compose.tunnel.yaml logs tunnel
```

Add `/webhooks/github` to the reported `trycloudflare.com` URL. It changes
when the tunnel restarts, so use it only for testing. A stable production URL
requires a managed domain or a tunnel provider's stable-hostname setup.

## Move checklist

1. Stop web, worker, and tunnel services on the old VM after the new VM is
   verified, so only one worker processes each GitHub delivery.
2. Copy the environment file through a secure channel and rotate credentials
   that were exposed or shared outside the intended deployment channel.
3. Reuse or provide a GitHub-readable SSH key with `--ssh-key` or
   `BOT_SSH_DIR`.
4. Bootstrap each monitored repository on the new VM.
5. Change each GitHub webhook to the new public HTTPS URL.
