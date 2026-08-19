"""Central course NotebookLM account repository and service.

Authorization material is encrypted at rest and only decrypted while a
client context is active.  The service never returns it from its public
models and deliberately records only coarse error codes.
"""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import os
import tempfile
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol, TypeVar

import aiosqlite

from config import settings
from database import DB
from services.crypto_service import decrypt_json, encrypt_json

logger = logging.getLogger(__name__)
T = TypeVar("T")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class CourseAccountRecord:
    email: str | None
    auth_encrypted: str | None
    auth_mode: str | None
    health_status: str
    last_success_at: str | None
    last_checked_at: str | None
    error_code: str | None
    auth_revision: int
    updated_at: str | None = None

    def safe_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("auth_encrypted", None)
        data.pop("auth_revision", None)
        return data


class AuthorizationPayload(dict[str, Any]):
    """Mutable storage state plus safe persistence outcome metadata."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(copy.deepcopy(payload))
        self.original_payload = copy.deepcopy(payload)
        self.persistence_error: str | None = None


def _validate_storage_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("storage state must be an object")
    cookies = payload.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise ValueError("storage state must contain cookies")
    names: set[str] = set()
    sid_is_usable = False
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise ValueError("cookie must be an object")
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not name:
            raise ValueError("cookie name is required")
        if not isinstance(value, str):
            raise ValueError("cookie value must be a string")
        names.add(name)
        if name == "SID" and value:
            sid_is_usable = True
    if "SID" not in names or not sid_is_usable:
        raise ValueError("SID cookie is required")
    return payload


def _storage_state_signature(payload: dict[str, Any]) -> str:
    """Compare storage states without treating cookie-list order as a change."""

    normalized = copy.deepcopy(payload)
    cookies = normalized.get("cookies")
    if isinstance(cookies, list):
        normalized["cookies"] = sorted(
            cookies,
            key=lambda cookie: (
                str(cookie.get("domain", "")),
                str(cookie.get("path", "")),
                str(cookie.get("name", "")),
            ),
        )
    return json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


class CourseAccountRepository:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DB

    async def get(self) -> CourseAccountRecord | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT email, auth_encrypted, auth_mode, health_status,
                       last_success_at, last_checked_at, error_code,
                       auth_revision, updated_at
                FROM course_notebook_account WHERE id=1
                """
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return CourseAccountRecord(**dict(row))

    async def replace_authorization(
        self,
        *,
        email: str,
        auth_payload: dict[str, Any],
        auth_mode: str,
        checked_at: str,
    ) -> CourseAccountRecord:
        """Atomically replace authorization only after caller validation."""

        encrypted = encrypt_json(auth_payload)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await (
                await db.execute("SELECT email FROM course_notebook_account WHERE id=1")
            ).fetchone()
            email_changed = bool(
                current
                and current[0]
                and str(current[0]).casefold() != email.casefold()
            )
            await db.execute(
                """
                INSERT INTO course_notebook_account (
                    id, email, auth_encrypted, auth_mode, health_status,
                    last_success_at, last_checked_at, error_code, updated_at
                    , auth_revision
                ) VALUES (1, ?, ?, ?, 'healthy', ?, ?, NULL, ?, 1)
                ON CONFLICT(id) DO UPDATE SET
                    email=excluded.email,
                    auth_encrypted=excluded.auth_encrypted,
                    auth_mode=excluded.auth_mode,
                    health_status='healthy',
                    last_success_at=excluded.last_success_at,
                    last_checked_at=excluded.last_checked_at,
                    error_code=NULL,
                    updated_at=excluded.updated_at,
                    auth_revision=course_notebook_account.auth_revision + 1
                """,
                (email, encrypted, auth_mode, checked_at, checked_at, checked_at),
            )
            if email_changed:
                # Notebooks were shared with the previous Gmail. Keep every
                # mapping, but require a fresh access check after an account swap.
                await db.execute(
                    """
                    UPDATE channels
                    SET binding_status='course_account_unavailable',
                        binding_revision=binding_revision + 1,
                        binding_operation_id=NULL,
                        binding_check_previous_status=NULL, updated_at=?
                    WHERE notebook_id IS NOT NULL AND binding_status<>'unbound'
                    """,
                    (checked_at,),
                )
            await db.commit()
        record = await self.get()
        assert record is not None
        return record

    async def update_health_if_current(
        self,
        record: CourseAccountRecord,
        status: str,
        *,
        error_code: str | None = None,
        success: bool = False,
    ) -> tuple[CourseAccountRecord | None, bool]:
        """Use the authorization revision as a CAS guard around health writes."""

        checked_at = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            if success:
                cursor = await db.execute(
                    """
                    UPDATE course_notebook_account
                    SET health_status=?, last_success_at=?, last_checked_at=?,
                        error_code=?, updated_at=?
                    WHERE id=1
                      AND auth_revision=?
                    """,
                    (
                        status,
                        checked_at,
                        checked_at,
                        error_code,
                        checked_at,
                        record.auth_revision,
                    ),
                )
            else:
                cursor = await db.execute(
                    """
                    UPDATE course_notebook_account
                    SET health_status=?, last_checked_at=?, error_code=?, updated_at=?
                    WHERE id=1
                      AND auth_revision=?
                    """,
                    (
                        status,
                        checked_at,
                        error_code,
                        checked_at,
                        record.auth_revision,
                    ),
                )
            await db.commit()
        return await self.get(), cursor.rowcount == 1

    async def persist_authorization_if_current(
        self,
        record: CourseAccountRecord,
        auth_payload: dict[str, Any],
    ) -> tuple[CourseAccountRecord | None, bool]:
        """Encrypt and save rotated cookies without overwriting a newer login."""

        encrypted = encrypt_json(auth_payload)
        updated_at = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE course_notebook_account
                SET auth_encrypted=?, auth_revision=auth_revision + 1, updated_at=?
                WHERE id=1 AND auth_revision=?
                """,
                (encrypted, updated_at, record.auth_revision),
            )
            await db.commit()
        return await self.get(), cursor.rowcount == 1


class NotebookClient(Protocol):
    notebooks: Any
    chat: Any
    sources: Any


ClientContextBuilder = Callable[[dict[str, Any], float], Any]


@asynccontextmanager
async def default_client_context(
    auth_payload: dict[str, Any], timeout_seconds: float
) -> AsyncIterator[NotebookClient]:
    """Create a client from an explicit, short-lived 0600 storage file.

    Passing an explicit file avoids mutating ``NOTEBOOKLM_AUTH_JSON`` and the
    process-wide race that the former per-channel implementation had.
    """

    fd, path = tempfile.mkstemp(prefix="course-nlm-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(auth_payload, handle, separators=(",", ":"))
        fd = -1

        # Import lazily: application startup and unit tests do not require the
        # unofficial dependency until an actual NotebookLM request is made.
        from notebooklm import NotebookLMClient  # type: ignore[import-not-found]

        async with NotebookLMClient.from_storage(
            path,
            timeout=timeout_seconds,
            # notebooklm-py 0.8.x otherwise floors the automatic chat timeout
            # at the general HTTP timeout.  Keep this explicit and below the
            # outer lifecycle deadline so conversation cleanup and auth-state
            # persistence still get a chance to finish.
            chat_timeout=settings.notebook_chat_timeout_seconds,
            max_concurrent_rpcs=1,
        ) as client:
            yield client
        try:
            with open(path, encoding="utf-8") as handle:
                updated_payload = _validate_storage_state(json.load(handle))
        except Exception:
            if isinstance(auth_payload, AuthorizationPayload):
                auth_payload.persistence_error = "auth_persistence_failed"
            logger.error(
                "course_account_auth_persistence outcome=invalid_storage_state"
            )
        else:
            auth_payload.clear()
            auth_payload.update(updated_payload)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


class NotebookServiceError(Exception):
    """A safe, classified error suitable for route and LINE handling."""

    def __init__(
        self,
        code: str,
        *,
        status: str = "error",
        http_status: int = 502,
        user_message: str = "NotebookLM 暫時無法使用，請稍後再試。",
    ):
        super().__init__(code)
        self.code = code
        self.status = status
        self.http_status = http_status
        self.user_message = user_message


def classify_notebook_error(
    exc: BaseException, *, existing_binding: bool = False
) -> NotebookServiceError:
    """Map upstream failures without exposing their original messages."""

    if isinstance(exc, NotebookServiceError):
        return exc
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return NotebookServiceError(
            "upstream_timeout",
            http_status=504,
            user_message="NotebookLM 回應逾時，請稍後再試。",
        )

    name = type(exc).__name__.lower()
    status_code = getattr(exc, "status_code", None)
    rpc_code = getattr(exc, "rpc_code", None)
    # Exception text is used only for classification and is never logged or
    # returned.  This accommodates older notebooklm-py versions whose errors
    # did not expose structured status fields.
    message = str(exc).lower()

    if "ratelimit" in name or status_code == 429 or rpc_code == 429:
        return NotebookServiceError(
            "rate_limited",
            http_status=429,
            user_message="NotebookLM 目前查詢較多，請稍後再試。",
        )
    if (
        "auth" in name
        or "configuration" in name
        or status_code == 401
        or rpc_code == 401
        or any(
            marker in message
            for marker in (
                "sign in",
                "login expired",
                "cookie expired",
                "authentication expired or invalid",
            )
        )
    ):
        return NotebookServiceError(
            "course_auth_expired",
            status="course_account_unavailable",
            http_status=503,
            user_message="課程 NotebookLM 帳號暫時無法連線，請聯繫管理者。",
        )
    if (
        "notfound" in name
        or status_code in (403, 404)
        or rpc_code in (403, 404)
        or any(
            marker in message for marker in ("permission", "forbidden", "not shared")
        )
    ):
        return NotebookServiceError(
            "access_revoked" if existing_binding else "not_shared",
            status="access_revoked" if existing_binding else "error",
            http_status=403,
            user_message="目前無法讀取這本 Notebook，請確認已用檢視者權限分享給課程帳號。",
        )
    if "timeout" in name:
        return NotebookServiceError(
            "upstream_timeout",
            http_status=504,
            user_message="NotebookLM 回應逾時，請稍後再試。",
        )
    if (
        "network" in name
        or "servererror" in name
        or status_code in (500, 502, 503, 504)
    ):
        return NotebookServiceError(
            "upstream_unavailable",
            http_status=503,
            user_message="NotebookLM 服務暫時無法連線，請稍後再試。",
        )
    return NotebookServiceError("unknown_upstream_error")


class QueryMetrics:
    """Small in-process metrics surface without question or credential data."""

    def __init__(self):
        self._counts: Counter[str] = Counter()
        self._events: Counter[str] = Counter()
        self._latency_total = 0.0
        self._latency_max = 0.0

    def observe(self, outcome: str, duration: float) -> None:
        self._counts[outcome] += 1
        self._latency_total += duration
        self._latency_max = max(self._latency_max, duration)

    def record_event(self, event: str) -> None:
        self._events[event] += 1

    def snapshot(self) -> dict[str, Any]:
        total = sum(self._counts.values())
        return {
            "counts": dict(self._counts),
            "events": dict(self._events),
            "requests": total,
            "latency_seconds_avg": self._latency_total / total if total else 0.0,
            "latency_seconds_max": self._latency_max,
        }


class CentralNotebookClientFactory:
    def __init__(
        self,
        *,
        max_concurrency: int,
        max_queue_size: int = 20,
        timeout_seconds: float,
        context_builder: ClientContextBuilder = default_client_context,
    ):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_queue_size < 0:
            raise ValueError("max_queue_size cannot be negative")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._admission_lock = asyncio.Lock()
        self._admitted = 0
        self._max_admitted = max_concurrency + max_queue_size
        self.timeout_seconds = timeout_seconds
        self.context_builder = context_builder
        self.metrics = QueryMetrics()

    async def run(
        self,
        auth_payload: dict[str, Any],
        operation_name: str,
        operation: Callable[[NotebookClient], Awaitable[T]],
        *,
        existing_binding: bool = False,
    ) -> T:
        start = time.monotonic()
        admitted = False
        acquired = False
        outcome = "success"
        try:
            async with self._admission_lock:
                if self._admitted >= self._max_admitted:
                    outcome = "queue_saturated"
                    raise NotebookServiceError(
                        "query_capacity_exceeded",
                        http_status=429,
                        user_message="目前查詢人數較多，請稍後再試。",
                    )
                self._admitted += 1
                admitted = True

            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(), timeout=self.timeout_seconds
                )
                acquired = True
            except asyncio.TimeoutError as exc:
                outcome = "queue_saturated"
                raise NotebookServiceError(
                    "query_capacity_exceeded",
                    http_status=429,
                    user_message="目前查詢人數較多，請稍後再試。",
                ) from exc

            async def execute() -> T:
                async with self.context_builder(
                    auth_payload, self.timeout_seconds
                ) as client:
                    return await operation(client)

            return await asyncio.wait_for(execute(), timeout=self.timeout_seconds)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except BaseException as exc:
            error = classify_notebook_error(exc, existing_binding=existing_binding)
            outcome = error.code
            raise error from exc
        finally:
            if acquired:
                self._semaphore.release()
            if admitted:
                async with self._admission_lock:
                    self._admitted -= 1
            duration = time.monotonic() - start
            self.metrics.observe(outcome, duration)
            # Do not include auth, notebook URL, question, or exception text.
            logger.info(
                "notebook_operation operation=%s outcome=%s duration_ms=%d",
                operation_name,
                outcome,
                int(duration * 1000),
            )


class CourseAccountService:
    def __init__(
        self,
        repository: CourseAccountRepository,
        client_factory: CentralNotebookClientFactory,
    ):
        self.repository = repository
        self.client_factory = client_factory

    async def status(self) -> dict[str, Any]:
        record = await self.repository.get()
        if record is None:
            return {
                "email": None,
                "health_status": "unconfigured",
                "auth_mode": None,
                "last_success_at": None,
                "last_checked_at": None,
                "error_code": None,
            }
        return record.safe_dict()

    async def public_status(self) -> dict[str, Any]:
        status = await self.status()
        return {"email": status["email"], "health_status": status["health_status"]}

    async def require_authorization(
        self, record: CourseAccountRecord | None = None
    ) -> tuple[CourseAccountRecord, dict[str, Any]]:
        record = record or await self.repository.get()
        if record is None or not record.auth_encrypted:
            raise NotebookServiceError(
                "course_account_unconfigured",
                status="course_account_unavailable",
                http_status=503,
                user_message="課程 NotebookLM 帳號尚未完成設定，請聯繫管理者。",
            )
        if record.health_status == "expired":
            raise NotebookServiceError(
                "course_auth_expired",
                status="course_account_unavailable",
                http_status=503,
                user_message="課程 NotebookLM 帳號暫時無法連線，請聯繫管理者。",
            )
        try:
            payload = decrypt_json(record.auth_encrypted)
        except Exception as exc:
            await self.repository.update_health_if_current(
                record, "error", error_code="authorization_decrypt_failed"
            )
            raise NotebookServiceError(
                "course_authorization_unavailable",
                status="course_account_unavailable",
                http_status=503,
                user_message="課程 NotebookLM 帳號暫時無法連線，請聯繫管理者。",
            ) from exc
        return record, AuthorizationPayload(payload)

    async def _record_persistence_failure(
        self, record: CourseAccountRecord
    ) -> CourseAccountRecord | None:
        self.client_factory.metrics.record_event("auth_persistence_failed")
        logger.error("course_account_auth_persistence outcome=failed")
        try:
            updated, _ = await self.repository.update_health_if_current(
                record, "error", error_code="auth_persistence_failed"
            )
            return updated
        except Exception:
            logger.error("course_account_auth_persistence outcome=health_update_failed")
            return None

    async def mark_query_success(
        self,
        record: CourseAccountRecord,
        auth_payload: dict[str, Any] | None = None,
    ) -> bool:
        """Persist rotations, then mark a still-current account healthy.

        Persistence failures never discard an otherwise successful answer. A
        stale CAS candidate is also harmless: a concurrent request already
        saved a newer revision, so the next operation will load that revision.
        """

        current_record = record
        if isinstance(auth_payload, AuthorizationPayload):
            if auth_payload.persistence_error:
                await self._record_persistence_failure(record)
                return True
            try:
                changed = _storage_state_signature(
                    auth_payload.original_payload
                ) != _storage_state_signature(auth_payload)
                if changed:
                    (
                        current,
                        applied,
                    ) = await self.repository.persist_authorization_if_current(
                        record, _validate_storage_state(dict(auth_payload))
                    )
                    if applied and current is not None:
                        current_record = current
                    elif current is None:
                        return False
                    elif (current.email or "").casefold() != (
                        record.email or ""
                    ).casefold():
                        return False
                    else:
                        # A concurrent rotation/re-auth for the same account won.
                        return True
            except Exception:
                await self._record_persistence_failure(record)
                return True

        _, applied = await self.repository.update_health_if_current(
            current_record, "healthy", success=True
        )
        return applied

    async def configure(
        self,
        *,
        email: str,
        auth_payload: dict[str, Any],
        auth_mode: str = "storage_state",
    ) -> dict[str, Any]:
        if auth_mode != "storage_state":
            raise NotebookServiceError(
                "unsupported_auth_mode",
                http_status=422,
                user_message="不支援此授權格式。",
            )
        if (
            not isinstance(auth_payload.get("cookies"), list)
            or not auth_payload["cookies"]
        ):
            raise NotebookServiceError(
                "invalid_authorization",
                http_status=422,
                user_message="授權資料格式不正確。",
            )

        normalized_email = email.strip().lower()

        async def probe(client: NotebookClient) -> None:
            await client.notebooks.list()
            actual_email = getattr(getattr(client, "auth", None), "account_email", None)
            if (
                actual_email
                and str(actual_email).strip().casefold() != normalized_email.casefold()
            ):
                raise NotebookServiceError(
                    "authorization_email_mismatch",
                    http_status=422,
                    user_message="輸入的 Gmail 與授權資料所屬帳號不一致。",
                )

        # Validate first.  Any exception leaves the previous DB row untouched.
        candidate = AuthorizationPayload(auth_payload)
        await self.client_factory.run(candidate, "configure_probe", probe)
        if candidate.persistence_error:
            raise NotebookServiceError(
                "auth_persistence_failed",
                http_status=503,
                user_message="授權驗證成功，但無法安全保存更新後的登入狀態，請稍後再試。",
            )
        checked_at = utc_now_iso()
        record = await self.repository.replace_authorization(
            email=normalized_email,
            auth_payload=dict(candidate),
            auth_mode=auth_mode,
            checked_at=checked_at,
        )
        return record.safe_dict()

    async def check_health(self) -> dict[str, Any]:
        record = await self.repository.get()
        if record is None or not record.auth_encrypted:
            return await self.status()

        try:
            checked_record, payload = await self.require_authorization(record)

            async def probe(client: NotebookClient) -> None:
                await client.notebooks.list()

            await self.client_factory.run(payload, "health_check", probe)
        except NotebookServiceError as exc:
            health = "expired" if exc.code == "course_auth_expired" else "error"
            updated, _ = await self.repository.update_health_if_current(
                record, health, error_code=exc.code
            )
            return updated.safe_dict() if updated else await self.status()

        await self.mark_query_success(checked_record, payload)
        return await self.status()

    async def mark_query_failure(
        self, record: CourseAccountRecord, error: NotebookServiceError
    ) -> bool:
        if error.code in {"course_auth_expired", "course_authorization_unavailable"}:
            _, applied = await self.repository.update_health_if_current(
                record, "expired", error_code=error.code
            )
            return applied
        elif error.code in {"upstream_unavailable", "upstream_timeout"}:
            _, applied = await self.repository.update_health_if_current(
                record, "error", error_code=error.code
            )
            return applied
        current = await self.repository.get()
        return bool(current and current.auth_revision == record.auth_revision)


course_account_repository = CourseAccountRepository()
notebook_client_factory = CentralNotebookClientFactory(
    max_concurrency=getattr(settings, "notebook_query_concurrency", 2),
    max_queue_size=getattr(settings, "notebook_query_queue_limit", 20),
    timeout_seconds=getattr(settings, "notebook_query_timeout_seconds", 210.0),
)
course_account_service = CourseAccountService(
    course_account_repository, notebook_client_factory
)
