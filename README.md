# git-change-bot

A low-cost, incremental code-change analysis bot for large GitHub repositories.

Every push produces a structured, human-readable Slack summary of **what changed
and why it matters** — built from the actual git diff, not from regenerated
documentation, and never by feeding the whole repository to a model.

```
Code Change · acme/ai-caller-core

Branch                    Author
feature/dotcom-fix        Vishwa

Diff                      Risk
+28 −9 (1 file)           🟡 MEDIUM


Summary
Enabled language switching for Dotcom inbound calls.

What changed
• DOTCOM now registers the existing language tools.
• Existing cold-calling behaviour remains unchanged.

Affected
• Dotcom inbound listener
• Language tool registration

Impact
• Dotcom callers can dynamically switch language mid-call.

Risk reason
Runtime tool initialisation on the live call path changed.

Recommended tests
• Test English → Hindi → English.
• Verify cold-calling behaviour is unchanged.

[ View Diff ]
```

## Current VM deployment

This installation is deployed on the current VM with systemd:

| Component | Location or status |
|---|---|
| Application code | `/opt/git-change-bot` |
| Configuration | `/etc/git-change-bot.env` (root-owned, mode `640`) |
| Persistent queue, mirrors, and indexes | `/var/lib/git-change-bot` |
| Web service | `git-change-bot-web.service`, bound to `127.0.0.1:8088` |
| Background worker | `git-change-bot-worker.service` |
| Temporary public HTTPS tunnel | `git-change-bot-tunnel.service` via Cloudflare Quick Tunnel |
| Local health check | `curl -sS http://127.0.0.1:8088/health` |

Both services are enabled to start after a VM reboot. A public domain, TLS
certificate, Nginx configuration, and GitHub webhook are still required before
GitHub can reach this installation.

For no-cost testing, a Cloudflare Quick Tunnel is active. Obtain its current
temporary URL with:

```bash
sudo journalctl -u git-change-bot-tunnel --no-pager -o cat \
  | sed -n 's/.*\(https:\/\/[-a-z0-9]*\.trycloudflare\.com\).*/\1/p' \
  | tail -1
```

Append `/webhooks/github` to that URL in GitHub. The URL changes if the tunnel
service restarts, so it is for testing only.

---

## 1. Architecture

```
GitHub
   │
   │ Push webhook (HMAC-SHA256 signed)
   ▼
FastAPI webhook receiver          app/main.py
   │
   │ verify → dedupe → enqueue → 202 (no git, no LLM in the request)
   ▼
Durable SQLite queue              app/queue.py
   │
   ▼
Analyzer worker                   app/worker.py, app/analyzer.py
   │
   ├── persistent bare mirror     /var/lib/git-change-bot/repos/<id>.git
   ├── git fetch (incremental)
   ├── git diff BEFORE..AFTER     app/git_repo.py, app/diff_parser.py
   ├── ignore generated/binary    app/ignore.py
   ├── re-index changed files     app/repo_map.py
   ├── enclosing function/class   app/context.py
   ├── redact secrets             app/redaction.py
   │
   ▼
LLM provider (Gemini by default)  app/llm/
   │
   ▼
Structured ChangeSummary JSON     app/models.py
   │
   ▼
Slack Incoming Webhook            app/slack.py
```

The design principle everything follows:

```
git diff       = truth
repository map = context
LLM            = explanation
Slack          = presentation
```

There is no polling loop, no "regenerate the docs and diff them" step, and no
full-repository analysis. A push that changes 4 files in a 20 000-file
repository causes work proportional to those 4 files.

### What keeps it cheap

| Behaviour | Effect |
|---|---|
| One persistent bare mirror per repo, incremental `git fetch` | No re-clone per push |
| Ignore list applied *before* patch generation | Lockfiles/`node_modules` cost nothing |
| Repo map rebuilt only for changed files | 20 000 unchanged files are never re-parsed |
| Exact enclosing function via Python AST, bounded window otherwise | Small, relevant prompts |
| Hard character caps on diff and context | A 5 000-file push cannot blow up the bill |
| One LLM call per normal push | Predictable cost |
| Zero LLM calls for branch deletes, ignored-only pushes and bootstrap | Free where analysis adds nothing |
| `gemini-2.5-flash` with thinking disabled by default | Cheapest capable tier |

