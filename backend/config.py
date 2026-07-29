from __future__ import annotations

import json
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = ""
    # APP_ENV remains a backwards-compatible alias; an explicit ENVIRONMENT
    # always wins so a stale alias cannot disable production checks.
    app_env: str = ""
    encryption_key: str = ""
    webhook_base_url: str = "https://your-domain.com"
    db_path: str = "data.db"
    admin_password: str = "changeme"
    # Transitional V2 compatibility for the active V1 password. Production
    # remains at 12 characters unless this narrowly scoped flag is explicit.
    allow_legacy_11_char_admin_password: bool = False

    # Comma-separated strings are intentionally used here instead of List fields.
    # pydantic-settings treats List environment values as JSON before validators
    # run, which makes the common `A,B` deployment syntax unnecessarily fragile.
    cors_allowed_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "https://ai-notebook-v2.leopilot.com"
    )
    notebook_host_allowlist: str = (
        "notebooklm.google.com,notebooklm.google,notebook.google.com"
    )

    setup_session_ttl_seconds: int = 3600
    admin_session_ttl_seconds: int = 900
    invite_code_ttl_seconds: int = 7_776_000
    notebook_query_concurrency: int = 3
    notebook_query_queue_limit: int = 20
    notebook_query_timeout_seconds: float = 90.0
    line_background_task_limit: int = 100
    line_background_shutdown_timeout_seconds: float = 15.0
    line_webhook_max_body_bytes: int = 1_048_576
    line_webhook_max_events_per_request: int = 100
    line_event_dedupe_retention_seconds: int = 604_800
    line_event_claim_timeout_seconds: int = 360
    line_event_pending_limit: int = 5_000
    line_event_pending_per_channel_limit: int = 50
    line_event_replay_per_channel_batch: int = 10
    line_event_max_attempts: int = 4
    line_event_retry_base_seconds: float = 5.0
    line_event_replay_interval_seconds: float = 2.0
    line_event_pending_retention_seconds: int = 86_400
    course_account_health_check_interval_seconds: int = 900
    shared_notebook_binding_enabled: bool = True
    legacy_nlm_binding_enabled: bool = False

    rate_limit_window_seconds: int = 60
    rate_limit_invite_attempts: int = 10
    rate_limit_admin_login_attempts: int = 5
    rate_limit_channel_updates: int = 10
    rate_limit_notebook_bindings: int = 5
    rate_limit_course_account_reauth: int = 5
    trust_proxy_headers: bool = False

    @property
    def runtime_environment(self) -> str:
        raw_environment = (
            self.environment.strip() or self.app_env.strip() or "development"
        ).lower()
        aliases = {
            "dev": "development",
            "development": "development",
            "test": "test",
            "testing": "test",
            "prod": "production",
            "production": "production",
        }
        try:
            return aliases[raw_environment]
        except KeyError as error:
            raise ValueError(
                "ENVIRONMENT must be development, test, or production"
            ) from error

    @property
    def is_production(self) -> bool:
        return self.runtime_environment == "production"

    @staticmethod
    def _split_setting(value: str) -> list[str]:
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            parsed = json.loads(raw)
            if not isinstance(parsed, list) or not all(
                isinstance(v, str) for v in parsed
            ):
                raise ValueError("allowlist must be a JSON string array")
            return [v.strip() for v in parsed if v.strip()]
        return [v.strip() for v in raw.split(",") if v.strip()]

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        origins = self._split_setting(self.cors_allowed_origins)
        for origin in origins:
            parsed = urlparse(origin)
            if origin == "*":
                raise ValueError("CORS wildcard origin is not allowed")
            if not parsed.scheme or not parsed.netloc or parsed.path not in {"", "/"}:
                raise ValueError(f"Invalid CORS origin: {origin}")
            if self.is_production and parsed.scheme != "https":
                raise ValueError("Production CORS origins must use HTTPS")
        return origins

    @property
    def notebook_host_allowlist_list(self) -> list[str]:
        hosts = [
            host.lower().rstrip(".")
            for host in self._split_setting(self.notebook_host_allowlist)
        ]
        if any("/" in host or ":" in host or host == "*" for host in hosts):
            raise ValueError("Notebook host allowlist must contain exact host names")
        return hosts

    def validate_runtime(self) -> None:
        """Reject unsafe or nonsensical production configuration at startup."""

        # Resolve this first so a misspelled production environment cannot
        # silently skip the production-only checks below.
        self.runtime_environment

        if self.notebook_query_concurrency < 1:
            raise ValueError("NOTEBOOK_QUERY_CONCURRENCY must be at least 1")
        if self.notebook_query_queue_limit < 0:
            raise ValueError("NOTEBOOK_QUERY_QUEUE_LIMIT cannot be negative")
        if self.notebook_query_timeout_seconds <= 0:
            raise ValueError("NOTEBOOK_QUERY_TIMEOUT_SECONDS must be positive")
        if self.line_background_task_limit < 1:
            raise ValueError("LINE_BACKGROUND_TASK_LIMIT must be at least 1")
        if self.line_background_shutdown_timeout_seconds < 0:
            raise ValueError(
                "LINE_BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS cannot be negative"
            )
        if self.line_webhook_max_body_bytes < 1:
            raise ValueError("LINE_WEBHOOK_MAX_BODY_BYTES must be at least 1")
        if self.line_webhook_max_events_per_request < 1:
            raise ValueError("LINE_WEBHOOK_MAX_EVENTS_PER_REQUEST must be at least 1")
        if self.line_event_dedupe_retention_seconds < 1:
            raise ValueError("LINE_EVENT_DEDUPE_RETENTION_SECONDS must be at least 1")
        minimum_claim_seconds = (3 * self.notebook_query_timeout_seconds) + 60
        if self.line_event_claim_timeout_seconds < minimum_claim_seconds:
            raise ValueError(
                "LINE_EVENT_CLAIM_TIMEOUT_SECONDS must cover three query timeout "
                "windows plus a 60-second delivery margin"
            )
        if self.line_event_pending_limit < 1:
            raise ValueError("LINE_EVENT_PENDING_LIMIT must be at least 1")
        if self.line_event_pending_per_channel_limit < 1:
            raise ValueError("LINE_EVENT_PENDING_PER_CHANNEL_LIMIT must be at least 1")
        if self.line_event_pending_per_channel_limit > self.line_event_pending_limit:
            raise ValueError(
                "LINE_EVENT_PENDING_PER_CHANNEL_LIMIT cannot exceed "
                "LINE_EVENT_PENDING_LIMIT"
            )
        if self.line_event_replay_per_channel_batch < 1:
            raise ValueError("LINE_EVENT_REPLAY_PER_CHANNEL_BATCH must be at least 1")
        if self.line_event_max_attempts < 1:
            raise ValueError("LINE_EVENT_MAX_ATTEMPTS must be at least 1")
        if self.line_event_retry_base_seconds <= 0:
            raise ValueError("LINE_EVENT_RETRY_BASE_SECONDS must be positive")
        if self.line_event_replay_interval_seconds <= 0:
            raise ValueError("LINE_EVENT_REPLAY_INTERVAL_SECONDS must be positive")
        if (
            self.line_event_pending_retention_seconds
            <= self.line_event_claim_timeout_seconds
        ):
            raise ValueError(
                "LINE_EVENT_PENDING_RETENTION_SECONDS must exceed "
                "LINE_EVENT_CLAIM_TIMEOUT_SECONDS"
            )
        if self.line_event_pending_retention_seconds > 86_400:
            raise ValueError(
                "LINE_EVENT_PENDING_RETENTION_SECONDS cannot exceed the "
                "24-hour LINE retry-key window"
            )
        if self.course_account_health_check_interval_seconds < 1:
            raise ValueError(
                "COURSE_ACCOUNT_HEALTH_CHECK_INTERVAL_SECONDS must be at least 1"
            )
        positive_security_values = {
            "SETUP_SESSION_TTL_SECONDS": self.setup_session_ttl_seconds,
            "ADMIN_SESSION_TTL_SECONDS": self.admin_session_ttl_seconds,
            "INVITE_CODE_TTL_SECONDS": self.invite_code_ttl_seconds,
            "RATE_LIMIT_WINDOW_SECONDS": self.rate_limit_window_seconds,
            "RATE_LIMIT_INVITE_ATTEMPTS": self.rate_limit_invite_attempts,
            "RATE_LIMIT_ADMIN_LOGIN_ATTEMPTS": self.rate_limit_admin_login_attempts,
            "RATE_LIMIT_CHANNEL_UPDATES": self.rate_limit_channel_updates,
            "RATE_LIMIT_NOTEBOOK_BINDINGS": self.rate_limit_notebook_bindings,
            "RATE_LIMIT_COURSE_ACCOUNT_REAUTH": self.rate_limit_course_account_reauth,
        }
        for name, value in positive_security_values.items():
            if value < 1:
                raise ValueError(f"{name} must be at least 1")

        # Accessing these properties also validates both allowlists.
        self.cors_allowed_origins_list
        self.notebook_host_allowlist_list
        if not self.is_production:
            return
        legacy_password_allowed = (
            self.allow_legacy_11_char_admin_password
            and len(self.admin_password) == 11
            and self.admin_password != "changeme"
        )
        if (
            len(self.admin_password) < 12 and not legacy_password_allowed
        ) or self.admin_password == "changeme":
            raise ValueError("Production requires a non-default ADMIN_PASSWORD")
        webhook = urlparse(self.webhook_base_url)
        if webhook.scheme != "https" or not webhook.netloc:
            raise ValueError("Production WEBHOOK_BASE_URL must be an HTTPS origin")


settings = Settings()
