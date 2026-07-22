from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiosqlite
import pytest

from database import init_db
from services.course_account_service import (
    CentralNotebookClientFactory,
    NotebookServiceError,
)
from services import nlm_service


class FakeCourseAccountService:
    def __init__(self):
        self.failures = []

    async def require_authorization(self):
        return SimpleNamespace(email="course@example.com"), {"cookies": [{}]}

    async def mark_query_failure(self, _record, error):
        self.failures.append(error.code)
        return True

    async def mark_query_success(self, _record):
        return True


class FakeNotebooks:
    def __init__(self, title="Notebook", failure=None):
        self.title = title
        self.failure = failure

    async def get(self, notebook_id):
        if self.failure:
            raise self.failure
        return SimpleNamespace(id=notebook_id, title=self.title)

    async def list(self):
        return []


class SharedChatState:
    def __init__(self):
        self.current_id = "old-global-conversation"
        self.deleted = []
        self.answers = []
        self.overlap = 0
        self.max_overlap = 0


class FakeChat:
    def __init__(self, state: SharedChatState):
        self.state = state

    async def get_conversation_id(self, _notebook_id):
        return self.state.current_id

    async def delete_conversation(self, _notebook_id, conversation_id):
        self.state.deleted.append(conversation_id)
        if self.state.current_id == conversation_id:
            self.state.current_id = None
        return True

    async def ask(self, _notebook_id, question):
        # A previous user's current conversation must always have been removed
        # before an answer is generated.
        assert self.state.current_id is None
        self.state.overlap += 1
        self.state.max_overlap = max(self.state.max_overlap, self.state.overlap)
        await asyncio.sleep(0.005)
        conversation_id = f"conversation-{len(self.state.answers) + 1}"
        self.state.current_id = conversation_id
        self.state.answers.append(question)
        self.state.overlap -= 1
        return SimpleNamespace(
            answer="連線成功", conversation_id=conversation_id, references=[]
        )


class FakeSources:
    async def list(self, _notebook_id):
        return []


class FakeClient:
    def __init__(self, state=None, *, failure=None, title="Notebook"):
        state = state or SharedChatState()
        self.notebooks = FakeNotebooks(title=title, failure=failure)
        self.chat = FakeChat(state)
        self.sources = FakeSources()


def fake_builder(client):
    @asynccontextmanager
    async def builder(_auth, _timeout):
        yield client

    return builder


