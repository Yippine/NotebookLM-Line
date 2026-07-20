import asyncio
import hashlib
import hmac
import base64
import logging
import re
from datetime import datetime, timedelta, timezone
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

_BEIJING_TZ = timezone(timedelta(hours=8))

# The bot's display name in LINE (set separately in the LINE Official
# Account Manager — this constant only controls what @-mention text this
# code recognizes, it doesn't rename the account itself).
BOT_NAME = "小選"
_TEXT_MENTION_TRIGGERS = (f"@{BOT_NAME}", "@機器人")


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
    """Check if text explicitly @-mentions this bot by name — "@小選" or the
    generic "@機器人" — rather than matching any bare "@" character. Fallback
    path for desktop LINE clients that don't populate the structured
    mention payload reliably."""
    return any(trigger in text for trigger in _TEXT_MENTION_TRIGGERS)


def _strip_text_mention_trigger(text: str) -> str:
    """Remove a matched text-mention trigger (see _has_text_mention) from
    the message. Used when there's no structured mention payload to strip
    via index/length instead — otherwise the literal "@小選"/"@機器人" text
    would leak into the question sent to NotebookLM."""
    for trigger in _TEXT_MENTION_TRIGGERS:
        text = text.replace(trigger, "")
    return text.strip()


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
_RESTRICTED_TOPIC_REPLY = (
    "不好意思，像是授權、承諾或保證等問題，這邊沒辦法直接答覆您，"
    "還是要麻煩您與業務人員確認，才能給您最準確的說明 🙏"
)
_PURCHASE_INTENT_PREFIX = (
    "不好意思，購車、報價這類交易細節得由業務人員親自為您服務，"
    "沒辦法透過機器人直接回覆喔！"
)
_PURCHASE_INTENT_CONTACT_LEAD_IN = "您可以直接聯繫以下廠商，由專人為您服務："

_CONTACT_INFO_QUERY_TEMPLATE = (
    "使用者詢問以下車輛相關內容，請根據知識庫找出符合描述的廠商，"
    "只回覆該廠商的 con_info 聯絡資訊；不要提供價格、報價或任何交易相關內容：\n"
    "{question}"
)


def _purchase_intent_reply() -> str:
    """Generic fallback contact reply, used when no specific vendor can be matched."""
    return f"{_PURCHASE_INTENT_PREFIX}\n{settings.dealer_contact_info}"

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


async def _resolve_mention(
    source_type: str,
    sender_user_id: str | None,
    group_id: str | None,
    room_id: str | None,
    access_token: str,
) -> tuple[str | None, str | None]:
    """Resolve the asker's (user_id, display_name) so the reply can
    @mention them — only meaningful in group/room chats where several
    people may be asking around the same time, and only possible when LINE
    gave us a sender_user_id (it doesn't for group members who haven't
    friended the OA)."""
    if source_type not in ("group", "room") or not sender_user_id:
        return None, None
    display_name = await get_display_name(access_token, sender_user_id, group_id, room_id)
    return sender_user_id, display_name


