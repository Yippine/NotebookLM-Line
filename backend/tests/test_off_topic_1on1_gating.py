import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3

import database
import routers.webhook as webhook


class _FakeRequest:
    """滿足 `webhook()` 對 Request 的唯二需求（`.body()` 與
    `.headers.get(...)`），不需要真的架一個 FastAPI TestClient。"""

    def __init__(self, body: bytes, signature: str):
        self._body = body
        self.headers = {"x-line-signature": signature}

    async def body(self) -> bytes:
        return self._body


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _setup(tmp_path, monkeypatch, channel_id="test-channel", secret="secret", token="token"):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB", db_path)
    monkeypatch.setattr(webhook, "DB", db_path)
    asyncio.run(database.init_db())

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO channels (channel_id, channel_secret, channel_access_token) VALUES (?,?,?)",
        (channel_id, secret, token),
    )
    conn.commit()
    conn.close()
    return channel_id, secret, token


def _text_event(question: str, source_type: str = "user") -> dict:
    source = {"type": source_type, "userId": "user-1"}
    if source_type in ("group", "room"):
        key = "groupId" if source_type == "group" else "roomId"
        source[key] = "container-1"
    return {
        "type": "message",
        "replyToken": "reply-token-1",
        "source": source,
        "message": {"type": "text", "id": "msg-1", "text": question},
    }


def _run_webhook(channel_id, secret, question, source_type="user"):
    payload = {"events": [_text_event(question, source_type)]}
    body = json.dumps(payload).encode("utf-8")
    request = _FakeRequest(body, _sign(body, secret))
    return asyncio.run(webhook.webhook(channel_id, request))


def test_off_topic_question_in_1on1_chat_is_declined_without_reaching_notebooklm(tmp_path, monkeypatch):
    """重現實際發生過的真實案例：1:1 聊天問了跟車輛完全無關的問題
    （颱風動態），因為 1:1 完全沒有經過群組/聊天室才有的車輛相關性
    判斷，直接被轉給 NotebookLM——NotebookLM 有時會為了「幫忙」而
    觸發它自己的網路搜尋功能，回答裡夾帶只有網頁版看得懂的 UI
    元件描述，在 LINE 上會變成一大坨亂碼。1:1 也該做跟群組一樣的
    車輛相關性把關，跟知識庫無關的問題一律當閒聊回絕，不送進
    NotebookLM。"""
    channel_id, secret, _ = _setup(tmp_path, monkeypatch)
    asked = []
    replied = []

    async def fake_ask_question(cid, question, line_user_id=None):
        asked.append(question)
        return ["不應該被呼叫到"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    async def fake_show_loading(target_id, access_token, seconds=30):
        return True

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "show_loading", fake_show_loading)

    _run_webhook(channel_id, secret, "颱風會不會襲台")

    assert asked == []  # 完全沒有轉給 NotebookLM
    assert replied == [webhook._OFF_TOPIC_REPLY]


def test_car_related_question_in_1on1_chat_still_reaches_notebooklm(tmp_path, monkeypatch):
    """確保這次把關沒有連帶擋掉正常的 1:1 車輛諮詢。"""
    channel_id, secret, _ = _setup(tmp_path, monkeypatch)
    asked = []
    replied = []

    async def fake_ask_question(cid, question, line_user_id=None):
        asked.append(question)
        return ["【SUM】\n\n答案內容"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    async def fake_show_loading(target_id, access_token, seconds=30):
        return True

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "show_loading", fake_show_loading)

    _run_webhook(channel_id, secret, "Corolla Cross 有哪些現車")

    assert asked == ["Corolla Cross 有哪些現車"]
    assert replied == [["【SUM】\n\n答案內容"]]


def test_off_topic_question_mentioned_in_group_chat_is_not_affected_by_this_change(tmp_path, monkeypatch):
    """群組聊天原本就有自己的一套把關（車輛相關性或 @mention），
    這次改動只補 1:1 的漏洞，不該動到群組既有的行為——即使群組訊息
    離題，只要有 @mention 就仍然會被處理（沿用既有行為），不受這次
    新增的 1:1 專屬檢查影響。"""
    channel_id, secret, _ = _setup(tmp_path, monkeypatch)
    asked = []

    async def fake_ask_question(cid, question, line_user_id=None):
        asked.append(question)
        return ["【SUM】\n\n答案內容"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        pass

    async def fake_push_text(target_id, access_token, text):
        pass

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "push_text", fake_push_text)

    _run_webhook(channel_id, secret, f"@{webhook.BOT_NAME} 颱風會不會襲台", source_type="group")

    assert asked == ["颱風會不會襲台"]