---

## 2. Prerequisites

* Linux VM (Ubuntu 22.04/24.04 or Debian 12 assumed below)
* Python 3.12 or newer
* `git` 2.30+
* Outbound HTTPS to `generativelanguage.googleapis.com` and `hooks.slack.com`
* Outbound SSH (or HTTPS) to GitHub for fetching
* Nginx, if you terminate TLS on the same host

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv git nginx
python3.12 --version
git --version
```

Sizing: the service is I/O-bound and tiny. 1 vCPU / 1 GB RAM is enough for
dozens of repositories; disk must fit one bare mirror per repository.

---

## 3. Install

The bot runs from `/opt/git-change-bot`. This is the bot's **own** code — not
the repositories you want analysed; those are only ever fetched as bare mirrors
(§11) and are never checked out here.

If the project directory is already on this machine (the usual case — you
developed or unpacked it here), just copy it into place:

```bash
SRC=/home/aisqy/square_yards/gitlab-change-bot     # where the project is now

sudo mkdir -p /opt/git-change-bot
sudo chown "$USER" /opt/git-change-bot

rsync -a --delete \
    --exclude .venv --exclude .data --exclude .git --exclude .env \
    --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
    "$SRC"/ /opt/git-change-bot/
```

`.env` is excluded deliberately: deployed secrets belong in
`/etc/git-change-bot.env` (§10), owned by `gitbot` and mode `600`, not in a
world-readable file under `/opt`.

If instead you keep the bot in a git repository of your own, clone that:

```bash
# e.g. git@github.com:acme/git-change-bot.git — optional, not a prerequisite
git clone "$BOT_REPO" /opt/git-change-bot
```

Either way, build the virtualenv from the code now in `/opt`:

```bash
cd /opt/git-change-bot
python3.12 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt
```

Verify the install:

```bash
./.venv/bin/python -m pytest -q
```

---

## 4. Create the Linux user

A system account with no login shell and no password. It owns the data
directory and runs both services; it never owns the code.

```bash
sudo useradd --system --home-dir /var/lib/git-change-bot \
    --shell /usr/sbin/nologin gitbot \
    || echo "gitbot already exists"

id gitbot
# uid=997(gitbot) gid=988(gitbot) groups=988(gitbot)
```

---

## 5. Configure directories

```bash
# Data: owned and written by the service account.
sudo mkdir -p /var/lib/git-change-bot/{repos,indexes,locks,.ssh}
sudo chown -R gitbot:gitbot /var/lib/git-change-bot
sudo chmod 750 /var/lib/git-change-bot
sudo chmod 700 /var/lib/git-change-bot/.ssh

# Code: readable by the service, writable only by whoever deploys it. A
# compromised service must not be able to rewrite its own code, so do NOT
# chown /opt to gitbot.
sudo chown -R "$USER":gitbot /opt/git-change-bot
sudo chmod -R g-w,o-rwx /opt/git-change-bot
```

Check the split holds:

```bash
sudo -u gitbot touch /var/lib/git-change-bot/repos/.probe && echo "data writable"
sudo -u gitbot rm /var/lib/git-change-bot/repos/.probe
sudo -u gitbot touch /opt/git-change-bot/CANARY 2>/dev/null \
    && echo "PROBLEM: service can modify its own code" \
    || echo "code not writable by service (correct)"
```

Layout that the bot creates and owns:

```
/var/lib/git-change-bot/
├── queue.sqlite3                 durable job queue (survives restarts)
├── repos/<project_id>.git        persistent bare mirrors
├── indexes/<project_id>/repo-map.json
└── locks/<project_id>.lock       per-project git lock
```

---

## 6. GitHub read-only deploy key

This VM currently reuses its existing SSH key. The service account has a
protected copy at `/var/lib/git-change-bot/.ssh/id_ed25519`; do not place the
private key in the code directory or commit it.

```bash
sudo -u gitbot ssh-keygen -y \
    -f /var/lib/git-change-bot/.ssh/id_ed25519
