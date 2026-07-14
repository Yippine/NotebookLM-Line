import httpx

LINE_API = "https://api.line.me/v2/bot"
LINE_CONTENT_API = "https://api-data.line.me/v2/bot/message"


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _chunk_messages(text: str) -> list[dict]:
    """Split long text into LINE messages (max 5000 chars each, max 5 messages)."""
    chunks = [text[i : i + 5000] for i in range(0, len(text), 5000)]
    return [{"type": "text", "text": c} for c in chunks[:5]]


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
