from __future__ import annotations

import pytest

from config import Settings


def _production_settings(**overrides) -> Settings:
    values = {
        "environment": "production",
        "admin_password": "a-long-unique-admin-password",
        "webhook_base_url": "https://ai-notebook-v2.example.com",
        "cors_allowed_origins": "https://ai-notebook-v2.example.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_environment_wins_over_stale_app_env_alias() -> None:
    configured = _production_settings(app_env="development")

    assert configured.is_production is True
    configured.validate_runtime()


@pytest.mark.parametrize("environment", ["prd", "stage", "prodution"])
def test_unknown_or_malformed_environment_fails_closed(environment: str) -> None:
    configured = Settings(_env_file=None, environment=environment)

    with pytest.raises(ValueError, match="ENVIRONMENT"):
        configured.validate_runtime()


@pytest.mark.parametrize("password", ["", "changeme", "too-short"])
def test_production_rejects_unsafe_admin_password(password: str) -> None:
    configured = _production_settings(admin_password=password)

    with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
        configured.validate_runtime()


def test_explicit_legacy_compatibility_accepts_only_an_11_character_password() -> None:
    _production_settings(
        admin_password="legacy-pass",
        allow_legacy_11_char_admin_password=True,
    ).validate_runtime()

    with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
        _production_settings(
            admin_password="too-short",
            allow_legacy_11_char_admin_password=True,
        ).validate_runtime()


def test_production_rejects_non_https_webhook_origin() -> None:
    configured = _production_settings(webhook_base_url="http://localhost:8000")

    with pytest.raises(ValueError, match="WEBHOOK_BASE_URL"):
        configured.validate_runtime()


def test_production_rejects_disabled_security_rate_limit() -> None:
    configured = _production_settings(rate_limit_admin_login_attempts=0)

    with pytest.raises(ValueError, match="RATE_LIMIT_ADMIN_LOGIN_ATTEMPTS"):
        configured.validate_runtime()


def test_event_claim_lease_must_outlive_notebook_query_timeout() -> None:
    configured = _production_settings(
        notebook_chat_timeout_seconds=60,
        notebook_query_timeout_seconds=90,
        line_event_claim_timeout_seconds=329,
    )

    with pytest.raises(ValueError, match="LINE_EVENT_CLAIM_TIMEOUT_SECONDS"):
        configured.validate_runtime()

    _production_settings(
        notebook_chat_timeout_seconds=60,
        notebook_query_timeout_seconds=90,
        line_event_claim_timeout_seconds=330,
    ).validate_runtime()


def test_query_timeout_keeps_lifecycle_margin_after_chat_timeout() -> None:
    configured = _production_settings(
        notebook_chat_timeout_seconds=180,
        notebook_query_timeout_seconds=209,
    )

    with pytest.raises(ValueError, match="NOTEBOOK_QUERY_TIMEOUT_SECONDS"):
        configured.validate_runtime()

    _production_settings(
        notebook_chat_timeout_seconds=180,
        notebook_query_timeout_seconds=210,
        line_event_claim_timeout_seconds=720,
    ).validate_runtime()


def test_default_timeout_budget_supports_slow_shared_notebook_chat() -> None:
    configured = Settings(_env_file=None)

    assert configured.notebook_chat_timeout_seconds == 180
    assert configured.notebook_query_timeout_seconds == 210
    assert configured.line_event_claim_timeout_seconds == 720
    configured.validate_runtime()


def test_event_pending_limit_must_be_positive() -> None:
    configured = _production_settings(line_event_pending_limit=0)

    with pytest.raises(ValueError, match="LINE_EVENT_PENDING_LIMIT"):
        configured.validate_runtime()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"line_webhook_max_body_bytes": 0}, "LINE_WEBHOOK_MAX_BODY_BYTES"),
        (
            {"line_webhook_max_events_per_request": 0},
            "LINE_WEBHOOK_MAX_EVENTS_PER_REQUEST",
        ),
        (
            {"line_event_pending_per_channel_limit": 0},
            "LINE_EVENT_PENDING_PER_CHANNEL_LIMIT",
        ),
        (
            {"line_event_replay_per_channel_batch": 0},
            "LINE_EVENT_REPLAY_PER_CHANNEL_BATCH",
        ),
        (
            {
                "line_event_pending_limit": 10,
                "line_event_pending_per_channel_limit": 11,
            },
            "LINE_EVENT_PENDING_PER_CHANNEL_LIMIT",
        ),
        (
            {"line_event_pending_retention_seconds": 86_401},
            "LINE_EVENT_PENDING_RETENTION_SECONDS",
        ),
    ],
)
def test_webhook_resource_limits_are_validated(
    overrides: dict[str, int],
    message: str,
) -> None:
    configured = _production_settings(**overrides)

    with pytest.raises(ValueError, match=message):
        configured.validate_runtime()


def test_background_shutdown_timeout_cannot_be_negative() -> None:
    configured = _production_settings(line_background_shutdown_timeout_seconds=-1)

    with pytest.raises(ValueError, match="LINE_BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS"):
        configured.validate_runtime()
