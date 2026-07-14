import hashlib
import hmac
import base64
import asyncio
import logging
import re
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException
import aiosqlite
from config import settings
from database import DB
from services import google_log_service
from services.nlm_service import ask_question, update_knowledge_base
from services.line_service import (
    show_loading,
    reply_text,
    push_text,
    download_content,
    get_display_name,
)

router = APIRouter(tags=["webhook"])
logger = logging.getLogger(__name__)


def verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    hash_val = hmac.HMAC(
        channel_secret.encode(), body, hashlib.sha256
    ).digest()
    return hmac.compare_digest(base64.b64encode(hash_val).decode(), signature)


def _self_mentions(message: dict) -> list[dict]:
    """Return the list of @mentions in this message that point at this bot account.

    Some LINE desktop clients do not populate ``isSelf`` reliably for mentions,
    so we fall back to treating any mention payload as a valid mention when the
    message is from a group/room chat.
    """
    mentionees = message.get("mention", {}).get("mentionees", [])
    if not mentionees:
        return []

    self_mentions = [m for m in mentionees if m.get("isSelf")]
    if self_mentions:
        return self_mentions

    return mentionees


def _has_text_mention(text: str) -> bool:
    """Check if text contains @-mention syntax (fallback for desktop LINE)."""
    return "@" in text


_UPDATE_KB_KEYWORDS = ("更新知識庫", "更新資料庫", "更新資料")


def _is_update_kb_request(text: str) -> bool:
    """Check if the (mention-stripped) text is asking to update the knowledge base."""
    return any(keyword in text for keyword in _UPDATE_KB_KEYWORDS)


_PURCHASE_KEYWORDS = (
    "交易", "買賣", "買", "賣", "購買", "出售", "成交", "待售", "簽約", "合約", "訂單", "報價", "議價", "付款", "下單",
    # price / discount phrasing that doesn't contain the words above
    "多少錢", "多少費用", "折價", "折扣", "促銷", "團購", "收購", "加價", "優惠",
    # payment / money-transfer phrasing
    "定金", "匯款", "收款", "轉帳", "儲值", "尾款", "續約", "現金", "發票", "統編",
    "信用卡", "加密貨幣", "比特幣", "扣款", "價目表", "選配金",
)
_AUTHORIZATION_PROMISE_KEYWORDS = (
    "授權", "承諾", "保證",
    # promise/permission synonyms
    "答應", "准許", "商標",
    # commercial-use / authorization phrasing without the word "授權"
    "商業廣告", "協力廠商", "推薦指定",
)
_RESTRICTED_TOPIC_REPLY = "本機器人僅供諮詢用途，不可以回答有關交易、買賣、授權、承諾等事宜。"


def _purchase_intent_reply() -> str:
    return (
        "本機器人僅供諮詢用途，不可以回答交易、報價等購車相關事宜。\n"
        f"{settings.dealer_contact_info}"
    )

# Phrases that look restricted by keyword alone but are actually plain info
# questions (e.g. "does this authorized service center exist"), not requests
# for a transaction/authorization/promise. Stripped before keyword matching
# so the rest of the message is still checked normally.
_RESTRICTED_TOPIC_EXEMPT_PATTERNS = (
    re.compile(r"授權的?(展示中心|保養廠|維修廠|服務廠)"),
    re.compile(r"申請購買.{0,4}保固|購買.{0,4}延長保固"),
    # "我要買 X，請給我這款車型的資訊" — buy/sell used only as framing before a
    # plain info request. Only strips this clause; a genuinely transactional
    # tail elsewhere in the message (price, payment, etc.) still blocks.
    re.compile(r"(買|賣)\S{0,14}[,，].{0,12}(資訊|規格|資料|介紹|說明)"),
)


def _strip_exempt_phrases(text: str) -> str:
    for pattern in _RESTRICTED_TOPIC_EXEMPT_PATTERNS:
        text = pattern.sub("", text)
    return text


def _is_purchase_intent(text: str) -> bool:
    """Check if the question touches on buying/selling a car (routes to dealer contact)."""
    text = _strip_exempt_phrases(text)
    return any(keyword in text for keyword in _PURCHASE_KEYWORDS)


