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


def _insert_conversation(db_path: str, channel_id: str, line_user_id: str, minutes_ago: float) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO user_conversations (channel_id, line_user_id, conversation_id, updated_at) "
        "VALUES (?, ?, 'conv-1', datetime('now', ?))",
        (channel_id, line_user_id, f"-{minutes_ago} minutes"),
    )
    conn.commit()
    conn.close()


def test_follow_up_without_car_keywords_still_reaches_notebooklm_during_active_conversation(tmp_path, monkeypatch):
    """重現實際發生過的真實案例：機器人一次回覆了跨多家廠商的比較
    表，使用者馬上追問「其他5家呢？」——這句話完全沒有任何車輛
    關鍵字，卻被當成閒聊直接打槍。使用者剛剛才跟機器人聊過車
    （2 分鐘前才有一則對話紀錄），這時候不該重新套用嚴格的關鍵字
    判斷，應該讓追問自然延續下去。"""
    channel_id, secret, _ = _setup(tmp_path, monkeypatch)
    db_path = database.DB
    _insert_conversation(db_path, channel_id, "user-1", minutes_ago=2)

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

    _run_webhook(channel_id, secret, "其他5家呢？")

    assert asked == ["其他5家呢？"]


def test_stale_conversation_does_not_bypass_the_off_topic_gate(tmp_path, monkeypatch):
    """對話紀錄如果是很久以前的（超過 `_RECENT_CONVERSATION_WINDOW`），
    不該被當成「現在仍在同一輪對話裡」——否則使用者幾天前問過車，
    之後隨口問一句完全無關的天氣，也會被誤判成延續話題、直接送進
    NotebookLM，重新暴露這道關卡原本要擋的網路搜尋亂碼風險。"""
    channel_id, secret, _ = _setup(tmp_path, monkeypatch)
    db_path = database.DB
    _insert_conversation(db_path, channel_id, "user-1", minutes_ago=60 * 24)  # 一天前

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

    assert asked == []
    assert replied == [webhook._OFF_TOPIC_REPLY]
