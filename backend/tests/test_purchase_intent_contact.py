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
    """Non-critical: losing the loading indicator to the push quota must
    never block or fail the actual answer flow."""
    async def fake_push_text(target_id, access_token, text):
        raise RuntimeError("LINE push 失敗：429 monthly limit")

    monkeypatch.setattr(webhook, "push_text", fake_push_text)

    _run(webhook._show_working_indicator("group", "target-1", "token-1", "⏳正在查詢中，請稍後"))
    # no exception raised — success


def test_purchase_intent_replies_with_matched_vendor_contact_info(monkeypatch):
    """When the con_info lookup finds a matching vendor, the reply should
    lead with the refusal prefix, then the vendor-labeled contact info —
    not the generic single dealer_contact_info fallback. Delivered via
    reply_text (not push_text), since replies aren't quota-metered."""
    replied = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        assert line_user_id is None  # standalone lookup, not tied to user's thread
        assert "con_info" in question
        return ["【SUM】\n\n電話：02-1234-5678"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    async def fake_record_interaction(*args, **kwargs):
        pass

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "_record_interaction", fake_record_interaction)

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
    """If the lookup can't attribute any vendor (e.g. a generic 'I want to
    buy a car' question with nothing to match), fall back to the generic
    dealer_contact_info reply instead of an unlabeled/confusing message."""
    replied = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return ["很抱歉，目前的資料無法明確對應到特定廠商，請提供更明確的條件（如廠牌、車型）以便查詢。"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    async def fake_record_interaction(*args, **kwargs):
        pass

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "_record_interaction", fake_record_interaction)

    _run(webhook._purchase_intent_ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "我要買車",
        sender_user_id="user-1",
    ))

    assert len(replied) == 1
    assert settings.dealer_contact_info in replied[0]


def test_purchase_intent_ask_question_error_is_marked_abnormal_not_no_match(monkeypatch):
    """A real ask_question failure (e.g. NotebookLM not bound) must not be
    indistinguishable from a legitimate "no vendor matched" case — it
    should still reply politely, but the tracking log needs to show 異常
    so admins can tell a broken lookup apart from a normal no-match."""
    replied = []
    recorded = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return ["⚠️ NotebookLM 尚未綁定，請聯繫管理員完成設定。"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    async def fake_record_interaction(channel_id, group_id, room_id, sender_user_id, access_token, question, answer, status, display_name=None):
        recorded.append(status)

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "_record_interaction", fake_record_interaction)

    _run(webhook._purchase_intent_ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "我要買 Corolla Cross",
        sender_user_id="user-1",
    ))

    assert len(replied) == 1
    assert settings.dealer_contact_info in replied[0]  # still a polite reply, not the raw ⚠️ text
    assert recorded == ["異常"]


def test_purchase_intent_no_match_case_keeps_normal_status(monkeypatch):
    """Contrast with the error case above: a genuine no-vendor-matched
    answer (no ⚠️, just no 【vendor】 header either) stays 需人工聯絡, not 異常."""
    recorded = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return ["很抱歉，目前的資料無法明確對應到特定廠商，請提供更明確的條件（如廠牌、車型）以便查詢。"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        pass

    async def fake_record_interaction(channel_id, group_id, room_id, sender_user_id, access_token, question, answer, status, display_name=None):
        recorded.append(status)

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "_record_interaction", fake_record_interaction)

    _run(webhook._purchase_intent_ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "我要買車",
        sender_user_id="user-1",
    ))

    assert recorded == ["需人工聯絡"]


def test_purchase_intent_falls_back_on_lookup_error(monkeypatch):
    replied = []
    recorded = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        raise RuntimeError("boom")

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    async def fake_record_interaction(channel_id, group_id, room_id, sender_user_id, access_token, question, answer, status, display_name=None):
        recorded.append(status)

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "_record_interaction", fake_record_interaction)

    _run(webhook._purchase_intent_ask_and_reply(
        "channel-1", "reply-token-1", "token-1", "我要買 Corolla Cross",
        sender_user_id="user-1",
    ))

    assert len(replied) == 1
    assert settings.dealer_contact_info in replied[0]
    assert recorded == ["異常"]


def test_ask_and_reply_delivers_answer_via_reply(monkeypatch):
    replied = []

    async def fake_ask_question(channel_id, question, line_user_id=None):
        return ["【SUM】\n\n答案內容"]

    async def fake_reply_text(reply_token, access_token, text, mention_user_id=None, mention_display_name=None):
        replied.append(text)

    async def fake_record_interaction(*args, **kwargs):
        pass

    monkeypatch.setattr(webhook, "ask_question", fake_ask_question)
    monkeypatch.setattr(webhook, "reply_text", fake_reply_text)
    monkeypatch.setattr(webhook, "_record_interaction", fake_record_interaction)

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
