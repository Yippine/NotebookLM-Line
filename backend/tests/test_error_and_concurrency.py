from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from services.course_account_service import (
    CentralNotebookClientFactory,
    NotebookServiceError,
    classify_notebook_error,
)


class _StatusError(RuntimeError):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize(
    ("error", "existing_binding", "code", "status", "http_status"),
    [
        (asyncio.TimeoutError(), False, "upstream_timeout", "error", 504),
        (_StatusError(429), False, "rate_limited", "error", 429),
        (
            _StatusError(401),
            False,
            "course_auth_expired",
            "course_account_unavailable",
            503,
        ),
        (_StatusError(403, "permission denied"), False, "not_shared", "error", 403),
        (_StatusError(404), True, "access_revoked", "access_revoked", 403),
        (_StatusError(503), True, "upstream_unavailable", "error", 503),
        (
            RuntimeError("unclassified internal details"),
            False,
            "unknown_upstream_error",
            "error",
            502,
        ),
    ],
)
def test_notebook_error_classification_is_stable_and_safe(
    error: BaseException,
    existing_binding: bool,
    code: str,
    status: str,
    http_status: int,
) -> None:
    classified = classify_notebook_error(error, existing_binding=existing_binding)
    assert classified.code == code
    assert classified.status == status
    assert classified.http_status == http_status
    assert "internal details" not in classified.user_message


@pytest.mark.anyio
async def test_client_factory_serializes_queries_to_configured_concurrency() -> None:
    active = 0
    maximum_active = 0

    @asynccontextmanager
    async def context_builder(_auth, _timeout):
        yield SimpleNamespace()

    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        timeout_seconds=1,
        context_builder=context_builder,
    )

    async def operation(_client):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return "ok"

    results = await asyncio.gather(
        factory.run({}, "channel-a", operation),
        factory.run({}, "channel-b", operation),
        factory.run({}, "channel-c", operation),
    )
    assert results == ["ok", "ok", "ok"]
    assert maximum_active == 1
    assert factory.metrics.snapshot()["counts"] == {"success": 3}


@pytest.mark.anyio
async def test_client_factory_bounds_queue_wait_and_returns_classified_rate_limit() -> (
    None
):
    @asynccontextmanager
    async def context_builder(_auth, _timeout):
        yield SimpleNamespace()

    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        timeout_seconds=0.02,
        context_builder=context_builder,
    )
    # Hold the single permit so the tested operation can only wait in queue.
    await factory._semaphore.acquire()
    try:
        with pytest.raises(NotebookServiceError) as failure:
            await factory.run({}, "saturated", lambda _client: asyncio.sleep(0))
    finally:
        factory._semaphore.release()

    assert failure.value.code == "query_capacity_exceeded"
    assert failure.value.http_status == 429
    assert factory.metrics.snapshot()["counts"]["query_capacity_exceeded"] == 1
