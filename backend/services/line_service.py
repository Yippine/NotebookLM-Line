import httpx

LINE_API = "https://api.line.me/v2/bot"


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _chunk_messages(text: str) -> list[dict]:
    """Split long text into LINE messages (max 5000 chars each, max 5 messages)."""
    chunks = [text[i : i + 5000] for i in range(0, len(text), 5000)]
    return [{"type": "text", "text": c} for c in chunks[:5]]


async def show_loading(user_id: str, access_token: str, seconds: int = 20):
    """Show LINE official loading animation (typing indicator)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.line.me/v2/bot/chat/loading",
            headers=_headers(access_token),
            json={"chatId": user_id, "loadingSeconds": min(seconds, 60)},
        )
        if resp.status_code != 200:
            import logging
            logging.getLogger(__name__).warning(f"Loading animation failed: {resp.status_code} {resp.text}")


async def reply_text(reply_token: str, access_token: str, text: str):
    """Reply to LINE user. Auto-chunk long text."""
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{LINE_API}/message/reply",
            headers=_headers(access_token),
            json={"replyToken": reply_token, "messages": _chunk_messages(text)},
        )


async def push_text(user_id: str, access_token: str, text: str):
    """Push message to LINE user (for async results)."""
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{LINE_API}/message/push",
            headers=_headers(access_token),
            json={"to": user_id, "messages": _chunk_messages(text)},
        )
