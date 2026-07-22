from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
import uuid

import httpx
import pytest

from routers import webhook as webhook_router
from services.crypto_service import encrypt_text


def _insert_webhook_channel(
    db_path: Path, channel_id: str, secret: str, token: str
) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            INSERT INTO channels (
                channel_id, channel_secret_encrypted,
                channel_access_token_encrypted, binding_status
            ) VALUES (?, ?, ?, 'bound')
            """,
            (channel_id, encrypt_text(secret), encrypt_text(token)),
        )
        db.commit()


def _line_event(question: str = "slow question", event_id: str | None = None) -> bytes:
    event = {
        "type": "message",
        "replyToken": "short-lived-reply-token",
        "source": {"userId": "line-user-a"},
        "message": {"type": "text", "text": question},
    }
    if event_id:
        event["webhookEventId"] = event_id
    return json.dumps(
        {"events": [event]},
        separators=(",", ":"),
    ).encode("utf-8")


async def _wait_for_event_workers() -> None:
    """Wait for the currently scheduled webhook workers without polling DB."""

    for _ in range(10):
        active = [task for task in webhook_router._background_tasks if not task.done()]
        if not active:
            return
        await asyncio.wait_for(
            asyncio.gather(*active, return_exceptions=True),
            timeout=2,
        )
        await asyncio.sleep(0)
    raise AssertionError("webhook workers did not drain")


@pytest.mark.anyio
async def test_invalid_line_signature_never_starts_notebook_or_push_work(
    backend_db: Path,
    asgi_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_webhook_channel(backend_db, "channel-a", "line-secret", "line-access-token")
    background_calls: list[tuple] = []

    async def should_not_run(*args):
        background_calls.append(args)

    monkeypatch.setattr(webhook_router, "_load_ask_and_push", should_not_run)
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/webhook/channel-a",
            content=_line_event(),
            headers={"X-Line-Signature": "invalid-signature"},
        )

    await asyncio.sleep(0)
    assert response.status_code == 403
    assert background_calls == []


@pytest.mark.anyio
async def test_invalid_signature_is_checked_before_access_token_decryption(
    backend_db: Path,
    asgi_app,
) -> None:
    _insert_webhook_channel(backend_db, "channel-a", "line-secret", "line-token")
    with sqlite3.connect(backend_db) as db:
        db.execute(
            """
            UPDATE channels SET channel_access_token_encrypted='corrupt-ciphertext'
            WHERE channel_id='channel-a'
            """
        )
        db.commit()

    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/webhook/channel-a",
            content=_line_event(),
            headers={"X-Line-Signature": "invalid-signature"},
        )

    assert response.status_code == 403


@pytest.mark.anyio
async def test_slow_notebook_work_does_not_delay_webhook_2xx(
    backend_db: Path,
    asgi_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "line-secret"
    _insert_webhook_channel(backend_db, "channel-a", secret, "line-access-token")
    body = _line_event()
    signature = (
        __import__("base64")
        .b64encode(
            __import__("hmac")
            .new(secret.encode(), body, __import__("hashlib").sha256)
            .digest()
        )
        .decode()
    )
    started = asyncio.Event()
    release = asyncio.Event()
    tracked_tasks: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    async def slow_background(*_args):
        started.set()
        await release.wait()

    def track_task(coro, *, name=None):
        task = real_create_task(coro, name=name)
        tracked_tasks.append(task)
        return task

    monkeypatch.setattr(webhook_router, "_load_ask_and_push", slow_background)
    monkeypatch.setattr(webhook_router.asyncio, "create_task", track_task)
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        began = time.monotonic()
        response = await client.post(
            "/webhook/channel-a",
            content=body,
            headers={"X-Line-Signature": signature},
        )
        elapsed = time.monotonic() - began

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert elapsed < 0.2
    await asyncio.wait_for(started.wait(), timeout=0.2)
    assert tracked_tasks and not tracked_tasks[0].done()
    release.set()
    await asyncio.gather(*tracked_tasks)


@pytest.mark.anyio
async def test_redelivered_line_event_is_scheduled_only_once(
    backend_db: Path,
    asgi_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "line-secret"
    _insert_webhook_channel(backend_db, "channel-a", secret, "line-access-token")
    body = _line_event(event_id="event-unique-123")
    signature = (
        __import__("base64")
        .b64encode(
            __import__("hmac")
            .new(secret.encode(), body, __import__("hashlib").sha256)
            .digest()
        )
        .decode()
    )
    scheduled: list[str] = []

    def schedule(_channel, _user, _reply, _access, question, _event_id=None):
        scheduled.append(question)
        return True

    monkeypatch.setattr(webhook_router, "_schedule_question", schedule)
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        first = await client.post(
            "/webhook/channel-a",
            content=body,
            headers={"X-Line-Signature": signature},
        )
        redelivery = await client.post(
            "/webhook/channel-a",
            content=body,
            headers={"X-Line-Signature": signature},
        )

    assert first.status_code == 200
    assert redelivery.status_code == 200
    assert scheduled == ["slow question"]


@pytest.mark.anyio
async def test_background_answer_uses_push_without_requiring_reply_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies: list[str] = []
    pushes: list[str] = []

    async def no_loading(*_args, **_kwargs):
        return False

    async def reply(_reply_token, _access_token, text):
        replies.append(text)

    async def ask(_channel_id, _user_id, _question):
        await asyncio.sleep(0.01)
        return ["answer one", "answer two"]

    async def push(_user_id, _access_token, text):
        pushes.append(text)

    monkeypatch.setattr(webhook_router, "show_loading", no_loading)
    monkeypatch.setattr(webhook_router, "reply_text", reply)
    monkeypatch.setattr(webhook_router, "ask_question", ask)
    monkeypatch.setattr(webhook_router, "push_text", push)

    await webhook_router._load_ask_and_push(
        "channel-a",
        "line-user-a",
        "",  # An absent/expired Reply Token is not needed for the answer.
        "line-access-token",
        "question",
    )
    assert replies == []
    assert pushes == ["answer one", "answer two"]


@pytest.mark.anyio
async def test_expired_progress_reply_does_not_block_background_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pushes: list[str] = []

    async def no_loading(*_args, **_kwargs):
        return False

    async def expired_reply(*_args, **_kwargs):
        raise httpx.HTTPStatusError(
            "expired",
            request=httpx.Request("POST", "https://api.line.me/reply"),
            response=httpx.Response(400),
        )

    async def ask(*_args):
        return ["answer"]

    async def push(_user_id, _access_token, text):
        pushes.append(text)

    monkeypatch.setattr(webhook_router, "show_loading", no_loading)
    monkeypatch.setattr(webhook_router, "reply_text", expired_reply)
    monkeypatch.setattr(webhook_router, "ask_question", ask)
    monkeypatch.setattr(webhook_router, "push_text", push)

    await webhook_router._load_ask_and_push(
        "channel-a",
        "line-user-a",
        "expired-reply-token",
        "line-access-token",
        "question",
    )

    assert pushes == ["answer"]


@pytest.mark.anyio
async def test_loading_network_error_does_not_block_background_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pushes: list[str] = []

    async def loading_failure(*_args, **_kwargs):
        raise httpx.ConnectError("network unavailable")

    async def ask(*_args):
        return ["answer after loading failure"]

    async def push(_user_id, _access_token, text):
        pushes.append(text)

    monkeypatch.setattr(webhook_router, "show_loading", loading_failure)
    monkeypatch.setattr(webhook_router, "ask_question", ask)
    monkeypatch.setattr(webhook_router, "push_text", push)

    await webhook_router._load_ask_and_push(
        "channel-a", "line-user-a", "", "line-access-token", "question"
    )

    assert pushes == ["answer after loading failure"]


@pytest.mark.anyio
async def test_loading_is_renewed_after_fifty_seconds_until_answer_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits = 0
    loading_seconds: list[int] = []
    stop = asyncio.Event()

    async def advance_once(awaitable, *, timeout):
        nonlocal waits
        assert timeout == 50
        awaitable.close()
        waits += 1
        if waits == 1:
            raise TimeoutError
        return None

    async def loading_ok(_user_id, _access_token, *, seconds):
        loading_seconds.append(seconds)
        stop.set()
        return True

    monkeypatch.setattr(webhook_router.asyncio, "wait_for", advance_once)
    monkeypatch.setattr(webhook_router, "show_loading", loading_ok)

    await webhook_router._renew_loading_until_stopped(
        "channel-a", "line-user-a", "access-token", stop
    )

    assert loading_seconds == [60]


@pytest.mark.anyio
async def test_background_task_admission_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = asyncio.current_task()
    assert current is not None
    webhook_router._background_tasks.add(current)
    monkeypatch.setattr(webhook_router.settings, "line_background_task_limit", 1)
    try:
        accepted = webhook_router._schedule_question(
            "channel-a", "line-user-a", "reply", "access", "question"
        )
    finally:
        webhook_router._background_tasks.discard(current)

    assert accepted is False


@pytest.mark.anyio
async def test_event_claim_is_pending_until_delivery_then_completed(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = await webhook_router._claim_event("event-lifecycle-success")
    assert claim is not None
    assert await webhook_router._claim_event("event-lifecycle-success") is None

    async def delivered(*_args):
        return True

    monkeypatch.setattr(webhook_router, "_load_ask_and_push", delivered)
    await webhook_router._run_question(
        "channel-a", "user-a", "reply", "access", "question", claim
    )

    with sqlite3.connect(backend_db) as db:
        row = db.execute(
            """
            SELECT status, claimed_at, completed_at, attempt_count, claim_token
            FROM line_webhook_events WHERE event_id='event-lifecycle-success'
            """
        ).fetchone()
    assert row[0] == "completed"
    assert row[1] is None
    assert row[2] is not None
    assert row[3] == 1
    assert row[4] is None
    assert await webhook_router._claim_event("event-lifecycle-success") is None


@pytest.mark.anyio
async def test_failed_delivery_releases_claim_for_immediate_redelivery(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_claim = await webhook_router._claim_event("event-lifecycle-failure")
    assert first_claim is not None

    async def not_delivered(*_args):
        return False

    monkeypatch.setattr(webhook_router, "_load_ask_and_push", not_delivered)
    await webhook_router._run_question(
        "channel-a", "user-a", "reply", "access", "question", first_claim
    )

    with sqlite3.connect(backend_db) as db:
        pending = db.execute(
            """
            SELECT status, claimed_at, completed_at, claim_token
            FROM line_webhook_events WHERE event_id='event-lifecycle-failure'
            """
        ).fetchone()
    assert pending == ("pending", None, None, None)

    retry_claim = await webhook_router._claim_event("event-lifecycle-failure")
    assert retry_claim is not None
    assert retry_claim.claim_token != first_claim.claim_token
    with sqlite3.connect(backend_db) as db:
        attempts = db.execute(
            """
            SELECT attempt_count FROM line_webhook_events
            WHERE event_id='event-lifecycle-failure'
            """
        ).fetchone()[0]
    assert attempts == 2


@pytest.mark.anyio
async def test_error_notification_does_not_mark_missing_answer_completed(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = await webhook_router._claim_event("event-answer-failed")
    assert claim is not None
    pushed: list[str] = []

    async def loading_ok(*_args, **_kwargs):
        return True

    async def ask_failed(*_args):
        raise RuntimeError("upstream unavailable")

    async def push(_user, _token, text):
        pushed.append(text)

    monkeypatch.setattr(webhook_router, "show_loading", loading_ok)
    monkeypatch.setattr(webhook_router, "ask_question", ask_failed)
    monkeypatch.setattr(webhook_router, "push_text", push)
    await webhook_router._run_question(
        "channel-a", "user-a", "reply", "access", "question", claim
    )

    assert pushed == ["⚠️ 系統發生錯誤，請稍後再試。"]
    with sqlite3.connect(backend_db) as db:
        row = db.execute(
            """
            SELECT status, claimed_at, claim_token FROM line_webhook_events
            WHERE event_id='event-answer-failed'
            """
        ).fetchone()
    assert row == ("pending", None, None)


@pytest.mark.anyio
async def test_stale_pending_claim_can_be_reclaimed_without_old_worker_winning(
    backend_db: Path,
) -> None:
    first_claim = await webhook_router._claim_event("event-stale-pending")
    assert first_claim is not None
    with sqlite3.connect(backend_db) as db:
        db.execute(
            """
            UPDATE line_webhook_events SET claimed_at='2000-01-01T00:00:00+00:00'
            WHERE event_id='event-stale-pending'
            """
        )
        db.commit()

    second_claim = await webhook_router._claim_event("event-stale-pending")
    assert second_claim is not None
    assert second_claim.claim_token != first_claim.claim_token
    # A late first worker cannot complete or release the newer lease.
    assert await webhook_router._complete_event(first_claim) is False
    assert await webhook_router._release_event(first_claim) is False
    assert await webhook_router._complete_event(second_claim) is True

    with sqlite3.connect(backend_db) as db:
        row = db.execute(
            """
            SELECT status, attempt_count FROM line_webhook_events
            WHERE event_id='event-stale-pending'
            """
        ).fetchone()
    assert row == ("completed", 2)


@pytest.mark.anyio
async def test_startup_releases_claim_left_by_previous_process(
    backend_db: Path,
) -> None:
    old_claim = await webhook_router._claim_event("event-before-restart")
    assert old_claim is not None

    released = await webhook_router.recover_pending_event_claims()
    assert released == 1
    new_claim = await webhook_router._claim_event("event-before-restart")
    assert new_claim is not None
    assert new_claim.claim_token != old_claim.claim_token
    # A late callback retaining the old token cannot affect this new lease.
    assert await webhook_router._complete_event(old_claim) is False

    with sqlite3.connect(backend_db) as db:
        row = db.execute(
            """
            SELECT status, attempt_count, claim_token FROM line_webhook_events
            WHERE event_id='event-before-restart'
            """
        ).fetchone()
    assert row == ("pending", 2, new_claim.claim_token)


@pytest.mark.anyio
async def test_cancelled_worker_releases_pending_claim(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_webhook_channel(backend_db, "channel-a", "secret", "access")
    claim = await webhook_router._claim_event(
        "event-cancelled",
        webhook_router._event_payload_ciphertext("channel-a", "user-a", "question"),
    )
    assert claim is not None
    started = asyncio.Event()

    async def blocked(*_args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(webhook_router, "_load_ask_and_push", blocked)
    task = asyncio.create_task(webhook_router._run_claimed_event(claim, "reply"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with sqlite3.connect(backend_db) as db:
        row = db.execute(
            """
            SELECT status, claimed_at, claim_token FROM line_webhook_events
            WHERE event_id='event-cancelled'
            """
        ).fetchone()
    assert row == ("pending", None, None)
    assert (
        await webhook_router._claim_event(
            "event-cancelled",
            event_tenant_key=webhook_router._tenant_key("channel-a"),
        )
        is not None
    )


@pytest.mark.anyio
async def test_shutdown_cancels_slow_worker_and_releases_claim(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webhook_router.start_background_processing()
    _insert_webhook_channel(backend_db, "channel-a", "secret", "access")
    claim = await webhook_router._claim_event(
        "event-shutdown",
        webhook_router._event_payload_ciphertext("channel-a", "user-a", "question"),
    )
    assert claim is not None
    started = asyncio.Event()

    async def blocked(*_args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(webhook_router, "_load_ask_and_push", blocked)
    accepted = webhook_router._schedule_question(
        "channel-a", "user-a", "reply", "access", "question", claim
    )
    assert accepted is True
    await started.wait()

    result = await webhook_router.drain_background_tasks(0.01)
    assert result == {"completed": 0, "cancelled": 1}
    with sqlite3.connect(backend_db) as db:
        row = db.execute(
            """
            SELECT status, claimed_at, claim_token FROM line_webhook_events
            WHERE event_id='event-shutdown'
            """
        ).fetchone()
    assert row == ("pending", None, None)
    assert (
        await webhook_router._claim_event(
            "event-shutdown",
            event_tenant_key=webhook_router._tenant_key("channel-a"),
        )
        is not None
    )
    webhook_router.start_background_processing()


@pytest.mark.anyio
async def test_shutdown_timeout_is_a_hard_bound_for_cancellation_resistant_task() -> (
    None
):
    webhook_router.start_background_processing()
    release = asyncio.Event()

    async def cancellation_resistant() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(cancellation_resistant())
    webhook_router._background_tasks.add(task)
    task.add_done_callback(webhook_router._background_task_finished)
    started = asyncio.get_running_loop().time()

    result = await webhook_router.drain_background_tasks(0.05)

    assert asyncio.get_running_loop().time() - started < 0.15
    assert result == {"completed": 0, "cancelled": 1}
    release.set()
    await task
    webhook_router.start_background_processing()


@pytest.mark.anyio
async def test_crash_recovery_replays_encrypted_payload_with_current_channel_token(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    question = "private question that must never be stored as plaintext 93847"
    _insert_webhook_channel(backend_db, "channel-a", "secret", "old-token")
    old_claim = await webhook_router._claim_event(
        "event-crash-replay",
        webhook_router._event_payload_ciphertext("channel-a", "line-user-a", question),
    )
    assert old_claim is not None
    with sqlite3.connect(backend_db) as db:
        stored_payload = db.execute(
            """
            SELECT payload_encrypted FROM line_webhook_events
            WHERE event_id='event-crash-replay'
            """
        ).fetchone()[0]
    assert stored_payload
    assert question not in stored_payload
    assert question.encode("utf-8") not in backend_db.read_bytes()

    # Simulate a credential rotation and process crash after admission but
    # before the in-memory task could finish.
    with sqlite3.connect(backend_db) as db:
        db.execute(
            """
            UPDATE channels SET channel_access_token_encrypted=?
            WHERE channel_id='channel-a'
            """,
            (encrypt_text("rotated-token"),),
        )
        db.commit()
    assert await webhook_router.recover_pending_event_claims() == 1

    asked: list[tuple[str, str, str]] = []
    pushed: list[tuple[str, str, str]] = []

    async def loading_ok(*_args, **_kwargs):
        return True

    async def ask(channel_id, user_id, text):
        asked.append((channel_id, user_id, text))
        return ["durably replayed answer"]

    async def push(user_id, access_token, text, **_kwargs):
        pushed.append((user_id, access_token, text))

    monkeypatch.setattr(webhook_router, "show_loading", loading_ok)
    monkeypatch.setattr(webhook_router, "ask_question", ask)
    monkeypatch.setattr(webhook_router, "push_text", push)
    webhook_router.start_background_processing()
    assert await webhook_router.replay_due_pending_events() == 1
    await _wait_for_event_workers()

    assert asked == [("channel-a", "line-user-a", question)]
    assert pushed == [("line-user-a", "rotated-token", "durably replayed answer")]
    with sqlite3.connect(backend_db) as db:
        row = db.execute(
            """
            SELECT status, attempt_count, payload_encrypted, payload_version,
                   failed_at, claim_token
            FROM line_webhook_events WHERE event_id='event-crash-replay'
            """
        ).fetchone()
    assert row == ("completed", 2, None, None, None, None)
    assert question not in caplog.text
    assert question.encode("utf-8") not in backend_db.read_bytes()


@pytest.mark.anyio
async def test_failed_durable_delivery_is_retried_internally_then_scrubbed(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_webhook_channel(backend_db, "channel-a", "secret", "token")
    claim = await webhook_router._claim_event(
        "event-internal-retry",
        webhook_router._event_payload_ciphertext(
            "channel-a", "line-user-a", "retry me"
        ),
    )
    assert claim is not None
    outcomes = iter((False, True))
    attempts: list[str] = []

    async def deliver(*_args):
        attempts.append("attempt")
        return next(outcomes)

    monkeypatch.setattr(webhook_router, "_load_ask_and_push", deliver)
    monkeypatch.setattr(webhook_router.settings, "line_event_retry_base_seconds", 0.01)
    webhook_router.start_background_processing()
    assert webhook_router._schedule_claimed_event(claim, channel_id_hint="channel-a")
    await _wait_for_event_workers()

    with sqlite3.connect(backend_db) as db:
        pending = db.execute(
            """
            SELECT status, failure_count, payload_encrypted, next_attempt_at
            FROM line_webhook_events WHERE event_id='event-internal-retry'
            """
        ).fetchone()
        db.execute(
            """
            UPDATE line_webhook_events
            SET next_attempt_at='2000-01-01T00:00:00+00:00'
            WHERE event_id='event-internal-retry'
            """
        )
        db.commit()
    assert pending[0] == "pending"
    assert pending[1] == 1
    assert pending[2]
    assert pending[3]

    assert await webhook_router.replay_due_pending_events() == 1
    await _wait_for_event_workers()
    with sqlite3.connect(backend_db) as db:
        completed = db.execute(
            """
            SELECT status, attempt_count, failure_count, payload_encrypted,
                   next_attempt_at, failed_at
            FROM line_webhook_events WHERE event_id='event-internal-retry'
            """
        ).fetchone()
    assert attempts == ["attempt", "attempt"]
    assert completed == ("completed", 2, 1, None, None, None)


@pytest.mark.anyio
async def test_invalid_encrypted_payload_is_terminal_and_scrubbed(
    backend_db: Path,
) -> None:
    with sqlite3.connect(backend_db) as db:
        db.execute(
            """
            INSERT INTO line_webhook_events (
                event_id, status, received_at, attempt_count,
                payload_encrypted, payload_version
            ) VALUES (?, 'pending', ?, 0, 'corrupt-ciphertext', 1)
            """,
            (
                "event-corrupt-payload",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()

    webhook_router.start_background_processing()
    assert await webhook_router.replay_due_pending_events() == 0
    await _wait_for_event_workers()
    with sqlite3.connect(backend_db) as db:
        row = db.execute(
            """
            SELECT status, failed_at, last_error_code, payload_encrypted,
                   payload_version, claim_token
            FROM line_webhook_events WHERE event_id='event-corrupt-payload'
            """
        ).fetchone()
    assert row[0] == "completed"
    assert row[1] is not None
    assert row[2:] == ("invalid_event_payload", None, None, None)


@pytest.mark.anyio
async def test_durable_retries_stop_at_configured_attempt_limit(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_webhook_channel(backend_db, "channel-a", "secret", "token")
    monkeypatch.setattr(webhook_router.settings, "line_event_max_attempts", 2)
    claim = await webhook_router._claim_event(
        "event-max-attempts",
        webhook_router._event_payload_ciphertext(
            "channel-a", "line-user-a", "bounded retries"
        ),
    )
    assert claim is not None
    calls = 0

    async def fail_delivery(*_args):
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(webhook_router, "_load_ask_and_push", fail_delivery)
    webhook_router.start_background_processing()
    assert webhook_router._schedule_claimed_event(claim)
    await _wait_for_event_workers()
    with sqlite3.connect(backend_db) as db:
        db.execute(
            """
            UPDATE line_webhook_events
            SET next_attempt_at='2000-01-01T00:00:00+00:00'
            WHERE event_id='event-max-attempts'
            """
        )
        db.commit()

    assert await webhook_router.replay_due_pending_events() == 1
    await _wait_for_event_workers()
    assert await webhook_router.replay_due_pending_events() == 0
    with sqlite3.connect(backend_db) as db:
        row = db.execute(
            """
            SELECT status, attempt_count, failure_count, failed_at,
                   last_error_code, payload_encrypted
            FROM line_webhook_events WHERE event_id='event-max-attempts'
            """
        ).fetchone()
    assert calls == 2
    assert row[:3] == ("completed", 2, 2)
    assert row[3] is not None
    assert row[4:] == ("answer_delivery_failed", None)


@pytest.mark.anyio
async def test_replay_does_not_reclaim_a_fresh_live_worker_lease(
    backend_db: Path,
) -> None:
    claim = await webhook_router._claim_event(
        "event-live-lease",
        webhook_router._event_payload_ciphertext(
            "channel-a", "line-user-a", "still running"
        ),
    )
    assert claim is not None
    webhook_router.start_background_processing()

    assert await webhook_router.replay_due_pending_events() == 0
    with sqlite3.connect(backend_db) as db:
        row = db.execute(
            """
            SELECT status, attempt_count, claim_token
            FROM line_webhook_events WHERE event_id='event-live-lease'
            """
        ).fetchone()
    assert row == ("pending", 1, claim.claim_token)


@pytest.mark.anyio
async def test_pending_ledger_limit_is_atomic_and_returns_503_for_new_event(
    backend_db: Path,
    asgi_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "line-secret"
    _insert_webhook_channel(backend_db, "channel-a", secret, "token")
    monkeypatch.setattr(webhook_router.settings, "line_event_pending_limit", 1)
    monkeypatch.setattr(webhook_router, "_schedule_question", lambda *_args: False)

    def signed(body: bytes) -> str:
        return (
            __import__("base64")
            .b64encode(
                __import__("hmac")
                .new(secret.encode(), body, __import__("hashlib").sha256)
                .digest()
            )
            .decode()
        )

    first_body = _line_event("first durable event", "event-ledger-first")
    second_body = _line_event("second durable event", "event-ledger-second")
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        first = await client.post(
            "/webhook/channel-a",
            content=first_body,
            headers={"X-Line-Signature": signed(first_body)},
        )
        second = await client.post(
            "/webhook/channel-a",
            content=second_body,
            headers={"X-Line-Signature": signed(second_body)},
        )

    assert first.status_code == 200
    assert second.status_code == 503
    with sqlite3.connect(backend_db) as db:
        rows = db.execute(
            "SELECT event_id, payload_encrypted FROM line_webhook_events"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "event-ledger-first"
    assert rows[0][1]


@pytest.mark.anyio
async def test_oversized_question_is_rejected_before_durable_storage(
    backend_db: Path,
    asgi_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "line-secret"
    _insert_webhook_channel(backend_db, "channel-a", secret, "token")
    scheduled: list[tuple] = []
    monkeypatch.setattr(
        webhook_router,
        "_schedule_question",
        lambda *args: scheduled.append(args) or True,
    )
    body = _line_event("q" * 10_001, "event-too-large")
    signature = (
        __import__("base64")
        .b64encode(
            __import__("hmac")
            .new(secret.encode(), body, __import__("hashlib").sha256)
            .digest()
        )
        .decode()
    )
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/webhook/channel-a",
            content=body,
            headers={"X-Line-Signature": signature},
        )

    assert response.status_code == 200
    assert scheduled == []
    with sqlite3.connect(backend_db) as db:
        assert db.execute("SELECT COUNT(*) FROM line_webhook_events").fetchone()[0] == 0


@pytest.mark.anyio
async def test_retention_cleanup_deletes_terminal_failed_events(
    backend_db: Path,
) -> None:
    from main import cleanup_expired

    with sqlite3.connect(backend_db) as db:
        db.execute(
            """
            INSERT INTO line_webhook_events (
                event_id, status, received_at, completed_at, failed_at,
                last_error_code, attempt_count
            ) VALUES (
                'event-old-terminal-failure', 'completed',
                '2000-01-01T00:00:00+00:00',
                '2000-01-01T00:00:01+00:00',
                '2000-01-01T00:00:01+00:00',
                'answer_delivery_failed', 4
            )
            """
        )
        db.commit()

    await cleanup_expired()
    with sqlite3.connect(backend_db) as db:
        row = db.execute(
            """
            SELECT 1 FROM line_webhook_events
            WHERE event_id='event-old-terminal-failure'
            """
        ).fetchone()
    assert row is None


@pytest.mark.anyio
async def test_webhook_rejects_declared_and_streamed_body_over_limit(
    backend_db: Path,
    asgi_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webhook_router.settings, "line_webhook_max_body_bytes", 10)
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        declared = await client.post(
            "/webhook/channel-a",
            content=b"x" * 11,
        )

        async def chunked_body():
            yield b"123456"
            yield b"78901"

        streamed = await client.post(
            "/webhook/channel-a",
            content=chunked_body(),
        )

    assert declared.status_code == 413
    assert streamed.status_code == 413
    with sqlite3.connect(backend_db) as db:
        assert db.execute("SELECT COUNT(*) FROM line_webhook_events").fetchone()[0] == 0


@pytest.mark.anyio
async def test_webhook_event_count_limit_rejects_before_any_ledger_write(
    backend_db: Path,
    asgi_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "line-secret"
    _insert_webhook_channel(backend_db, "channel-a", secret, "token")
    monkeypatch.setattr(
        webhook_router.settings,
        "line_webhook_max_events_per_request",
        1,
    )
    first = json.loads(_line_event("first", "event-count-1"))["events"][0]
    second = json.loads(_line_event("second", "event-count-2"))["events"][0]
    body = json.dumps({"events": [first, second]}, separators=(",", ":")).encode()
    signature = (
        __import__("base64")
        .b64encode(
            __import__("hmac")
            .new(secret.encode(), body, __import__("hashlib").sha256)
            .digest()
        )
        .decode()
    )

    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/webhook/channel-a",
            content=body,
            headers={"X-Line-Signature": signature},
        )

    assert response.status_code == 413
    with sqlite3.connect(backend_db) as db:
        assert db.execute("SELECT COUNT(*) FROM line_webhook_events").fetchone()[0] == 0


@pytest.mark.anyio
async def test_tenant_cap_does_not_block_another_channel_and_replay_is_fair(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_webhook_channel(backend_db, "channel-a", "secret-a", "token-a")
    _insert_webhook_channel(backend_db, "channel-b", "secret-b", "token-b")
    monkeypatch.setattr(webhook_router.settings, "line_event_pending_limit", 3)
    monkeypatch.setattr(
        webhook_router.settings,
        "line_event_pending_per_channel_limit",
        2,
    )
    monkeypatch.setattr(
        webhook_router.settings,
        "line_event_replay_per_channel_batch",
        1,
    )

    async def admit(channel_id: str, event_id: str):
        claim = await webhook_router._claim_event(
            event_id,
            webhook_router._event_payload_ciphertext(
                channel_id,
                f"user-{channel_id}",
                f"question-{event_id}",
            ),
            webhook_router._tenant_key(channel_id),
        )
        assert claim is not None
        assert await webhook_router._release_unstarted_event(claim)

    await admit("channel-a", "event-a-1")
    await admit("channel-a", "event-a-2")
    with pytest.raises(webhook_router.EventLedgerFull) as tenant_full:
        await webhook_router._claim_event(
            "event-a-3",
            webhook_router._event_payload_ciphertext(
                "channel-a", "user-a", "question-a-3"
            ),
            webhook_router._tenant_key("channel-a"),
        )
    assert tenant_full.value.scope == "tenant"

    await admit("channel-b", "event-b-1")
    with pytest.raises(webhook_router.EventLedgerFull) as global_full:
        await webhook_router._claim_event(
            "event-c-1",
            webhook_router._event_payload_ciphertext(
                "channel-c", "user-c", "question-c-1"
            ),
            webhook_router._tenant_key("channel-c"),
        )
    assert global_full.value.scope == "global"

    with sqlite3.connect(backend_db) as db:
        rows = db.execute(
            """
            SELECT tenant_key, COUNT(*) FROM line_webhook_events
            WHERE status='pending' GROUP BY tenant_key
            """
        ).fetchall()
    assert sorted(count for _tenant, count in rows) == [1, 2]
    assert all(len(tenant) == 64 for tenant, _count in rows)
    assert {tenant for tenant, _count in rows} == {
        webhook_router._tenant_key("channel-a"),
        webhook_router._tenant_key("channel-b"),
    }

    delivered_channels: list[str] = []

    async def delivered(channel_id, *_args):
        delivered_channels.append(channel_id)
        return True

    monkeypatch.setattr(webhook_router, "_load_ask_and_push", delivered)
    webhook_router.start_background_processing()
    assert await webhook_router.replay_due_pending_events() == 2
    await _wait_for_event_workers()
    assert sorted(delivered_channels) == ["channel-a", "channel-b"]


@pytest.mark.anyio
async def test_cross_tenant_event_id_cannot_reclaim_or_replace_payload(
    backend_db: Path,
) -> None:
    tenant_a = webhook_router._tenant_key("channel-a")
    tenant_b = webhook_router._tenant_key("channel-b")
    original_payload = webhook_router._event_payload_ciphertext(
        "channel-a", "user-a", "private-a"
    )
    claim = await webhook_router._claim_event(
        "shared-event-id",
        original_payload,
        tenant_a,
    )
    assert claim is not None
    assert await webhook_router._release_unstarted_event(claim)

    collision = await webhook_router._claim_event(
        "shared-event-id",
        webhook_router._event_payload_ciphertext("channel-b", "user-b", "private-b"),
        tenant_b,
    )
    assert collision is None
    with sqlite3.connect(backend_db) as db:
        stored = db.execute(
            """
            SELECT tenant_key, payload_encrypted FROM line_webhook_events
            WHERE event_id='shared-event-id'
            """
        ).fetchone()
    assert stored == (tenant_a, original_payload)


@pytest.mark.anyio
async def test_active_worker_limit_is_enforced_per_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = asyncio.current_task()
    assert current is not None
    tenant_a = webhook_router._tenant_key("channel-a")
    monkeypatch.setattr(
        webhook_router.settings,
        "line_event_replay_per_channel_batch",
        1,
    )
    webhook_router._background_tasks.add(current)
    webhook_router._background_task_tenants[current] = tenant_a
    try:
        accepted = webhook_router._schedule_claimed_event(
            webhook_router.EventClaim("event-active-cap", "claim-token"),
            event_tenant_key=tenant_a,
        )
    finally:
        webhook_router._background_task_tenants.pop(current, None)
        webhook_router._background_tasks.discard(current)

    assert accepted is False


@pytest.mark.anyio
async def test_crash_after_push_reuses_stable_uuid_retry_key_on_replay(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_webhook_channel(backend_db, "channel-a", "secret", "token")
    claim = await webhook_router._claim_event(
        "event-push-idempotency",
        webhook_router._event_payload_ciphertext(
            "channel-a", "line-user-a", "question"
        ),
        webhook_router._tenant_key("channel-a"),
    )
    assert claim is not None
    retry_keys: list[str] = []

    async def loading_ok(*_args, **_kwargs):
        return True

    async def ask(*_args):
        return ["stable answer"]

    async def push(_user, _token, _text, *, retry_key=None):
        assert retry_key is not None
        retry_keys.append(retry_key)

    real_complete = webhook_router._complete_event
    complete_calls = 0

    async def crash_once(event_claim):
        nonlocal complete_calls
        complete_calls += 1
        if complete_calls == 1:
            raise RuntimeError("simulated crash after LINE accepted Push")
        return await real_complete(event_claim)

    monkeypatch.setattr(webhook_router, "show_loading", loading_ok)
    monkeypatch.setattr(webhook_router, "ask_question", ask)
    monkeypatch.setattr(webhook_router, "push_text", push)
    monkeypatch.setattr(webhook_router, "_complete_event", crash_once)
    webhook_router.start_background_processing()
    assert webhook_router._schedule_claimed_event(
        claim,
        event_tenant_key=webhook_router._tenant_key("channel-a"),
    )
    await _wait_for_event_workers()
    with sqlite3.connect(backend_db) as db:
        db.execute(
            """
            UPDATE line_webhook_events
            SET next_attempt_at='2000-01-01T00:00:00+00:00'
            WHERE event_id='event-push-idempotency'
            """
        )
        db.commit()

    assert await webhook_router.replay_due_pending_events() == 1
    await _wait_for_event_workers()
    assert len(retry_keys) == 2
    assert retry_keys[0] == retry_keys[1]
    assert str(uuid.UUID(retry_keys[0])) == retry_keys[0]
    with sqlite3.connect(backend_db) as db:
        status = db.execute(
            """
            SELECT status FROM line_webhook_events
            WHERE event_id='event-push-idempotency'
            """
        ).fetchone()[0]
    assert status == "completed"


@pytest.mark.anyio
async def test_fallback_error_push_uses_stable_separate_retry_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_keys: list[str] = []

    async def loading_ok(*_args, **_kwargs):
        return True

    async def ask_failed(*_args):
        raise RuntimeError("upstream unavailable")

    async def push(_user, _token, _text, *, retry_key=None):
        assert retry_key is not None
        retry_keys.append(retry_key)

    monkeypatch.setattr(webhook_router, "show_loading", loading_ok)
    monkeypatch.setattr(webhook_router, "ask_question", ask_failed)
    monkeypatch.setattr(webhook_router, "push_text", push)
    for _ in range(2):
        assert not await webhook_router._load_ask_and_push(
            "channel-a",
            "line-user-a",
            "",
            "token",
            "question",
            "event-fallback-key",
        )

    assert retry_keys == [
        webhook_router._push_retry_key("event-fallback-key", "error"),
        webhook_router._push_retry_key("event-fallback-key", "error"),
    ]
    assert retry_keys[0] != webhook_router._push_retry_key(
        "event-fallback-key", "answer", 0
    )


@pytest.mark.anyio
async def test_legacy_pending_payload_backfills_full_tenant_hash(
    backend_db: Path,
) -> None:
    encrypted_payload = webhook_router._event_payload_ciphertext(
        "legacy-channel", "legacy-user", "legacy private question"
    )
    with sqlite3.connect(backend_db) as db:
        db.execute(
            """
            INSERT INTO line_webhook_events (
                event_id, status, received_at, attempt_count,
                payload_encrypted, payload_version, tenant_key
            ) VALUES (?, 'pending', ?, 0, ?, 1, NULL)
            """,
            (
                "event-legacy-tenant",
                datetime.now(timezone.utc).isoformat(),
                encrypted_payload,
            ),
        )
        db.commit()

    assert await webhook_router.recover_pending_event_claims() == 0
    with sqlite3.connect(backend_db) as db:
        row = db.execute(
            """
            SELECT status, tenant_key, payload_encrypted
            FROM line_webhook_events WHERE event_id='event-legacy-tenant'
            """
        ).fetchone()
    assert row == (
        "pending",
        webhook_router._tenant_key("legacy-channel"),
        encrypted_payload,
    )


@pytest.mark.anyio
async def test_replay_cursor_prevents_cross_round_tenant_starvation(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = ["channel-a", "channel-b", "channel-c"]
    for channel in channels:
        _insert_webhook_channel(backend_db, channel, f"secret-{channel}", "token")
        for index in range(2):
            claim = await webhook_router._claim_event(
                f"event-{channel}-{index}",
                webhook_router._event_payload_ciphertext(
                    channel, f"user-{channel}", f"question-{index}"
                ),
                webhook_router._tenant_key(channel),
            )
            assert claim is not None
            assert await webhook_router._release_unstarted_event(claim)

    monkeypatch.setattr(webhook_router.settings, "line_background_task_limit", 1)
    monkeypatch.setattr(
        webhook_router.settings,
        "line_event_replay_per_channel_batch",
        1,
    )
    delivered: list[str] = []

    async def deliver(channel_id, *_args):
        delivered.append(channel_id)
        return True

    monkeypatch.setattr(webhook_router, "_load_ask_and_push", deliver)
    webhook_router.start_background_processing()
    for _ in channels:
        assert await webhook_router.replay_due_pending_events() == 1
        await _wait_for_event_workers()

    assert set(delivered) == set(channels)
    with sqlite3.connect(backend_db) as db:
        cursor_value = db.execute(
            """
            SELECT last_tenant_key FROM line_webhook_replay_state WHERE id=1
            """
        ).fetchone()[0]
    assert cursor_value == webhook_router._tenant_key(delivered[-1])


@pytest.mark.anyio
async def test_each_answer_push_position_has_stable_distinct_retry_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_keys: list[str] = []

    async def loading_ok(*_args, **_kwargs):
        return True

    async def ask(*_args):
        return ["answer part one", "answer part two"]

    async def push(_user, _token, _text, *, retry_key=None):
        assert retry_key is not None
        retry_keys.append(retry_key)

    monkeypatch.setattr(webhook_router, "show_loading", loading_ok)
    monkeypatch.setattr(webhook_router, "ask_question", ask)
    monkeypatch.setattr(webhook_router, "push_text", push)
    for _ in range(2):
        assert await webhook_router._load_ask_and_push(
            "channel-a",
            "line-user-a",
            "",
            "token",
            "question",
            "event-multipart-answer",
        )

    assert retry_keys == [
        webhook_router._push_retry_key("event-multipart-answer", "answer", 0),
        webhook_router._push_retry_key("event-multipart-answer", "answer", 1),
        webhook_router._push_retry_key("event-multipart-answer", "answer", 0),
        webhook_router._push_retry_key("event-multipart-answer", "answer", 1),
    ]
    assert retry_keys[0] != retry_keys[1]
