import httpx
import uuid

LINE_API = "https://api.line.me/v2/bot"


def _headers(access_token: str, retry_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    if retry_key:
        try:
            headers["X-Line-Retry-Key"] = str(uuid.UUID(retry_key))
        except (ValueError, AttributeError) as error:
            raise ValueError("LINE retry key must be a UUID") from error
    return headers


def _chunk_messages(text: str) -> list[dict]:
    """Split long text into LINE messages (max 5000 chars each, max 5 messages)."""
    chunks = [text[i : i + 5000] for i in range(0, len(text), 5000)]
    return [{"type": "text", "text": c} for c in chunks[:5]]


async def show_loading(user_id: str, access_token: str, seconds: int = 20) -> bool:
    """Show LINE loading animation. Returns True if successful."""
    normalized_seconds = max(5, min(60, int(seconds) // 5 * 5))
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.line.me/v2/bot/chat/loading/start",
            headers=_headers(access_token),
            json={"chatId": user_id, "loadingSeconds": normalized_seconds},
        )
        if resp.status_code != 202:
            import logging

            logging.getLogger(__name__).warning(
                "Loading animation failed: status=%s", resp.status_code
            )
            return False
        return True


async def reply_text(reply_token: str, access_token: str, text: str):
    """Reply to LINE user. Auto-chunk long text."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{LINE_API}/message/reply",
            headers=_headers(access_token),
            json={"replyToken": reply_token, "messages": _chunk_messages(text)},
        )
        response.raise_for_status()


async def push_text(
    user_id: str,
    access_token: str,
    text: str,
    *,
    retry_key: str | None = None,
):
    """Push message to LINE user (for async results)."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{LINE_API}/message/push",
            headers=_headers(access_token, retry_key),
            json={"to": user_id, "messages": _chunk_messages(text)},
        )
        if (
            retry_key
            and response.status_code == 409
            and response.headers.get("x-line-accepted-request-id")
        ):
            return
        response.raise_for_status()
