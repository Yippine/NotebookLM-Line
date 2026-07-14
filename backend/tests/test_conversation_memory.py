import asyncio
import sqlite3

import database
import services.nlm_service as nlm_service
from services.crypto_service import encrypt


class _FakeAskResult:
    def __init__(self, answer, conversation_id, references=None):
        self.answer = answer
        self.conversation_id = conversation_id
        self.references = references or []


class _FakeChatAPI:
    def __init__(self):
        self.calls = []

    async def ask(self, notebook_id, question, source_ids=None, conversation_id=None):
        self.calls.append({"question": question, "conversation_id": conversation_id})
        # Simulate the server: a fresh id when none was passed in, otherwise
        # just echo back the same thread's id (continuing it).
        returned_id = conversation_id or f"conv-{len(self.calls)}"
        return _FakeAskResult(answer=f"answer to: {question}", conversation_id=returned_id)


class _FakeSourcesAPI:
    async def list(self, notebook_id):
        return []


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


def _setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB", db_path)
    monkeypatch.setattr(nlm_service, "DB", db_path)
    asyncio.run(database.init_db())

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

    client = _FakeClient()

    class _FakeNotebookLMClient:
        @staticmethod
        def from_storage():
            return _FakeStorageContext(client)

    monkeypatch.setattr(nlm_service, "NotebookLMClient", _FakeNotebookLMClient)

    return channel_id, client


def test_same_user_reuses_conversation_id_across_questions(tmp_path, monkeypatch):
    channel_id, client = _setup(tmp_path, monkeypatch)

    asyncio.run(nlm_service.ask_question(channel_id, "第一個問題", line_user_id="user-A"))
    asyncio.run(nlm_service.ask_question(channel_id, "第二個問題", line_user_id="user-A"))

    assert client.chat.calls[0]["conversation_id"] is None
    assert client.chat.calls[1]["conversation_id"] == "conv-1"


def test_different_users_get_independent_conversations(tmp_path, monkeypatch):
    channel_id, client = _setup(tmp_path, monkeypatch)

    asyncio.run(nlm_service.ask_question(channel_id, "Q1", line_user_id="user-A"))
    asyncio.run(nlm_service.ask_question(channel_id, "Q2", line_user_id="user-B"))

    # user-B's first question must NOT continue user-A's thread.
    assert client.chat.calls[0]["conversation_id"] is None
    assert client.chat.calls[1]["conversation_id"] is None


def test_missing_user_id_never_persists_or_reuses_conversation(tmp_path, monkeypatch):
    channel_id, client = _setup(tmp_path, monkeypatch)

    asyncio.run(nlm_service.ask_question(channel_id, "Q1", line_user_id=None))
    asyncio.run(nlm_service.ask_question(channel_id, "Q2", line_user_id=None))

    assert client.chat.calls[0]["conversation_id"] is None
    assert client.chat.calls[1]["conversation_id"] is None