```

Add the resulting public key to GitHub. You can either grant the corresponding
GitHub user read access to the repositories, or register it as a read-only
deploy key under **Settings → Deploy keys**. GitHub must accept the key before
the bot can fetch repository mirrors.

For a fresh deployment that does not have an existing key to reuse, generate a
dedicated key as `gitbot` instead:

```bash
sudo -u gitbot ssh-keygen -t ed25519 -N '' \
    -f /var/lib/git-change-bot/.ssh/id_ed25519 \
    -C "git-change-bot@$(hostname)"
sudo cat /var/lib/git-change-bot/.ssh/id_ed25519.pub
```

For several repositories, use an organisation-level machine user with read
access instead of one deploy key per repository.

---

## 7. Test git SSH access

```bash
sudo -u gitbot ssh -i /var/lib/git-change-bot/.ssh/id_ed25519 \
    -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
    -T git@github.com
# Expected: "Hi acme/repo! You've successfully authenticated, but GitHub does
#            not provide shell access."
```

`Permission denied (publickey)` here almost always means the key from §6 has
not been added to GitHub yet — the key is generated locally, but registering
it is a manual step in the GitHub UI. Confirm the local half is sound before
looking further:

```bash
sudo ls -l /var/lib/git-change-bot/.ssh/          # id_ed25519 must be 0600 gitbot:gitbot
sudo -u gitbot ssh-keygen -y -f /var/lib/git-change-bot/.ssh/id_ed25519 | head -c 60
sudo ssh-keygen -lf /var/lib/git-change-bot/.ssh/id_ed25519.pub
```

If those succeed and the fingerprint matches the key listed on GitHub, the
problem is registration, not the key. A deploy key is scoped to **one**
repository, so the authoritative test is against that repository rather than
`ssh -T`:

```bash
sudo -u gitbot env \
  GIT_SSH_COMMAND="ssh -i /var/lib/git-change-bot/.ssh/id_ed25519 -o IdentitiesOnly=yes" \
  git ls-remote git@github.com:acme/ai-caller-core.git | head -3
```

A deploy key registered on `acme/ai-caller-core` authenticates for that
repository only; `ls-remote` against any other repository still fails with
`Permission denied`. To watch several repositories, either add the same public
key as a deploy key on each (GitHub rejects re-use of one key across repos, so
generate one key per repository and give each its own `GIT_SSH_COMMAND`), or
use a single organisation **machine user** with read access to all of them —
which is why the machine-user route is recommended past two or three repos.

If you must use HTTPS instead, set `GITHUB_TOKEN` and use the `https://` clone
URL. The token is injected per-command and is never written into the mirror's
`config` file.

---

## 8. Slack webhook

1. <https://api.slack.com/apps> → **Create New App** → *From scratch*.
2. **Incoming Webhooks** → toggle **On** → **Add New Webhook to Workspace**.
3. Choose the destination channel and copy the
   `https://hooks.slack.com/services/…` URL into `SLACK_WEBHOOK_URL`.

Test it:

```bash
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"text":"git-change-bot connectivity test"}' \
  "$SLACK_WEBHOOK_URL"
# => ok
```

The webhook URL is a secret: it is never logged, and it is scrubbed from error
messages.

---

## 9. LLM configuration

Default provider is **Google Gemini** through the Generative Language API using
a developer API key.

1. Create a key at <https://aistudio.google.com/apikey>.
2. Set `GOOGLE_API_KEY` in `/etc/git-change-bot.env`.

```
LLM_PROVIDER=google
GOOGLE_API_KEY=AIza…
GOOGLE_MODEL=gemini-2.5-flash
GOOGLE_THINKING_BUDGET=0
```

Structured output is enforced with `responseMimeType=application/json` plus an
explicit `responseSchema`, so the model returns `ChangeSummary` JSON — no
Markdown parsing anywhere.

**Other providers.** The application depends only on the `LLMProvider`
protocol (`app/llm/base.py`):

* `LLM_PROVIDER=openai` — OpenAI Responses API with strict JSON schema.
* `LLM_PROVIDER=null` — runs the entire pipeline with no model spend; useful
  for staging and for verifying a deployment.
* Azure OpenAI, Anthropic, Vertex or a self-hosted model: subclass
  `BaseLLMProvider`, implement `_complete`, register it in
  `app/llm/__init__.py::build_provider`. Nothing else changes.

