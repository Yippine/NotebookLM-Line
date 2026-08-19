from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import sqlite3
import sys
from types import SimpleNamespace
import pytest

from config import settings
from database import init_db
from services.course_account_service import (
    AuthorizationPayload,
    CentralNotebookClientFactory,
    CourseAccountRepository,
    CourseAccountService,
    NotebookServiceError,
    classify_notebook_error,
    default_client_context,
)
from services.crypto_service import decrypt_json, reset_encryption_cache


class FakeNotebooks:
    def __init__(self, failure: Exception | None = None):
        self.failure = failure

    async def list(self):
        if self.failure:
            raise self.failure
        return []


class FakeClient:
    def __init__(
        self, failure: Exception | None = None, account_email: str | None = None
    ):
        self.notebooks = FakeNotebooks(failure)
        self.auth = SimpleNamespace(account_email=account_email)


def builder_for(failure_selector=None):
    @asynccontextmanager
    async def builder(auth_payload, _timeout):
        failure = failure_selector(auth_payload) if failure_selector else None
        yield FakeClient(failure, auth_payload.get("_actual_email"))

    return builder


@pytest.fixture(autouse=True)
def fixed_encryption_key(monkeypatch):
    monkeypatch.setattr(
        settings,
        "encryption_key",
        "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    )
    reset_encryption_cache()
    yield
    reset_encryption_cache()


@pytest.mark.anyio
async def test_configure_validates_then_encrypts_authorization(tmp_path):
    path = str(tmp_path / "account.db")
    await init_db(path)
    repository = CourseAccountRepository(path)
    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        timeout_seconds=1,
        context_builder=builder_for(),
    )
    service = CourseAccountService(repository, factory)

    status = await service.configure(
        email="COURSE@example.com",
        auth_payload={"cookies": [{"name": "SID", "value": "secret"}]},
    )

    assert status["email"] == "course@example.com"
    assert status["health_status"] == "healthy"
    assert "auth_encrypted" not in status
    record = await repository.get()
    assert record is not None and record.auth_encrypted
    assert "secret" not in record.auth_encrypted
    assert decrypt_json(record.auth_encrypted)["cookies"][0]["value"] == "secret"


@pytest.mark.anyio
async def test_failed_reauthentication_does_not_replace_working_authorization(tmp_path):
    class AuthError(Exception):
        pass

    path = str(tmp_path / "account.db")
    await init_db(path)
    repository = CourseAccountRepository(path)
    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        timeout_seconds=1,
        context_builder=builder_for(
            lambda auth: (
                AuthError("expired cookie secret") if auth.get("fail") else None
            )
        ),
    )
    service = CourseAccountService(repository, factory)
    old_payload = {"cookies": [{"name": "SID", "value": "old"}]}
    await service.configure(email="old@example.com", auth_payload=old_payload)

    with pytest.raises(NotebookServiceError) as caught:
        await service.configure(
            email="new@example.com",
            auth_payload={"cookies": [{"name": "SID", "value": "new"}], "fail": True},
        )

    assert caught.value.code == "course_auth_expired"
    record = await repository.get()
    assert record is not None
    assert record.email == "old@example.com"
    assert decrypt_json(record.auth_encrypted) == old_payload


@pytest.mark.anyio
async def test_configure_rejects_known_authorization_email_mismatch(tmp_path):
    path = str(tmp_path / "account.db")
    await init_db(path)
    repository = CourseAccountRepository(path)
    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        timeout_seconds=1,
        context_builder=builder_for(),
    )
    service = CourseAccountService(repository, factory)

    with pytest.raises(NotebookServiceError) as caught:
        await service.configure(
            email="expected@example.com",
            auth_payload={
                "cookies": [{"name": "SID", "value": "secret"}],
                "_actual_email": "different@example.com",
            },
        )

    assert caught.value.code == "authorization_email_mismatch"
    assert await repository.get() is not None
    assert (await repository.get()).email is None


