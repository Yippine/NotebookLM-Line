import asyncio

import routers.webhook as webhook
from config import settings


def _run(coro):
    return asyncio.run(coro)


def test_working_indicator_uses_native_loading_for_1on1_chats(monkeypatch):
    calls = []

    async def fake_show_loading(target_id, access_token, seconds=30):
        calls.append(("show_loading", target_id))
        return True

    async def fake_push_text(target_id, access_token, text):
        calls.append(("push_text", target_id, text))

    monkeypatch.setattr(webhook, "show_loading", fake_show_loading)
    monkeypatch.setattr(webhook, "push_text", fake_push_text)

    _run(webhook._show_working_indicator("user", "target-1", "token-1", "⏳正在查詢中，請稍後"))

    assert calls == [("show_loading", "target-1")]


def test_working_indicator_falls_back_to_push_for_group_chats(monkeypatch):
    calls = []

    async def fake_show_loading(target_id, access_token, seconds=30):
        calls.append(("show_loading", target_id))
        return True

    async def fake_push_text(target_id, access_token, text):
        calls.append(("push_text", target_id, text))

    monkeypatch.setattr(webhook, "show_loading", fake_show_loading)
    monkeypatch.setattr(webhook, "push_text", fake_push_text)

    _run(webhook._show_working_indicator("group", "target-1", "token-1", "⏳正在查詢中，請稍後"))

    assert calls == [("push_text", "target-1", "⏳正在查詢中，請稍後")]


def test_working_indicator_push_failure_is_swallowed_not_raised(monkeypatch):
    """非關鍵路徑：載入提示因為 push 額度用盡而遺失，絕不能阻塞
    或使真正的回答流程失敗。"""
    async def fake_push_text(target_id, access_token, text):
        raise RuntimeError("LINE push 失敗：429 monthly limit")

    monkeypatch.setattr(webhook, "push_text", fake_push_text)

    _run(webhook._show_working_indicator("group", "target-1", "token-1", "⏳正在查詢中，請稍後"))
    # 沒有拋出例外——即為成功


def test_purchase_intent_replies_with_matched_vendor_contact_info(monkeypatch):
    """當 con_info 查詢比對到相符的廠商時，回覆內容應該先是拒答的
    前綴，接著才是帶有廠商標籤的聯絡資訊——而不是通用的單一
    dealer_contact_info 備援回覆。透過 reply_text（而非 push_text）
    送出，因為 reply 不會被計入額度。"""
    replied = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        assert line_user_id is None  # 這是獨立查詢，不依附於使用者的對話串
        assert "con_info" in question
        return ["【SUM】\n\n電話：02-1234-5678"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)

    _run(webhook._purchase_intent_ask_and_reply(
        "channel-1", "reply-token-1", "token-1",
        "我要買 Corolla Cross2024年尊爵版，請給我廠商的聯絡資訊",
        sender_user_id="user-1",
    ))

    assert len(replied) == 1
    messages = replied[0]
    assert webhook._PURCHASE_INTENT_PREFIX in messages[0]
    assert webhook._PURCHASE_INTENT_CONTACT_LEAD_IN in messages[0]
    assert messages[1] == "【SUM】\n\n電話：02-1234-5678"
    assert settings.dealer_contact_info not in "".join(messages)


def test_purchase_intent_falls_back_when_no_vendor_matched(monkeypatch):
    """如果查詢無法歸屬到任何廠商（例如一個通用的「我想買車」問題，
    沒有任何東西可以比對），就退回使用通用的 dealer_contact_info
    回覆，而不是一則沒有標籤、令人困惑的訊息。"""
    replied = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return ["很抱歉，目前的資料無法明確對應到特定廠商，請提供更明確的條件（如廠牌、車型）以便查詢。"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)

    _run(webhook._purchase_intent_ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "我要買車",
        sender_user_id="user-1",
    ))

    assert len(replied) == 1
    assert settings.dealer_contact_info in replied[0]


def test_purchase_intent_ask_question_error_still_gets_polite_reply(monkeypatch):
    """一個真正的 ask_question 失敗（例如 NotebookLM 未綁定），
    不能讓使用者看到原始的 ⚠️ 錯誤文字——仍然要給禮貌的通用回覆，
    跟「沒有比對到廠商」時看起來一樣，不讓使用者感覺到後端出錯。"""
    replied = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return ["⚠️ NotebookLM 尚未綁定，請聯繫管理員完成設定。"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)

    _run(webhook._purchase_intent_ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "我要買 Corolla Cross",
        sender_user_id="user-1",
    ))

    assert len(replied) == 1
    assert settings.dealer_contact_info in replied[0]  # 仍是禮貌的回覆，而不是原始的 ⚠️ 文字


def test_purchase_intent_falls_back_on_lookup_error(monkeypatch):
    replied = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        raise RuntimeError("boom")

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)

    _run(webhook._purchase_intent_ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "我要買 Corolla Cross",
        sender_user_id="user-1",
    ))

    assert len(replied) == 1
    assert settings.dealer_contact_info in replied[0]


def test_ask_and_reply_delivers_answer_via_reply(monkeypatch):
    replied = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return ["【SUM】\n\n答案內容"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)

    _run(webhook._ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "Corolla Cross 有哪些",
        sender_user_id="user-1",
    ))

    assert replied == [["【SUM】\n\n答案內容"]]


def test_upload_and_reply_delivers_confirmation_via_reply(monkeypatch):
    replied = []

    async def fake_download_content(file_id, access_token):
        return b"file bytes"

    async def fake_update_knowledge_base(channel_id, file_bytes, file_name):
        return "✅ 知識庫已更新。已新增檔案：8891_20260715.md"

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    monkeypatch.setattr(webhook, "download_content", fake_download_content)
    monkeypatch.setattr(webhook, "update_knowledge_base", fake_update_knowledge_base)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)

    _run(webhook._upload_and_reply(
        "channel-1", "reply-token-1", "token-1", "file-id-1", "8891_20260715.md",
    ))

    assert replied == ["✅ 知識庫已更新。已新增檔案：8891_20260715.md"]


def test_upload_and_reply_falls_back_on_missing_file_id(monkeypatch):
    replied = []

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)

    _run(webhook._upload_and_reply(
        "channel-1", "reply-token-1", "token-1", None, "file.md",
    ))

    assert len(replied) == 1
    assert "更新知識庫失敗" in replied[0]