Verify the key before deploying:

```bash
curl -sS -H "x-goog-api-key: $GOOGLE_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"contents":[{"parts":[{"text":"reply with OK"}]}]}' \
  'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent' \
  | head -20
```

---

## 10. Environment variables

Copy the template and edit it:

```bash
sudo cp /opt/git-change-bot/.env.example /etc/git-change-bot.env
sudo chown gitbot:gitbot /etc/git-change-bot.env
sudo chmod 600 /etc/git-change-bot.env
sudo -e /etc/git-change-bot.env
```

**Where configuration comes from**, highest precedence first:

1. environment variables — what systemd's `EnvironmentFile=/etc/git-change-bot.env`
   supplies, and the only mechanism used in production;
2. a `.env` file in the working directory (`/opt/git-change-bot` under systemd),
   or the path in `BOT_ENV_FILE` — convenient for local development;
3. the defaults in `app/config.py`.

Keep deployed secrets in `/etc/git-change-bot.env` only. A stray `.env` under
`/opt` would be read as well, giving you two sources of truth — which is why
the deploy copy in §3 excludes it. If a `.env` is present but unreadable by
the service account, it is skipped with a warning rather than crashing
startup.

| Variable | Default | Purpose |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | — | HMAC-SHA256 secret configured on the webhook. **Required.** |
| `GITHUB_LEGACY_SECRET_TOKEN` | — | Optional plain-token header for forwarders |
| `GITHUB_TOKEN` | — | Optional PAT, HTTPS clone URLs only |
| `REQUIRE_WEBHOOK_SIGNATURE` | `true` | Set `false` only for local development |
| `WEBHOOK_MAX_AGE_SECONDS` | `300` | Replay window when a proxy supplies a timestamp |
| `SLACK_WEBHOOK_URL` | — | Incoming webhook destination |
| `NOTIFY_ON_IGNORED_ONLY` | `false` | Send a cheap card for lockfile-only pushes |
| `NOTIFY_ON_BRANCH_DELETE` / `_CREATE` | `true` | Lightweight, no-LLM notices |
| `LLM_PROVIDER` | `google` | `google` \| `openai` \| `null` |
| `GOOGLE_API_KEY` / `GOOGLE_MODEL` | — / `gemini-2.5-flash` | Gemini credentials |
| `GOOGLE_THINKING_BUDGET` | `0` | Thinking tokens; `0` is cheapest |
| `BOT_DATA_DIR` | `/var/lib/git-change-bot` | Mirrors, indexes, queue |
| `GIT_SSH_COMMAND` | — | Deploy-key ssh invocation |
| `MAX_DIFF_CHARS` | `60000` | Total diff sent to the model |
| `MAX_CONTEXT_CHARS` | `90000` | Total surrounding-code budget |
| `MAX_FILE_CONTEXT_CHARS` | `16000` | Per-file context cap |
| `MAX_CHANGED_FILES` | `80` | Files analysed per push |
| `HIERARCHICAL_FILE_THRESHOLD` | `25` | Above this, group by subsystem |
| `MAX_JOB_ATTEMPTS` | `5` | Retries before a job is failed |
| `WORKER_POLL_SECONDS` | `2` | Idle poll interval |
| `JOB_LEASE_SECONDS` | `1800` | Crash-recovery lease |
| `IGNORE_PATTERNS` | built-in list | Replaces the defaults entirely |
| `EXTRA_IGNORE_PATTERNS` | empty | Adds to the defaults |
| `WATCHED_BRANCHES` | empty (all) | Restrict analysis to given branches |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `false` | Logging |

Configuration is validated at process start: a missing model key or webhook
secret fails the service immediately rather than at the first push.

---

## 11. Bootstrap each repository

Run once per repository, before enabling its webhook. This builds the mirror
and the initial repository map. **No LLM calls are made.**

```bash
cd /opt/git-change-bot
sudo -u gitbot ./.venv/bin/python scripts/bootstrap_repo.py \
    --project-id 987654 \
    --repo-url git@github.com:acme/ai-caller-core.git \
    --project-name acme/ai-caller-core \
    --ref main
```

`--project-id` must be GitHub's numeric repository id, the same value the
webhook sends in `repository.id`:

