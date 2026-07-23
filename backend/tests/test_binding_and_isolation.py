from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from services.course_account_service import (
    CourseAccountRepository,
    NotebookServiceError,
)
from services import nlm_service
from services.session_service import issue_admin_session, issue_setup_session


def _insert_channel(
    db_path: Path,
    channel_id: str,
    *,
    notebook_id: str | None = None,
    title: str | None = None,
    legacy_auth: str | None = None,
) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            INSERT INTO channels (
                channel_id, notebook_id, notebook_display_name,
                binding_status, nlm_auth_json_encrypted
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                channel_id,
                notebook_id,
                title,
                "bound" if notebook_id else "unbound",
                legacy_auth,
            ),
        )
        db.commit()


async def _noop(*_args, **_kwargs) -> bool:
    return True


class _NotebookApi:
    async def get(self, notebook_id: str):
        return SimpleNamespace(id=notebook_id, title=f"Title {notebook_id}")


class _BindingChatApi:
    def __init__(self) -> None:
        self.current: dict[str, str | None] = {}

    async def get_conversation_id(self, notebook_id: str) -> str | None:
        return self.current.get(notebook_id)

    async def delete_conversation(self, notebook_id: str, conversation_id: str) -> bool:
        if self.current.get(notebook_id) == conversation_id:
            self.current[notebook_id] = None
        return True

    async def ask(self, notebook_id: str, _question: str):
        conversation_id = f"conversation-{notebook_id}"
        self.current[notebook_id] = conversation_id
        return SimpleNamespace(
            answer="連線成功", conversation_id=conversation_id, references=[]
        )


