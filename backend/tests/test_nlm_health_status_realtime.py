import asyncio
import sqlite3

import database
import services.nlm_service as nlm_service
from services.crypto_service import encrypt


class _FakeReference:
    def __init__(self, citation_number, source_id):
        self.citation_number = citation_number
        self.source_id = source_id


class _FakeAskResult:
    def __init__(self, answer, conversation_id, references):
        self.answer = answer
        self.conversation_id = conversation_id
        self.references = references


class _FakeChatAPI:
    """`ask` 每次呼叫都拋出指定的例外，模擬 cookie 失效等查詢失敗。"""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls = 0

    async def ask(self, notebook_id, question, source_ids=None, conversation_id=None):
        self.calls += 1
        raise self._exc


class _FakeSucceedingChatAPI:
    async def ask(self, notebook_id, question, source_ids=None, conversation_id=None):
        return _FakeAskResult(answer="有現車喔 [1]。", conversation_id="conv-1", references=[
            _FakeReference(1, "src-a"),
        ])


class _FakeSourcesAPI:
    async def list(self, notebook_id):
        return []


class _FakeClient:
    def __init__(self, chat):
        self.chat = chat
        self.sources = _FakeSourcesAPI()


def _install_fake_client(monkeypatch, client):
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


def _setup(tmp_path, monkeypatch, chat, *, initial_health_status=None):
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
        "(channel_id, channel_secret, channel_access_token, nlm_auth_json_encrypted, "
        "notebook_id, nlm_health_status) VALUES (?,?,?,?,?,?)",
        (channel_id, "secret", "token", encrypted, "notebook-1", initial_health_status),
    )
    conn.commit()
    conn.close()

    client = _FakeClient(chat)
    _install_fake_client(monkeypatch, client)

    async def fake_notify_admin(key, message, **kwargs):
        pass

    monkeypatch.setattr(nlm_service, "notify_admin", fake_notify_admin)

    return channel_id, db_path


def _read_health_status(db_path, channel_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT nlm_health_status FROM channels WHERE channel_id=?", (channel_id,)
    ).fetchone()
    conn.close()
    return row[0]


def test_auth_failure_marks_channel_expired_immediately(tmp_path, monkeypatch):
    """cookie 失效當場撞到的那次提問，就該立刻把 nlm_health_status
    標成 expired——不能只靠 check_all_channels_health() 的排程（最多
    半小時後）才發現，那樣 scripts/nlm_cookie_refresh.py 的偵測閘門
    要等很久才會看到這個 channel 需要刷新。"""
    exc = ValueError(
        "Authentication expired or invalid. Redirected to: "
        "https://accounts.google.com/foo\nRun 'notebooklm login' to re-authenticate."
    )
    channel_id, db_path = _setup(tmp_path, monkeypatch, _FakeChatAPI(exc), initial_health_status="healthy")

    asyncio.run(nlm_service.ask_question(channel_id, "有什麼車", line_user_id="user-A"))

    assert _read_health_status(db_path, channel_id) == "expired"


def test_successful_ask_after_expired_flips_status_back_to_healthy(tmp_path, monkeypatch):
    """channel 原本被標成 expired，但這次提問其實成功了（例如套件自動
    換發了新 cookie）——應該立刻改回 healthy，不要讓
    scripts/nlm_cookie_refresh.py 誤以為還在壞、白白再刷新一次。"""
    channel_id, db_path = _setup(
        tmp_path, monkeypatch, _FakeSucceedingChatAPI(), initial_health_status="expired"
    )

    messages = asyncio.run(nlm_service.ask_question(channel_id, "有什麼車", line_user_id="user-A"))

    assert messages  # 確認提問真的成功了
    assert _read_health_status(db_path, channel_id) == "healthy"


class _FakeNotebook:
    def __init__(self, id, title):
        self.id = id
        self.title = title


class _FakeNotebooksAPI:
    async def list(self):
        return [_FakeNotebook("notebook-1", "示範筆記本")]


class _FakeChatConfigureAPI:
    async def configure(self, notebook_id, goal=None, custom_prompt=None):
        return None


def test_successful_rebind_flips_expired_status_back_to_healthy(tmp_path, monkeypatch):
    """回歸測試：channel 原本被標成 expired，scripts/nlm_cookie_refresh.py
    偵測到之後自動重新綁定（呼叫的正是 bind_nlm，跟使用者手動重新登入
    走同一條路）——重新綁定當下已經用新的認證資訊實際打過一次
    list_notebooks() 並且成功了，nlm_health_status 應該立刻改回
    healthy，不能維持在上一次留下的 expired。

    根本原因：bind_nlm() 原本只更新 nlm_auth_json_encrypted / notebook_id，
    完全沒碰 nlm_health_status——cookie 明明已經刷新成功，這個欄位卻
    一直卡在 expired，直到剛好某次排程健康檢查或剛好有人問了問題才會
    被動清掉，中間這段空窗期會讓下一次健康檢查（例如每次部署重啟後端
    都會立刻做一次）誤以為又是一次新的失效、重複發出告警，讓管理員
    感覺不管 cookie 續期成功還是失敗都一直收到通知。"""
    chat = _FakeChatConfigureAPI()
    channel_id, db_path = _setup(tmp_path, monkeypatch, chat, initial_health_status="expired")

    client = nlm_service._client_cache.get(channel_id)
    # bind_nlm 用的是 NotebookLMClient.from_storage()（見 _install_fake_client），
    # 跟 ask_question 快取的 client 是分開的一條路，這裡幫它補上
    # bind_nlm 會用到的 .notebooks 屬性。
    monkeypatch.setattr(
        nlm_service,
        "NotebookLMClient",
        type(
            "_FakeNotebookLMClient",
            (),
            {
                "from_storage": staticmethod(
                    lambda: _FakeBindStorageContext(chat)
                )
            },
        ),
    )

    notebook_id, notebooks = asyncio.run(
        nlm_service.bind_nlm(channel_id, {"cookies": []})
    )

    assert notebook_id == "notebook-1"
    assert _read_health_status(db_path, channel_id) == "healthy"


class _FakeBindClient:
    def __init__(self, chat):
        self.chat = chat
        self.notebooks = _FakeNotebooksAPI()


class _FakeBindStorageContext:
    def __init__(self, chat):
        self._client = _FakeBindClient(chat)

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None
