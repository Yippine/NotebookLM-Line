"""NotebookLM binding and question service.

New shared bindings use one encrypted course account plus a per-channel
Notebook ID.  Legacy per-channel cookie helpers remain only for the
feature-flagged administrator migration endpoints in ``routers.auth``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
from typing import Any
import weakref
import logging

import aiosqlite

from config import settings
from database import DB
from services.course_account_service import (
    NotebookClient,
    NotebookServiceError,
    course_account_service,
    notebook_client_factory,
)
from services.crypto_service import decrypt_json, encrypt_json
from services.text_formatter import format_for_line


MINIMAL_BINDING_QUESTION = "請只回答「連線成功」。"
logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class ChannelBinding:
    channel_id: str
    notebook_id: str | None
    notebook_title: str | None
    status: str
    bound_at: str | None
    last_access_checked_at: str | None
    revision: int
    operation_id: str | None
    updated_at: str | None

    def response_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "notebook_id": self.notebook_id,
            "notebook_title": self.notebook_title,
            "last_access_checked_at": self.last_access_checked_at,
        }


class ChannelBindingRepository:
    def __init__(
        self,
        db_path: str | None = None,
        *,
        check_lease_seconds: float | None = None,
    ):
        self.db_path = db_path or DB
        # ``CentralNotebookClientFactory.run`` can spend one timeout waiting
        # for a concurrency slot and another running the upstream operation.
        # Keep a margin above that worst case, while ensuring a crashed worker
        # cannot leave a Channel in `checking` forever.
        self.check_lease_seconds = (
            check_lease_seconds
            if check_lease_seconds is not None
            else max(300.0, settings.notebook_query_timeout_seconds * 2 + 30.0)
        )
        if self.check_lease_seconds <= 0:
            raise ValueError("check_lease_seconds must be positive")

    async def _recover_expired_check(
        self,
        db: aiosqlite.Connection,
        channel_id: str,
        *,
        now: str,
    ) -> bool:
        """Release an expired check lease while preserving its prior status.

        ``binding_operation_id`` is the ownership token. Clearing it prevents
        the abandoned worker from committing a late result, because every
        finish operation compares the old token. Older databases may contain
        a stuck ``checking`` row without ``binding_check_previous_status``;
        those rows recover to a safe status inferred from whether a mapping
        exists.
        """

        cutoff = (
            datetime.fromisoformat(now) - timedelta(seconds=self.check_lease_seconds)
        ).isoformat()
        cursor = await db.execute(
            """
            UPDATE channels
            SET binding_status=CASE
                    WHEN binding_check_previous_status IN (
                        'unbound', 'bound', 'access_revoked',
                        'course_account_unavailable', 'error'
                    ) THEN binding_check_previous_status
                    WHEN notebook_id IS NULL THEN 'unbound'
                    ELSE 'error'
                END,
                binding_operation_id=NULL,
                binding_check_previous_status=NULL,
                updated_at=?
            WHERE channel_id=? AND binding_status='checking'
              AND (
                  binding_operation_id IS NULL
                  OR updated_at IS NULL
                  OR julianday(updated_at) IS NULL
                  OR julianday(updated_at) <= julianday(?)
              )
            """,
            (now, channel_id, cutoff),
        )
        return cursor.rowcount == 1

    async def get(self, channel_id: str) -> ChannelBinding | None:
        async with aiosqlite.connect(self.db_path) as db:
            await self._recover_expired_check(db, channel_id, now=utc_now_iso())
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT channel_id, notebook_id, notebook_display_name,
                       binding_status, bound_at, last_access_checked_at,
                       binding_revision, binding_operation_id, updated_at
                FROM channels WHERE channel_id=?
                """,
                (channel_id,),
            )
            row = await cur.fetchone()
            await db.commit()
        if row is None:
            return None
        return ChannelBinding(
            channel_id=row["channel_id"],
            notebook_id=row["notebook_id"],
            notebook_title=row["notebook_display_name"],
            status=row["binding_status"]
            or ("bound" if row["notebook_id"] else "unbound"),
            bound_at=row["bound_at"],
            last_access_checked_at=row["last_access_checked_at"],
            revision=row["binding_revision"],
            operation_id=row["binding_operation_id"],
            updated_at=row["updated_at"],
        )

    async def replace(
        self, channel_id: str, notebook_id: str, notebook_title: str | None
    ) -> ChannelBinding:
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                """
                UPDATE channels
                SET notebook_id=?, notebook_display_name=?, binding_status='bound',
                    bound_at=?, last_access_checked_at=?,
                    nlm_auth_json_encrypted=NULL,
                    binding_revision=binding_revision + 1,
                    binding_operation_id=NULL,
                    binding_check_previous_status=NULL, updated_at=?
                WHERE channel_id=?
                """,
                (notebook_id, notebook_title, now, now, now, channel_id),
            )
            if cur.rowcount != 1:
                await db.rollback()
                raise KeyError("channel_not_found")
            await db.commit()
        binding = await self.get(channel_id)
        assert binding is not None
        return binding

    async def update_status_if_current(
        self, binding: ChannelBinding, status: str
    ) -> tuple[ChannelBinding | None, bool]:
        """Update status only if no rebind/unbind/account swap raced this call."""

        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE channels
                SET binding_status=?, last_access_checked_at=?, updated_at=?
                WHERE channel_id=? AND notebook_id=?
                  AND binding_revision=? AND binding_operation_id IS NULL
                """,
                (
                    status,
                    now,
                    now,
                    binding.channel_id,
                    binding.notebook_id,
                    binding.revision,
                ),
            )
            await db.commit()
        return await self.get(binding.channel_id), cursor.rowcount == 1

    async def begin_check(self, binding: ChannelBinding) -> ChannelBinding:
        now = utc_now_iso()
        operation_id = secrets.token_urlsafe(18)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            # A caller may have read a row just before its previous owner died.
            # Recover the expired owner inside the same write transaction used
            # to acquire the replacement lease, avoiding a recovery/acquire race.
            await self._recover_expired_check(db, binding.channel_id, now=now)
            cursor = await db.execute(
                """
                UPDATE channels
                SET binding_check_previous_status=binding_status,
                    binding_status='checking', binding_operation_id=?, updated_at=?
                WHERE channel_id=? AND binding_revision=?
                  AND binding_operation_id IS NULL
                """,
                (
                    operation_id,
                    now,
                    binding.channel_id,
                    binding.revision,
                ),
            )
            await db.commit()
        if cursor.rowcount != 1:
            raise NotebookServiceError(
                "binding_check_in_progress",
                status="checking",
                http_status=409,
                user_message="Notebook 正在檢查中，請稍後再試。",
            )
        current = await self.get(binding.channel_id)
        if current is None:
            raise KeyError("channel_not_found")
        return current

    async def finish_check(
        self,
        checking: ChannelBinding,
        *,
        status: str,
        notebook_title: str | None = None,
    ) -> tuple[ChannelBinding | None, bool]:
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE channels
                SET notebook_display_name=COALESCE(?, notebook_display_name),
                    binding_status=?, binding_operation_id=NULL,
                    binding_check_previous_status=NULL,
                    last_access_checked_at=?, updated_at=?
                WHERE channel_id=? AND binding_revision=?
                  AND binding_operation_id=?
                """,
                (
                    notebook_title,
                    status,
                    now,
                    now,
                    checking.channel_id,
                    checking.revision,
                    checking.operation_id,
                ),
            )
            await db.commit()
        return await self.get(checking.channel_id), cursor.rowcount == 1

    async def complete_bind(
        self,
        checking: ChannelBinding,
        notebook_id: str,
        notebook_title: str | None,
    ) -> tuple[ChannelBinding | None, bool]:
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE channels
                SET notebook_id=?, notebook_display_name=?, binding_status='bound',
                    bound_at=?, last_access_checked_at=?,
                    nlm_auth_json_encrypted=NULL,
                    binding_revision=binding_revision + 1,
                    binding_operation_id=NULL,
                    binding_check_previous_status=NULL, updated_at=?
                WHERE channel_id=? AND binding_revision=?
                  AND binding_operation_id=?
                """,
                (
                    notebook_id,
                    notebook_title,
                    now,
                    now,
                    now,
                    checking.channel_id,
                    checking.revision,
                    checking.operation_id,
                ),
            )
            await db.commit()
        return await self.get(checking.channel_id), cursor.rowcount == 1

    async def unbind(self, channel_id: str) -> ChannelBinding:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                UPDATE channels
                SET notebook_id=NULL, notebook_display_name=NULL,
                    binding_status='unbound', bound_at=NULL,
                    last_access_checked_at=NULL,
                    nlm_auth_json_encrypted=CASE
                        WHEN bound_at IS NULL AND nlm_auth_json_encrypted IS NOT NULL
                        THEN nlm_auth_json_encrypted
                        ELSE NULL
                    END,
                    binding_revision=binding_revision + 1,
                    binding_operation_id=NULL,
                    binding_check_previous_status=NULL, updated_at=?
                WHERE channel_id=?
                """,
                (utc_now_iso(), channel_id),
            )
            if cur.rowcount != 1:
                raise KeyError("channel_not_found")
            await db.commit()
        binding = await self.get(channel_id)
        assert binding is not None
        return binding


