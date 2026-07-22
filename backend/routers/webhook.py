import hashlib
import hmac
import base64
import asyncio
import logging
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Awaitable
from typing import TypeVar
from fastapi import APIRouter, Request, HTTPException
import aiosqlite
from database import DB
from services.nlm_service import ask_question
from services.line_service import show_loading, reply_text, push_text
from services.channel_credentials import read_channel_access_token, read_channel_secret
from services.crypto_service import decrypt_json, encrypt_json
from services.logging_service import mask_identifier
from config import settings

router = APIRouter(tags=["webhook"])
logger = logging.getLogger(__name__)
_background_tasks: set[asyncio.Task[None]] = set()
_background_task_tenants: dict[asyncio.Task[None], str] = {}
_accepting_background_tasks = True
T = TypeVar("T")
_EVENT_PAYLOAD_VERSION = 1
_RETRY_BACKOFF_CAP_SECONDS = 300.0
_PUSH_RETRY_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://notebooklm-line.local/line-push",
)


@dataclass(frozen=True, slots=True)
class EventClaim:
    event_id: str
    claim_token: str


@dataclass(frozen=True, slots=True)
class EventPayload:
    channel_id: str
    user_id: str
    question: str


class EventProcessingError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class EventClaimLost(RuntimeError):
    pass


class EventLedgerFull(RuntimeError):
    def __init__(self, scope: str) -> None:
        super().__init__(scope)
        self.scope = scope


def _safe_error_code(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    if not normalized or len(normalized) > 64:
        return "internal_error"
    if not all(
        char.isascii() and (char.isalnum() or char == "_") for char in normalized
    ):
        return "internal_error"
    return normalized


def _tenant_key(channel_id: str) -> str:
    """Return the full non-reversible tenant identifier used by the ledger."""

    return hashlib.sha256(channel_id.encode("utf-8")).hexdigest()


def _push_retry_key(event_id: str | None, purpose: str, index: int = 0) -> str | None:
    if not event_id:
        return None
    return str(
        uuid.uuid5(
            _PUSH_RETRY_NAMESPACE,
            f"{event_id}:{purpose}:{index}",
        )
    )


async def _read_bounded_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise HTTPException(400, "Invalid Content-Length") from error
        if declared_length < 0:
            raise HTTPException(400, "Invalid Content-Length")
        if declared_length > settings.line_webhook_max_body_bytes:
            raise HTTPException(413, "Webhook payload is too large")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > settings.line_webhook_max_body_bytes:
            raise HTTPException(413, "Webhook payload is too large")
        body.extend(chunk)
    return bytes(body)


def _event_payload_ciphertext(
    channel_id: str,
    user_id: str,
    question: str,
) -> str:
    return encrypt_json(
        {
            "v": _EVENT_PAYLOAD_VERSION,
            "channel_id": channel_id,
            "user_id": user_id,
            "question": question,
        }
    )


async def _claim_event(
    event_id: str,
    payload_encrypted: str | None = None,
    event_tenant_key: str | None = None,
) -> EventClaim | None:
    """Atomically claim a new event or reclaim an abandoned pending event."""

    if payload_encrypted and event_tenant_key is None:
        try:
            decoded_payload = decrypt_json(payload_encrypted)
            decoded_channel_id = decoded_payload.get("channel_id")
            if isinstance(decoded_channel_id, str) and decoded_channel_id:
                event_tenant_key = _tenant_key(decoded_channel_id)
        except Exception:
            # Invalid ciphertext is terminalized by the durable worker. This
            # fallback only preserves compatibility for internal migration tests.
            pass
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=settings.line_event_claim_timeout_seconds)
    retention_cutoff = now - timedelta(
        seconds=settings.line_event_pending_retention_seconds
    )
    claim = EventClaim(event_id=event_id, claim_token=secrets.token_urlsafe(24))
    async with aiosqlite.connect(DB) as db:
        await db.execute("BEGIN IMMEDIATE")
        existing = await db.execute(
            "SELECT 1 FROM line_webhook_events WHERE event_id=?",
            (event_id,),
        )
        if await existing.fetchone() is None:
            count_cursor = await db.execute(
                "SELECT COUNT(*) FROM line_webhook_events WHERE status='pending'"
            )
            pending_count = (await count_cursor.fetchone())[0]
            if pending_count >= settings.line_event_pending_limit:
                await db.rollback()
                raise EventLedgerFull("global")
            tenant_count_cursor = await db.execute(
                """
                SELECT COUNT(*) FROM line_webhook_events
                WHERE status='pending' AND tenant_key IS ?
                """,
                (event_tenant_key,),
            )
            tenant_pending_count = (await tenant_count_cursor.fetchone())[0]
            if tenant_pending_count >= settings.line_event_pending_per_channel_limit:
                await db.rollback()
                raise EventLedgerFull("tenant")
        cursor = await db.execute(
            """
            INSERT INTO line_webhook_events (
                event_id, status, received_at, claimed_at, completed_at,
                attempt_count, claim_token, tenant_key, payload_encrypted,
                payload_version, next_attempt_at, failure_count,
                last_error_code, failed_at
            ) VALUES (
                ?, 'pending', ?, ?, NULL, 1, ?, ?, ?, ?, NULL, 0, NULL, NULL
            )
            ON CONFLICT(event_id) DO UPDATE SET
                status='pending',
                claimed_at=excluded.claimed_at,
                completed_at=NULL,
                attempt_count=line_webhook_events.attempt_count + 1,
                claim_token=excluded.claim_token,
                tenant_key=COALESCE(
                    line_webhook_events.tenant_key,
                    excluded.tenant_key
                ),
                payload_encrypted=COALESCE(
                    line_webhook_events.payload_encrypted,
                    excluded.payload_encrypted
                ),
                payload_version=COALESCE(
                    line_webhook_events.payload_version,
                    excluded.payload_version
                ),
                next_attempt_at=NULL
            WHERE line_webhook_events.status='pending'
              AND line_webhook_events.tenant_key IS excluded.tenant_key
              AND line_webhook_events.attempt_count < ?
              AND line_webhook_events.received_at > ?
              AND (
                  line_webhook_events.next_attempt_at IS NULL
                  OR line_webhook_events.next_attempt_at <= ?
              )
              AND (
                  line_webhook_events.claimed_at IS NULL
                  OR line_webhook_events.claimed_at <= ?
              )
            """,
            (
                event_id,
                now.isoformat(),
                now.isoformat(),
                claim.claim_token,
                event_tenant_key,
                payload_encrypted,
                _EVENT_PAYLOAD_VERSION if payload_encrypted else None,
                settings.line_event_max_attempts,
                retention_cutoff.isoformat(),
                now.isoformat(),
                stale_before.isoformat(),
            ),
        )
        await db.commit()
    return claim if cursor.rowcount == 1 else None


