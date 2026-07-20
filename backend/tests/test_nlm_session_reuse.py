import asyncio
import sqlite3

import database
import services.nlm_service as nlm_service
from services.crypto_service import encrypt


class _FakeAskResult:
    def __init__(self, answer, conversation_id):
        self.answer = answer
        self.conversation_id = conversation_id
        self.references = []


class _FakeChatAPI:
    def __init__(self, fail_calls=0):
        self.calls = 0
        self._fail_calls = fail_calls

    async def ask(self, notebook_id, question, source_ids=None, conversation_id=None):
        self.calls += 1
        if self.calls <= self._fail_calls:
            raise RuntimeError("session dropped")
        return _FakeAskResult(answer=f"answer to: {question}", conversation_id=f"conv-{self.calls}")


class _FakeSourcesAPI:
    async def list(self, notebook_id):
        return []


class _FakeClient:
    def __init__(self, fail_calls=0):
        self.chat = _FakeChatAPI(fail_calls=fail_calls)
        self.sources = _FakeSourcesAPI()


def _setup(tmp_path, monkeypatch, client_factory=None):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB", db_path)
    monkeypatch.setattr(nlm_service, "DB", db_path)
    asyncio.run(database.init_db())

    # Each test gets isolated caches — the real caches are module-global.
    monkeypatch.setattr(nlm_service, "_sources_cache", {})
    monkeypatch.setattr(nlm_service, "_client_cache", {})

    channel_id = "test-channel"
    encrypted = encrypt({"cookies": []})
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO channels "
        "(channel_id, channel_secret, channel_access_token, nlm_auth_json_encrypted, notebook_id) "
        "VALUES (?,?,?,?,?)",
        (channel_id, "secret", "token", encrypted, "notebook-1"),
    )
    conn.commit()
    conn.close()

    # Every from_storage().__aenter__()/__aexit__() call is recorded so tests
    # can assert how many sessions were actually opened/closed, independent
    # of how many questions were asked.
    opened = []
    closed = []

    class _FakeStorageContext:
        def __init__(self, client):
            self._client = client

        async def __aenter__(self):
            opened.append(self._client)
            return self._client

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            closed.append(self._client)
            return None

    factory = client_factory or (lambda: _FakeClient())

    class _FakeNotebookLMClient:
        @staticmethod
        def from_storage():
            return _FakeStorageContext(factory())

    monkeypatch.setattr(nlm_service, "NotebookLMClient", _FakeNotebookLMClient)

    return channel_id, opened, closed


def test_session_is_reused_across_questions(tmp_path, monkeypatch):
    channel_id, opened, closed = _setup(tmp_path, monkeypatch)

    asyncio.run(nlm_service.ask_question(channel_id, "Q1", line_user_id="user-A"))
    asyncio.run(nlm_service.ask_question(channel_id, "Q2", line_user_id="user-A"))
    asyncio.run(nlm_service.ask_question(channel_id, "Q3", line_user_id="user-A"))

    assert len(opened) == 1  # one session opened, reused for all three questions
    assert opened[0].chat.calls == 3
    assert closed == []  # never torn down between questions


def test_stale_session_is_invalidated_and_retried(tmp_path, monkeypatch):
    # First session's very first ask fails (simulating a dropped connection
    # or expired cookies); the second session (opened on retry) succeeds.
    clients = [_FakeClient(fail_calls=1), _FakeClient()]

    def factory():
        return clients.pop(0)

    channel_id, opened, closed = _setup(tmp_path, monkeypatch, client_factory=factory)

    messages = asyncio.run(nlm_service.ask_question(channel_id, "Q1", line_user_id="user-A"))

    assert len(opened) == 2  # first session failed, a fresh one was opened
    assert len(closed) == 1  # the failed session was closed, not leaked
    assert "查詢失敗" not in messages[0]  # the retry succeeded transparently


def test_manual_invalidate_forces_fresh_session_next_time(tmp_path, monkeypatch):
    channel_id, opened, closed = _setup(tmp_path, monkeypatch)

    asyncio.run(nlm_service.ask_question(channel_id, "Q1", line_user_id="user-A"))
    assert len(opened) == 1

    asyncio.run(nlm_service._invalidate_client(channel_id))
    assert len(closed) == 1
    assert channel_id not in nlm_service._client_cache

    asyncio.run(nlm_service.ask_question(channel_id, "Q2", line_user_id="user-A"))
    assert len(opened) == 2


def test_aclose_all_clients_closes_and_clears_cache(tmp_path, monkeypatch):
    channel_id, opened, closed = _setup(tmp_path, monkeypatch)

    asyncio.run(nlm_service.ask_question(channel_id, "Q1", line_user_id="user-A"))
    assert nlm_service._client_cache  # something is cached

    asyncio.run(nlm_service.aclose_all_clients())

    assert closed == opened
    assert nlm_service._client_cache == {}
