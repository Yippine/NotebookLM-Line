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

    monkeypatch.setattr(nlm_service, "_client_cache", {})
    monkeypatch.setattr(nlm_service, "_answer_cache", {})

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


def test_identical_fresh_question_is_served_from_cache_without_asking_notebooklm_again(tmp_path, monkeypatch):
    channel_id, client = _setup(tmp_path, monkeypatch)

    first = asyncio.run(nlm_service.ask_question(channel_id, "你們家有現車嗎", line_user_id=None))
    second = asyncio.run(nlm_service.ask_question(channel_id, "你們家有現車嗎", line_user_id=None))

    assert len(client.chat.calls) == 1  # 第二次沒有真的再打一次 NotebookLM
    assert second == first


def test_different_question_text_is_not_served_from_cache(tmp_path, monkeypatch):
    channel_id, client = _setup(tmp_path, monkeypatch)

    asyncio.run(nlm_service.ask_question(channel_id, "你們家有現車嗎", line_user_id=None))
    asyncio.run(nlm_service.ask_question(channel_id, "有 Altis 嗎", line_user_id=None))

    assert len(client.chat.calls) == 2


def test_same_question_differing_only_in_punctuation_or_spacing_hits_cache(tmp_path, monkeypatch):
    channel_id, client = _setup(tmp_path, monkeypatch)

    asyncio.run(nlm_service.ask_question(channel_id, "你們家有現車嗎", line_user_id=None))
    asyncio.run(nlm_service.ask_question(channel_id, "你們家有 現車嗎？", line_user_id=None))
    asyncio.run(nlm_service.ask_question(channel_id, "你們家有現車嗎!", line_user_id=None))

    assert len(client.chat.calls) == 1


def test_same_question_differing_only_in_trailing_particle_hits_cache(tmp_path, monkeypatch):
    channel_id, client = _setup(tmp_path, monkeypatch)

    asyncio.run(nlm_service.ask_question(channel_id, "你們家有現車嗎", line_user_id=None))
    asyncio.run(nlm_service.ask_question(channel_id, "你們家有現車", line_user_id=None))

    assert len(client.chat.calls) == 1


def test_question_with_different_sentence_structure_is_not_merged_by_normalization(tmp_path, monkeypatch):
    # 「有沒有」跟「有」句型不同，即使去掉標點/語氣助詞後看起來相近，
    # 也不該被合併成同一個快取 key——避免把語意不同的問法誤判成
    # 同一句而快取命中錯誤答案。
    channel_id, client = _setup(tmp_path, monkeypatch)

    asyncio.run(nlm_service.ask_question(channel_id, "你們家有沒有現車", line_user_id=None))
    asyncio.run(nlm_service.ask_question(channel_id, "你們家有現車嗎", line_user_id=None))

    assert len(client.chat.calls) == 2


def test_same_question_in_a_different_channel_is_not_shared(tmp_path, monkeypatch):
    channel_id, client = _setup(tmp_path, monkeypatch)

    # 另外插入第二個 channel，指向同一個 fake client。
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.execute(
        "INSERT INTO channels "
        "(channel_id, channel_secret, channel_access_token, nlm_auth_json_encrypted, notebook_id) "
        "VALUES (?,?,?,?,?)",
        ("other-channel", "secret", "token", encrypt({"cookies": []}), "notebook-1"),
    )
    conn.commit()
    conn.close()

    asyncio.run(nlm_service.ask_question(channel_id, "你們家有現車嗎", line_user_id=None))
    asyncio.run(nlm_service.ask_question("other-channel", "你們家有現車嗎", line_user_id=None))

    assert len(client.chat.calls) == 2


def test_continuing_an_existing_conversation_never_uses_or_populates_the_cache(tmp_path, monkeypatch):
    channel_id, client = _setup(tmp_path, monkeypatch)

    # user-A 先問一題建立起對話串，接著問一個「剛好」跟後面 user-B
    # 的問題文字一模一樣的追問——這是延續對話的追問，不該被快取，
    # 也不該去用快取（即使文字剛好對得上）。
    asyncio.run(nlm_service.ask_question(channel_id, "第一題", line_user_id="user-A"))
    asyncio.run(nlm_service.ask_question(channel_id, "還有其他的嗎", line_user_id="user-A"))
    asyncio.run(nlm_service.ask_question(channel_id, "還有其他的嗎", line_user_id=None))

    # 三次都是真的問了 NotebookLM，沒有任何一次是從快取拿到的。
    assert len(client.chat.calls) == 3


def test_expired_cache_entry_is_not_reused(tmp_path, monkeypatch):
    channel_id, client = _setup(tmp_path, monkeypatch)

    fake_now = [1000.0]
    monkeypatch.setattr(nlm_service.time, "time", lambda: fake_now[0])

    asyncio.run(nlm_service.ask_question(channel_id, "你們家有現車嗎", line_user_id=None))
    fake_now[0] += nlm_service._ANSWER_CACHE_TTL_SECONDS + 1
    asyncio.run(nlm_service.ask_question(channel_id, "你們家有現車嗎", line_user_id=None))

    assert len(client.chat.calls) == 2


def test_answer_not_yet_expired_is_still_served_from_cache(tmp_path, monkeypatch):
    channel_id, client = _setup(tmp_path, monkeypatch)

    fake_now = [1000.0]
    monkeypatch.setattr(nlm_service.time, "time", lambda: fake_now[0])

    asyncio.run(nlm_service.ask_question(channel_id, "你們家有現車嗎", line_user_id=None))
    fake_now[0] += nlm_service._ANSWER_CACHE_TTL_SECONDS - 1
    asyncio.run(nlm_service.ask_question(channel_id, "你們家有現車嗎", line_user_id=None))

    assert len(client.chat.calls) == 1