async def _claim_pending_event(
    event_id: str,
    event_tenant_key: str,
) -> EventClaim | None:
    """Claim an already persisted due event without changing its payload."""

    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=settings.line_event_claim_timeout_seconds)
    retention_cutoff = now - timedelta(
        seconds=settings.line_event_pending_retention_seconds
    )
    claim = EventClaim(event_id=event_id, claim_token=secrets.token_urlsafe(24))
    async with aiosqlite.connect(DB) as db:
        cursor = await db.execute(
            """
            UPDATE line_webhook_events
            SET claimed_at=?, claim_token=?, attempt_count=attempt_count + 1,
                next_attempt_at=NULL
            WHERE event_id=? AND status='pending'
              AND tenant_key=?
              AND payload_encrypted IS NOT NULL
              AND payload_version=?
              AND attempt_count < ?
              AND received_at > ?
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
              AND (claimed_at IS NULL OR claimed_at <= ?)
            """,
            (
                now.isoformat(),
                claim.claim_token,
                event_id,
                event_tenant_key,
                _EVENT_PAYLOAD_VERSION,
                settings.line_event_max_attempts,
                retention_cutoff.isoformat(),
                now.isoformat(),
                stale_before.isoformat(),
            ),
        )
        await db.commit()
    return claim if cursor.rowcount == 1 else None


async def _complete_event(claim: EventClaim) -> bool:
    async with aiosqlite.connect(DB) as db:
        cursor = await db.execute(
            """
            UPDATE line_webhook_events
            SET status='completed', completed_at=?, claimed_at=NULL,
                claim_token=NULL, payload_encrypted=NULL, payload_version=NULL,
                next_attempt_at=NULL, last_error_code=NULL, failed_at=NULL
            WHERE event_id=? AND status='pending' AND claim_token=?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                claim.event_id,
                claim.claim_token,
            ),
        )
        await db.commit()
    return cursor.rowcount == 1


async def _release_event(claim: EventClaim) -> bool:
    """Release only this worker's lease so the internal scheduler can retry."""

    async with aiosqlite.connect(DB) as db:
        cursor = await db.execute(
            """
            UPDATE line_webhook_events
            SET claimed_at=NULL, claim_token=NULL, next_attempt_at=?
            WHERE event_id=? AND status='pending' AND claim_token=?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                claim.event_id,
                claim.claim_token,
            ),
        )
        await db.commit()
    return cursor.rowcount == 1


async def _release_unstarted_event(claim: EventClaim) -> bool:
    """Return a queue-rejected claim without consuming a processing attempt."""

    async with aiosqlite.connect(DB) as db:
        cursor = await db.execute(
            """
            UPDATE line_webhook_events
            SET claimed_at=NULL, claim_token=NULL, next_attempt_at=?,
                attempt_count=MAX(0, attempt_count - 1)
            WHERE event_id=? AND status='pending' AND claim_token=?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                claim.event_id,
                claim.claim_token,
            ),
        )
        await db.commit()
    return cursor.rowcount == 1


