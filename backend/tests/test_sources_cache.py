import asyncio
import sqlite3

import database
import services.nlm_service as nlm_service
from services.crypto_service import encrypt


class _FakeReference:
    def __init__(self, citation_number, source_id):
        self.citation_number = citation_number
        self.source_id = source_id


class _FakeSource:
    def __init__(self, id, title):
        self.id = id
        self.title = title


class _FakeAskResult:
    def __init__(self, answer, conversation_id, references=None):
        self.answer = answer
        self.conversation_id = conversation_id
        self.references = references or []


class _FakeChatAPI:
    def __init__(self):
        self.calls = 0

    async def ask(self, notebook_id, question, source_ids=None, conversation_id=None):
        self.calls += 1
        return _FakeAskResult(
            answer=f"answer [1] to: {question}",
            conversation_id=f"conv-{self.calls}",
            references=[_FakeReference(citation_number=1, source_id="src-1")],
        )


class _FakeSourcesAPI:
    def __init__(self):
        self.list_calls = 0

    async def list(self, notebook_id):
        self.list_calls += 1
        return [_FakeSource(id="src-1", title="McLaren_型錄.md")]


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChatAPI()
        self.sources = _FakeSourcesAPI()


class _FakeStorageContext:
    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


def _setup(tmp_path, monkeypatch, notebook_id="notebook-1"):
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
        (channel_id, "secret", "token", encrypted, notebook_id),
    )
    conn.commit()
    conn.close()

    client = _FakeClient()

    class _FakeNotebookLMClient:
        @staticmethod
        def from_storage():
            return _FakeStorageContext(client)

    monkeypatch.setattr(nlm_service, "NotebookLMClient", _FakeNotebookLMClient)

    return channel_id, client


def test_sources_list_is_cached_across_questions(tmp_path, monkeypatch):
    channel_id, client = _setup(tmp_path, monkeypatch)

    asyncio.run(nlm_service.ask_question(channel_id, "Q1", line_user_id="user-A"))
    asyncio.run(nlm_service.ask_question(channel_id, "Q2", line_user_id="user-A"))
    asyncio.run(nlm_service.ask_question(channel_id, "Q3", line_user_id="user-A"))

    assert client.chat.calls == 3
    assert client.sources.list_calls == 1  # fetched once, reused for Q2 and Q3


def test_sources_cache_invalidated_after_knowledge_base_update(tmp_path, monkeypatch):
    channel_id, client = _setup(tmp_path, monkeypatch, notebook_id="notebook-1")

    asyncio.run(nlm_service.ask_question(channel_id, "Q1", line_user_id="user-A"))
    assert client.sources.list_calls == 1

    nlm_service._invalidate_sources_cache("notebook-1")

    asyncio.run(nlm_service.ask_question(channel_id, "Q2", line_user_id="user-A"))
    assert client.sources.list_calls == 2  # re-fetched after invalidation
