import hashlib
import hmac
import base64
import asyncio
import logging
from fastapi import APIRouter, Request, HTTPException
import aiosqlite
from database import DB
from services.nlm_service import ask_question
from services.line_service import show_loading, push_text

router = APIRouter(tags=["webhook"])
logger = logging.getLogger(__name__)


def verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    hash_val = hmac.HMAC(
        channel_secret.encode(), body, hashlib.sha256
    ).digest()
    return hmac.compare_digest(base64.b64encode(hash_val).decode(), signature)


@router.post("/webhook/{channel_id}")
async def webhook(channel_id: str, request: Request):
    body = await request.body()

    # Look up channel
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT channel_secret, channel_access_token FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        channel = await cur.fetchone()

    if not channel:
        raise HTTPException(404, "Channel not found")

    # Verify LINE signature
    signature = request.headers.get("x-line-signature", "")
    if not verify_signature(body, signature, channel["channel_secret"]):
        raise HTTPException(403, "Invalid signature")

    access_token = channel["channel_access_token"]

    # Parse events
    import json
    payload = json.loads(body)

    for event in payload.get("events", []):
        if event.get("type") != "message" or event["message"].get("type") != "text":
            continue

        question = event["message"]["text"]
        reply_token = event["replyToken"]
        user_id = event["source"]["userId"]

        logger.info(f"[{channel_id}] Q: {question[:50]}")

        # Show typing animation instead of text reply
        await show_loading(user_id, access_token, seconds=30)

        # Background task: ask NLM and push result
        asyncio.create_task(
            _ask_and_push(channel_id, user_id, access_token, question)
        )

    return {"status": "ok"}


async def _ask_and_push(channel_id: str, user_id: str, access_token: str, question: str):
    try:
        messages = await ask_question(channel_id, question)
        for msg in messages:
            await push_text(user_id, access_token, msg)
    except Exception as e:
        logger.error(f"[{channel_id}] Error: {e}")
        await push_text(user_id, access_token, "⚠️ 系統發生錯誤，請稍後再試。")