async def _record_event_failure(
    claim: EventClaim,
    error_code: str,
    *,
    retryable: bool,
) -> bool:
    """CAS a claimed event into delayed retry or scrubbed terminal failure."""

    now = datetime.now(timezone.utc)
    safe_error_code = _safe_error_code(error_code)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT received_at, attempt_count, failure_count
            FROM line_webhook_events
            WHERE event_id=? AND status='pending' AND claim_token=?
            """,
            (claim.event_id, claim.claim_token),
        )
        row = await cursor.fetchone()
        if row is None:
            await db.rollback()
            return False

        try:
            received_at = datetime.fromisoformat(row["received_at"])
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            received_at = now
            retryable = False
            safe_error_code = "invalid_event_timestamp"

        retention_expired = (
            now - received_at
        ).total_seconds() >= settings.line_event_pending_retention_seconds
        terminal = (
            not retryable
            or retention_expired
            or row["attempt_count"] >= settings.line_event_max_attempts
        )
        if terminal:
            update = await db.execute(
                """
                UPDATE line_webhook_events
                SET status='completed', completed_at=?, failed_at=?,
                    claimed_at=NULL, claim_token=NULL,
                    payload_encrypted=NULL, payload_version=NULL,
                    next_attempt_at=NULL, failure_count=failure_count + 1,
                    last_error_code=?
                WHERE event_id=? AND status='pending' AND claim_token=?
                """,
                (
                    now.isoformat(),
                    now.isoformat(),
                    safe_error_code,
                    claim.event_id,
                    claim.claim_token,
                ),
            )
        else:
            backoff_seconds = min(
                settings.line_event_retry_base_seconds
                * (2 ** min(row["failure_count"], 16)),
                _RETRY_BACKOFF_CAP_SECONDS,
            )
            next_attempt = now + timedelta(seconds=backoff_seconds)
            update = await db.execute(
                """
                UPDATE line_webhook_events
                SET claimed_at=NULL, claim_token=NULL, next_attempt_at=?,
                    failure_count=failure_count + 1, last_error_code=?
                WHERE event_id=? AND status='pending' AND claim_token=?
                """,
                (
                    next_attempt.isoformat(),
                    safe_error_code,
                    claim.event_id,
                    claim.claim_token,
                ),
            )
        await db.commit()
    return update.rowcount == 1


async def recover_pending_event_claims() -> int:
    """Release leases left by the previous single-service process.

    This runs once during startup before webhook admission begins. Graceful
    shutdown normally releases each worker's claim; this recovery covers a
    crash or forced restart where ``finally`` could not execute.
    """

    async with aiosqlite.connect(DB) as db:
        cursor = await db.execute(
            """
            UPDATE line_webhook_events
            SET claimed_at=NULL, claim_token=NULL,
                next_attempt_at=COALESCE(next_attempt_at, ?)
            WHERE status='pending'
              AND (claimed_at IS NOT NULL OR claim_token IS NOT NULL)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        await db.commit()
    released = max(0, cursor.rowcount)
    await _backfill_pending_tenant_keys()
    return released


