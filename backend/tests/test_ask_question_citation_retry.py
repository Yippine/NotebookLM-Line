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


_VENDOR_A_SOURCE = _FakeSource("src-a", "廠商甲_20260101.csv")
_VENDOR_B_SOURCE = _FakeSource("src-b", "廠商乙_20260101.csv")

# NotebookLM 有時候會漏給答案文字裡實際引用到的某個編號對應的
# reference（這裡故意漏掉 [2]），藉此重現 source_map 缺一個 key、
# 但答案文字仍然用到該編號的情況。
_INCOMPLETE_ANSWER = (
    "【廠商甲】\n甲有現車 [1]。\n\n【廠商乙】\n乙有現車 [2]。"
)
_INCOMPLETE_REFERENCES = [_FakeReference(1, "src-a")]  # 缺了 citation 2

_COMPLETE_ANSWER = _INCOMPLETE_ANSWER
_COMPLETE_REFERENCES = [_FakeReference(1, "src-a"), _FakeReference(2, "src-b")]


class _FakeChatAPI:
    """依序回傳預先準備好的答案，讓測試能精準控制每一次呼叫的結果。"""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0
        # 記錄每一次呼叫實際帶入的 conversation_id，讓測試能驗證
        # 「重試時是不是真的換成了全新對話（None）」。
        self.received_conversation_ids = []

    async def ask(self, notebook_id, question, source_ids=None, conversation_id=None):
        self.calls += 1
        self.received_conversation_ids.append(conversation_id)
        answer, references = self._results.pop(0)
        return _FakeAskResult(
            answer=answer,
            conversation_id=f"conv-{self.calls}",
            references=references,
        )


class _FakeSourcesAPI:
    async def list(self, notebook_id):
        return [_VENDOR_A_SOURCE, _VENDOR_B_SOURCE]


class _FakeClient:
    def __init__(self, results):
        self.chat = _FakeChatAPI(results)
        self.sources = _FakeSourcesAPI()


def _setup(tmp_path, monkeypatch, results):
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

    client = _FakeClient(results)

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

    return channel_id, client


def test_incomplete_references_trigger_one_retry(tmp_path, monkeypatch):
    """第一次的 references 漏了答案裡用到的 [2]，應該自動重問一次，
    並改用重問後（完整）的結果做廠商分割。"""
    results = [
        (_INCOMPLETE_ANSWER, _INCOMPLETE_REFERENCES),
        (_COMPLETE_ANSWER, _COMPLETE_REFERENCES),
    ]
    channel_id, client = _setup(tmp_path, monkeypatch, results)

    messages = asyncio.run(nlm_service.ask_question(channel_id, "有什麼車", line_user_id="user-A"))

    assert client.chat.calls == 2  # 重問了一次
    # 用重問後完整的 source_map 分割，兩個廠商應該各自成一則訊息。
    assert len(messages) == 2
    assert any("廠商甲" in m for m in messages)
    assert any("廠商乙" in m for m in messages)


def test_complete_references_do_not_trigger_retry(tmp_path, monkeypatch):
    """第一次的 references 就已經完整涵蓋所有引用編號時，不應該多問一次。"""
    results = [(_COMPLETE_ANSWER, _COMPLETE_REFERENCES)]
    channel_id, client = _setup(tmp_path, monkeypatch, results)

    messages = asyncio.run(nlm_service.ask_question(channel_id, "有什麼車", line_user_id="user-A"))

    assert client.chat.calls == 1
    assert len(messages) == 2


def test_retry_gives_up_after_one_attempt_if_still_incomplete(tmp_path, monkeypatch):
    """重問一次之後如果還是殘缺，就直接採用，不會無限重問。"""
    results = [
        (_INCOMPLETE_ANSWER, _INCOMPLETE_REFERENCES),
        (_INCOMPLETE_ANSWER, _INCOMPLETE_REFERENCES),
    ]
    channel_id, client = _setup(tmp_path, monkeypatch, results)

    messages = asyncio.run(nlm_service.ask_question(channel_id, "有什麼車", line_user_id="user-A"))

    assert client.chat.calls == 2  # 只重試了一次，沒有繼續重問
    # 缺了對照的 [2] 那段找不到廠商，會被併入前一個（唯一）廠商的訊息裡。
    assert len(messages) == 1
    assert "廠商甲" in messages[0]