async def add_channel(path: str, channel_id: str, notebook_id: str | None = None):
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            INSERT INTO channels (
                channel_id, notebook_id, notebook_display_name, binding_status
            ) VALUES (?, ?, ?, ?)
            """,
            (
                channel_id,
                notebook_id,
                "Original" if notebook_id else None,
                "bound" if notebook_id else "unbound",
            ),
        )
        await db.commit()


@pytest.mark.anyio
async def test_test_and_bind_is_atomic_and_clears_legacy_cookie(tmp_path, monkeypatch):
    path = str(tmp_path / "binding.db")
    await init_db(path)
    await add_channel(path, "channel-a")
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE channels SET nlm_auth_json_encrypted='legacy' WHERE channel_id='channel-a'"
        )
        await db.commit()

    state = SharedChatState()
    factory = CentralNotebookClientFactory(
        max_concurrency=2,
        timeout_seconds=1,
        context_builder=fake_builder(FakeClient(state, title="Shared A")),
    )
    monkeypatch.setattr(
        nlm_service,
        "channel_binding_repository",
        nlm_service.ChannelBindingRepository(path),
    )
    monkeypatch.setattr(nlm_service, "notebook_client_factory", factory)
    monkeypatch.setattr(
        nlm_service, "course_account_service", FakeCourseAccountService()
    )

    binding = await nlm_service.test_and_bind_notebook("channel-a", "notebook-aaaaaaaa")

    assert binding.notebook_id == "notebook-aaaaaaaa"
    assert binding.notebook_title == "Shared A"
    assert binding.status == "bound"
    assert state.deleted == ["old-global-conversation", "conversation-1"]
    async with aiosqlite.connect(path) as db:
        row = await (
            await db.execute(
                "SELECT nlm_auth_json_encrypted FROM channels WHERE channel_id='channel-a'"
            )
        ).fetchone()
    assert row[0] is None


@pytest.mark.anyio
async def test_failed_rebind_preserves_original_mapping(tmp_path, monkeypatch):
    class NotebookNotFoundError(Exception):
        pass

    path = str(tmp_path / "binding.db")
    await init_db(path)
    await add_channel(path, "channel-a", "notebook-original")
    client = FakeClient(failure=NotebookNotFoundError("upstream body secret"))
    factory = CentralNotebookClientFactory(
        max_concurrency=1,
        timeout_seconds=1,
        context_builder=fake_builder(client),
    )
    repository = nlm_service.ChannelBindingRepository(path)
    monkeypatch.setattr(nlm_service, "channel_binding_repository", repository)
    monkeypatch.setattr(nlm_service, "notebook_client_factory", factory)
    monkeypatch.setattr(
        nlm_service, "course_account_service", FakeCourseAccountService()
    )

    with pytest.raises(NotebookServiceError) as caught:
        await nlm_service.test_and_bind_notebook("channel-a", "notebook-new")

    assert caught.value.code == "not_shared"
    assert (await repository.get("channel-a")).notebook_id == "notebook-original"


@pytest.mark.anyio
async def test_stateless_chat_serializes_same_notebook_and_cleans_each_context():
    state = SharedChatState()
    client = FakeClient(state)
    isolation = nlm_service.StatelessConversationIsolation()

    async def ask_as(channel_id, user_id, question):
        # channel/user are the caller's security boundary; no server context is
        # retained for either identity after the call.
        assert channel_id and user_id
        async with isolation.lock("same-notebook"):
            return await isolation.ask_once(client, "same-notebook", question)

    first, second = await asyncio.gather(
        ask_as("channel-a", "user-1", "Question A"),
        ask_as("channel-b", "user-2", "Question B"),
    )

    assert first.answer == second.answer == "連線成功"
    assert state.max_overlap == 1
    assert state.current_id is None
    assert state.deleted == [
        "old-global-conversation",
        "conversation-1",
        "conversation-2",
    ]


@pytest.mark.anyio
async def test_unbind_only_changes_target_channel(tmp_path, monkeypatch):
    path = str(tmp_path / "binding.db")
    await init_db(path)
    await add_channel(path, "channel-a", "notebook-a")
    await add_channel(path, "channel-b", "notebook-b")
    repository = nlm_service.ChannelBindingRepository(path)
    monkeypatch.setattr(nlm_service, "channel_binding_repository", repository)

    result = await nlm_service.unbind_notebook("channel-a")

    assert result.status == "unbound" and result.notebook_id is None
    assert (await repository.get("channel-b")).notebook_id == "notebook-b"


@pytest.mark.anyio
async def test_expired_check_lease_restores_previous_status_and_allows_retry(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "binding.db")
    await init_db(path)
    await add_channel(path, "channel-a", "notebook-original")
    repository = nlm_service.ChannelBindingRepository(path, check_lease_seconds=1)
    monkeypatch.setattr(nlm_service, "channel_binding_repository", repository)

    original = await repository.get("channel-a")
    first_check = await repository.begin_check(original)
    assert first_check.status == "checking"
    assert first_check.operation_id

    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE channels SET updated_at=? WHERE channel_id='channel-a'",
            (expired_at,),
        )
        await db.commit()

    # The status API calls this service function, so a page refresh also
    # releases the abandoned lease instead of displaying `checking` forever.
    recovered = await nlm_service.get_binding_status("channel-a")
    assert recovered.status == "bound"
    assert recovered.operation_id is None

    retry = await repository.begin_check(recovered)
    assert retry.status == "checking"
    assert retry.operation_id
    assert retry.operation_id != first_check.operation_id

    # A worker that resumes after losing its lease cannot commit over the new
    # owner, even though both operations started from the same revision.
    current, stale_applied = await repository.complete_bind(
        first_check, "notebook-stale", "Stale result"
    )
    assert stale_applied is False
    assert current is not None and current.operation_id == retry.operation_id

    restored, applied = await repository.finish_check(retry, status="bound")
    assert applied is True
    assert restored is not None and restored.status == "bound"


@pytest.mark.anyio
async def test_old_stuck_check_without_previous_status_recovers_safely(tmp_path):
    path = str(tmp_path / "binding.db")
    await init_db(path)
    await add_channel(path, "channel-a")
    repository = nlm_service.ChannelBindingRepository(path, check_lease_seconds=1)
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            UPDATE channels
            SET binding_status='checking', binding_operation_id='abandoned',
                binding_check_previous_status=NULL, updated_at=?
            WHERE channel_id='channel-a'
            """,
            (expired_at,),
        )
        await db.commit()

    recovered = await repository.get("channel-a")
    assert recovered.status == "unbound"
    assert recovered.operation_id is None


