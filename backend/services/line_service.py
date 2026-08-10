import asyncio
import logging

import httpx

LINE_API = "https://api.line.me/v2/bot"
LINE_CONTENT_API = "https://api-data.line.me/v2/bot/message"

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """共用、延遲建立的客戶端，讓多次呼叫（單一請求常常會發出
    2-3 次：一個處理中指示、一次回覆、一次顯示名稱查詢）能重複
    使用連線池，而不是每次都要重新做一次 TLS 交握。之所以延遲
    建立，是因為它必須在一個正在執行的事件迴圈內建立，而不是在
    模組匯入時就建立。"""
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


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _chunk_messages(text: str) -> list[dict]:
    """將長文字切分成多則 LINE 訊息（每則最多 5000 字元，最多 5 則）。"""
    chunks = [text[i : i + 5000] for i in range(0, len(text), 5000)]
    return [{"type": "text", "text": c} for c in chunks[:5]]


def _build_messages(texts: list[str]) -> list[dict]:
    """將一則或多則各自獨立的訊息文字（例如每個廠商各一則）轉換成
    LINE 的訊息物件，各自依長度限制切分，並將總數上限控制在
    LINE 每次呼叫 5 則訊息的限制內。"""
    messages: list[dict] = []
    for text in texts:
        messages.extend(_chunk_messages(text))
    return messages[:5]


async def show_loading(user_id: str, access_token: str, seconds: int = 20) -> bool:
    """顯示 LINE 的載入動畫。成功則回傳 True。"""
    client = _get_client()
    resp = await client.post(
        "https://api.line.me/v2/bot/chat/loading",
        headers=_headers(access_token),
        json={"chatId": user_id, "loadingSeconds": min(seconds, 60)},
    )
    if resp.status_code != 200:
        logger.warning(f"Loading animation failed: {resp.status_code} {resp.text}")
        return False
    return True


def _prepend_mention(message: dict, user_id: str, display_name: str) -> None:
    """修改一則文字訊息，讓它以對指定使用者真正的 LINE @mention
    開頭（而不只是字面上的 "@name" 文字）——用來讓忙碌的群組聊天
    中的回覆，能明顯標示出這個回答是在回應誰的問題。"""
    prefix = f"@{display_name} "
    message["text"] = prefix + message["text"]
    message["mention"] = {
        "mentionees": [
            {"index": 0, "length": len(prefix) - 1, "type": "user", "userId": user_id}
        ]
    }


async def reply_text(
    reply_token: str,
    access_token: str,
    text: str | list[str],
    mention_user_id: str | None = None,
    mention_display_name: str | None = None,
):
    """以一則文字，或一組各自獨立的訊息（例如每個廠商各一則）
    回覆 LINE 使用者——依 LINE 的長度／則數限制自動切分。

    當同時提供 ``mention_user_id`` 與 ``mention_display_name`` 時，
    第一則訊息會加上對該使用者的 @mention 前綴（見
    ``_prepend_mention``）——用於群組/聊天室中，讓人一看就知道
    這則回覆是在回答誰的問題。

    與 push 不同，reply 訊息不會被計入帳號的每月訊息額度，
    因此是優先採用的送出方式。這裡仍會檢查回應內容，讓失敗
    （例如 reply token 無效或過期等）被記錄並拋出，而不是
    悄悄被吞掉。
    """
    texts = text if isinstance(text, list) else [text]
    client = _get_client()
    messages = _build_messages(texts)
    if mention_user_id and mention_display_name and messages:
        _prepend_mention(messages[0], mention_user_id, mention_display_name)
    resp = await client.post(
        f"{LINE_API}/message/reply",
        headers=_headers(access_token),
        json={"replyToken": reply_token, "messages": messages},
    )
    if resp.status_code != 200:
        logger.error(f"LINE reply failed: {resp.status_code} {resp.text}")
        raise RuntimeError(f"LINE reply 失敗：{resp.status_code} {resp.text}")


async def push_text(user_id: str, access_token: str, text: str):
    """推送訊息給 LINE 使用者（用於非同步結果）。

    與 ``reply_text`` 不同，這是背景任務結果*唯一*的送出方式
    （這些任務觸發時已經沒有 reply token 可用了），所以如果這裡
    的失敗被吞掉，使用者就會悄悄地永遠收不到答案。收到 429 時，
    會依照 ``Retry-After`` 等待後重試一次；其他任何非 2xx 的
    回應（或重試後仍是 429）都會被拋出，讓呼叫端的錯誤處理——
    以及互動紀錄——能真正看見這個失敗，而不是什麼事都沒發生。
    """
    client = _get_client()
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
    """下載檔案或圖片附件的 LINE 訊息內容位元組。"""
    client = _get_client()
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
    """查詢使用者的顯示名稱。若個人資料查詢失敗（例如對方尚未
    加官方帳號為好友），則退回使用其原始 userId。"""
    if group_id:
        url = f"{LINE_API}/group/{group_id}/member/{user_id}"
    elif room_id:
        url = f"{LINE_API}/room/{room_id}/member/{user_id}"
    else:
        url = f"{LINE_API}/profile/{user_id}"

    client = _get_client()
    resp = await client.get(url, headers=_headers(access_token))
    if resp.status_code != 200:
        return user_id
    return resp.json().get("displayName", user_id)