def _is_authorization_or_promise(text: str) -> bool:
    """Check if the question touches on authorization, trademarks, or promises."""
    text = _strip_exempt_phrases(text)
    return any(keyword in text for keyword in _AUTHORIZATION_PROMISE_KEYWORDS)


def classify_restricted_topic(text: str) -> str | None:
    """Classify a question as a restricted topic, if any.

    Returns "authorization_promise", "purchase", or None. Checked in this
    order because a message that both promises/guarantees *and* mentions
    buying in passing (e.g. "if I buy this, can you promise...") is
    fundamentally a promise question, not a purchase inquiry — it should get
    the generic refusal, not the dealer-contact reply.
    """
    if _is_authorization_or_promise(text):
        return "authorization_promise"
    if _is_purchase_intent(text):
        return "purchase"
    return None


def _is_restricted_topic(text: str) -> bool:
    """Check if the question touches on transactions, sales, authorization, or promises."""
    return classify_restricted_topic(text) is not None


def _strip_mentions(text: str, mentionees: list[dict]) -> str:
    """Remove the @mention substrings (e.g. '@BotName') from the message text."""
    for m in sorted(mentionees, key=lambda m: m["index"], reverse=True):
        start, length = m["index"], m["length"]
        text = text[:start] + text[start + length:]
    return text.strip()


@router.post("/webhook/{channel_id}")
async def webhook(channel_id: str, request: Request):
    body = await request.body()

    # Look up channel
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT channel_secret, channel_access_token, expires_at FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        channel = await cur.fetchone()

    if not channel:
        raise HTTPException(404, "Channel not found")

    # Check expiry
    if channel["expires_at"]:
        from datetime import datetime, timezone
        if channel["expires_at"] < datetime.now(timezone.utc).isoformat():
            return {"status": "expired"}

    # Verify LINE signature
    signature = request.headers.get("x-line-signature", "")
    if not verify_signature(body, signature, channel["channel_secret"]):
        raise HTTPException(403, "Invalid signature")

    access_token = channel["channel_access_token"]

    # Parse events
    import json
    payload = json.loads(body)

    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue

        message = event["message"]
        reply_token = event["replyToken"]
        source = event["source"]
        source_type = source.get("type")  # "user" | "group" | "room"
        message_type = message.get("type")
        logger.info(f"[{channel_id}] Event payload: {event}")

        # In group/room chats, only respond to TEXT messages when the bot is
        # explicitly @mentioned. 1:1 chats don't need a mention. File messages
        # can never carry an @mention on LINE, so this gate doesn't apply to
        # them — any file sent in a group/room is treated as an update request.
        if source_type in ("group", "room") and message_type == "text":
            mentions = _self_mentions(message)
            message_text = message.get("text", "")
            has_text_mention = _has_text_mention(message_text)
            logger.info(f"[{channel_id}] Parsed mentions for group/room: payload={mentions}, text_has_@={has_text_mention}")
            # Accept if either mention payload exists OR text contains @-mention syntax (fallback for desktop)
            if not mentions and not has_text_mention:
                logger.info(f"[{channel_id}] Ignored group/room message without mention or @-syntax: {message}")
                continue

        # Target for the push reply: group/room chat if present, otherwise the user.
        target_id = source.get("groupId") or source.get("roomId") or source.get("userId")
        if not target_id:
            # Anonymous group member (hasn't friended the OA) with no usable target — skip.
            logger.info(f"[{channel_id}] Skipped: no usable target id in source={source}")
            continue

        # The actual sender, distinct from target_id in group/room chats —
        # used to keep each person's own NotebookLM conversation thread
        # separate. LINE omits this for group members who haven't friended
        # the OA; those questions are still answered, just without memory.
        sender_user_id = source.get("userId")
        group_id = source.get("groupId")
        room_id = source.get("roomId")

        if message_type == "text":
            question = message["text"]
            if source_type in ("group", "room"):
                question = _strip_mentions(question, _self_mentions(message))
                if not question:
                    await reply_text(reply_token, access_token, "請問想問什麼問題呢？")
                    continue

            logger.info(f"[{channel_id}] ({source_type}) Q: {question[:50]}")

            if _is_update_kb_request(question):
                await reply_text(reply_token, access_token, "請上傳更新後的資料")
                continue

            restricted_topic = classify_restricted_topic(question)
            if restricted_topic == "authorization_promise":
                await reply_text(reply_token, access_token, _RESTRICTED_TOPIC_REPLY)
                asyncio.create_task(_record_interaction(
                    channel_id, group_id, room_id, sender_user_id, access_token,
                    question, _RESTRICTED_TOPIC_REPLY, "需人工聯絡",
                ))
                continue
            if restricted_topic == "purchase":
                reply = _purchase_intent_reply()
                await reply_text(reply_token, access_token, reply)
                asyncio.create_task(_record_interaction(
                    channel_id, group_id, room_id, sender_user_id, access_token,
                    question, reply, "需人工聯絡",
                ))
                continue

            if source_type == "user":
                loading_ok = await show_loading(target_id, access_token, seconds=30)
            else:
                loading_ok = False
            if not loading_ok:
                await reply_text(reply_token, access_token, "⏳正在查詢中，請稍後")

            asyncio.create_task(
                _ask_and_push(
                    channel_id, target_id, access_token, question,
                    sender_user_id, group_id, room_id,
                )
            )
            continue

        if message_type == "file":
            file_name = message.get("fileName") or "uploaded_file"
            file_id = message.get("id")

            if source_type == "user":
                loading_ok = await show_loading(target_id, access_token, seconds=30)
            else:
                loading_ok = False
            if not loading_ok:
                await reply_text(reply_token, access_token, "⏳正在更新知識庫，請稍後")

            asyncio.create_task(
                _upload_and_push(
                    channel_id,
                    target_id,
                    access_token,
                    file_id,
                    file_name,
                )
            )

    return {"status": "ok"}