```bash
curl -sS https://api.github.com/repos/acme/ai-caller-core | grep '"id"' | head -1
```

Output:

```
Repository : git@github.com:acme/ai-caller-core.git
Project id : 987654
Mirror     : /var/lib/git-change-bot/repos/987654.git
Mirror created.
Ref        : main → 4f2a1c9d8e77

Repository map written to /var/lib/git-change-bot/indexes/987654/repo-map.json
  tracked files      : 21043
  indexed files      : 3187
  ignored source     : 17402
  symbols            : 24815
  lines of code      : 498221
  languages          :
      python       2410
      typescript    611
      go            166
  elapsed            : 41.3s

No LLM calls were made.
```

---

## 12. systemd installation

```bash
sudo cp /opt/git-change-bot/systemd/git-change-bot-web.service /etc/systemd/system/
sudo cp /opt/git-change-bot/systemd/git-change-bot-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now git-change-bot-web git-change-bot-worker

sudo systemctl status git-change-bot-web --no-pager
sudo systemctl status git-change-bot-worker --no-pager
curl -sS http://127.0.0.1:8088/health
# {"status":"ok"}
```

Both units restart on failure. The worker gets `TimeoutStopSec=300` so that
`systemctl restart` lets the job in flight finish rather than killing it —
though a killed job is recovered from the queue anyway.

### Container deployment

For Docker Compose, copy `.env.example` to `.env`, replace every placeholder,
and create a directory that holds the read-only SSH deploy key. The directory
must be readable by container uid `10001` and should contain `id_ed25519` and
`known_hosts`.

```bash
cp .env.example .env
mkdir -p ./deploy-ssh
# Copy the deploy key and known_hosts into ./deploy-ssh, then restrict them.
chmod 700 ./deploy-ssh
chmod 600 ./deploy-ssh/id_ed25519

export BOT_SSH_DIR="$PWD/deploy-ssh"
docker compose up --build -d
docker compose ps
curl -sS http://127.0.0.1:8088/health
```

The Compose stack uses one persistent named volume for the SQLite queue,
mirrors, indexes, and locks. Put TLS termination in front of port 8088 (for
example with the Nginx configuration below). Do not expose that port directly
to the internet.

The active VM deployment uses systemd, not Docker Compose. Keep Compose for a
future portable deployment or development environment.

---

## 13. Nginx

```bash
sudo cp /opt/git-change-bot/systemd/nginx-example.conf \
        /etc/nginx/sites-available/code-change-bot
sudo ln -s /etc/nginx/sites-available/code-change-bot /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Edit `server_name` and the certificate paths first. Only `/webhooks/github` is
exposed publicly; `/health` is limited to internal networks and everything else
returns 404. `client_max_body_size 25m` matches GitHub's payload limit.

---

## 14. GitHub webhook configuration

Repository → **Settings** → **Webhooks** → **Add webhook**:

| Field | Value |
|---|---|
| Payload URL | `https://code-change-bot.company.com/webhooks/github` |
| Content type | `application/json` |
| Secret | the same value as `GITHUB_WEBHOOK_SECRET` |
| SSL verification | Enable |
| Events | *Just the push event* |
| Active | ✓ |

GitHub immediately sends a `ping`; the service answers `200 {"status":"pong"}`.

For many repositories, configure the webhook once at the **organisation**
level with the push event only.

---

## 15. Test a real push

```bash
git checkout -b feature/test-change
# edit a source file
git add .
git commit -m "Enable language tools for dotcom"
git push origin feature/test-change
```

Then watch it flow through:

```bash
journalctl -u git-change-bot-web -n 20 --no-pager
# push enqueued | job_id=1 project=acme/ai-caller-core branch=feature/test-change …

journalctl -u git-change-bot-worker -n 20 --no-pager
# job started   | job_id=1 …
# diff computed | changed_files=1 ignored_files=1 diff_chars=812 git_ms=340 mode=single
# llm response  | provider=google model=gemini-2.5-flash latency_ms=2210 prompt_tokens=1483
# slack notified| status=200
# job completed | outcome=analyzed llm_calls=1 duration_ms=3104
```

Slack receives the card shown at the top of this file.

### Testing without GitHub