async def _show_working_indicator(source_type: str, target_id: str, access_token: str, message: str) -> None:
    """Best-effort "working on it" feedback while we synchronously wait for
    the real reply.

    1:1 chats get LINE's native loading animation (show_loading), which
    doesn't touch the metered push quota. Group/room chats don't support
    that API at all, so they fall back to a throwaway push message instead
    — best-effort and silently ignored on failure, since losing this one
    to the push quota just means no interim feedback, not a lost answer
    (the actual answer always goes out via reply_text, never push).
    """
    if source_type == "user":
        await show_loading(target_id, access_token, seconds=30)
        return
    try:
        await push_text(target_id, access_token, message)
    except Exception as e:
        logger.warning(f"Best-effort working indicator failed (non-critical): {e}")


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
                mentions = _self_mentions(message)
                question = (
                    _strip_mentions(question, mentions) if mentions
                    else _strip_text_mention_trigger(question)
                )

            # Resolved once per message and threaded through every reply
            # below, so a busy group chat can see at a glance whose
            # question each answer belongs to. None/None in 1:1 chats or
            # when LINE didn't give us the sender's id.
            mention_user_id, mention_display_name = await _resolve_mention(
                source_type, sender_user_id, group_id, room_id, access_token
            )

            if source_type in ("group", "room") and not question:
                # No question text left after stripping the mention —
                # kept deliberately generic, no vendor name needed here.
                await reply_text(
                    reply_token, access_token, "請問想問什麼問題呢？",
                    mention_user_id, mention_display_name,
                )
                continue

            logger.info(f"[{channel_id}] ({source_type}) Q: {question[:50]}")

            if _is_update_kb_request(question):
                await reply_text(
                    reply_token, access_token, "請上傳更新後的資料",
                    mention_user_id, mention_display_name,
                )
                continue

            restricted_topic = classify_restricted_topic(question)
            if restricted_topic == "authorization_promise":
                await reply_text(
                    reply_token, access_token, _RESTRICTED_TOPIC_REPLY,
                    mention_user_id, mention_display_name,
                )
                await _record_interaction(
                    channel_id, group_id, room_id, sender_user_id, access_token,
                    question, _RESTRICTED_TOPIC_REPLY, "需人工聯絡",
                    display_name=mention_display_name,
                )
                continue
            if restricted_topic == "purchase":
                await _show_working_indicator(source_type, target_id, access_token, "⏳正在查詢中，請稍後")

                await _purchase_intent_ask_and_reply(
                    channel_id, reply_token, access_token, question,
                    sender_user_id, group_id, room_id,
                    mention_user_id, mention_display_name,
                )
                continue

            await _show_working_indicator(source_type, target_id, access_token, "⏳正在查詢中，請稍後")

            await _ask_and_reply(
                channel_id, reply_token, access_token, question,
                sender_user_id, group_id, room_id,
                mention_user_id, mention_display_name,
            )
            continue

        if message_type == "file":
            file_name = message.get("fileName") or "uploaded_file"
            file_id = message.get("id")

            await _show_working_indicator(source_type, target_id, access_token, "⏳正在更新知識庫，請稍後")

            await _upload_and_reply(
                channel_id, reply_token, access_token, file_id, file_name,
            )

    return {"status": "ok"}


async def _ask_and_reply(
    channel_id: str,
    reply_token: str,
    access_token: str,
    question: str,
    sender_user_id: str | None = None,
    group_id: str | None = None,
    room_id: str | None = None,
    mention_user_id: str | None = None,
    mention_display_name: str | None = None,
):
    """Answer a question and deliver it via the reply token (not push) —
    reply messages aren't metered against the account's monthly LINE
    message quota the way push messages are. This blocks the webhook
    handler for as long as the NotebookLM query takes, so very slow
    queries risk the reply token expiring; that's the accepted tradeoff
    for not depending on push."""
    status = "已完成"
    answer_text = ""
    try:
        messages = await ask_question(channel_id, question, sender_user_id)
        if not messages:
            messages = ["⚠️ 沒有取得回覆內容，請稍後再試。"]
        answer_text = "\n".join(messages)
        if any(msg.startswith("⚠️") for msg in messages):
            status = "異常"
        await reply_text(reply_token, access_token, messages, mention_user_id, mention_display_name)
    except Exception as e:
        logger.error(f"[{channel_id}] Error: {e}")
        status = "異常"
        answer_text = f"系統發生錯誤：{e}"
        try:
            await reply_text(
                reply_token, access_token, "⚠️ 系統發生錯誤，請稍後再試。",
                mention_user_id, mention_display_name,
            )
        except Exception as reply_error:
            logger.error(f"[{channel_id}] Fallback reply also failed: {reply_error}")

    await _record_interaction(
        channel_id, group_id, room_id, sender_user_id, access_token,
        question, answer_text, status, display_name=mention_display_name,
    )