async def _ask_and_push(
    channel_id: str,
    target_id: str,
    access_token: str,
    question: str,
    sender_user_id: str | None = None,
    group_id: str | None = None,
    room_id: str | None = None,
):
    status = "已完成"
    answer_text = ""
    try:
        messages = await ask_question(channel_id, question, sender_user_id)
        answer_text = "\n".join(messages)
        if any(msg.startswith("⚠️") for msg in messages):
            status = "異常"
        for msg in messages:
            await push_text(target_id, access_token, msg)
    except Exception as e:
        logger.error(f"[{channel_id}] Error: {e}")
        status = "異常"
        answer_text = f"系統發生錯誤：{e}"
        await push_text(target_id, access_token, "⚠️ 系統發生錯誤，請稍後再試。")

    asyncio.create_task(_record_interaction(
        channel_id, group_id, room_id, sender_user_id, access_token,
        question, answer_text, status,
    ))


async def _record_interaction(
    channel_id: str,
    group_id: str | None,
    room_id: str | None,
    sender_user_id: str | None,
    access_token: str,
    question: str,
    answer: str,
    status: str,
):
    """Log a Q&A as a text record in the user's Drive folder plus a row in
    the tracking Sheet, ~2s after the reply is sent. Best-effort: failures
    here never affect the LINE conversation, only get logged server-side."""
    if not sender_user_id:
        return  # can't build a per-user folder without a stable identity

    await asyncio.sleep(2)
    try:
        display_name = await get_display_name(access_token, sender_user_id, group_id, room_id)
        folder_id, folder_link = await google_log_service.get_or_create_user_folder(
            channel_id, sender_user_id, display_name
        )
        now = datetime.now()
        await google_log_service.save_text_record(folder_id, question, answer, now)
        await google_log_service.append_sheet_row(now, display_name, folder_link, status)
    except Exception as e:
        logger.error(f"[{channel_id}] Failed to record interaction to Google: {e}")


async def _upload_and_push(
    channel_id: str,
    target_id: str,
    access_token: str,
    file_id: str | None,
    file_name: str,
):
    try:
        if not file_id:
            raise ValueError("缺少 LINE 附件 ID")

        file_bytes = await download_content(file_id, access_token)
        message = await update_knowledge_base(channel_id, file_bytes, file_name)
        await push_text(target_id, access_token, message)
    except Exception as e:
        logger.error(f"[{channel_id}] Upload error: {e}")
        await push_text(target_id, access_token, f"⚠️ 更新知識庫失敗：{e}")