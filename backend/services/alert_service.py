import asyncio
import logging
import time
from typing import Awaitable

from config import settings
from services.google_chat_service import send_message as send_google_chat_message
from services.line_service import push_text
from services.telegram_service import send_message as send_telegram_message

logger = logging.getLogger(__name__)

# 對同一個問題要保持多久的靜默期，才會再次發出告警。
# 沒有這個機制的話，若很多學生在幾分鐘內連續碰到同一個壞掉的
# NotebookLM session，就會每個問題各發一次 LINE push，
# 而不是每個事件只發一次。
_COOLDOWN_SECONDS = 300

_last_sent: dict[str, float] = {}


def _admin_user_ids() -> list[str]:
    """解析 ADMIN_LINE_USER_ID——支援用逗號分隔設定多個人的
    LINE userId（例如 "Uxxx,Uyyy"），讓告警不是只有一個人收得到。
    保留原本單一 userId 的寫法也能正常運作（就是長度 1 的清單）。"""
    return [uid.strip() for uid in settings.admin_line_user_id.split(",") if uid.strip()]


async def notify_admin(key: str, message: str, *, cooldown_seconds: float | None = None) -> None:
    """
    針對某個維運問題，盡力發送告警通知給管理員——目前支援三條
    彼此獨立的管道：LINE push（給 ADMIN_LINE_USER_ID 中的每個人）、
    Google Chat（透過 GOOGLE_CHAT_WEBHOOK_URL）與 Telegram（透過
    TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID）。只要任一條有設定就會
    發送；三者都沒設定則什麼事都不做。若沒有這個機制，這類問題
    往往要等到學生抱怨或有人剛好去查看日誌時才會被發現。

    ``key`` 用來將同一個根本問題的多次重複（例如某個 channel_id，
    或 "google_log"）歸為一組，讓一連串相同的失敗在一個冷卻視窗內
    只合併成一則告警，而不是每個請求都各發一則。此函式絕不拋出
    例外——告警路徑本身故障，不應該連帶弄壞它所代為告警的那個
    呼叫端。

    ``cooldown_seconds`` 讓已經有自己一套精確判斷機制的呼叫端
    （例如健康檢查用 DB 裡的 healthy/expired 狀態轉換來判斷，而
    不是單純看時間）可以覆寫、甚至傳入 0 完全略過這裡的冷卻視窗
    ——不然兩套各自獨立的防重複機制疊在一起，反而可能讓一個
    真正的新事件被這裡的時間冷卻擋下來。
    """
    user_ids = _admin_user_ids()
    line_enabled = bool(user_ids and settings.admin_alert_access_token)
    webhook_url = settings.google_chat_webhook_url.strip()
    google_chat_enabled = bool(webhook_url)
    bot_token = settings.telegram_bot_token.strip()
    chat_id = settings.telegram_chat_id.strip()
    telegram_enabled = bool(bot_token and chat_id)
    if not line_enabled and not google_chat_enabled and not telegram_enabled:
        return

    window = _COOLDOWN_SECONDS if cooldown_seconds is None else cooldown_seconds
    now = time.monotonic()
    if now - _last_sent.get(key, 0.0) < window:
        return
    # 在實際發送之前（即使下面發送失敗也一樣）就先標記為「已送出」
    # ——否則在發送本身的延遲視窗內同時湧入的多個呼叫端都會通過上面
    # 的冷卻檢查，各自發出自己的告警；而且一旦某個管道發生中斷，每一次
    # 後續失敗都會變成立即重試，而不是像真正成功時那樣進入冷卻視窗等待。
    _last_sent[key] = now

    # 每個收件人／管道各自發送、彼此獨立——其中一個人已經退出好友、
    # token 有問題，或 Google Chat webhook 掛掉，都不該連帶讓其他人
    # 或其他管道也收不到這則告警。
    targets: list[tuple[str, Awaitable[None]]] = []
    if line_enabled:
        for uid in user_ids:
            targets.append((f"line:{uid}", push_text(uid, settings.admin_alert_access_token, message)))
    if google_chat_enabled:
        targets.append(("google_chat", send_google_chat_message(webhook_url, message)))
    if telegram_enabled:
        targets.append(("telegram", send_telegram_message(bot_token, chat_id, message)))

    results = await asyncio.gather(*(coro for _, coro in targets), return_exceptions=True)
    for (label, _), result in zip(targets, results):
        if isinstance(result, BaseException):
            # 告警是盡力而為：這裡選擇記錄並吞掉例外，而不是往外拋出，
            # 因為這個函式總是從另一個操作的錯誤處理路徑中被呼叫——
            # 若讓一個壞掉的告警路徑在這裡拋出例外，會用一個無關的錯誤
            # 蓋掉那個操作原本真正的錯誤。
            logger.error(f"Failed to send admin alert via {label} (key={key}): {result}")
