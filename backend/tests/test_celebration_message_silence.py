import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3

from fastapi import BackgroundTasks

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


async def _call_webhook_and_run_background_tasks(channel_id, request):
    """webhook() 現在只把每個事件排進 background_tasks、不在請求
    處理期間 await 它們（見 routers/webhook.py 裡的說明：不想讓
    webhook 的回應卡在 NotebookLM 查詢完成之前）。真正的 FastAPI
    TestClient 在回應送出後會自動接著執行這些背景任務，但這裡是
    直接呼叫 route function、繞過整個 ASGI 流程，所以要自己補上
    這一步——否則測試只驗證得到「排進去了」，驗證不到「實際執行的
    結果」（有沒有回覆、有沒有呼叫 ask_question）。"""
    background_tasks = BackgroundTasks()
    result = await webhook.webhook(channel_id, request, background_tasks)
    await background_tasks()
    return result


def _run_webhook(channel_id, secret, question, source_type="user"):
    payload = {"events": [_text_event(question, source_type)]}
    body = json.dumps(payload).encode("utf-8")
    request = _FakeRequest(body, _sign(body, secret))
    return asyncio.run(_call_webhook_and_run_background_tasks(channel_id, request))


def _patch_common(monkeypatch, asked, replied):
    async def fake_ask_question(cid, question, line_user_id=None):
        asked.append(question)
        return ["不應該被呼叫到"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    async def fake_push_text(target_id, access_token, text):
        replied.append(text)

    async def fake_show_loading(target_id, access_token, seconds=30):
        return True

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "push_text", fake_push_text)
    monkeypatch.setattr(webhook, "show_loading", fake_show_loading)


def test_celebration_post_in_group_chat_gets_no_reply_at_all(tmp_path, monkeypatch):
    """經銷商群組常見的「賀成交」報喜貼文即使提到實際車型（會被判定
    為車輛相關而通過群組把關），也不該被機器人回應——這種公告不是
    在提問，連轉給 NotebookLM 或導向廠商聯絡資訊的回覆都不該送。"""
    channel_id, secret, _ = _setup(tmp_path, monkeypatch)
    asked, replied = [], []
    _patch_common(monkeypatch, asked, replied)

    _run_webhook(channel_id, secret, "賀成交！恭喜客戶入主 X-Trail", source_type="group")

    assert asked == []
    assert replied == []


def test_message_with_only_deal_closed_keyword_in_1on1_gets_no_reply(tmp_path, monkeypatch):
    """1:1 私訊也適用同一條靜默規則。"""
    channel_id, secret, _ = _setup(tmp_path, monkeypatch)
    asked, replied = [], []
    _patch_common(monkeypatch, asked, replied)

    _run_webhook(channel_id, secret, "這台車已經成交了嗎？")

    assert asked == []
    assert replied == []


def test_celebration_keyword_overrides_explicit_mention_in_group_chat(tmp_path, monkeypatch):
    """即使明確 @mention 機器人，只要訊息含「賀」或「成交」，一樣完全
    不回覆——這條規則的優先權高於 @mention 的既有處理路徑。"""
    channel_id, secret, _ = _setup(tmp_path, monkeypatch)
    asked, replied = [], []
    _patch_common(monkeypatch, asked, replied)

    _run_webhook(
        channel_id, secret, f"@{webhook.BOT_NAME} 賀成交！恭喜客戶", source_type="group",
    )

    assert asked == []
    assert replied == []


def test_normal_car_question_without_celebration_keywords_is_unaffected(tmp_path, monkeypatch):
    """確保這條新規則沒有連帶擋掉不含「賀」「成交」字樣的正常車輛
    諮詢。"""
    channel_id, secret, _ = _setup(tmp_path, monkeypatch)
    asked = []

    async def fake_ask_question(cid, question, line_user_id=None):
        asked.append(question)
        return ["【SUM】\n\n答案內容"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        pass

    async def fake_show_loading(target_id, access_token, seconds=30):
        return True

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "show_loading", fake_show_loading)

    _run_webhook(channel_id, secret, "Corolla Cross 有哪些現車")

    assert asked == ["Corolla Cross 有哪些現車"]
