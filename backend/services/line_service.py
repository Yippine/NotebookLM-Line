import asyncio
import logging

import httpx

LINE_API = "https://api.line.me/v2/bot"
LINE_CONTENT_API = "https://api-data.line.me/v2/bot/message"

logger = logging.getLogger(__name__)


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _chunk_messages(text: str) -> list[dict]:
    """Split long text into LINE messages (max 5000 chars each, max 5 messages)."""
    chunks = [text[i : i + 5000] for i in range(0, len(text), 5000)]
    return [{"type": "text", "text": c} for c in chunks[:5]]


def _build_messages(texts: list[str]) -> list[dict]:
    """Turn one or more separate message texts (e.g. one per vendor) into
    LINE message objects, chunking each for the length limit and capping
    the total at LINE's 5-messages-per-call limit."""
    messages: list[dict] = []
    for text in texts:
        messages.extend(_chunk_messages(text))
    return messages[:5]


async def show_loading(user_id: str, access_token: str, seconds: int = 20) -> bool:
    """Show LINE loading animation. Returns True if successful."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.line.me/v2/bot/chat/loading",
            headers=_headers(access_token),
            json={"chatId": user_id, "loadingSeconds": min(seconds, 60)},
        )
        if resp.status_code != 200:
            import logging
            logging.getLogger(__name__).warning(f"Loading animation failed: {resp.status_code} {resp.text}")
            return False
        return True


async def reply_text(reply_token: str, access_token: str, text: str | list[str]):
    """Reply to LINE user with one text, or a list of separate messages
    (e.g. one per vendor) — auto-chunked per LINE's length/count limits.

    Unlike push, reply messages aren't metered against the account's
    monthly message quota, so this is the preferred delivery path. Still
    checks the response so a failure (bad/expired reply token, etc.) is
    logged and raised instead of silently swallowed.
    """
    texts = text if isinstance(text, list) else [text]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{LINE_API}/message/reply",
            headers=_headers(access_token),
            json={"replyToken": reply_token, "messages": _build_messages(texts)},
        )
        if resp.status_code != 200:
            logger.error(f"LINE reply failed: {resp.status_code} {resp.text}")
            raise RuntimeError(f"LINE reply 失敗：{resp.status_code} {resp.text}")


async def push_text(user_id: str, access_token: str, text: str):
    """Push message to LINE user (for async results).

    Unlike ``reply_text`` this is the *only* delivery path for
    background-task results (there's no reply token by the time these
    fire), so a swallowed failure here means the user silently never gets
    an answer. A 429 is retried once after honoring ``Retry-After``; any
    other non-2xx (or a still-429 after retry) is raised so callers' error
    handling — and the interaction log — actually see it instead of nothing
    happening.
    """
    async with httpx.AsyncClient() as client:
        payload = {"to": user_id, "messages": _chunk_messages(text)}
        resp = await client.post(f"{LINE_API}/message/push", headers=_headers(access_token), json=payload)

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 1))
            logger.warning(f"LINE push rate-limited (429), retrying after {retry_after}s")
            await asyncio.sleep(retry_after)
            resp = await client.post(f"{LINE_API}/message/push", headers=_headers(access_token), json=payload)

        if resp.status_code != 200:
            logger.error(f"LINE push failed: {resp.status_code} {resp.text}")
            raise RuntimeError(f"LINE push 失敗：{resp.status_code} {resp.text}")


async def download_content(message_id: str, access_token: str) -> bytes:
    """Download LINE message content bytes for file or image attachments."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{LINE_CONTENT_API}/{message_id}/content",
            headers=_headers(access_token),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"下載 LINE 附件失敗：{resp.status_code} {resp.text}")
        return resp.content


async def get_display_name(
    access_token: str,
    user_id: str,
    group_id: str | None = None,
    room_id: str | None = None,
) -> str:
    """Look up a user's display name. Falls back to their raw userId if the
    profile lookup fails (e.g. they haven't friended the OA)."""
    if group_id:
        url = f"{LINE_API}/group/{group_id}/member/{user_id}"
    elif room_id:
        url = f"{LINE_API}/room/{room_id}/member/{user_id}"
    else:
        url = f"{LINE_API}/profile/{user_id}"

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=_headers(access_token))
        if resp.status_code != 200:
            return user_id
        return resp.json().get("displayName", user_id)