@pytest.mark.anyio
async def test_fresh_check_lease_still_rejects_concurrent_retry(tmp_path):
    path = str(tmp_path / "binding.db")
    await init_db(path)
    await add_channel(path, "channel-a", "notebook-original")
    repository = nlm_service.ChannelBindingRepository(path, check_lease_seconds=60)

    original = await repository.get("channel-a")
    checking = await repository.begin_check(original)

    with pytest.raises(NotebookServiceError) as caught:
        await repository.begin_check(checking)

    assert caught.value.code == "binding_check_in_progress"
    current = await repository.get("channel-a")
    assert current.status == "checking"
    assert current.operation_id == checking.operation_id


@pytest.mark.anyio
async def test_unexpected_probe_error_releases_check_immediately(tmp_path, monkeypatch):
    path = str(tmp_path / "binding.db")
    await init_db(path)
    await add_channel(path, "channel-a", "notebook-original")
    repository = nlm_service.ChannelBindingRepository(path, check_lease_seconds=600)
    monkeypatch.setattr(nlm_service, "channel_binding_repository", repository)
    monkeypatch.setattr(
        nlm_service, "course_account_service", FakeCourseAccountService()
    )

    async def fail_unexpectedly(*_args, **_kwargs):
        raise RuntimeError("unexpected upstream failure")

    monkeypatch.setattr(nlm_service.notebook_client_factory, "run", fail_unexpectedly)

    with pytest.raises(RuntimeError, match="unexpected upstream failure"):
        await nlm_service.test_and_bind_notebook("channel-a", "notebook-new")

    current = await repository.get("channel-a")
    assert current.status == "bound"
    assert current.notebook_id == "notebook-original"
    assert current.operation_id is None


@pytest.mark.anyio
async def test_channels_targeting_same_notebook_queue_before_check_lease(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "binding.db")
    await init_db(path)
    await add_channel(path, "channel-a")
    await add_channel(path, "channel-b")
    repository = nlm_service.ChannelBindingRepository(path, check_lease_seconds=60)
    monkeypatch.setattr(nlm_service, "channel_binding_repository", repository)
    monkeypatch.setattr(
        nlm_service, "course_account_service", FakeCourseAccountService()
    )

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0
    client = FakeClient(SharedChatState(), title="Shared notebook")

    async def queued_run(_auth, _operation_name, operation, *, existing_binding=False):
        nonlocal calls
        del existing_binding
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
        return await operation(client)

    monkeypatch.setattr(nlm_service.notebook_client_factory, "run", queued_run)

    first = asyncio.create_task(
        nlm_service.test_and_bind_notebook("channel-a", "same-notebook")
    )
    await first_started.wait()
    second = asyncio.create_task(
        nlm_service.test_and_bind_notebook("channel-b", "same-notebook")
    )
    await asyncio.sleep(0.01)

    # Channel B is queued on the Notebook lock and has not started a check
    # lease that could expire while Channel A is still running.
    queued = await repository.get("channel-b")
    assert queued.status == "unbound"
    assert queued.operation_id is None
    assert calls == 1

    release_first.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result.status == second_result.status == "bound"
    assert first_result.notebook_id == second_result.notebook_id == "same-notebook"
    assert calls == 2