async def _backfill_pending_tenant_keys() -> int:
    """Backfill pre-tenant durable rows without exposing their Channel IDs."""

    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=settings.line_event_claim_timeout_seconds)
    changed = 0
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT event_id, payload_encrypted, payload_version
            FROM line_webhook_events
            WHERE status='pending' AND tenant_key IS NULL
              AND (claimed_at IS NULL OR claimed_at <= ?)
            """,
            (stale_before.isoformat(),),
        )
        for row in await cursor.fetchall():
            if (
                not row["payload_encrypted"]
                or row["payload_version"] != _EVENT_PAYLOAD_VERSION
            ):
                continue
            try:
                payload = decrypt_json(row["payload_encrypted"])
                channel_id = payload.get("channel_id")
                if not isinstance(channel_id, str) or not channel_id:
                    raise ValueError("invalid channel")
            except Exception:
                update = await db.execute(
                    """
                    UPDATE line_webhook_events
                    SET status='completed', completed_at=?, failed_at=?,
                        claimed_at=NULL, claim_token=NULL,
                        payload_encrypted=NULL, payload_version=NULL,
                        next_attempt_at=NULL, last_error_code='invalid_event_payload'
                    WHERE event_id=? AND status='pending' AND tenant_key IS NULL
                    """,
                    (now.isoformat(), now.isoformat(), row["event_id"]),
                )
            else:
                update = await db.execute(
                    """
                    UPDATE line_webhook_events SET tenant_key=?
                    WHERE event_id=? AND status='pending' AND tenant_key IS NULL
                    """,
                    (_tenant_key(channel_id), row["event_id"]),
                )
            changed += max(0, update.rowcount)
        await db.commit()
    return changed


async def _terminalize_unrecoverable_pending_events() -> int:
    """Scrub payloads that are expired, exhausted, or structurally missing."""

    now = datetime.now(timezone.utc)
    retention_cutoff = now - timedelta(
        seconds=settings.line_event_pending_retention_seconds
    )
    stale_before = now - timedelta(seconds=settings.line_event_claim_timeout_seconds)
    total = 0
    async with aiosqlite.connect(DB) as db:
        for predicate, code, parameters in (
            (
                "received_at <= ?",
                "pending_retention_expired",
                (retention_cutoff.isoformat(),),
            ),
            (
                "attempt_count >= ? AND (claimed_at IS NULL OR claimed_at <= ?)",
                "max_attempts_exhausted",
                (settings.line_event_max_attempts, stale_before.isoformat()),
            ),
            (
                "payload_encrypted IS NULL AND (claimed_at IS NULL OR claimed_at <= ?)",
                "payload_missing",
                (stale_before.isoformat(),),
            ),
            (
                "payload_encrypted IS NOT NULL AND "
                "(payload_version IS NULL OR payload_version <> ?) AND "
                "(claimed_at IS NULL OR claimed_at <= ?)",
                "invalid_event_payload",
                (_EVENT_PAYLOAD_VERSION, stale_before.isoformat()),
            ),
            (
                "tenant_key IS NULL AND (claimed_at IS NULL OR claimed_at <= ?)",
                "invalid_event_tenant",
                (stale_before.isoformat(),),
            ),
        ):
            cursor = await db.execute(
                f"""
                UPDATE line_webhook_events
                SET status='completed', completed_at=?, failed_at=?,
                    claimed_at=NULL, claim_token=NULL,
                    payload_encrypted=NULL, payload_version=NULL,
                    next_attempt_at=NULL, last_error_code=?
                WHERE status='pending' AND {predicate}
                """,
                (now.isoformat(), now.isoformat(), code, *parameters),
            )
            total += max(0, cursor.rowcount)
        await db.commit()
    return total


async def _shield_transition(operation: Awaitable[T]) -> T:
    """Finish a tiny ledger transition even if worker cancellation races it."""

    task = asyncio.create_task(operation, name="line-event-transition")
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A second cancellation while a transition is in flight must not leave
        # the event permanently claimed. Wait for this bounded SQLite write,
        # then preserve cancellation for the caller.
        await task
        raise


async def _read_claimed_payload(claim: EventClaim) -> EventPayload:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT payload_encrypted, payload_version
            FROM line_webhook_events
            WHERE event_id=? AND status='pending' AND claim_token=?
            """,
            (claim.event_id, claim.claim_token),
        )
        row = await cursor.fetchone()
    if row is None:
        raise EventClaimLost
    if not row["payload_encrypted"] or row["payload_version"] != _EVENT_PAYLOAD_VERSION:
        raise EventProcessingError("invalid_event_payload", retryable=False)
    try:
        value = decrypt_json(row["payload_encrypted"])
    except Exception as error:
        raise EventProcessingError("invalid_event_payload", retryable=False) from error
    if not isinstance(value, dict) or value.get("v") != _EVENT_PAYLOAD_VERSION:
        raise EventProcessingError("invalid_event_payload", retryable=False)

    channel_id = value.get("channel_id")
    user_id = value.get("user_id")
    question = value.get("question")
    if (
        not isinstance(channel_id, str)
        or not 1 <= len(channel_id) <= 255
        or not isinstance(user_id, str)
        or not 1 <= len(user_id) <= 255
        or not isinstance(question, str)
        or not 1 <= len(question) <= 10_000
    ):
        raise EventProcessingError("invalid_event_payload", retryable=False)
    return EventPayload(
        channel_id=channel_id,
        user_id=user_id,
        question=question,
    )


