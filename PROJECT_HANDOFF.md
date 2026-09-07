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

## Pending external setup

These items require your GitHub, Slack, cloud, and server credentials. They
cannot be completed from this workspace alone.

- Create or select the GitHub repository that will host this bot.
- Give this machine access to that repository and provide its SSH or HTTPS URL
  so `main` can be pushed.
- Create a GitHub webhook secret and configure a push-event webhook.
- Create a read-only GitHub deploy key (or machine-user access) for every
  repository the bot will analyse.
- Create a Slack incoming webhook, if Slack notifications are required.
- Create a Google AI Studio or OpenAI API key for production summaries.
- Choose a public hostname, TLS certificate, and deployment host.

## What you need to do

1. Create an empty GitHub repository and send me its SSH or HTTPS URL. I will
   add it as `origin` and push the committed project.
2. Pick the deployment method:
   - **Docker Compose:** copy `.env.example` to `.env`, set every required
     secret, create the `deploy-ssh` key directory, then follow the container
     deployment section in `README.md`.
   - **systemd/Nginx:** follow sections 3–14 of `README.md` on the target VM.
3. Set the required values in the deployment environment:
   `GITHUB_WEBHOOK_SECRET`, `SLACK_WEBHOOK_URL`, `LLM_PROVIDER`, and either
   `GOOGLE_API_KEY` or `OPENAI_API_KEY`.
4. Add the bot's read-only deploy key to each source repository that Git must
   mirror and analyse.
5. Configure a GitHub push webhook to:
   `https://YOUR-DOMAIN/webhooks/github`.
6. Bootstrap each source repository before enabling its webhook. The exact
   command is documented in README section 11.
7. Make a small test push and confirm the worker completes the job and Slack
   receives the change summary.

## Useful references

- Deployment and operational guide: `README.md`
- Environment template: `.env.example`
- Container stack: `compose.yaml`
- Service deployment files: `systemd/`
- CI workflow: `.github/workflows/ci.yml`