@pytest.mark.anyio
async def test_notebook_lock_wait_is_bounded_without_entering_checking(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "binding.db")
    await init_db(path)
    await add_channel(path, "channel-a")
    repository = nlm_service.ChannelBindingRepository(path, check_lease_seconds=60)
    monkeypatch.setattr(nlm_service, "channel_binding_repository", repository)
    monkeypatch.setattr(nlm_service.settings, "notebook_query_timeout_seconds", 0.01)
    held_lock = nlm_service.conversation_isolation.lock("busy-notebook")
    await held_lock.acquire()
    try:
        with pytest.raises(NotebookServiceError) as caught:
            await nlm_service.test_and_bind_notebook("channel-a", "busy-notebook")
    finally:
        held_lock.release()

    assert caught.value.code == "notebook_busy"
    assert caught.value.http_status == 429
    assert caught.value.status == "unbound"
    current = await repository.get("channel-a")
    assert current.status == "unbound"
    assert current.operation_id is None


def test_question_lock_wait_reserves_durable_claim_delivery_budget(monkeypatch):
    monkeypatch.setattr(nlm_service.settings, "notebook_query_timeout_seconds", 90.0)
    monkeypatch.setattr(nlm_service.settings, "line_event_claim_timeout_seconds", 240)

    lock_timeout = nlm_service._question_lock_timeout_seconds()

    assert lock_timeout == 30.0
    assert lock_timeout + (2 * 90.0) < 240


@pytest.mark.anyio
async def test_concurrent_questions_same_notebook_lock_timeout_is_retryable(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "binding.db")
    await init_db(path)
    await add_channel(path, "channel-a", "same-notebook")
    await add_channel(path, "channel-b", "same-notebook")
    repository = nlm_service.ChannelBindingRepository(path)
    monkeypatch.setattr(nlm_service, "channel_binding_repository", repository)
    monkeypatch.setattr(nlm_service.settings, "notebook_query_timeout_seconds", 0.02)
    account_service = FakeCourseAccountService()
    monkeypatch.setattr(nlm_service, "course_account_service", account_service)

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    run_calls = 0
    client = FakeClient(SharedChatState(), title="Shared notebook")

    async def slow_first_run(
        _auth, _operation_name, operation, *, existing_binding=False
    ):
        nonlocal run_calls
        del existing_binding
        run_calls += 1
        first_started.set()
        await release_first.wait()
        return await operation(client)

    monkeypatch.setattr(nlm_service.notebook_client_factory, "run", slow_first_run)

    first = asyncio.create_task(
        nlm_service.ask_question("channel-a", "user-a", "first question")
    )
    await first_started.wait()
    try:
        with pytest.raises(NotebookServiceError) as caught:
            await nlm_service.ask_question("channel-b", "user-b", "second question")
    finally:
        release_first.set()
    first_messages = await first

    assert caught.value.code == "notebook_busy"
    assert caught.value.http_status == 429
    assert "正在處理其他請求" in caught.value.user_message
    assert account_service.failures == []
    assert run_calls == 1
    assert first_messages == ["連線成功"]
    second_binding = await repository.get("channel-b")
    assert second_binding.status == "bound"
    assert second_binding.notebook_id == "same-notebook"