```bash
cd /opt/git-change-bot
BODY='{"ref":"refs/heads/feature/test","before":"<sha-a>","after":"<sha-b>",
"repository":{"id":987654,"full_name":"acme/ai-caller-core",
"ssh_url":"git@github.com:acme/ai-caller-core.git",
"html_url":"https://github.com/acme/ai-caller-core","default_branch":"main"},
"pusher":{"name":"vishwa"},"head_commit":{"message":"test","author":{"name":"Vishwa"}},
"commits":[{"id":"<sha-b>","message":"test"}]}'

SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" | awk '{print $2}')"

curl -sS -X POST http://127.0.0.1:8088/webhooks/github \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: push' \
  -H "X-GitHub-Delivery: $(uuidgen)" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$BODY"
# {"status":"accepted","job_id":1}
```

Set `LLM_PROVIDER=null` first if you want to exercise the pipeline with no
model spend.

---

## 16. Logs

```bash
journalctl -u git-change-bot-web -f
journalctl -u git-change-bot-worker -f
journalctl -u git-change-bot-worker --since '1 hour ago' | grep 'job completed'
```

Set `LOG_JSON=true` for one JSON object per line, suitable for shipping.

Logged per job: job id, project id and name, branch, short before/after SHAs,
changed-file count, diff size, context size, LLM latency and token counts,
Slack status, job duration.

**Never logged:** the Slack webhook URL, the GitHub secret, the model API key,
complete source files, or unredacted prompts. Prompt text is available only at
`LOG_LEVEL=DEBUG`, and only as a character count unless you are debugging.

Inspect the queue directly:

```bash
sudo -u gitbot sqlite3 /var/lib/git-change-bot/queue.sqlite3 \
  "SELECT id, status, attempt_count, project_name, substr(after_sha,1,8), last_error
     FROM jobs ORDER BY id DESC LIMIT 10;"
```

---

## 17. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Webhook shows `401` in GitHub's *Recent Deliveries* | Secret mismatch | Ensure the webhook secret equals `GITHUB_WEBHOOK_SECRET` exactly, then `systemctl restart git-change-bot-web` |
| `202 {"status":"duplicate"}` | GitHub retried a delivery, or the same push arrived twice | Expected; the job already exists |
| `202 {"status":"ignored"}` | Tag push, non-push event, or a push with no commits | Expected |
| Nothing in Slack, job `completed` with `outcome=ignored_only` | Push touched only ignored files | Expected; set `NOTIFY_ON_IGNORED_ONLY=true` to be told anyway |
| Job `failed` with "commits not present in the mirror" | The branch was force-pushed or GC'd before the worker ran | Not retryable; the next push analyses normally |
| Job stuck retrying with an ssh error | Deploy key missing or wrong `GIT_SSH_COMMAND` | Re-run the check in §7 as `gitbot` |
| `Permission denied (publickey)` from `ssh -T git@github.com` | The §6 key was generated but never added to GitHub, or it is registered on a different repository | Add the public key under the repository's *Deploy keys* (§6); a deploy key only authenticates for its own repository |
| `git clone` times out on a huge repository | First mirror exceeds `GIT_TIMEOUT_SECONDS` | Raise it, or bootstrap the repository manually (§11) |
| Service fails at start with "GOOGLE_API_KEY is required" | Missing key | Set it in `/etc/git-change-bot.env` |
| `gemini returned 429` in the logs | Rate limited | The provider retries with backoff; the job then retries up to `MAX_JOB_ATTEMPTS` |
| Slack `invalid_blocks` | A malformed webhook URL or a revoked hook | Re-issue the incoming webhook |
| Jobs stuck in `processing` | The worker was killed | Restart it; `processing` jobs are re-queued at startup and after `JOB_LEASE_SECONDS` |
| `ModuleNotFoundError: No module named 'app'`, or `pip install -r requirements.txt` says the file is missing | `/opt/git-change-bot` holds only a `.venv` — the code was never copied in | Re-run the `rsync` in §3, then `ls /opt/git-change-bot` and confirm `app/` and `requirements.txt` are there |
| `.venv/bin/pip: No such file or directory`, or a stray `~ip` directory in `site-packages` | An interrupted `pip install --upgrade pip` left the venv half-built | `rm -rf .venv && python3.12 -m venv .venv && ./.venv/bin/python -m pip install -r requirements.txt` |
| Log line `ignoring unreadable env file` | A `.env` exists in the working directory that `gitbot` cannot read | Harmless — but delete it, since production config belongs in `/etc/git-change-bot.env` (§10) |