class StatelessConversationIsolation:
    """Serialize and clean NotebookLM's account-global current conversation.

    notebooklm-py 0.7.3 documents that ``conversation_id=None`` continues the
    account's current conversation for a notebook.  Therefore a mere
    ``(channel, user) -> conversation_id`` cache does *not* isolate each
    user's first question.  The safe first version is stateless: under a
    per-notebook lock, remove any current conversation, ask once, and remove
    the newly created conversation before another channel/user may query the
    same notebook.  This deliberately trades follow-up memory for isolation.
    """

    def __init__(self):
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def lock(self, notebook_id: str) -> asyncio.Lock:
        lock = self._locks.get(notebook_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[notebook_id] = lock
        return lock

    async def ask_once(
        self,
        client: NotebookClient,
        notebook_id: str,
        question: str,
    ) -> Any:
        current_id = await client.chat.get_conversation_id(notebook_id)
        if current_id:
            deleted = await client.chat.delete_conversation(notebook_id, current_id)
            if not deleted:
                raise NotebookServiceError("conversation_isolation_failed")

        result: Any | None = None
        primary_error: BaseException | None = None
        try:
            result = await client.chat.ask(notebook_id, question)
            return result
        except BaseException as error:
            primary_error = error
            raise
        finally:
            conversation_id = (
                getattr(result, "conversation_id", None) if result else None
            )
            try:
                # The upstream can create the server-side conversation and
                # then fail while resolving the response.  In that case
                # ``result`` is still None, so discover the current id before
                # cleanup instead of leaking context into the next user.
                if not conversation_id:
                    conversation_id = await client.chat.get_conversation_id(notebook_id)
                if conversation_id:
                    deleted = await client.chat.delete_conversation(
                        notebook_id, str(conversation_id)
                    )
                    if not deleted:
                        raise NotebookServiceError("conversation_cleanup_failed")
            except BaseException as cleanup_error:
                if primary_error is None:
                    raise
                # Preserve the original, more useful classification.  The
                # next request remains protected because it performs a
                # mandatory pre-delete under the same notebook lock.
                logger.warning(
                    "conversation_cleanup outcome=failed error_type=%s",
                    type(cleanup_error).__name__,
                )


channel_binding_repository = ChannelBindingRepository()
conversation_isolation = StatelessConversationIsolation()


def _notebook_title(notebook: Any) -> str | None:
    if notebook is None:
        return None
    if isinstance(notebook, dict):
        value = notebook.get("title") or notebook.get("name")
    else:
        value = getattr(notebook, "title", None) or getattr(notebook, "name", None)
    return str(value)[:500] if value else None


def _notebook_id(notebook: Any) -> str | None:
    if notebook is None:
        return None
    if isinstance(notebook, dict):
        value = notebook.get("id") or notebook.get("notebook_id")
    else:
        value = getattr(notebook, "id", None) or getattr(notebook, "notebook_id", None)
    return str(value) if value else None


async def get_binding_status(channel_id: str) -> ChannelBinding:
    binding = await channel_binding_repository.get(channel_id)
    if binding is None:
        raise KeyError("channel_not_found")
    return binding


async def _best_effort_release_check(
    checking: ChannelBinding, fallback_status: str
) -> None:
    """Release this worker's lease after an unclassified failure/cancellation.

    Normal success and classified failures already consume the operation token;
    the repository CAS then makes this cleanup a no-op. A process termination
    cannot run ``finally``, so the repository's time-bounded lease remains the
    second recovery path.
    """

    try:
        await channel_binding_repository.finish_check(checking, status=fallback_status)
    except Exception as error:
        logger.warning(
            "binding_check_cleanup outcome=failed error_type=%s",
            type(error).__name__,
        )


async def _acquire_notebook_lock(
    notebook_id: str,
    *,
    fallback_status: str,
    timeout_seconds: float | None = None,
) -> asyncio.Lock:
    """Acquire the per-Notebook isolation lock without waiting forever.

    This happens before a binding enters ``checking``. Consequently queueing
    behind another Channel that targets the same Notebook consumes no check
    lease and cannot make a healthy worker look abandoned.
    """

    lock = conversation_isolation.lock(notebook_id)
    timeout = (
        settings.notebook_query_timeout_seconds
        if timeout_seconds is None
        else timeout_seconds
    )
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except TimeoutError as error:
        raise NotebookServiceError(
            "notebook_busy",
            status=fallback_status,
            http_status=429,
            user_message="這本 Notebook 目前正在處理其他請求，請稍後再試。",
        ) from error
    return lock


def _question_lock_timeout_seconds() -> float:
    """Reserve enough of the durable event claim for query and LINE delivery.

    A factory call can consume two query-timeout windows (queue + upstream).
    Runtime validation guarantees at least 60 additional seconds in the event
    claim. Use at most half of the configured remainder for this lock wait, so
    an admitted worker cannot merely queue here until its claim is reclaimable.
    """

    query_timeout = float(settings.notebook_query_timeout_seconds)
    claim_timeout = float(settings.line_event_claim_timeout_seconds)
    claim_margin = max(0.0, claim_timeout - (2 * query_timeout))
    return min(query_timeout, claim_margin / 2)


async def test_and_bind_notebook(channel_id: str, notebook_id: str) -> ChannelBinding:
    """Validate get + minimal chat before atomically replacing the mapping."""

    original = await get_binding_status(channel_id)
    notebook_lock = await _acquire_notebook_lock(
        notebook_id, fallback_status=original.status
    )
    try:
        checking = await channel_binding_repository.begin_check(original)
        try:
            return await _test_and_bind_notebook(checking, original, notebook_id)
        finally:
            await _best_effort_release_check(checking, original.status)
    finally:
        notebook_lock.release()


async def _test_and_bind_notebook(
    checking: ChannelBinding,
    original: ChannelBinding,
    notebook_id: str,
) -> ChannelBinding:
    account_record = None

    async def validate(client: NotebookClient) -> str | None:
        notebook = await client.notebooks.get(notebook_id)
        result = await conversation_isolation.ask_once(
            client, notebook_id, MINIMAL_BINDING_QUESTION
        )
        if not getattr(result, "answer", None):
            raise NotebookServiceError("empty_test_answer")
        return _notebook_title(notebook)

    try:
        (
            account_record,
            auth_payload,
        ) = await course_account_service.require_authorization()
        title = await notebook_client_factory.run(
            auth_payload, "test_and_bind", validate, existing_binding=False
        )
    except NotebookServiceError as error:
        if account_record is not None:
            account_current = await course_account_service.mark_query_failure(
                account_record, error
            )
            if not account_current:
                await channel_binding_repository.finish_check(
                    checking, status=original.status
                )
                raise NotebookServiceError(
                    "course_account_changed",
                    status="course_account_unavailable",
                    http_status=409,
                    user_message="課程帳號剛剛已更新，請重新執行測試綁定。",
                ) from error
        failure_status = (
            "course_account_unavailable"
            if error.status == "course_account_unavailable"
            else original.status
        )
        await channel_binding_repository.finish_check(checking, status=failure_status)
        raise

    if not await course_account_service.mark_query_success(
        account_record, auth_payload
    ):
        await channel_binding_repository.finish_check(
            checking, status="course_account_unavailable"
        )
        raise NotebookServiceError(
            "course_account_changed",
            status="course_account_unavailable",
            http_status=409,
            user_message="課程帳號剛剛已更新，請重新執行測試綁定。",
        )
    binding, applied = await channel_binding_repository.complete_bind(
        checking, notebook_id, title
    )
    if not applied or binding is None:
        raise NotebookServiceError(
            "binding_changed",
            status="error",
            http_status=409,
            user_message="Notebook 綁定狀態已變更，請重新整理後再試。",
        )
    return binding


async def recheck_notebook_binding(channel_id: str) -> ChannelBinding:
    binding = await get_binding_status(channel_id)
    if not binding.notebook_id:
        return binding
    if binding.status == "unbound":
        raise NotebookServiceError(
            "binding_probe_required",
            status="unbound",
            http_status=409,
            user_message="此 Channel 尚未完成新版綁定，請貼上 Notebook 網址並執行測試綁定。",
        )
    checking = await channel_binding_repository.begin_check(binding)
    try:
        return await _recheck_notebook_binding(checking, binding)
    finally:
        await _best_effort_release_check(checking, binding.status)


async def _recheck_notebook_binding(
    checking: ChannelBinding, binding: ChannelBinding
) -> ChannelBinding:

    async def check(client: NotebookClient) -> str | None:
        notebook = await client.notebooks.get(binding.notebook_id)
        return _notebook_title(notebook)

    account_record = None
    try:
        (
            account_record,
            auth_payload,
        ) = await course_account_service.require_authorization()
        title = await notebook_client_factory.run(
            auth_payload, "binding_recheck", check, existing_binding=True
        )
    except NotebookServiceError as error:
        if account_record is not None:
            account_current = await course_account_service.mark_query_failure(
                account_record, error
            )
            if not account_current:
                await channel_binding_repository.finish_check(
                    checking, status=binding.status
                )
                raise NotebookServiceError(
                    "course_account_changed",
                    status="course_account_unavailable",
                    http_status=409,
                    user_message="課程帳號剛剛已更新，請重新檢查。",
                ) from error
        status = (
            "course_account_unavailable"
            if error.code in {"course_auth_expired", "course_authorization_unavailable"}
            else error.status
        )
        await channel_binding_repository.finish_check(checking, status=status)
        raise

    # A successful check also refreshes the title atomically without changing
    # the notebook id.  This is safe for renamed notebooks.
    if not await course_account_service.mark_query_success(
        account_record, auth_payload
    ):
        await channel_binding_repository.finish_check(checking, status=binding.status)
        raise NotebookServiceError(
            "course_account_changed",
            status="course_account_unavailable",
            http_status=409,
            user_message="課程帳號剛剛已更新，請重新檢查。",
        )
    current, applied = await channel_binding_repository.finish_check(
        checking, status="bound", notebook_title=title
    )
    if not applied or current is None:
        raise NotebookServiceError(
            "binding_changed",
            status="error",
            http_status=409,
            user_message="Notebook 綁定狀態已變更，請重新整理後再試。",
        )
    return current


async def unbind_notebook(channel_id: str) -> ChannelBinding:
    binding = await channel_binding_repository.unbind(channel_id)
    return binding


async def ask_question(channel_id: str, user_id: str, question: str) -> list[str]:
    """Ask only the notebook mapped to ``channel_id`` using central auth."""

    binding = await channel_binding_repository.get(channel_id)
    if binding is None or not binding.notebook_id:
        return ["⚠️ NotebookLM 尚未綁定，請聯繫管理者完成設定。"]
    if binding.status == "unbound":
        return ["⚠️ NotebookLM 尚未完成新版綁定，請先分享 Notebook 並在設定頁測試連線。"]
    if binding.status == "access_revoked":
        return ["⚠️ 目前無法讀取 Notebook，請重新分享給課程帳號後再測試綁定。"]
    if binding.status == "checking":
        return ["⏳ Notebook 正在檢查中，請稍後再送出問題。"]

    try:
        (
            account_record,
            auth_payload,
        ) = await course_account_service.require_authorization()
    except NotebookServiceError as error:
        await channel_binding_repository.update_status_if_current(
            binding, "course_account_unavailable"
        )
        return [error.user_message]

    async def ask(client: NotebookClient) -> str:
        # channel_id/user_id identify the isolation boundary at the service
        # call.  The first version intentionally retains no conversation
        # state, which is stricter than keeping a per-boundary history.
        result = await conversation_isolation.ask_once(
            client, binding.notebook_id, question
        )
        return str(getattr(result, "answer", ""))

    notebook_lock = await _acquire_notebook_lock(
        binding.notebook_id,
        fallback_status=binding.status,
        timeout_seconds=_question_lock_timeout_seconds(),
    )
    try:
        try:
            answer = await notebook_client_factory.run(
                auth_payload, "chat_ask", ask, existing_binding=True
            )
        except NotebookServiceError as error:
            if not await course_account_service.mark_query_failure(
                account_record, error
            ):
                return ["⚠️ 課程帳號剛剛已更新，請重新送出問題。"]
            status = (
                "course_account_unavailable"
                if error.code
                in {"course_auth_expired", "course_authorization_unavailable"}
                else error.status
            )
            await channel_binding_repository.update_status_if_current(binding, status)
            return [error.user_message]
    finally:
        notebook_lock.release()

    if not await course_account_service.mark_query_success(
        account_record, auth_payload
    ):
        return ["⚠️ 課程帳號剛剛已更新，請重新送出問題。"]
    _, still_current = await channel_binding_repository.update_status_if_current(
        binding, "bound"
    )
    if not still_current:
        return ["⚠️ Notebook 綁定剛剛已更新，請重新送出問題。"]
    return [format_for_line(answer)]


# ---------------------------------------------------------------------------
# Administrator-only legacy migration helpers
# ---------------------------------------------------------------------------


async def list_notebooks(storage_state: dict[str, Any]) -> list[dict[str, str | None]]:
    async def list_operation(client: NotebookClient) -> list[dict[str, str | None]]:
        notebooks = await client.notebooks.list()
        return [
            {"id": _notebook_id(item), "title": _notebook_title(item)}
            for item in notebooks
        ]

    return await notebook_client_factory.run(
        storage_state, "legacy_list", list_operation
    )


async def bind_nlm(channel_id: str, storage_state: dict[str, Any]):
    notebooks = await list_notebooks(storage_state)
    notebook_id = notebooks[0]["id"] if notebooks else None
    encrypted = encrypt_json(storage_state)
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE channels SET nlm_auth_json_encrypted=?, notebook_id=? WHERE channel_id=?",
            (encrypted, notebook_id, channel_id),
        )
        await db.commit()
    return notebook_id, notebooks


async def list_notebooks_for_channel(channel_id: str) -> list[dict[str, str | None]]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nlm_auth_json_encrypted FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        row = await cur.fetchone()
    if not row or not row["nlm_auth_json_encrypted"]:
        return []
    return await list_notebooks(decrypt_json(row["nlm_auth_json_encrypted"]))


async def select_notebook(channel_id: str, notebook_id: str) -> None:
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE channels SET notebook_id=? WHERE channel_id=?",
            (notebook_id, channel_id),
        )
        await db.commit()