@pytest.mark.anyio
async def test_successful_rebind_is_atomic_and_failed_rebind_keeps_previous_mapping(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_channel(
        backend_db,
        "channel-a",
        notebook_id="OldNotebook_123",
        title="Old title",
        legacy_auth="legacy-cookie-ciphertext",
    )
    client = SimpleNamespace(
        notebooks=_NotebookApi(), chat=_BindingChatApi(), sources=None
    )
    should_fail = False

    async def require_authorization():
        return SimpleNamespace(email="course@example.com"), {
            "cookies": [{"name": "SID"}]
        }

    async def run(_auth, _operation_name, operation, *, existing_binding=False):
        del existing_binding
        if should_fail:
            raise NotebookServiceError("not_shared", status="error", http_status=403)
        return await operation(client)

    monkeypatch.setattr(
        nlm_service.course_account_service,
        "require_authorization",
        require_authorization,
    )
    monkeypatch.setattr(nlm_service.course_account_service, "mark_query_success", _noop)
    monkeypatch.setattr(nlm_service.course_account_service, "mark_query_failure", _noop)
    monkeypatch.setattr(nlm_service.notebook_client_factory, "run", run)

    first = await nlm_service.test_and_bind_notebook("channel-a", "NewNotebook_456")
    assert first.notebook_id == "NewNotebook_456"
    assert first.notebook_title == "Title NewNotebook_456"
    assert first.status == "bound"
    with sqlite3.connect(backend_db) as db:
        legacy_auth = db.execute(
            "SELECT nlm_auth_json_encrypted FROM channels WHERE channel_id='channel-a'"
        ).fetchone()[0]
    assert legacy_auth is None

    should_fail = True
    with pytest.raises(NotebookServiceError) as failure:
        await nlm_service.test_and_bind_notebook("channel-a", "DeniedNotebook_999")
    assert failure.value.code == "not_shared"

    preserved = await nlm_service.get_binding_status("channel-a")
    assert preserved.notebook_id == "NewNotebook_456"
    assert preserved.notebook_title == "Title NewNotebook_456"
    assert preserved.status == "bound"


@pytest.mark.anyio
async def test_two_channels_query_only_their_own_notebook_and_unbind_is_isolated(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_channel(
        backend_db, "channel-a", notebook_id="Notebook_A123", title="Notebook A"
    )
    _insert_channel(
        backend_db, "channel-b", notebook_id="Notebook_B456", title="Notebook B"
    )
    asked: list[tuple[str, str]] = []

    class Chat:
        async def get_conversation_id(self, _notebook_id: str):
            return None

        async def delete_conversation(self, _notebook_id: str, _conversation_id: str):
            return True

        async def ask(self, notebook_id: str, question: str):
            asked.append((notebook_id, question))
            return SimpleNamespace(
                answer=f"answer from {notebook_id}",
                conversation_id=f"conversation-{notebook_id}",
                references=[],
            )

    client = SimpleNamespace(chat=Chat(), notebooks=_NotebookApi(), sources=None)

    async def require_authorization():
        return SimpleNamespace(email="course@example.com"), {
            "cookies": [{"name": "SID"}]
        }

    async def run(_auth, _operation_name, operation, *, existing_binding=False):
        del existing_binding
        return await operation(client)

    monkeypatch.setattr(
        nlm_service.course_account_service,
        "require_authorization",
        require_authorization,
    )
    monkeypatch.setattr(nlm_service.course_account_service, "mark_query_success", _noop)
    monkeypatch.setattr(nlm_service.course_account_service, "mark_query_failure", _noop)
    monkeypatch.setattr(nlm_service.notebook_client_factory, "run", run)

    answer_a, answer_b = await asyncio.gather(
        nlm_service.ask_question("channel-a", "user-a", "question-a"),
        nlm_service.ask_question("channel-b", "user-b", "question-b"),
    )
    # Different notebook locks may complete in either order; the invariant is
    # the exact Channel-to-Notebook mapping, not scheduler order.
    assert set(asked) == {
        ("Notebook_A123", "question-a"),
        ("Notebook_B456", "question-b"),
    }
    assert "Notebook_A123" in answer_a[0]
    assert "Notebook_B456" in answer_b[0]

    unbound = await nlm_service.unbind_notebook("channel-a")
    untouched = await nlm_service.get_binding_status("channel-b")
    assert unbound.status == "unbound"
    assert unbound.notebook_id is None
    assert untouched.status == "bound"
    assert untouched.notebook_id == "Notebook_B456"


@pytest.mark.anyio
async def test_legacy_unbound_notebook_id_cannot_answer_before_shared_probe(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_channel(
        backend_db,
        "legacy-channel",
        notebook_id="LegacyNotebook_123",
        legacy_auth="legacy-cookie-ciphertext",
    )
    with sqlite3.connect(backend_db) as db:
        db.execute(
            "UPDATE channels SET binding_status='unbound' WHERE channel_id='legacy-channel'"
        )
        db.commit()

    async def must_not_read_authorization():
        raise AssertionError("unverified legacy binding reached centralized auth")

    monkeypatch.setattr(
        nlm_service.course_account_service,
        "require_authorization",
        must_not_read_authorization,
    )

    messages = await nlm_service.ask_question("legacy-channel", "line-user", "question")
    assert "尚未完成新版綁定" in messages[0]


@pytest.mark.anyio
async def test_legacy_unbound_id_cannot_recheck_and_unbind_preserves_rollback_cookie(
    backend_db: Path,
) -> None:
    _insert_channel(
        backend_db,
        "legacy-channel",
        notebook_id="LegacyNotebook_123",
        legacy_auth="legacy-cookie-ciphertext",
    )
    with sqlite3.connect(backend_db) as db:
        db.execute(
            "UPDATE channels SET binding_status='unbound' WHERE channel_id='legacy-channel'"
        )
        db.commit()

    with pytest.raises(NotebookServiceError) as rejected:
        await nlm_service.recheck_notebook_binding("legacy-channel")
    assert rejected.value.code == "binding_probe_required"

    await nlm_service.unbind_notebook("legacy-channel")
    with sqlite3.connect(backend_db) as db:
        notebook_id, legacy_auth = db.execute(
            "SELECT notebook_id, nlm_auth_json_encrypted FROM channels WHERE channel_id='legacy-channel'"
        ).fetchone()
    assert notebook_id is None
    assert legacy_auth == "legacy-cookie-ciphertext"


@pytest.mark.anyio
async def test_recheck_persists_course_account_unavailable(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_channel(backend_db, "channel-a", notebook_id="Notebook_A123")

    async def unavailable():
        raise NotebookServiceError(
            "course_auth_expired",
            status="course_account_unavailable",
            http_status=503,
        )

    monkeypatch.setattr(
        nlm_service.course_account_service, "require_authorization", unavailable
    )

    with pytest.raises(NotebookServiceError):
        await nlm_service.recheck_notebook_binding("channel-a")
    current = await nlm_service.get_binding_status("channel-a")
    assert current.status == "course_account_unavailable"


@pytest.mark.anyio
async def test_stale_recheck_cannot_restore_previous_notebook_after_rebind(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_channel(backend_db, "channel-a", notebook_id="Notebook_Old")
    client = SimpleNamespace(notebooks=_NotebookApi())

    async def require_authorization():
        return SimpleNamespace(email="course@example.com"), {"cookies": [{}]}

    async def run(_auth, _name, operation, *, existing_binding=False):
        del existing_binding
        title = await operation(client)
        await nlm_service.channel_binding_repository.replace(
            "channel-a", "Notebook_New", "New title"
        )
        return title

    monkeypatch.setattr(
        nlm_service.course_account_service,
        "require_authorization",
        require_authorization,
    )
    monkeypatch.setattr(nlm_service.course_account_service, "mark_query_success", _noop)
    monkeypatch.setattr(nlm_service.notebook_client_factory, "run", run)

    with pytest.raises(NotebookServiceError) as conflict:
        await nlm_service.recheck_notebook_binding("channel-a")

    assert conflict.value.code == "binding_changed"
    result = await nlm_service.get_binding_status("channel-a")
    assert result.notebook_id == "Notebook_New"
    assert result.notebook_title == "New title"


@pytest.mark.anyio
async def test_inflight_answer_is_not_returned_after_concurrent_unbind(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_channel(backend_db, "channel-a", notebook_id="Notebook_Old")

    class Chat:
        async def get_conversation_id(self, _notebook_id):
            return None

        async def delete_conversation(self, _notebook_id, _conversation_id):
            return True

        async def ask(self, _notebook_id, _question):
            return SimpleNamespace(
                answer="stale private answer",
                conversation_id="conversation-old",
                references=[],
            )

    client = SimpleNamespace(chat=Chat(), sources=None)

    async def require_authorization():
        return SimpleNamespace(email="course@example.com"), {"cookies": [{}]}

    async def run(_auth, _name, operation, *, existing_binding=False):
        del existing_binding
        result = await operation(client)
        await nlm_service.unbind_notebook("channel-a")
        return result

    monkeypatch.setattr(
        nlm_service.course_account_service,
        "require_authorization",
        require_authorization,
    )
    monkeypatch.setattr(nlm_service.course_account_service, "mark_query_success", _noop)
    monkeypatch.setattr(nlm_service.notebook_client_factory, "run", run)

    messages = await nlm_service.ask_question(
        "channel-a", "line-user", "private question"
    )

    assert messages == ["⚠️ Notebook 綁定剛剛已更新，請重新送出問題。"]
    assert "stale private answer" not in messages[0]
    current = await nlm_service.get_binding_status("channel-a")
    assert current.status == "unbound"


@pytest.mark.anyio
async def test_checking_status_is_persisted_during_binding_probe(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_channel(backend_db, "channel-a")
    started = asyncio.Event()
    release = asyncio.Event()

    async def require_authorization():
        return SimpleNamespace(email="course@example.com"), {"cookies": [{}]}

    async def run(*_args, **_kwargs):
        started.set()
        await release.wait()
        return "Shared title"

    monkeypatch.setattr(
        nlm_service.course_account_service,
        "require_authorization",
        require_authorization,
    )
    monkeypatch.setattr(nlm_service.course_account_service, "mark_query_success", _noop)
    monkeypatch.setattr(nlm_service.notebook_client_factory, "run", run)

    task = asyncio.create_task(
        nlm_service.test_and_bind_notebook("channel-a", "Notebook_New")
    )
    await started.wait()
    checking = await nlm_service.get_binding_status("channel-a")
    assert checking.status == "checking"
    assert checking.operation_id

    release.set()
    result = await task
    assert result.status == "bound"
    assert result.operation_id is None


@pytest.mark.anyio
async def test_course_account_swap_during_probe_cannot_create_stale_binding(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_channel(
        backend_db,
        "channel-a",
        notebook_id="Notebook_Old",
        title="Old title",
    )

    async def require_authorization():
        return SimpleNamespace(email="old@example.com"), {"cookies": [{}]}

    async def run(*_args, **_kwargs):
        return "New title from old account"

    async def stale_account(_record, _auth_payload=None):
        return False

    monkeypatch.setattr(
        nlm_service.course_account_service,
        "require_authorization",
        require_authorization,
    )
    monkeypatch.setattr(
        nlm_service.course_account_service, "mark_query_success", stale_account
    )
    monkeypatch.setattr(nlm_service.notebook_client_factory, "run", run)

    with pytest.raises(NotebookServiceError) as changed:
        await nlm_service.test_and_bind_notebook("channel-a", "Notebook_New")

    assert changed.value.code == "course_account_changed"
    current = await nlm_service.get_binding_status("channel-a")
    assert current.notebook_id == "Notebook_Old"
    assert current.status == "course_account_unavailable"


@pytest.mark.anyio
async def test_concurrent_questions_on_same_binding_both_return_answers(
    backend_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_channel(backend_db, "channel-a", notebook_id="Notebook_A")

    class Chat:
        def __init__(self):
            self.current = None

        async def get_conversation_id(self, _notebook_id):
            return self.current

        async def delete_conversation(self, _notebook_id, _conversation_id):
            self.current = None
            return True

        async def ask(self, _notebook_id, question):
            self.current = f"conversation-{question}"
            return SimpleNamespace(
                answer=f"answer-{question}",
                conversation_id=self.current,
                references=[],
            )

    client = SimpleNamespace(chat=Chat(), sources=None)

    async def require_authorization():
        return SimpleNamespace(email="course@example.com"), {"cookies": [{}]}

    async def run(_auth, _name, operation, *, existing_binding=False):
        del existing_binding
        return await operation(client)

    monkeypatch.setattr(
        nlm_service.course_account_service,
        "require_authorization",
        require_authorization,
    )
    monkeypatch.setattr(nlm_service.course_account_service, "mark_query_success", _noop)
    monkeypatch.setattr(nlm_service.notebook_client_factory, "run", run)

    first, second = await asyncio.gather(
        nlm_service.ask_question("channel-a", "user-1", "one"),
        nlm_service.ask_question("channel-a", "user-2", "two"),
    )

    assert first == ["answer-one"]
    assert second == ["answer-two"]


@pytest.mark.anyio
async def test_stateless_conversation_removes_old_and_new_context() -> None:
    calls: list[tuple[str, str]] = []

    class Chat:
        def __init__(self) -> None:
            self.current = "old-conversation"

        async def get_conversation_id(self, _notebook_id: str):
            return self.current

        async def delete_conversation(self, notebook_id: str, conversation_id: str):
            calls.append((notebook_id, conversation_id))
            self.current = None
            return True

        async def ask(self, _notebook_id: str, _question: str):
            self.current = "new-conversation"
            return SimpleNamespace(answer="answer", conversation_id="new-conversation")

    client = SimpleNamespace(chat=Chat())
    result = await nlm_service.StatelessConversationIsolation().ask_once(
        client, "Notebook_A123", "question"
    )
    assert result.answer == "answer"
    assert calls == [
        ("Notebook_A123", "old-conversation"),
        ("Notebook_A123", "new-conversation"),
    ]
    assert client.chat.current is None


@pytest.mark.anyio
async def test_stateless_conversation_cleans_server_context_after_ask_failure() -> None:
    deleted: list[str] = []

    class Chat:
        def __init__(self) -> None:
            self.current: str | None = None

        async def get_conversation_id(self, _notebook_id: str):
            return self.current

        async def delete_conversation(self, _notebook_id: str, conversation_id: str):
            deleted.append(conversation_id)
            self.current = None
            return True

        async def ask(self, _notebook_id: str, _question: str):
            self.current = "server-created-before-failure"
            raise RuntimeError("upstream failed after creating context")

    client = SimpleNamespace(chat=Chat())
    with pytest.raises(RuntimeError):
        await nlm_service.StatelessConversationIsolation().ask_once(
            client, "Notebook_A123", "question"
        )
    assert deleted == ["server-created-before-failure"]
    assert client.chat.current is None


@pytest.mark.anyio
async def test_course_account_public_and_admin_status_never_return_authorization(
    backend_db: Path,
    asgi_app,
) -> None:
    secret = "central-google-cookie-secret"
    repository = CourseAccountRepository(str(backend_db))
    await repository.replace_authorization(
        email="course@example.com",
        auth_payload={"cookies": [{"name": "SID", "value": secret}]},
        auth_mode="storage_state",
        checked_at="2026-07-22T00:00:00+00:00",
    )
    with sqlite3.connect(backend_db) as db:
        db.execute("INSERT INTO invite_codes (code) VALUES ('student-invite')")
        db.commit()
    setup = await issue_setup_session("student-invite")
    admin = await issue_admin_session()

    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        public = await client.get(
            "/api/course-account/public",
            headers={"Authorization": f"Bearer {setup.token}"},
        )
        admin_status = await client.get(
            "/api/admin/course-account",
            headers={"Authorization": f"Bearer {admin.token}"},
        )

    assert public.status_code == 200
    assert admin_status.status_code == 200
    assert public.json() == {"email": "course@example.com", "health_status": "healthy"}
    for response in (public, admin_status):
        assert secret not in response.text
        assert "cookies" not in response.text
        assert "auth_encrypted" not in response.text
        assert "storage_state_json" not in response.text