def _seed_existing_conversation(tmp_path, channel_id, line_user_id, conversation_id):
    """在 user_conversations 裡預先塞一筆既有對話，讓 ask_question()
    這次提問會帶著這個 conversation_id 延續下去，而不是全新開始。"""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO user_conversations (channel_id, line_user_id, conversation_id) "
        "VALUES (?,?,?)",
        (channel_id, line_user_id, conversation_id),
    )
    conn.commit()
    conn.close()


def test_empty_answer_on_existing_conversation_retries_with_fresh_conversation(tmp_path, monkeypatch):
    """回歸測試：重現真實發生過的案例——同一個使用者的對話串經過長時間
    大量輪次累積之後，繼續延續這個既有 conversation_id 追問，NotebookLM
    開始每次都回傳完全空白的答案（沒有拋例外）。應該自動改用全新對話
    （conversation_id=None）重試一次，而不是直接讓使用者收到「沒有取得
    回覆內容」。"""
    line_user_id = "user-A"
    results = [
        ("", []),  # 延續舊對話：回傳空答案
        (_COMPLETE_ANSWER, _COMPLETE_REFERENCES),  # 改用全新對話：正常回答
    ]
    channel_id, client = _setup(tmp_path, monkeypatch, results)
    _seed_existing_conversation(tmp_path, channel_id, line_user_id, "stuck-conv-id")

    messages = asyncio.run(nlm_service.ask_question(channel_id, "有什麼車", line_user_id=line_user_id))

    assert client.chat.calls == 2
    assert client.chat.received_conversation_ids == ["stuck-conv-id", None]
    assert len(messages) == 2
    assert any("廠商甲" in m for m in messages)


def test_empty_answer_without_existing_conversation_does_not_retry(tmp_path, monkeypatch):
    """全新對話（一開始就沒有既有 conversation_id）本來就沒有更舊的
    對話可以退回去，回傳空答案時不該多重試一次——沒有意義，直接讓
    後面的「沒有取得回覆內容」備援訊息接手即可。"""
    results = [("", [])]
    channel_id, client = _setup(tmp_path, monkeypatch, results)

    messages = asyncio.run(nlm_service.ask_question(channel_id, "有什麼車", line_user_id="user-A"))

    assert client.chat.calls == 1
    # build_answer_messages() 對完全空白的答案回傳空列表——這正是
    # routers/webhook.py._ask_and_reply() 判斷「沒有取得回覆內容，
    # 請稍後再試」備援訊息的依據（見該函式裡的 `if not messages:`）。
    assert messages == []


def test_empty_answer_persists_after_retry_gives_up(tmp_path, monkeypatch):
    """改用全新對話重試後，如果還是空答案，就直接採用（沿用既有的
    「只重試一次」原則），不會無限重問。"""
    line_user_id = "user-A"
    results = [("", []), ("", [])]
    channel_id, client = _setup(tmp_path, monkeypatch, results)
    _seed_existing_conversation(tmp_path, channel_id, line_user_id, "stuck-conv-id")

    messages = asyncio.run(nlm_service.ask_question(channel_id, "有什麼車", line_user_id=line_user_id))

    assert client.chat.calls == 2  # 只重試了一次
    assert client.chat.received_conversation_ids == ["stuck-conv-id", None]
    # build_answer_messages() 對完全空白的答案回傳空列表——這正是
    # routers/webhook.py._ask_and_reply() 判斷「沒有取得回覆內容，
    # 請稍後再試」備援訊息的依據（見該函式裡的 `if not messages:`）。
    assert messages == []
