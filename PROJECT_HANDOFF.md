# Project handoff

## Completed

- GitHub push webhook receiver with HMAC verification, replay protection, and
  idempotent job enqueueing.
- Durable SQLite job queue with retry backoff, crash recovery, and deduplication.
- Incremental bare Git mirrors, bounded diff extraction, generated-file
  filtering, secret redaction, and repository-map context.
- Google Gemini, OpenAI, and offline null LLM providers.
- Slack change-summary notifications and low-cost notices for branch deletion,
  branch creation, and ignored-only changes.
- Systemd and Nginx deployment files.
- Docker image and Docker Compose deployment configuration.
- GitHub Actions CI workflow for Python 3.12 and 3.13.
- Local Git repository initialized on `main` and committed as `37aa3da`.
- Validation completed: 229 tests pass, Ruff passes, Docker Compose validates,
  and the Docker image builds and imports successfully.

## Current VM deployment

The bot is deployed on this VM with systemd.

| Component | Current state |
|---|---|
| Application code | `/opt/git-change-bot` |
| Runtime data and Git mirrors | `/var/lib/git-change-bot` |
| Configuration and secrets | `/etc/git-change-bot.env` (mode `640`) |
| Web service | `git-change-bot-web.service`, active and enabled at boot |
| Worker service | `git-change-bot-worker.service`, active and enabled at boot |
| Local health endpoint | `http://127.0.0.1:8088/health` |
| Temporary public HTTPS endpoint | `git-change-bot-tunnel.service` via Cloudflare Quick Tunnel |
| Permanent public HTTPS endpoint | Pending a domain, TLS certificate, Nginx configuration, and firewall rule |

The existing SSH private key is reused through a protected service-account copy
at `/var/lib/git-change-bot/.ssh/id_ed25519`. GitHub currently rejects that
key, so it must be granted access before the worker can clone source
repositories.

The first monitored repository is prepared:

| Repository | GitHub ID | Bootstrap state |
|---|---:|---|
| `jvishwa-sqy/github-change-bot` | `1359830861` | Mirror and repository map created successfully |

## Pending external setup

These items require your GitHub, Slack, cloud, and server credentials. They
cannot be completed from this workspace alone.

- The bot source is published at
  <https://github.com/jvishwa-sqy/github-change-bot> on the `main` branch.
- Create a GitHub webhook secret and configure a push-event webhook.
- Grant the existing SSH key read access to every repository the bot will
  analyse, either through its GitHub user account or a read-only deploy-key
  registration.
- Create a Slack incoming webhook, if Slack notifications are required.
- Create a Google AI Studio or OpenAI API key for production summaries.
- Choose a public hostname, TLS certificate, and deployment host.

## What you need to do

1. Pick the deployment method:
   - **Docker Compose:** copy `.env.example` to `.env`, set every required
     secret, create the `deploy-ssh` key directory, then follow the container
     deployment section in `README.md`.
   - **systemd/Nginx:** follow sections 3–14 of `README.md` on the target VM.
2. Set the required values in the deployment environment:
   `GITHUB_WEBHOOK_SECRET`, `SLACK_WEBHOOK_URL`, `LLM_PROVIDER`, and either
   `GOOGLE_API_KEY` or `OPENAI_API_KEY`.
3. Grant the existing SSH key read access to each source repository that Git
   must mirror and analyse.
4. Configure a GitHub push webhook to:
   `https://YOUR-DOMAIN/webhooks/github`.
5. Bootstrap each source repository before enabling its webhook. The exact
   command is documented in README section 11.
6. Make a small test push and confirm the worker completes the job and Slack
   receives the change summary.

## Immediate remaining actions

1. Add a GitHub webhook to `jvishwa-sqy/github-change-bot`: get the current
   tunnel URL with `sudo journalctl -u git-change-bot-tunnel --no-pager -o cat`,
   append `/webhooks/github`, choose content type `application/json`, select
   the **Pushes** event, and use the current VM webhook secret.
2. For permanent use, provide a public domain name. Point its DNS record to
   this VM and allow inbound TCP ports 80 and 443 in the cloud firewall. A TLS
   certificate is required before replacing the temporary tunnel.
3. Before further public use, rotate the exposed Google API key and Slack
   incoming webhook URL through their respective provider consoles.

## How to obtain the required `.env` values

| Setting | How to obtain it |
|---|---|
| `GITHUB_WEBHOOK_SECRET` | Generate a long random value with `openssl rand -hex 32`. Keep it private. Enter the exact same value in GitHub when adding the repository webhook under **Settings → Webhooks → Add webhook → Secret**. |
| `SLACK_WEBHOOK_URL` | In Slack, create an app at <https://api.slack.com/apps>, enable **Incoming Webhooks**, add a webhook to the channel that should receive summaries, then copy its URL. |
| `GOOGLE_API_KEY` | Go to <https://aistudio.google.com/apikey>, sign in with the Google account that will pay for usage, create an API key, then copy it into the environment file. Use `LLM_PROVIDER=google`. |
| `OPENAI_API_KEY` | Go to <https://platform.openai.com/api-keys>, create a secret API key, then copy it into the environment file. Use `LLM_PROVIDER=openai`; leave `GOOGLE_API_KEY` empty. |
| `GIT_SSH_COMMAND` | This is the command Git uses for the existing SSH key. The VM service uses its protected copy at `/var/lib/git-change-bot/.ssh/id_ed25519`. Grant the matching public key read access in GitHub, either through a GitHub user or under **Settings → Deploy keys** without write access. |
| `BOT_DATA_DIR` | Use `/var/lib/git-change-bot` for systemd. Docker Compose already uses this path inside the container and stores it in a persistent named volume. |

### GitHub webhook URL

After the bot is deployed behind HTTPS, add this webhook to each repository
that should be analysed:

```text
https://YOUR-DOMAIN/webhooks/github
```

In GitHub, select **Settings → Webhooks → Add webhook**, use content type
`application/json`, enter `GITHUB_WEBHOOK_SECRET` as the secret, and select
only the **Pushes** event. GitHub sends a ping immediately; a working bot
returns `{"status":"pong"}`.

### Generate a secure webhook secret

```bash
openssl rand -hex 32
```

Paste the output into `.env` as `GITHUB_WEBHOOK_SECRET`. Do not commit `.env`
or send its contents in chat.

The VM secret was rotated after credentials were exposed in chat. Retrieve the
current value only on the VM when adding the GitHub webhook:

```bash
sudo awk -F= '$1 == "GITHUB_WEBHOOK_SECRET" {print $2}' /etc/git-change-bot.env
```

## Useful references

- Deployment and operational guide: `README.md`
- Environment template: `.env.example`
- Container stack: `compose.yaml`
- Service deployment files: `systemd/`
- CI workflow: `.github/workflows/ci.yml`