@pytest.mark.anyio
async def test_changing_course_email_invalidates_bound_status_but_keeps_mapping(
    tmp_path,
):
    path = str(tmp_path / "account.db")
    await init_db(path)
    repository = CourseAccountRepository(path)
    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        timeout_seconds=1,
        context_builder=builder_for(),
    )
    service = CourseAccountService(repository, factory)
    await service.configure(
        email="old@example.com",
        auth_payload={"cookies": [{"name": "SID", "value": "old"}]},
    )
    with sqlite3.connect(path) as db:
        db.execute(
            """
            INSERT INTO channels (
                channel_id, notebook_id, binding_status, updated_at
            ) VALUES ('channel-a', 'notebook-a', 'bound', 'before-swap')
            """
        )
        db.commit()

    await service.configure(
        email="new@example.com",
        auth_payload={"cookies": [{"name": "SID", "value": "new"}]},
    )

    with sqlite3.connect(path) as db:
        notebook_id, status, updated_at = db.execute(
            "SELECT notebook_id, binding_status, updated_at FROM channels WHERE channel_id='channel-a'"
        ).fetchone()
    assert notebook_id == "notebook-a"
    assert status == "course_account_unavailable"
    assert updated_at != "before-swap"


@pytest.mark.anyio
async def test_stale_health_result_cannot_overwrite_new_authorization(tmp_path):
    path = str(tmp_path / "account.db")
    await init_db(path)
    repository = CourseAccountRepository(path)
    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        timeout_seconds=1,
        context_builder=builder_for(),
    )
    service = CourseAccountService(repository, factory)
    await service.configure(
        email="course@example.com",
        auth_payload={"cookies": [{"name": "SID", "value": "old"}]},
    )
    old_record = await repository.get()
    assert old_record is not None
    await service.configure(
        email="course@example.com",
        auth_payload={"cookies": [{"name": "SID", "value": "new"}]},
    )

    applied = await service.mark_query_failure(
        old_record,
        NotebookServiceError(
            "course_auth_expired", status="course_account_unavailable"
        ),
    )

    current = await repository.get()
    assert applied is False
    assert current is not None
    assert current.health_status == "healthy"
    assert decrypt_json(current.auth_encrypted)["cookies"][0]["value"] == "new"


@pytest.mark.anyio
async def test_same_authorization_revision_accepts_multiple_health_results(tmp_path):
    path = str(tmp_path / "account.db")
    await init_db(path)
    repository = CourseAccountRepository(path)
    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        timeout_seconds=1,
        context_builder=builder_for(),
    )
    service = CourseAccountService(repository, factory)
    await service.configure(
        email="course@example.com",
        auth_payload={"cookies": [{"name": "SID", "value": "current"}]},
    )
    record = await repository.get()
    assert record is not None

    assert await service.mark_query_success(record) is True
    assert await service.mark_query_success(record) is True


@pytest.mark.anyio
async def test_factory_has_bounded_queue_wait_and_safe_metrics():
    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        timeout_seconds=0.02,
        context_builder=builder_for(),
    )
    await factory._semaphore.acquire()
    try:
        with pytest.raises(NotebookServiceError) as caught:
            await factory.run(
                {"cookies": [{}]}, "probe", lambda _client: asyncio.sleep(0)
            )
    finally:
        factory._semaphore.release()

    assert caught.value.code == "query_capacity_exceeded"
    snapshot = factory.metrics.snapshot()
    assert snapshot["counts"]["query_capacity_exceeded"] == 1
    assert "cookies" not in str(snapshot)


@pytest.mark.anyio
async def test_factory_rejects_work_beyond_configured_queue_capacity():
    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        max_queue_size=0,
        timeout_seconds=1,
        context_builder=builder_for(),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_operation(_client):
        started.set()
        await release.wait()

    first = asyncio.create_task(
        factory.run({"cookies": [{}]}, "first", blocking_operation)
    )
    await started.wait()
    try:
        with pytest.raises(NotebookServiceError) as caught:
            await factory.run(
                {"cookies": [{}]}, "overflow", lambda _client: asyncio.sleep(0)
            )
    finally:
        release.set()
        await first

    assert caught.value.code == "query_capacity_exceeded"


@pytest.mark.anyio
async def test_factory_times_out_slow_operation():
    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        timeout_seconds=0.02,
        context_builder=builder_for(),
    )
    with pytest.raises(NotebookServiceError) as caught:
        await factory.run({"cookies": [{}]}, "slow", lambda _client: asyncio.sleep(0.2))
    assert caught.value.code == "upstream_timeout"


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (type("RateLimitError", (Exception,), {})("secret"), "rate_limited"),
        (type("NotebookNotFoundError", (Exception,), {})("secret"), "not_shared"),
        (type("AuthError", (Exception,), {})("secret"), "course_auth_expired"),
        (type("NetworkError", (Exception,), {})("secret"), "upstream_unavailable"),
    ],
)
def test_upstream_errors_are_classified_without_returning_details(exception, code):
    result = classify_notebook_error(exception)
    assert result.code == code
    assert "secret" not in result.user_message


