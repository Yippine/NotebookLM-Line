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
    def __init__(self, answer, conversation_id, references):
        self.answer = answer
        self.conversation_id = conversation_id
        self.references = references


class _FakeChatAPI:
    def __init__(self, answer, references):
        self._answer = answer
        self._references = references
        self.calls = 0

    async def ask(self, notebook_id, question, source_ids=None, conversation_id=None):
        self.calls += 1
        return _FakeAskResult(answer=self._answer, conversation_id="conv-1", references=self._references)


class _FakeSourcesAPI:
    def __init__(self, sources):
        self._sources = sources

    async def list(self, notebook_id):
        return self._sources


class _FakeClient:
    def __init__(self, answer, references, sources):
        self.chat = _FakeChatAPI(answer, references)
        self.sources = _FakeSourcesAPI(sources)


def _setup(tmp_path, monkeypatch, answer, references, sources):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB", db_path)
    monkeypatch.setattr(nlm_service, "DB", db_path)
    asyncio.run(database.init_db())

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

    client = _FakeClient(answer, references, sources)

    class _FakeStorageContext:
        async def __aenter__(self):
            return client

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    class _FakeNotebookLMClient:
        @staticmethod
        def from_storage():
            return _FakeStorageContext()

    monkeypatch.setattr(nlm_service, "NotebookLMClient", _FakeNotebookLMClient)

    alerts = []

    async def fake_notify_admin(key, message, **kwargs):
        alerts.append((key, message))

    monkeypatch.setattr(nlm_service, "notify_admin", fake_notify_admin)

    return channel_id, alerts


def test_unclassified_drop_triggers_admin_alert(tmp_path, monkeypatch):
    """實際發生過的真實案例：新上傳的來源檔名不符合命名慣例
    （例如「notes.md」沒有「{廠商}_{日期}」前綴），內容因此被
    `build_answer_messages` 靜默捨棄。這種捨棄過去完全沒有任何
    記錄，現在應該觸發一次管理員告警。"""
    answer = "甲廠商有現車 [1]。\n\n這段查不到廠商 [2]。"
    references = [_FakeReference(1, "src-a"), _FakeReference(2, "src-b")]
    sources = [
        _FakeSource("src-a", "廠商甲_20260101.csv"),
        _FakeSource("src-b", "notes.md"),  # 不符合命名慣例，會被判定為未分類
    ]
    channel_id, alerts = _setup(tmp_path, monkeypatch, answer, references, sources)

    messages = asyncio.run(nlm_service.ask_question(channel_id, "有什麼車", line_user_id="user-A"))

    assert any("廠商甲" in m for m in messages)
    assert len(alerts) == 1
    key, message = alerts[0]
    assert key == f"unclassified-drop:{channel_id}"
    assert "未分類" in message
    assert channel_id in message


def test_no_unclassified_content_does_not_trigger_the_drop_alert(tmp_path, monkeypatch):
    """答案裡所有內容都能正確歸屬到廠商時，不該誤發這個告警。"""
    answer = "甲廠商有現車 [1]。\n\n乙廠商也有現車 [2]。"
    references = [_FakeReference(1, "src-a"), _FakeReference(2, "src-b")]
    sources = [
        _FakeSource("src-a", "廠商甲_20260101.csv"),
        _FakeSource("src-b", "廠商乙_20260101.csv"),
    ]
    channel_id, alerts = _setup(tmp_path, monkeypatch, answer, references, sources)

    asyncio.run(nlm_service.ask_question(channel_id, "有什麼車", line_user_id="user-A"))

    assert alerts == []