Requeue a failed job by hand:

```bash
sudo -u gitbot sqlite3 /var/lib/git-change-bot/queue.sqlite3 \
  "UPDATE jobs SET status='queued', attempt_count=0, available_at=datetime('now')
    WHERE id=42;"
```

---

## 18. Security considerations

* **Webhook authenticity.** Every request must carry a valid
  `X-Hub-Signature-256` HMAC over the *raw* body, compared in constant time.
  A `X-GitHub-Delivery` id is accepted only once, which blocks replays;
  a timestamp header, if a proxy adds one, is additionally checked against a
  five-minute window.
* **No shell.** Every git invocation is an argument list; `shell=True` appears
  nowhere. Commit SHAs are validated against `^[0-9a-fA-F]{7,64}$` before they
  reach a command line, so a hostile payload cannot smuggle git options.
* **Credential hygiene.** Deploy keys stay in `gitbot`'s home. HTTPS tokens are
  passed per-command and immediately replaced in the mirror config, so no token
  is persisted. `sanitize_url` strips credentials from anything logged.
* **Secret redaction.** Diffs, file context and commit messages pass through
  `app/redaction.py` before reaching the model or Slack: private keys, AWS
  keys, GitHub/GitLab/Slack/OpenAI/Google tokens, JWTs, bearer headers,
  database URLs with passwords and secret-looking assignments are replaced with
  `[REDACTED]`.
* **Least privilege.** Both units run as `gitbot` with `ProtectSystem=strict`
  and a single writable path. The deploy key is read-only.
* **Attack surface.** Only `/webhooks/github` is public. FastAPI's docs and
  OpenAPI endpoints are disabled.
* **Data at rest.** Mirrors contain your source code — the data directory is
  `0750` and the VM should be treated as production infrastructure.
* **What leaves the network.** Redacted diff hunks and bounded code context go
  to your configured model provider, and the summary goes to Slack. Nothing
  else. Use `LLM_PROVIDER=null` or a self-hosted provider if even that is
  unacceptable.

---

## 19. Cost control

Per **normal push**: exactly **one** LLM request, with a prompt bounded by
`MAX_DIFF_CHARS + MAX_CONTEXT_CHARS` (≈150 KB worst case, typically 2–8 KB).

**Zero** LLM requests for:

* repository bootstrap and indexing (fully deterministic),
* branch deletion,
* pushes that touch only ignored files (lockfiles, `dist/`, `node_modules/`, binaries),
* branch creation with no safe baseline,
* pushes to branches outside `WATCHED_BRANCHES`.

**More than one** request only when a push exceeds
`HIERARCHICAL_FILE_THRESHOLD` analysable files. The change is then grouped by
subsystem (`app/routing/**`, `app/mcp/**`, …), summarised per group and
synthesised once — bounded at `HIERARCHICAL_MAX_GROUPS + 1` calls, never one
call per file.

Worked example — 20 000 files, 500 000 LOC, a push touching 4 files / 100 lines:

```
git fetch                 only the new objects
git diff BEFORE AFTER     4 files
repo map update           4 files re-parsed, 19 996 untouched
context                   4 enclosing functions + neighbouring symbols
prompt                    ~6 KB
LLM calls                 1
```

Tuning levers: lower `MAX_CHANGED_FILES` and `MAX_CONTEXT_CHARS`, raise
`HIERARCHICAL_FILE_THRESHOLD` (fewer, larger calls), keep
`GOOGLE_THINKING_BUDGET=0`, and add noisy paths to `EXTRA_IGNORE_PATTERNS`.

---

## 20. Development

```bash
python3.12 -m venv .venv
./.venv/bin/pip install -r requirements.txt

./.venv/bin/python -m pytest -q          # full suite, no network needed
./.venv/bin/ruff check .
./.venv/bin/ruff format .
```

Run it locally end to end without GitHub or a model:

