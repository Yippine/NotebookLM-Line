import asyncio
import sqlite3

import database
import services.nlm_service as nlm_service
from services.crypto_service import encrypt


class _FakeChatAPI:
    """`ask` 每次呼叫都拋出指定的例外，模擬 cookie 失效等查詢失敗。"""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls = 0

    async def ask(self, notebook_id, question, source_ids=None, conversation_id=None):
        self.calls += 1
        raise self._exc


class _FakeSourcesAPI:
    async def list(self, notebook_id):
        return []


class _FakeClient:
    def __init__(self, exc: Exception):
        self.chat = _FakeChatAPI(exc)
        self.sources = _FakeSourcesAPI()


def _setup(tmp_path, monkeypatch, exc: Exception):
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

    client = _FakeClient(exc)

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


def test_auth_failure_shows_canned_reply_not_raw_error(tmp_path, monkeypatch):
    """實際發生過的真實情況：cookie 失效時，notebooklm-py 會拋出一個
    固定訊息的 ValueError（"Authentication expired or invalid...
    Run 'notebooklm login' to re-authenticate."）。這種內部技術訊息
    不該直接讓使用者看到，應該換成罐頭訊息；管理員告警則仍要保留
    完整的原始錯誤內容，才有辦法真正除錯。"""
    exc = ValueError(
        "Authentication expired or invalid. Redirected to: "
        "https://accounts.google.com/foo\nRun 'notebooklm login' to re-authenticate."
    )
    channel_id, alerts = _setup(tmp_path, monkeypatch, exc)

    messages = asyncio.run(nlm_service.ask_question(channel_id, "有什麼車", line_user_id="user-A"))

    assert messages == [nlm_service._AUTH_FAILURE_USER_REPLY]
    assert "Authentication expired" not in messages[0]
    assert "notebooklm login" not in messages[0]

    # 管理員告警仍然拿得到完整的原始錯誤，不受影響。
    assert len(alerts) == 1
    key, alert_message = alerts[0]
    assert key == f"nlm:{channel_id}"
    assert "Authentication expired" in alert_message


def test_other_errors_still_show_the_raw_message_not_the_canned_reply(tmp_path, monkeypatch):
    """非 cookie 失效的其他錯誤，行為維持原樣（不套用罐頭訊息），
    避免把使用者原本看得到的真正錯誤線索也一併蓋掉。"""
    exc = RuntimeError("some other unrelated failure")
    channel_id, alerts = _setup(tmp_path, monkeypatch, exc)

    messages = asyncio.run(nlm_service.ask_question(channel_id, "有什麼車", line_user_id="user-A"))

    assert messages == ["⚠️ 查詢失敗：some other unrelated failure"]
    assert len(alerts) == 1
