"""Runtime configuration for GitHub Change Bot."""

SETTINGS = {
    # --- Slack -------------------------------------------------------------
    "slack_webhook_url": None,
    "slack_timeout_seconds": 10,
    "notify_on_ignored_only": False,
    "notify_on_branch_delete": True,
    "notify_on_branch_create": True,
    # --- Gemini ------------------------------------------------------------
    "llm_provider": "google",
    "google_model": "gemini-2.5-flash",
    "google_thinking_budget": 0,
    "llm_timeout_seconds": 120,
    "llm_max_retries": 2,
    # --- Webhook -----------------------------------------------------------
    "require_webhook_signature": True,
    "webhook_max_age_seconds": 300,
    # --- Storage and Git ---------------------------------------------------
    "bot_data_dir": "/var/lib/git-change-bot",
    "git_timeout_seconds": 600,
    # --- Cost and input limits --------------------------------------------
    "max_diff_chars": 60_000,
    "max_context_chars": 90_000,
    "max_file_context_chars": 16_000,
    "max_changed_files": 80,
    "max_file_diff_chars": 12_000,
    "context_window_lines": 50,
    "hierarchical_file_threshold": 25,
    "hierarchical_max_groups": 8,
    # --- Queue -------------------------------------------------------------
    "max_job_attempts": 5,
    "worker_poll_seconds": 2,
    "job_lease_seconds": 1800,
    "retry_backoff_seconds": 30,
    # --- Filtering ---------------------------------------------------------
    "extra_ignore_patterns": [],
    "watched_branches": [],
    # --- Logging -----------------------------------------------------------
    "log_level": "INFO",
    "log_json": False,
}
