import logging

import httpx

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """共用、延遲建立的客戶端，做法比照 line_service._get_client。"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


async def aclose_client() -> None:
    """關閉共用的客戶端。應在應用程式關閉時呼叫。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def send_message(bot_token: str, chat_id: str, text: str) -> None:
    """透過 Telegram Bot API 送出一則純文字訊息。

    這是另一條獨立於 LINE、Google Chat 之外的告警管道——純粹是對
    Telegram 的 Bot API 發一個 HTTP POST。個人帳號就能經由 BotFather
    申請 bot token，不像 Google Chat 的 Incoming Webhook 管理功能
    只開放給 Workspace 帳號；也完全不佔用、不受 LINE 官方帳號每月
    訊息額度影響。任何非 2xx 的回應都會被拋出，讓呼叫端
    （alert_service）的錯誤處理能真正看見這個失敗，而不是悄悄被吞掉。
    """
    client = _get_client()
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = await client.post(url, json={"chat_id": chat_id, "text": text})
    if resp.status_code != 200:
        logger.error(f"Telegram sendMessage failed: {resp.status_code} {resp.text}")
        raise RuntimeError(f"Telegram sendMessage 失敗：{resp.status_code} {resp.text}")