async def _purchase_intent_ask_and_reply(
    channel_id: str,
    reply_token: str,
    access_token: str,
    question: str,
    sender_user_id: str | None,
    group_id: str | None = None,
    room_id: str | None = None,
    mention_user_id: str | None = None,
    mention_display_name: str | None = None,
):
    """Handle a purchase-intent question: refuse to discuss the transaction
    itself, but still look up which vendor's car matches and hand back that
    vendor's contact info (con_info) instead of one hardcoded phone number.

    The lookup query is a standalone NotebookLM question (not tied to the
    user's own conversation thread), so this meta-instruction doesn't leak
    into their ongoing chat history. Delivered via reply (not push) for the
    same quota reason as ``_ask_and_reply``.
    """
    reply = _purchase_intent_reply()
    status = "需人工聯絡"
    try:
        contact_query = _CONTACT_INFO_QUERY_TEMPLATE.format(question=question)
        messages = await ask_question(channel_id, contact_query, line_user_id=None)
        if any(m.startswith("⚠️") for m in messages):
            # A real ask_question failure (unbound notebook, API error, etc.)
            # — not "no vendor matched". Keep the polite generic reply for
            # the user, but flag it so it doesn't look identical to a
            # legitimate no-match case in the tracking log.
            logger.error(f"[{channel_id}] Contact info lookup returned an error: {messages}")
            status = "異常"
            await reply_text(reply_token, access_token, reply, mention_user_id, mention_display_name)
        else:
            vendor_messages = [m for m in messages if m.startswith("【")]
            if vendor_messages:
                intro = f"{_PURCHASE_INTENT_PREFIX}\n\n{_PURCHASE_INTENT_CONTACT_LEAD_IN}"
                reply_messages = [intro] + vendor_messages
                await reply_text(reply_token, access_token, reply_messages, mention_user_id, mention_display_name)
                reply = "\n".join(reply_messages)
            else:
                await reply_text(reply_token, access_token, reply, mention_user_id, mention_display_name)
    except Exception as e:
        logger.error(f"[{channel_id}] Error looking up vendor contact info: {e}")
        status = "異常"
        try:
            await reply_text(reply_token, access_token, reply, mention_user_id, mention_display_name)
        except Exception as reply_error:
            logger.error(f"[{channel_id}] Fallback reply also failed: {reply_error}")

    await _record_interaction(
        channel_id, group_id, room_id, sender_user_id, access_token,
        question, reply, status, display_name=mention_display_name,
    )


async def _record_interaction(
    channel_id: str,
    group_id: str | None,
    room_id: str | None,
    sender_user_id: str | None,
    access_token: str,
    question: str,
    answer: str,
    status: str,
    display_name: str | None = None,
):
    """Log a Q&A as a text record in the user's Drive folder plus a row in
    the tracking Sheet. Best-effort: failures here never affect the LINE
    conversation, only get logged server-side.

    ``display_name`` can be passed in already-resolved (e.g. group/room
    chats already looked it up to build the reply's @mention) to skip a
    redundant profile lookup; 1:1 chats don't resolve it beforehand, so
    this fetches it here instead.

    Awaited by the caller (not fire-and-forget) so it's still part of the
    in-flight webhook request — a `docker compose up -d` restart (e.g. from
    the tunnel watchdog swapping tunnels) sends SIGTERM and gives in-flight
    requests a grace period to finish; an orphaned background task isn't
    covered by that and would just get killed mid-write, silently dropping
    the record.
    """
    if not sender_user_id:
        return  # can't build a per-user folder without a stable identity

    try:
        if display_name is None:
            display_name = await get_display_name(access_token, sender_user_id, group_id, room_id)
        folder_id, folder_link = await google_log_service.get_or_create_user_folder(
            channel_id, sender_user_id, display_name
        )
        now = datetime.now(_BEIJING_TZ)
        # Independent of each other — both only need folder_id/folder_link
        # from the step above, neither depends on the other's result — so
        # run them concurrently instead of paying two sequential Google API
        # round trips after the user has already gotten their reply.
        # return_exceptions=True so a failure in one still lets the other
        # finish before this coroutine returns — with the default
        # return_exceptions=False, gather() raises as soon as the first
        # awaitable fails and leaves the other running as an orphaned task,
        # exactly the "gets killed mid-write on SIGTERM" risk this function
        # is documented to avoid.
        results = await asyncio.gather(
            google_log_service.save_text_record(folder_id, question, answer, now),
            google_log_service.append_sheet_row(now, display_name, folder_link, status),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
    except Exception as e:
        logger.error(f"[{channel_id}] Failed to record interaction to Google: {e}")


async def _upload_and_reply(
    channel_id: str,
    reply_token: str,
    access_token: str,
    file_id: str | None,
    file_name: str,
):
    """Update the knowledge base and confirm via reply (not push) — same
    quota reasoning as ``_ask_and_reply``."""
    try:
        if not file_id:
            raise ValueError("缺少 LINE 附件 ID")

        file_bytes = await download_content(file_id, access_token)
        message = await update_knowledge_base(channel_id, file_bytes, file_name)
        await reply_text(reply_token, access_token, message)
    except Exception as e:
        logger.error(f"[{channel_id}] Upload error: {e}")
        try:
            await reply_text(reply_token, access_token, f"⚠️ 更新知識庫失敗：{e}")
        except Exception as reply_error:
            logger.error(f"[{channel_id}] Fallback reply also failed: {reply_error}")