```bash
export BOT_DATA_DIR=./.data
export LLM_PROVIDER=null
export REQUIRE_WEBHOOK_SIGNATURE=false
export GITHUB_WEBHOOK_SECRET=dev-secret

./.venv/bin/uvicorn app.main:app --port 8088 &
./.venv/bin/python -m app.worker
```

### Module map

| Module | Responsibility |
|---|---|
| `app/config.py` | Env-driven settings, validated at startup |
| `app/errors.py` | Typed exceptions separating retryable from permanent failures |
| `app/logging_setup.py` | Structured/plain logging, with secrets kept out |
| `app/locks.py` | Per-project `flock`, so git operations never race |
| `app/models.py` | Domain models and the `ChangeSummary` schema |
| `app/security.py` | Webhook HMAC, replay and token verification |
| `app/db.py`, `app/queue.py` | SQLite schema and the durable queue |
| `app/git_repo.py` | Mirrors, fetch, safe subprocess, bounded diffs |
| `app/diff_parser.py` | Unified-diff → `ChangedFile`, exact changed lines |
| `app/ignore.py` | Gitignore-style path filtering |
| `app/repo_map.py` | Deterministic symbol/import extraction, incremental index |
| `app/context.py` | Enclosing-symbol and windowed context, budgeting, grouping |
| `app/redaction.py` | Secret scrubbing |
| `app/llm/` | Provider protocol, prompts, Gemini/OpenAI/null backends |
| `app/slack.py` | Block Kit rendering and delivery |
| `app/analyzer.py` | The per-push pipeline |
| `app/worker.py` | Claim → analyse → complete loop, graceful shutdown |

### Adding a language to the repo map

Register an extractor in `app/repo_map.py`:

```python
from app.repo_map import register_extractor


class RubyExtractor:
    language = "ruby"

    def extract(self, source: str) -> tuple[list[Symbol], list[str]]: ...


register_extractor(RubyExtractor())
```

The same hook is how Tree-sitter would replace the lightweight parsers later —
per language, without touching any caller. It is deliberately not a v1
dependency.

### Concurrency

One worker is the intended deployment. The queue is nonetheless safe for
several: claims are atomic (`UPDATE … WHERE id = (SELECT … LIMIT 1)
RETURNING`), and git operations take a per-project `flock`. Start more workers
by templating the unit; there is no Redis, broker or scheduler to run.

---

## 21. Upgrading the bot

Because the service account cannot write to `/opt` (§5), an upgrade is a
deploy-user action followed by a restart:

```bash
SRC=/home/aisqy/square_yards/gitlab-change-bot

# 1. Run the suite against the new code before it goes anywhere.
cd "$SRC" && ./.venv/bin/python -m pytest -q

# 2. Copy it into place. --delete removes files dropped from the project;
#    .env and .venv are preserved by the excludes.
rsync -a --delete \
    --exclude .venv --exclude .data --exclude .git --exclude .env \
    --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
    "$SRC"/ /opt/git-change-bot/

# 3. Restore the ownership split (rsync copies the source tree's modes).
sudo chown -R "$USER":gitbot /opt/git-change-bot
sudo chmod -R g-w,o-rwx /opt/git-change-bot

# 4. Pick up dependency changes, if requirements.txt moved.
/opt/git-change-bot/.venv/bin/python -m pip install -q -r /opt/git-change-bot/requirements.txt

# 5. Restart. In-flight jobs finish first; anything interrupted is recovered
#    from the queue on the next start.
sudo systemctl restart git-change-bot-web git-change-bot-worker
sudo systemctl status git-change-bot-worker --no-pager
```

Verify the deployed copy actually matches your source:

```bash
diff -rq --exclude=.venv --exclude=.env --exclude=__pycache__ \
    --exclude=.pytest_cache --exclude=.ruff_cache --exclude=.git \
    "$SRC" /opt/git-change-bot && echo identical
```

Nothing about an upgrade touches `/var/lib/git-change-bot`: the mirrors, the
repository maps and the queue all survive, so a restart costs one incremental
`git fetch` per repository and no re-indexing.

Confirm the service account can still start the code after any permission
change:

```bash
sudo -u gitbot env -C /opt/git-change-bot \
    /opt/git-change-bot/.venv/bin/python -c "import app.main; print('ok')"
```
