"""回歸測試：webhook() 必須在把事件排進 background_tasks 之後立刻
回應，不能等 NotebookLM 查詢（或任何回覆邏輯）真的跑完才回應。

背景：webhook() 原本是同步阻塞的，收到訊息會一路等到 NotebookLM
查詢完成才回應 LINE（實測過最久 5 分半）。LINE 對 webhook 回應有
嚴格的時間限制，這段等待期間 LINE 常常會判定逾時／失敗（nginx 側
可觀察到 499），而且 LINE 本身有內部的 webhook 品質評分機制，回應
慢/常失敗的端點之後可能被延遲或跳過投遞——這正是「服務明明正常，
訊息卻偶爾送不到」這個問題最可能的根本原因之一。這裡驗證的就是
修正後的行為：webhook() 回應時，實際的回覆邏輯應該*還沒有*被執行，
只有在事後真的把 background_tasks 跑完，才會看到效果。
"""
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


def _text_event(question: str) -> dict:
    return {
        "type": "message",
        "replyToken": "reply-token-1",
        "source": {"type": "user", "userId": "user-1"},
        "message": {"type": "text", "id": "msg-1", "text": question},
    }


def _payload_request(question: str, secret: str) -> _FakeRequest:
    payload = {"events": [_text_event(question)]}
    body = json.dumps(payload).encode("utf-8")
    return _FakeRequest(body, _sign(body, secret))


def test_webhook_returns_before_ask_and_reply_runs(tmp_path, monkeypatch):
    """webhook() 回應的當下，實際的問答邏輯（ask_question/reply_text）
    應該完全還沒執行——只有排進 background_tasks 而已。"""
    channel_id, secret, _ = _setup(tmp_path, monkeypatch)
    calls = []

    async def fake_ask_question(cid, question, line_user_id=None):
        calls.append(("ask", question))
        return ["【SUM】\n\n答案內容"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        calls.append(("reply", text))

    async def fake_show_loading(target_id, access_token, seconds=30):
        return True

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "show_loading", fake_show_loading)

    async def scenario():
        request = _payload_request("Corolla Cross 有哪些現車", secret)
        background_tasks = BackgroundTasks()

        result = await webhook.webhook(channel_id, request, background_tasks)

        # webhook() 已經回應了（拿到結果），但問答邏輯完全還沒被呼叫
        # 過——這就是修正的核心：回應不會被卡在 NotebookLM 查詢後面。
        assert result == {"status": "ok"}
        assert calls == []

        # 事後真的把背景任務跑完，才會看到問答邏輯真的執行了。
        await background_tasks()
        assert ("ask", "Corolla Cross 有哪些現車") in calls
        assert any(kind == "reply" for kind, _ in calls)

    asyncio.run(scenario())


def test_webhook_returns_before_file_batch_upload_runs(tmp_path, monkeypatch):
    """檔案上傳批次（_handle_file_batch）走的是另一條背景任務，
    同樣不該卡住 webhook 本身的回應。"""
    channel_id, secret, _ = _setup(tmp_path, monkeypatch)
    calls = []

    async def fake_update_knowledge_base(cid, file_bytes, file_name):
        calls.append(file_name)
        return f"已新增檔案：{file_name}"

    async def fake_download_content(file_id, access_token):
        return b"fake file bytes"

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        calls.append(("reply", text))

    async def fake_push_text(target_id, access_token, text):
        calls.append(("push", text))

    async def fake_show_loading(target_id, access_token, seconds=30):
        return True

    monkeypatch.setattr(webhook, "update_knowledge_base", fake_update_knowledge_base)
    monkeypatch.setattr(webhook, "download_content", fake_download_content)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "push_text", fake_push_text)
    monkeypatch.setattr(webhook, "show_loading", fake_show_loading)

    async def scenario():
        payload = {
            "events": [
                {
                    "type": "message",
                    "replyToken": "reply-token-1",
                    "source": {"type": "user", "userId": "user-1"},
                    "message": {"type": "file", "id": "file-1", "fileName": "型錄.pdf"},
                }
            ]
        }
        body = json.dumps(payload).encode("utf-8")
        request = _FakeRequest(body, _sign(body, secret))
        background_tasks = BackgroundTasks()

        result = await webhook.webhook(channel_id, request, background_tasks)

        assert result == {"status": "ok"}
        assert calls == []

        await background_tasks()
        assert "型錄.pdf" in calls

    asyncio.run(scenario())