def test_exact_expired_message_and_transient_503_are_distinguished():
    expired = classify_notebook_error(
        RuntimeError("Authentication expired or invalid. Please log in again.")
    )
    transient_type = type("ServerError", (Exception,), {"status_code": 503})
    transient = classify_notebook_error(transient_type("temporary secret"))

    assert expired.code == "course_auth_expired"
    assert transient.code == "upstream_unavailable"


@pytest.mark.anyio
async def test_default_client_context_reads_rotated_cookies_and_removes_temp_file(
    monkeypatch,
):
    captured_path = None
    captured_options = None

    class FakeStorageContext:
        def __init__(self, path):
            self.path = path

        async def __aenter__(self):
            nonlocal captured_path
            captured_path = self.path
            with open(self.path, encoding="utf-8") as handle:
                state = json.load(handle)
            state["cookies"][0]["value"] = "rotated"
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            return FakeClient()

        async def __aexit__(self, *_args):
            return None

    class FakeNotebookLMClient:
        @classmethod
        def from_storage(cls, path, **kwargs):
            nonlocal captured_options
            captured_options = kwargs
            return FakeStorageContext(path)

    monkeypatch.setitem(
        sys.modules,
        "notebooklm",
        SimpleNamespace(NotebookLMClient=FakeNotebookLMClient),
    )
    payload = AuthorizationPayload(
        {"cookies": [{"name": "SID", "value": "original"}], "origins": []}
    )

    async with default_client_context(payload, 1):
        pass

    assert payload["cookies"][0]["value"] == "rotated"
    assert payload.persistence_error is None
    assert captured_path is not None and not os.path.exists(captured_path)
    assert captured_options == {
        "timeout": 1,
        "chat_timeout": settings.notebook_chat_timeout_seconds,
        "max_concurrent_rpcs": 1,
    }


@pytest.mark.anyio
async def test_rotated_authorization_is_encrypted_and_stale_writer_is_discarded(
    tmp_path,
):
    path = str(tmp_path / "account.db")
    await init_db(path)
    repository = CourseAccountRepository(path)
    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        timeout_seconds=1,
        context_builder=builder_for(),
    )
    service = CourseAccountService(repository, factory)
    await service.configure(
        email="course@example.com",
        auth_payload={"cookies": [{"name": "SID", "value": "original"}]},
    )
    record, first = await service.require_authorization()
    _, stale = await service.require_authorization(record)
    first["cookies"][0]["value"] = "first-rotation"
    stale["cookies"][0]["value"] = "stale-rotation"

    assert await service.mark_query_success(record, first) is True
    assert await service.mark_query_success(record, stale) is True

    current = await repository.get()
    assert current is not None
    assert current.auth_revision == record.auth_revision + 1
    assert decrypt_json(current.auth_encrypted)["cookies"][0]["value"] == (
        "first-rotation"
    )
    assert "first-rotation" not in current.auth_encrypted

    restarted_service = CourseAccountService(
        CourseAccountRepository(path),
        CentralNotebookClientFactory(
            max_concurrency=1,
            timeout_seconds=1,
            context_builder=builder_for(),
        ),
    )
    _, restarted_payload = await restarted_service.require_authorization()
    assert restarted_payload["cookies"][0]["value"] == "first-rotation"


@pytest.mark.anyio
async def test_persistence_failure_keeps_answer_success_and_sets_safe_health_error(
    tmp_path,
):
    path = str(tmp_path / "account.db")
    await init_db(path)
    repository = CourseAccountRepository(path)
    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        timeout_seconds=1,
        context_builder=builder_for(),
    )
    service = CourseAccountService(repository, factory)
    await service.configure(
        email="course@example.com",
        auth_payload={"cookies": [{"name": "SID", "value": "original"}]},
    )
    record, payload = await service.require_authorization()
    payload.persistence_error = "auth_persistence_failed"

    assert await service.mark_query_success(record, payload) is True

    current = await repository.get()
    assert current is not None
    assert current.health_status == "error"
    assert current.error_code == "auth_persistence_failed"
    assert decrypt_json(current.auth_encrypted)["cookies"][0]["value"] == "original"
    assert factory.metrics.snapshot()["events"]["auth_persistence_failed"] == 1