async def _read_event_access_token(channel_id: str) -> str:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT channel_access_token, channel_access_token_encrypted, expires_at
            FROM channels WHERE channel_id=?
            """,
            (channel_id,),
        )
        channel = await cursor.fetchone()
    if channel is None:
        raise EventProcessingError("channel_missing", retryable=False)
    if channel["expires_at"]:
        try:
            expires_at = datetime.fromisoformat(channel["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError) as error:
            raise EventProcessingError(
                "channel_expiry_invalid", retryable=False
            ) from error
        if expires_at <= datetime.now(timezone.utc):
            raise EventProcessingError("channel_expired", retryable=False)
    try:
        return read_channel_access_token(channel)
    except ValueError as error:
        raise EventProcessingError(
            "channel_credentials_unavailable", retryable=False
        ) from error


def _schedule_claimed_event(
    claim: EventClaim,
    reply_token: str = "",
    channel_id_hint: str = "",
    event_tenant_key: str = "",
) -> bool:
    if not event_tenant_key and channel_id_hint:
        event_tenant_key = _tenant_key(channel_id_hint)
    active_for_tenant = sum(
        not task.done() and tenant == event_tenant_key
        for task, tenant in _background_task_tenants.items()
    )
    if (
        not _accepting_background_tasks
        or len(_background_tasks) >= settings.line_background_task_limit
        or (
            event_tenant_key
            and active_for_tenant >= settings.line_event_replay_per_channel_batch
        )
    ):
        return False
    task = asyncio.create_task(
        _run_claimed_event(claim, reply_token),
        name=(f"line-event-{mask_identifier(channel_id_hint or claim.event_id)}"),
    )
    _background_tasks.add(task)
    if event_tenant_key:
        _background_task_tenants[task] = event_tenant_key
    task.add_done_callback(_background_task_finished)
    return True


def _background_task_finished(task: asyncio.Task[None]) -> None:
    _background_tasks.discard(task)
    _background_task_tenants.pop(task, None)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error(
            "webhook_background_task outcome=failed error_type=%s",
            type(error).__name__,
        )


def _schedule_question(
    channel_id: str,
    user_id: str,
    reply_token: str,
    access_token: str,
    question: str,
    event_claim: EventClaim | None = None,
) -> bool:
    """Start work only while the configured in-process queue has capacity."""

    if (
        not _accepting_background_tasks
        or len(_background_tasks) >= settings.line_background_task_limit
    ):
        return False
    if event_claim is not None:
        return _schedule_claimed_event(
            event_claim,
            reply_token,
            channel_id,
            _tenant_key(channel_id),
        )
    event_tenant_key = _tenant_key(channel_id)
    active_for_tenant = sum(
        not task.done() and tenant == event_tenant_key
        for task, tenant in _background_task_tenants.items()
    )
    if active_for_tenant >= settings.line_event_replay_per_channel_batch:
        return False
    task = asyncio.create_task(
        _run_question(
            channel_id,
            user_id,
            reply_token,
            access_token,
            question,
            event_claim,
        ),
        name=f"line-question-{mask_identifier(channel_id)}",
    )
    _background_tasks.add(task)
    _background_task_tenants[task] = event_tenant_key
    task.add_done_callback(_background_task_finished)
    return True


async def replay_due_pending_events() -> int:
    """Claim and schedule durable events that are ready for an internal retry."""

    if not _accepting_background_tasks:
        return 0
    await _backfill_pending_tenant_keys()
    terminalized = await _terminalize_unrecoverable_pending_events()
    if terminalized:
        logger.warning(
            "line_event_replay terminalized=%d",
            terminalized,
        )

    active_count = sum(not task.done() for task in _background_tasks)
    capacity = max(0, settings.line_background_task_limit - active_count)
    if capacity == 0:
        return 0

    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=settings.line_event_claim_timeout_seconds)
    retention_cutoff = now - timedelta(
        seconds=settings.line_event_pending_retention_seconds
    )
    async with aiosqlite.connect(DB) as db:
        state_cursor = await db.execute(
            """
            SELECT last_tenant_key FROM line_webhook_replay_state WHERE id=1
            """
        )
        state_row = await state_cursor.fetchone()
        last_tenant_key = state_row[0] if state_row and state_row[0] else ""
        cursor = await db.execute(
            """
            WITH eligible AS (
                SELECT
                    event_id,
                    tenant_key,
                    COALESCE(next_attempt_at, received_at) AS due_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY tenant_key
                        ORDER BY COALESCE(next_attempt_at, received_at),
                                 received_at, event_id
                    ) AS tenant_rank
                FROM line_webhook_events
                WHERE status='pending'
                  AND tenant_key IS NOT NULL
                  AND payload_encrypted IS NOT NULL
                  AND payload_version=?
                  AND attempt_count < ?
                  AND received_at > ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND (claimed_at IS NULL OR claimed_at <= ?)
            )
            SELECT event_id, tenant_key
            FROM eligible
            WHERE tenant_rank <= ?
            ORDER BY tenant_rank,
                     CASE WHEN tenant_key > ? THEN 0 ELSE 1 END,
                     tenant_key, due_at, event_id
            """,
            (
                _EVENT_PAYLOAD_VERSION,
                settings.line_event_max_attempts,
                retention_cutoff.isoformat(),
                now.isoformat(),
                stale_before.isoformat(),
                settings.line_event_replay_per_channel_batch,
                last_tenant_key,
            ),
        )
        candidates = await cursor.fetchall()

    scheduled = 0
    last_scheduled_tenant = ""
    for event_id, event_tenant_key in candidates:
        if scheduled >= capacity:
            break
        active_for_tenant = sum(
            not task.done() and tenant == event_tenant_key
            for task, tenant in _background_task_tenants.items()
        )
        if active_for_tenant >= settings.line_event_replay_per_channel_batch:
            continue
        claim = await _claim_pending_event(event_id, event_tenant_key)
        if claim is None:
            continue
        if _schedule_claimed_event(
            claim,
            event_tenant_key=event_tenant_key,
        ):
            scheduled += 1
            last_scheduled_tenant = event_tenant_key
        else:
            await _release_unstarted_event(claim)
    if last_scheduled_tenant:
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                """
                UPDATE line_webhook_replay_state SET last_tenant_key=? WHERE id=1
                """,
                (last_scheduled_tenant,),
            )
            await db.commit()
    return scheduled


async def replay_pending_event_scheduler() -> None:
    while True:
        try:
            await replay_due_pending_events()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "line_event_replay outcome=failed error_type=%s",
                type(error).__name__,
            )
        await asyncio.sleep(settings.line_event_replay_interval_seconds)


def start_background_processing() -> None:
    global _accepting_background_tasks
    _accepting_background_tasks = True


async def drain_background_tasks(timeout_seconds: float) -> dict[str, int]:
    """Stop admission and bound the complete wait/cancellation interval."""

    global _accepting_background_tasks
    _accepting_background_tasks = False
    active = {task for task in _background_tasks if not task.done()}
    if not active:
        return {"completed": 0, "cancelled": 0}

    timeout = max(0.0, timeout_seconds)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    # Reserve a small part of the caller's total budget for cooperative task
    # cancellation and ledger release. A cancellation-resistant upstream
    # cleanup cannot hold deployment shutdown beyond the supplied timeout.
    cancellation_budget = min(1.0, timeout / 2)
    normal_wait = max(0.0, timeout - cancellation_budget)
    done, pending = await asyncio.wait(active, timeout=normal_wait)
    completed_before_cancel = len(done)
    for task in pending:
        task.cancel()
    if pending:
        remaining = max(0.0, deadline - loop.time())
        if remaining:
            _cancelled_done, still_pending = await asyncio.wait(
                pending, timeout=remaining
            )
            if still_pending:
                logger.warning(
                    "line_background_shutdown outcome=cancellation_timeout count=%d",
                    len(still_pending),
                )
    return {"completed": completed_before_cancel, "cancelled": len(pending)}


def verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    hash_val = hmac.HMAC(channel_secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(hash_val).decode(), signature)


@router.post("/webhook/{channel_id}")
async def webhook(channel_id: str, request: Request):
    body = await _read_bounded_body(request)

    # Look up channel
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT channel_secret, channel_access_token,
                   channel_secret_encrypted, channel_access_token_encrypted,
                   expires_at
            FROM channels WHERE channel_id=?
            """,
            (channel_id,),
        )
        channel = await cur.fetchone()

    if not channel:
        # A deleted/expired binding intentionally has no LINE response. Return
        # success so LINE does not keep retrying an event that can never be
        # processed after the course cleanup.
        return {"status": "unbound"}

    try:
        channel_secret = read_channel_secret(channel)
    except ValueError as error:
        logger.error(
            "webhook_credentials outcome=unavailable channel=%s",
            mask_identifier(channel_id),
        )
        raise HTTPException(503, "Channel credentials unavailable") from error

    # Verify LINE signature
    signature = request.headers.get("x-line-signature", "")
    if not verify_signature(body, signature, channel_secret):
        raise HTTPException(403, "Invalid signature")

    # Do not reveal Channel state until LINE's signature has been verified.
    if channel["expires_at"]:
        if channel["expires_at"] < datetime.now(timezone.utc).isoformat():
            return {"status": "expired"}

    # Parse events
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(400, "Invalid payload") from error
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload")

    raw_events = payload.get("events", [])
    if not isinstance(raw_events, list):
        raise HTTPException(400, "Invalid payload")
    if len(raw_events) > settings.line_webhook_max_events_per_request:
        raise HTTPException(413, "Too many webhook events")
    pending_questions: list[tuple[str, str, str, str | None]] = []
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        if (
            event.get("type") != "message"
            or not isinstance(message, dict)
            or message.get("type") != "text"
        ):
            continue

        question = message.get("text", "")
        reply_token = event.get("replyToken", "")
        raw_event_id = event.get("webhookEventId")
        event_id = (
            raw_event_id
            if isinstance(raw_event_id, str) and 1 <= len(raw_event_id) <= 255
            else None
        )
        source = event.get("source")
        user_id = source.get("userId", "") if isinstance(source, dict) else ""
        if (
            not isinstance(question, str)
            or not 1 <= len(question) <= 10_000
            or not isinstance(user_id, str)
            or not user_id
        ):
            continue
        if not isinstance(reply_token, str):
            reply_token = ""

        pending_questions.append((user_id, reply_token, question, event_id))
        logger.info(
            "webhook_message outcome=accepted channel=%s user=%s",
            mask_identifier(channel_id),
            mask_identifier(user_id),
        )

        # Loading, query, and Push all run after the webhook has been accepted,
        # so the endpoint is not held open by LINE or NotebookLM network calls.
    if not pending_questions:
        return {"status": "ok"}

    saturated = False
    access_token: str | None = None
    event_tenant_key = _tenant_key(channel_id)
    for user_id, reply_token, question, event_id in pending_questions:
        event_claim = None
        if event_id:
            payload_encrypted = _event_payload_ciphertext(
                channel_id,
                user_id,
                question,
            )
            try:
                event_claim = await _claim_event(
                    event_id,
                    payload_encrypted,
                    event_tenant_key,
                )
            except EventLedgerFull:
                saturated = True
                logger.warning(
                    "webhook_message outcome=ledger_saturated channel=%s",
                    mask_identifier(channel_id),
                )
                continue
        if event_id and event_claim is None:
            logger.info(
                "webhook_message outcome=duplicate channel=%s",
                mask_identifier(channel_id),
            )
            continue
        if not event_id and access_token is None:
            # Events without LINE's durable id cannot be replayed safely. This
            # fallback remains for compatibility with older webhook payloads.
            try:
                access_token = read_channel_access_token(channel)
            except ValueError as error:
                logger.error(
                    "webhook_access_token outcome=unavailable channel=%s",
                    mask_identifier(channel_id),
                )
                raise HTTPException(503, "Channel credentials unavailable") from error
        if not _schedule_question(
            channel_id,
            user_id,
            reply_token,
            access_token or "",
            question,
            event_claim,
        ):
            if event_claim:
                await _release_unstarted_event(event_claim)
                # The encrypted payload is durable, so LINE can receive a fast
                # 2xx while the internal scheduler processes it later.
                logger.warning(
                    "webhook_message outcome=queued_for_replay channel=%s",
                    mask_identifier(channel_id),
                )
            else:
                saturated = True
                logger.warning(
                    "webhook_message outcome=queue_saturated channel=%s",
                    mask_identifier(channel_id),
                )

    if saturated:
        # Only legacy events without an id depend on LINE redelivery.
        raise HTTPException(503, "Webhook queue is busy")
    return {"status": "ok"}


