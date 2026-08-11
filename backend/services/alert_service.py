import asyncio
import logging
import time

from config import settings
from services.line_service import push_text

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
    針對某個維運問題，盡力發送 LINE push 通知給管理員；
    若沒有這個機制，這類問題往往要等到學生抱怨或有人剛好
    去查看日誌時才會被發現。

    ``key`` 用來將同一個根本問題的多次重複（例如某個 channel_id，
    或 "google_log"）歸為一組，讓一連串相同的失敗在一個冷卻視窗內
    只合併成一則告警，而不是每個請求都各發一則。若管理員告警
    未設定則不做任何事，且此函式絕不拋出例外——告警路徑本身
    故障，不應該連帶弄壞它所代為告警的那個呼叫端。

    ``cooldown_seconds`` 讓已經有自己一套精確判斷機制的呼叫端
    （例如健康檢查用 DB 裡的 healthy/expired 狀態轉換來判斷，而
    不是單純看時間）可以覆寫、甚至傳入 0 完全略過這裡的冷卻視窗
    ——不然兩套各自獨立的防重複機制疊在一起，反而可能讓一個
    真正的新事件被這裡的時間冷卻擋下來。
    """
    user_ids = _admin_user_ids()
    if not user_ids or not settings.admin_alert_access_token:
        return

    window = _COOLDOWN_SECONDS if cooldown_seconds is None else cooldown_seconds
    now = time.monotonic()
    if now - _last_sent.get(key, 0.0) < window:
        return
    # 在 push 實際發生之前（即使下面發送失敗也一樣）就先標記為
    # 「已送出」——否則在 push 本身的延遲視窗內同時湧入的多個呼叫端
    # 都會通過上面的冷卻檢查，各自發出自己的告警；而且一旦 LINE API
    # 發生中斷，每一次後續失敗都會變成立即重試，而不是像真正成功
    # 時那樣進入冷卻視窗等待。
    _last_sent[key] = now

    # 每個收件人各自 push、彼此獨立——其中一個人已經退出好友或
    # token 有問題導致失敗，不該連帶讓其他人也收不到這則告警。
    results = await asyncio.gather(
        *(push_text(uid, settings.admin_alert_access_token, message) for uid in user_ids),
        return_exceptions=True,
    )
    for uid, result in zip(user_ids, results):
        if isinstance(result, BaseException):
            # 告警是盡力而為：這裡選擇記錄並吞掉例外，而不是往外拋出，
            # 因為這個函式總是從另一個操作的錯誤處理路徑中被呼叫——
            # 若讓一個壞掉的告警路徑在這裡拋出例外，會用一個無關的錯誤
            # 蓋掉那個操作原本真正的錯誤。
            logger.error(f"Failed to send admin alert to {uid} (key={key}): {result}")
