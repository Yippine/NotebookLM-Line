from __future__ import annotations

import io
import json
import logging

import pytest

from services.channel_credentials import (
    read_channel_access_token,
    read_channel_secret,
)
from services.logging_service import SensitiveDataFilter, sanitize_for_log
from services.rate_limit_service import InMemoryRateLimiter


def test_legacy_line_credentials_are_trimmed_when_read() -> None:
    row = {
        "channel_secret_encrypted": None,
        "channel_secret": "  line-secret\n",
        "channel_access_token_encrypted": None,
        "channel_access_token": "\tline-token ",
    }

    assert read_channel_secret(row) == "line-secret"
    assert read_channel_access_token(row) == "line-token"


def test_sensitive_logging_filter_redacts_supported_secret_shapes():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SensitiveDataFilter())
    logger = logging.getLogger("security-filter-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info(
        "url=/api?token=query-secret Authorization=Bearer bearer-secret "
        'password="admin-secret" channel_secret=line-secret '
        "https://notebooklm.google.com/notebook/private-notebook-id"
    )
    output = stream.getvalue()
    for secret in (
        "query-secret",
        "bearer-secret",
        "admin-secret",
        "line-secret",
        "private-notebook-id",
    ):
        assert secret not in output
    assert "<redacted>" in output


def test_invite_verification_request_json_is_redacted_without_hiding_error_codes():
    invite_secret = "student-invite-B5Dn_2zQ"
    exchange = {
        "request": {
            "method": "POST",
            "path": "/api/verify-invite",
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"code": invite_secret}),
        },
        "response": {
            "status_code": 400,
            "body": {
                "error_code": "invite_denied",
                "detail": {"code": "invalid_invite", "message": "邀請碼無效"},
            },
        },
    }

    sanitized = sanitize_for_log(json.dumps(exchange, ensure_ascii=False))
    sanitized_exchange = json.loads(sanitized)
    sanitized_request_body = json.loads(sanitized_exchange["request"]["body"])

    assert invite_secret not in sanitized
    assert sanitized_request_body["code"] == "<redacted>"
    assert sanitized_exchange["response"]["status_code"] == 400
    assert sanitized_exchange["response"]["body"]["error_code"] == "invite_denied"
    assert sanitized_exchange["response"]["body"]["detail"]["code"] == "invalid_invite"


@pytest.mark.anyio
async def test_rate_limiter_returns_retry_after_without_running_work():
    now = [100.0]
    limiter = InMemoryRateLimiter(clock=lambda: now[0])
    assert (await limiter.check("login", "ip", limit=2, window_seconds=60)).allowed
    assert (await limiter.check("login", "ip", limit=2, window_seconds=60)).allowed
    blocked = await limiter.check("login", "ip", limit=2, window_seconds=60)
    assert not blocked.allowed
    assert blocked.retry_after_seconds == 60
    now[0] += 61
    assert (await limiter.check("login", "ip", limit=2, window_seconds=60)).allowed


@pytest.mark.anyio
async def test_rate_limiter_enforces_hard_bucket_capacity():
    limiter = InMemoryRateLimiter(clock=lambda: 100.0, max_buckets=2)

    assert (
        await limiter.check("login", "subject-1", limit=5, window_seconds=60)
    ).allowed
    assert (
        await limiter.check("login", "subject-2", limit=5, window_seconds=60)
    ).allowed
    overflow = await limiter.check("login", "subject-3", limit=5, window_seconds=60)

    assert overflow.allowed is False
    assert len(limiter._events) == 2