async def _run_claimed_event(
    claim: EventClaim,
    reply_token: str = "",
) -> None:
    """Execute a durable event using only encrypted persisted context."""

    transitioned = False
    release_needed = True
    channel_id = ""
    try:
        payload = await _read_claimed_payload(claim)
        channel_id = payload.channel_id
        access_token = await _read_event_access_token(payload.channel_id)
        delivered = await _load_ask_and_push(
            payload.channel_id,
            payload.user_id,
            reply_token,
            access_token,
            payload.question,
            claim.event_id,
        )
        if delivered:
            transitioned = await _shield_transition(_complete_event(claim))
            if not transitioned:
                logger.warning(
                    "webhook_event_complete outcome=lease_lost channel=%s",
                    mask_identifier(channel_id),
                )
        else:
            transitioned = await _shield_transition(
                _record_event_failure(
                    claim,
                    "answer_delivery_failed",
                    retryable=True,
                )
            )
    except EventClaimLost:
        release_needed = False
        logger.warning("webhook_event_worker outcome=lease_lost")
    except EventProcessingError as error:
        transitioned = await _shield_transition(
            _record_event_failure(
                claim,
                error.code,
                retryable=error.retryable,
            )
        )
        logger.warning(
            "webhook_event_worker outcome=rejected channel=%s error_code=%s",
            mask_identifier(channel_id),
            _safe_error_code(error.code),
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.error(
            "webhook_event_worker outcome=failed channel=%s error_type=%s",
            mask_identifier(channel_id),
            type(error).__name__,
        )
        transitioned = await _shield_transition(
            _record_event_failure(
                claim,
                "worker_error",
                retryable=True,
            )
        )
    finally:
        if release_needed and not transitioned:
            try:
                await _shield_transition(_release_event(claim))
            except Exception as release_error:
                logger.error(
                    "webhook_event_release outcome=failed channel=%s error_type=%s",
                    mask_identifier(channel_id),
                    type(release_error).__name__,
                )


async def _run_question(
    channel_id: str,
    user_id: str,
    reply_token: str,
    access_token: str,
    question: str,
    event_claim: EventClaim | None = None,
) -> None:
    ledger_completed = False
    try:
        delivered = await _load_ask_and_push(
            channel_id, user_id, reply_token, access_token, question
        )
        if event_claim and delivered:
            ledger_completed = await _shield_transition(_complete_event(event_claim))
            if not ledger_completed:
                logger.warning(
                    "webhook_event_complete outcome=lease_lost channel=%s",
                    mask_identifier(channel_id),
                )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.error(
            "webhook_worker outcome=failed channel=%s error_type=%s",
            mask_identifier(channel_id),
            type(error).__name__,
        )
    finally:
        if event_claim and not ledger_completed:
            try:
                await _shield_transition(_release_event(event_claim))
            except Exception as release_error:
                logger.error(
                    "webhook_event_release outcome=failed channel=%s error_type=%s",
                    mask_identifier(channel_id),
                    type(release_error).__name__,
                )


async def _load_ask_and_push(
    channel_id: str,
    user_id: str,
    reply_token: str,
    access_token: str,
    question: str,
    event_id: str | None = None,
) -> bool:
    """Return true only after every intended NotebookLM answer is pushed."""

    loading_stop: asyncio.Event | None = None
    loading_renewal: asyncio.Task[None] | None = None
    try:
        loading_shown = False
        try:
            loading_shown = await show_loading(user_id, access_token, seconds=60)
        except Exception as error:
            # Loading is best-effort and must not block the answer path.
            logger.warning(
                "webhook_loading outcome=failed channel=%s error_type=%s",
                mask_identifier(channel_id),
                type(error).__name__,
            )

        if loading_shown:
            loading_stop = asyncio.Event()
            loading_renewal = asyncio.create_task(
                _renew_loading_until_stopped(
                    channel_id,
                    user_id,
                    access_token,
                    loading_stop,
                )
            )

        if not loading_shown and reply_token:
            try:
                await reply_text(reply_token, access_token, "⏳正在查詢中，請稍後")
            except Exception as error:
                # A stale one-time Reply Token must never prevent the later
                # Notebook query and Push response.
                logger.warning(
                    "webhook_progress_reply outcome=failed channel=%s error_type=%s",
                    mask_identifier(channel_id),
                    type(error).__name__,
                )

        try:
            messages = await ask_question(channel_id, user_id, question)
        finally:
            if loading_stop is not None:
                loading_stop.set()
            if loading_renewal is not None:
                await loading_renewal
        if not messages:
            raise RuntimeError("empty_answer_messages")
        for message_index, msg in enumerate(messages):
            retry_key = _push_retry_key(event_id, "answer", message_index)
            if retry_key:
                await push_text(
                    user_id,
                    access_token,
                    msg,
                    retry_key=retry_key,
                )
            else:
                await push_text(user_id, access_token, msg)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.error(
            "webhook_background outcome=failed channel=%s error_type=%s",
            mask_identifier(channel_id),
            type(error).__name__,
        )
        try:
            error_retry_key = _push_retry_key(event_id, "error")
            if error_retry_key:
                await push_text(
                    user_id,
                    access_token,
                    "⚠️ 系統發生錯誤，請稍後再試。",
                    retry_key=error_retry_key,
                )
            else:
                await push_text(
                    user_id,
                    access_token,
                    "⚠️ 系統發生錯誤，請稍後再試。",
                )
            # The user was notified, but the requested answer was not
            # delivered. Keep the event retryable instead of deduplicating it
            # permanently as a completed answer.
            return False
        except Exception as push_error:
            logger.error(
                "webhook_error_push outcome=failed channel=%s error_type=%s",
                mask_identifier(channel_id),
                type(push_error).__name__,
            )
            return False


async def _renew_loading_until_stopped(
    channel_id: str,
    user_id: str,
    access_token: str,
    stop: asyncio.Event,
) -> None:
    """Renew LINE's 60-second loading animation while the answer is pending."""

    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=50)
            return
        except TimeoutError:
            pass

        try:
            renewed = await show_loading(user_id, access_token, seconds=60)
        except Exception as error:
            logger.warning(
                "webhook_loading_renewal outcome=failed channel=%s error_type=%s",
                mask_identifier(channel_id),
                type(error).__name__,
            )
            return
        if not renewed:
            logger.warning(
                "webhook_loading_renewal outcome=rejected channel=%s",
                mask_identifier(channel_id),
            )
            return
