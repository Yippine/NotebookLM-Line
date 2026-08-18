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


async def send_message(webhook_url: str, text: str) -> None:
    """透過 Google Chat 的 Incoming Webhook 送出一則純文字訊息。

    這是獨立於 LINE 之外的告警管道，不佔用、也不受 LINE 官方帳號的
    每月訊息額度影響——純粹是對 Google 提供的 webhook URL 發一個
    HTTP POST。任何非 2xx 的回應都會被拋出，讓呼叫端（alert_service）
    的錯誤處理能真正看見這個失敗，而不是悄悄被吞掉。
    """
    client = _get_client()
    resp = await client.post(webhook_url, json={"text": text})
    if resp.status_code != 200:
        logger.error(f"Google Chat webhook failed: {resp.status_code} {resp.text}")
        raise RuntimeError(f"Google Chat webhook 失敗：{resp.status_code} {resp.text}")
