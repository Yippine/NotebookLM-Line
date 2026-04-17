import httpx

LINE_API = "https://api.line.me/v2/bot"


async def reply_text(reply_token: str, access_token: str, text: str):
    """Reply to LINE user. Auto-chunk long text."""
    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
    messages = [{"type": "text", "text": c} for c in chunks[:5]]

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{LINE_API}/message/reply",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"replyToken": reply_token, "messages": messages},
        )


async def push_text(user_id: str, access_token: str, text: str):
    """Push message to LINE user (for async results)."""
    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
    messages = [{"type": "text", "text": c} for c in chunks[:5]]

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{LINE_API}/message/push",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"to": user_id, "messages": messages},
        )
