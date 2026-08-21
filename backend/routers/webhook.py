import hashlib
import hmac
import base64
import logging
import re
from fastapi import APIRouter, BackgroundTasks, Request, HTTPException
import aiosqlite
from config import settings
from database import DB
from services.alert_service import notify_admin
from services.nlm_service import ask_question, update_knowledge_base
from services.text_formatter import vendor_from_title
from services.line_service import (
    show_loading,
    reply_text,
    push_text,
    download_content,
    get_display_name,
)

router = APIRouter(tags=["webhook"])
logger = logging.getLogger(__name__)

# 機器人在 LINE 上的顯示名稱（實際名稱是在 LINE Official
# Account Manager 另外設定的——這個常數只控制本程式碼會
# 辨識哪些 @-mention 文字，並不會改動帳號本身的名稱）。
BOT_NAME = "小選"
_TEXT_MENTION_TRIGGERS = (f"@{BOT_NAME}", "@機器人")


def verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    hash_val = hmac.HMAC(
        channel_secret.encode(), body, hashlib.sha256
    ).digest()
    return hmac.compare_digest(base64.b64encode(hash_val).decode(), signature)


def _self_mentions(message: dict) -> list[dict]:
    """回傳此訊息中所有指向本機器人帳號的 @mention 清單。

    部分 LINE 桌面版用戶端在提及（mention）中並不會可靠地填入 ``isSelf``，
    所以當訊息來自群組/聊天室時，我們退而將任何 mention payload
    都視為有效的提及。
    """
    mentionees = message.get("mention", {}).get("mentionees", [])
    if not mentionees:
        return []

    self_mentions = [m for m in mentionees if m.get("isSelf")]
    if self_mentions:
        return self_mentions

    return mentionees


def _has_text_mention(text: str) -> bool:
    """檢查文字中是否明確以名稱 @-mention 了這個機器人——「@小選」或
    通用的「@機器人」——而不是比對任何單獨的 "@" 字元。這是給那些
    無法可靠填入結構化 mention payload 的桌面版 LINE 用戶端的
    備援路徑。"""
    return any(trigger in text for trigger in _TEXT_MENTION_TRIGGERS)


def _strip_text_mention_trigger(text: str) -> str:
    """從訊息中移除已比對到的文字提及觸發詞（見 _has_text_mention）。
    用於沒有結構化 mention payload 可依 index/length 移除的情況——
    否則字面上的「@小選」／「@機器人」文字就會外洩進送給
    NotebookLM 的問題內容中。"""
    for trigger in _TEXT_MENTION_TRIGGERS:
        text = text.replace(trigger, "")
    return text.strip()


_CAR_RELATED_KEYWORDS = (
    "車", "汽車", "車輛", "車子", "車款", "車型", "廠牌", "品牌",
    # 機械／性能
    "引擎", "馬力", "扭力", "排氣量", "油耗", "續航", "充電", "電動車", "油電", "混合動力",
    "變速箱", "手排", "自排", "四驅", "底盤", "懸吊", "煞車", "輪胎",
    # 車型配置／規格／外觀
    "配備", "規格", "內裝", "外觀", "車色", "顏色", "空間", "座椅", "後座", "行李廂",
    "天窗", "大燈", "音響", "螢幕", "安全性", "安全配備", "氣囊", "駕駛輔助",
    # 車體類型
    "房車", "休旅車", "SUV", "掀背", "敞篷", "跑車", "轎車", "貨車", "商用車",
    # 持有／維修保養
    "試乘", "展示中心", "展示間", "現車", "現貨", "交車", "車主", "保養", "保固",
    "維修", "保修", "換油", "里程", "驗車", "回收", "中古車", "二手車",
    # 配置／等級用語（往往是訊息中唯一與車相關的線索，
    # 訊息本身可能只提到車型名稱，例如「X-Trail2019入門型」）
    "入門型", "入門版", "豪華型", "豪華版", "旗艦", "頂規", "尊爵", "精裝",
    "智能版", "經典版", "舒適版", "進化版", "運動版", "頂級", "標準型",
    # 「哪家經銷商／品牌有賣這個」的常見說法
    "誰家有", "哪家有", "哪裡有", "哪裡買得到",
    # 品牌名稱——英文／原文拼寫，使用者常見輸入方式
    "Toyota", "Lexus", "Honda", "Nissan", "Mazda", "Mitsubishi", "Suzuki",
    "Subaru", "Ford", "Hyundai", "Kia", "Volkswagen", "Audi", "BMW",
    "Benz", "Mercedes", "Volvo", "Peugeot", "Skoda", "Tesla", "Luxgen",
    # 品牌名稱——中文音譯
    "豐田", "雷克薩斯", "本田", "日產", "馬自達", "三菱", "鈴木", "速霸陸",
    "福特", "現代", "起亞", "福斯", "奧迪", "寶馬", "賓士", "朋馳", "富豪",
    "標緻", "斯柯達", "特斯拉", "納智捷", "裕隆", "中華",
    # 常見車型名稱——使用者很自然會直接用車型代號發問（例如「CRV多少錢」），
    # 完全不提廠牌或任何上面的通用字詞；這份清單無法窮舉所有車型
    # （知識庫日後可能匯入清單外的車型），只能盡量涵蓋現有庫存與市場
    # 常見的名稱，跟下面 `_MODEL_CODE_RE` 的全大寫代號比對互補。
    "X-Trail", "Corolla", "Altis", "Camry", "Yaris", "Vios", "Sienta",
    "Wish", "Premio", "Auris", "Prius", "Innova", "Golf", "Tiguan",
    "Focus", "Ranger", "Kicks", "Livina", "Outlander", "Delica",
    "Tucson", "Elantra", "Sportage", "Sorento", "Kona",
    # 常見車型名稱——中文口語音譯（跟上面品牌的中文音譯是同一種需求：
    # 使用者輸入習慣未必用原文拼寫）。目前只收錄真實發生過使用者
    # 這樣打字、卻沒被辨識出來的案例，一樣無法窮舉。
    "阿迪斯",  # Corolla Altis
)

# 車型代號常見用全大寫英文表示（CRV、SUV、RAV4、EV6、ABS、GTI...），
# 使用者只打代號、不帶廠牌或任何通用字詞發問是很常見的情況——這條
# 規則跟上面的關鍵字清單互補，不要求窮舉每一個車型名稱。
#
# 車型代號也常見「單一大寫字母＋數字」的寫法（例如知識庫裡「VERYCA
# A190」這款車，使用者會直接省略廠牌只打「A190」發問），原本只認
# 「兩個以上大寫字母開頭」的寫法會漏接這種——真實發生過的案例：
# 使用者連續問「A90」「A190」都被判定成跟車輛無關。要求至少兩位數字
# 接在單一大寫字母後面，是為了跟一般文字中偶然出現的「A1」「B2」
# 這類短代稱（樓層、選項編號等）做區隔，降低誤判機率。
_MODEL_CODE_RE = re.compile(
    r"(?<![A-Za-z])(?:[A-Z]{2,}[A-Z0-9]*|[A-Z]\d{2,})(?![a-z])"
)


def _is_car_related_question(text: str) -> bool:
    """檢查文字是否合理像是一個購車諮詢問題——這是用來判斷機器人
    在群組/聊天室中是否該回應（取代硬性要求 @mention）的判斷門檻。

    購買／授權限制類用語（例如「多少錢」、「報價」）本身通常不會
    包含字面上的汽車名詞，但在這個機器人的經銷商諮詢情境中，
    這些本質上就是與車相關的——因此這些類別也一併納入判斷，
    而不只是單純的關鍵字清單。

    關鍵字比對刻意不分大小寫（真實發生過的案例：清單裡放的是
    「Nissan」「Kicks」，使用者輸入全小寫的「nissan kicks」對不上，
    被誤判成非車輛問題）——但 `_MODEL_CODE_RE`（判斷裸的全大寫代號，
    例如「CRV」）刻意維持只認全大寫，這是它用來跟一般小寫英文單字
    做區分的訊號，不能一起變成不分大小寫，否則任何兩個字母的小寫
    單字都會被誤判成車型代號。
    """
    text_lower = text.lower()
    if any(keyword.lower() in text_lower for keyword in _CAR_RELATED_KEYWORDS):
        return True
    if _MODEL_CODE_RE.search(text):
        return True
    return classify_restricted_topic(text) is not None


_UPDATE_KB_KEYWORDS = ("更新知識庫", "更新資料庫", "更新資料")


def _is_update_kb_request(text: str) -> bool:
    """檢查（已去除 mention 的）文字是否在要求更新知識庫。"""
    return any(keyword in text for keyword in _UPDATE_KB_KEYWORDS)


# 經銷商群組常見的「賀成交」報喜貼文（例如「賀成交！恭喜客戶入主
# X-Trail」）本質上是同仁公告，不是在向機器人提問——但因為貼文裡
# 常常會提到實際車型名稱，會被 `_is_car_related_question` 判定為
# 車輛相關而觸發回覆，或被 `_PURCHASE_KEYWORDS`（例如「成交」）
# 判定為交易意圖而導向廠商聯絡資訊，兩種回覆對這種公告貼文來說都
# 是誤觸發、沒有意義。只要訊息中出現這裡任一個字，就整句完全不
# 回覆（連婉拒或引導聯絡方式的訊息都不送），1:1 私訊與群組/聊天室
# 都適用。
_CELEBRATION_KEYWORDS = ("賀", "成交")


def _is_celebration_message(text: str) -> bool:
    """檢查文字是否包含「賀」「成交」這類報喜／成交公告用語。"""
    return any(keyword in text for keyword in _CELEBRATION_KEYWORDS)


_PURCHASE_KEYWORDS = (
    "交易", "買賣", "買", "賣", "購買", "出售", "成交", "待售", "簽約", "合約", "訂單", "報價", "議價", "付款", "下單",
    # 不含上述字詞的價格／折扣用語
    "多少錢", "多少費用", "折價", "折扣", "促銷", "團購", "收購", "加價", "優惠",
    # 付款／匯款相關用語
    "定金", "匯款", "收款", "轉帳", "儲值", "尾款", "續約", "現金", "發票", "統編",
    "信用卡", "加密貨幣", "比特幣", "扣款", "價目表", "選配金",
)
_AUTHORIZATION_PROMISE_KEYWORDS = (
    "授權", "承諾", "保證",
    # 承諾／許可的同義詞
    "答應", "准許", "商標",
    # 不含「授權」字樣的商業使用／授權相關用語
    "商業廣告", "協力廠商", "推薦指定",
)
_RESTRICTED_TOPIC_REPLY = (
    "不好意思，像是授權、承諾或保證等問題，這邊沒辦法直接答覆您，"
    "還是要麻煩您與業務人員確認，才能給您最準確的說明 🙏"
)
_OFF_TOPIC_REPLY = (
    "不好意思，我只能協助車輛規格、配備、現有庫存等相關問題喔，"
    "有什麼想了解的車款嗎？"
)
_PURCHASE_INTENT_PREFIX = (
    "不好意思，購車、報價這類交易細節得由業務人員親自為您服務，"
    "沒辦法透過機器人直接回覆喔！"
)
_PURCHASE_INTENT_CONTACT_LEAD_IN = "您可以直接聯繫以下廠商，由專人為您服務："

_CONTACT_INFO_QUERY_TEMPLATE = (
    "使用者詢問以下車輛相關內容，請根據知識庫找出符合描述的廠商，"
    "只回覆該廠商的 con_info 聯絡資訊；不要提供價格、報價或任何交易相關內容：\n"
    "{question}"
)


def _purchase_intent_reply() -> str:
    """通用的備援聯絡回覆，用於無法比對出特定廠商時。"""
    return f"{_PURCHASE_INTENT_PREFIX}\n{settings.dealer_contact_info}"

# 這些說法單看關鍵字會被判定為限制主題，但實際上只是單純的資訊
# 詢問（例如「這個授權服務中心存在嗎」），而不是在要求交易／授權／
# 承諾。會在關鍵字比對前先被移除，讓訊息的其餘部分仍能照常被檢查。
_RESTRICTED_TOPIC_EXEMPT_PATTERNS = (
    re.compile(r"授權的?(展示中心|保養廠|維修廠|服務廠)"),
    re.compile(r"申請購買.{0,4}保固|購買.{0,4}延長保固"),
    # 「我要買 X，請給我這款車型的資訊」——買／賣只是拿來鋪陳
    # 一個單純資訊詢問的說法。這裡只會移除這個子句；若訊息其他
    # 地方仍有真正的交易性尾段（價格、付款等），依然會被擋下。
    re.compile(r"(買|賣)\S{0,14}[,，].{0,12}(資訊|規格|資料|介紹|說明)"),
)


def _strip_exempt_phrases(text: str) -> str:
    for pattern in _RESTRICTED_TOPIC_EXEMPT_PATTERNS:
        text = pattern.sub("", text)
    return text


def _is_purchase_intent(text: str) -> bool:
    """檢查問題是否涉及買賣車輛（會導向廠商聯絡資訊）。"""
    text = _strip_exempt_phrases(text)
    return any(keyword in text for keyword in _PURCHASE_KEYWORDS)


def _is_authorization_or_promise(text: str) -> bool:
    """檢查問題是否涉及授權、商標或承諾。"""
    text = _strip_exempt_phrases(text)
    return any(keyword in text for keyword in _AUTHORIZATION_PROMISE_KEYWORDS)


def classify_restricted_topic(text: str) -> str | None:
    """將問題分類為限制主題（若有的話）。

    回傳 "authorization_promise"、"purchase" 或 None。之所以按這個
    順序檢查，是因為一則同時涉及承諾／保證*又*順帶提到購買的訊息
    （例如「如果我買這台，你能保證……」）本質上是一個承諾問題，
    而不是購車詢問——它應該得到通用的婉拒回覆，而不是廠商聯絡資訊
    的回覆。
    """
    if _is_authorization_or_promise(text):
        return "authorization_promise"
    if _is_purchase_intent(text):
        return "purchase"
    return None


def _is_restricted_topic(text: str) -> bool:
    """檢查問題是否涉及交易、銷售、授權或承諾。"""
    return classify_restricted_topic(text) is not None


def _strip_mentions(text: str, mentionees: list[dict]) -> str:
    """從訊息文字中移除 @mention 子字串（例如 '@BotName'）。"""
    for m in sorted(mentionees, key=lambda m: m["index"], reverse=True):
        start, length = m["index"], m["length"]
        text = text[:start] + text[start + length:]
    return text.strip()


async def _resolve_mention(
    source_type: str,
    sender_user_id: str | None,
    group_id: str | None,
    room_id: str | None,
    access_token: str,
) -> tuple[str | None, str | None]:
    """解析出提問者的 (user_id, display_name)，以便回覆時能 @提及
    對方——這只有在群組/聊天室中才有意義（因為可能同時有多人在
    發問），而且只有在 LINE 有提供 sender_user_id 時才做得到
    （若群組成員尚未加官方帳號為好友，LINE 就不會提供）。"""
    if source_type not in ("group", "room") or not sender_user_id:
        return None, None
    display_name = await get_display_name(access_token, sender_user_id, group_id, room_id)
    return sender_user_id, display_name


_RECENT_CONVERSATION_WINDOW = "-10 minutes"

# 使用者「連續嘗試發問、但一句都還沒成功通過關鍵字判斷」時給的寬限
# 視窗，刻意比上面「已經成功問過一次」的視窗短很多——見
# `_touch_recent_attempt` 的說明，這裡完全沒有「這確實是車輛相關」
# 的任何實質證據（不像已經成功問過一次那樣，至少那句話本身通過了
# 關鍵字判斷），拉長視窗只會提高「使用者純聊天離題，也被誤判成
# 延續中」的風險，所以只給很短的緩衝時間讓使用者把話講清楚。
_RECENT_ATTEMPT_WINDOW = "-2 minutes"


async def _has_recent_conversation(channel_id: str, line_user_id: str | None) -> bool:
    """檢查這個 LINE 使用者在這個 channel 是否剛剛才跟機器人互動過，
    符合下面兩種情況之一：

    - 有進行中的 NotebookLM 對話串（曾經成功問過車輛相關問題），且
      最近一次互動在 `_RECENT_CONVERSATION_WINDOW`（10 分鐘）之內。
    - 還沒有任何一句話成功通過關鍵字判斷，但剛剛才嘗試發問過
      （見 `_touch_recent_attempt`），且在更短的
      `_RECENT_ATTEMPT_WINDOW`（2 分鐘）之內。

    給 1:1 聊天的車輛相關性把關（見下方 `_is_car_related_question`
    的呼叫處）用：如果對方現在仍在同一輪對話裡，單看這一則訊息本身
    的關鍵字並不夠——追問句常常不會重複任何車輛相關字詞。這涵蓋兩種
    真實發生過的案例：

    1. 機器人一次回覆了好幾家廠商的比較表，使用者追問「其他5家呢？」，
       卻被當成跟車輛無關的問題直接打槍，因為「其他」「5家」都不在
       關鍵字清單裡（`conversation_id` 已存在的情況）。
    2. 使用者連續打了好幾句話在嘗試講清楚自己要問什麼（例如「阿迪斯」
       →「尋 2022年阿迪斯 白色」→「A90」→「A190」，前後不到一分鐘），
       但第一句字面上就沒有踩中任何關鍵字，導致連 `conversation_id`
       都還沒建立——如果沒有 `_RECENT_ATTEMPT_WINDOW` 這條路徑，第一句
       判斷失敗之後，接下來每一句追問都要重新從頭通過同一道嚴格關鍵字
       檢查，等於連環誤判到底，即使關鍵字清單補得再齊也擋不住使用者
       打字本來就模糊的情況。

    第 1 種情況刻意用比較長的視窗，是因為至少有一句話真的通過了車輛
    相關性判斷，比較不用擔心對方其實在聊完全無關的話題；第 2 種完全
    沒有這個保證，所以視窗短很多——否則使用者幾天前問過車、之後隨口
    問一句完全無關的天氣，也會被誤判成延續話題、直接送進
    NotebookLM，重新暴露這道關卡原本要擋的網路搜尋亂碼風險（見
    `_A2UI_JSON_RE` 的說明）。"""
    if not line_user_id:
        return False
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT 1 FROM user_conversations WHERE channel_id=? AND line_user_id=? "
            "AND ("
            "  (conversation_id IS NOT NULL AND updated_at >= datetime('now', ?))"
            "  OR (conversation_id IS NULL AND updated_at >= datetime('now', ?))"
            ")",
            (channel_id, line_user_id, _RECENT_CONVERSATION_WINDOW, _RECENT_ATTEMPT_WINDOW),
        )
        row = await cur.fetchone()
    return row is not None


async def _touch_recent_attempt(channel_id: str, line_user_id: str | None) -> None:
    """記錄這個使用者剛剛在這個 channel 嘗試發問過一次——即使這次的
    訊息沒有通過車輛相關性判斷、被回覆罐頭的離題訊息（`_OFF_TOPIC_REPLY`），
    也要留下這個時間戳記，讓緊接著的下一句追問能在 `_RECENT_ATTEMPT_WINDOW`
    內透過 `_has_recent_conversation` 獲得比較寬鬆的判斷，而不是每一句
    都要重新從頭通過同一道嚴格關鍵字檢查（見該函式案例 2 的說明）。

    刻意用 `ON CONFLICT ... DO UPDATE SET updated_at=...`（只更新
    `updated_at`，不動 `conversation_id`）而不是整列覆寫——如果這個
    使用者先前其實已經成功問過車、`conversation_id` 欄位原本就有值，
    不該被這次「單純記錄嘗試過」的動作意外清空，那個值還要留給
    NotebookLM 用來延續對話上下文（見 `nlm_service._save_conversation_id`）。"""
    if not line_user_id:
        return
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """
            INSERT INTO user_conversations (channel_id, line_user_id, conversation_id, updated_at)
            VALUES (?, ?, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT(channel_id, line_user_id)
            DO UPDATE SET updated_at=CURRENT_TIMESTAMP
            """,
            (channel_id, line_user_id),
        )
        await db.commit()


async def _show_working_indicator(source_type: str, target_id: str, access_token: str, message: str) -> None:
    """
    在我們同步等待真正的回覆時，盡力提供「處理中」的回饋提示。
    1:1 聊天會使用 LINE 原生的載入動畫（show_loading），這不會
    佔用計量的 push 額度。群組/聊天室完全不支援這個 API，因此
    改用一次性的 push 訊息作為備援——這只是盡力而為，失敗時
    靜默忽略即可，因為就算這則 push 因額度用盡而失敗，頂多只是
    少了中間過程的回饋，並不會遺失真正的答案（真正的答案
    一律透過 reply_text 送出，不使用 push）。
    """
    if source_type == "user":
        await show_loading(target_id, access_token, seconds=30)
        return
    try:
        await push_text(target_id, access_token, message)
    except Exception as e:
        logger.warning(f"Best-effort working indicator failed (non-critical): {e}")


@router.post("/webhook/{channel_id}")
async def webhook(channel_id: str, request: Request, background_tasks: BackgroundTasks):
    """接收 LINE 的 webhook 事件。

    這個 handler 本身只做「驗證這個請求、把每個事件排進背景任務」，
    真正回答問題（可能要等 NotebookLM 查詢完成，實測過最久 5 分半）
    一律丟給 background_tasks 執行，不在這裡 await——LINE 對 webhook
    的回應有嚴格的時間限制，過去這裡是整個處理完才回應，查詢一慢
    LINE 那端就會判定逾時／失敗（nginx 側可觀察到 499），而且 LINE
    本身有內部的 webhook 品質評分機制，回應慢/常失敗的端點之後可能
    被延遲或跳過投遞，形成「明明服務正常，訊息卻偶爾送不到」的假象。
    背景任務仍然在同一個 event loop、回應送出後幾乎立刻就開始執行，
    不會因此拖慢 reply_token 的新鮮度。"""
    body = await request.body()

    # 查詢 channel
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT channel_secret, channel_access_token, expires_at FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        channel = await cur.fetchone()

    if not channel:
        raise HTTPException(404, "Channel not found")

    # 檢查是否過期
    if channel["expires_at"]:
        from datetime import datetime, timezone
        if channel["expires_at"] < datetime.now(timezone.utc).isoformat():
            return {"status": "expired"}

    # 驗證 LINE 簽章
    signature = request.headers.get("x-line-signature", "")
    if not verify_signature(body, signature, channel["channel_secret"]):
        raise HTTPException(403, "Invalid signature")

    access_token = channel["channel_access_token"]

    # 解析事件
    import json
    payload = json.loads(body)

    events = payload.get("events", [])

    # LINE 針對「一次選取多個檔案上傳」通常會把它們包成同一個
    # webhook 請求裡的多個 message event，各自帶有各自的
    # reply_token。整批先處理掉，才能只顯示一次「正在更新」提示、
    # 合併成一則完成通知，而不是每個檔案各自洗一次版（見
    # _handle_file_batch）——排進背景任務，理由同上，不在這裡 await。
    file_events = [
        e for e in events
        if e.get("type") == "message" and e.get("message", {}).get("type") == "file"
    ]
    if file_events:
        background_tasks.add_task(_handle_file_batch, channel_id, access_token, file_events)

    for event in events:
        if event.get("type") != "message":
            continue

        message = event["message"]
        if message.get("type") == "file":
            continue  # 已經在上面整批處理過了

        background_tasks.add_task(_handle_message_event, channel_id, access_token, event)

    return {"status": "ok"}


async def _handle_message_event(channel_id: str, access_token: str, event: dict) -> None:
    """處理單一個非檔案的 message event（文字訊息等）——內容是原本
    webhook() 裡逐一處理每個事件的邏輯，搬出來是為了能被排進
    background_tasks、不卡住 webhook 本身的回應（見 webhook() 的
    說明）。這裡面每一個原本迴圈裡的 `continue`，現在都對應這個
    函式的 `return`（提早結束這一個事件的處理，語意不變）。"""
    message = event["message"]
    reply_token = event["replyToken"]
    source = event["source"]
    source_type = source.get("type")  # "user" | "group" | "room"
    message_type = message.get("type")
    logger.info(f"[{channel_id}] Event payload: {event}")
    """
     在群組/聊天室中，只回應「文字」訊息，且該訊息必須是合理的
     購車諮詢問題（見 _is_car_related_question）或明確 @提及了
     機器人——@mention 這條路徑保留作為關鍵字清單漏接時的備援，
     而不是必要條件。1:1 聊天則兩者都不需要。檔案訊息在 LINE
     上永遠不可能帶有 @mention，所以這道門檻不適用於它們——
     群組/聊天室中傳送的任何檔案都會被視為更新請求。
    """
    if source_type in ("group", "room") and message_type == "text":
        mentions = _self_mentions(message)
        message_text = message.get("text", "")
        has_text_mention = _has_text_mention(message_text)
        is_car_related = _is_car_related_question(message_text)
        logger.info(
            f"[{channel_id}] Parsed group/room message: mentions={mentions}, "
            f"text_has_@={has_text_mention}, car_related={is_car_related}"
        )
        if not mentions and not has_text_mention and not is_car_related:
            logger.info(f"[{channel_id}] Ignored unrelated group/room message: {message}")
            return

    # push 回覆的目標對象：若有群組/聊天室則優先使用，否則使用該使用者。
    target_id = source.get("groupId") or source.get("roomId") or source.get("userId")
    if not target_id:
        # 匿名群組成員（尚未加官方帳號為好友），沒有可用的目標對象——跳過。
        logger.info(f"[{channel_id}] Skipped: no usable target id in source={source}")
        return

    # 實際發訊者，在群組/聊天室中與 target_id 不同——
    # 用來讓每個人各自的 NotebookLM 對話串保持獨立。
    # 若群組成員尚未加官方帳號為好友，LINE 不會提供這個值；
    # 這種情況下問題依然會被回答，只是沒有記憶。
    sender_user_id = source.get("userId")
    group_id = source.get("groupId")
    room_id = source.get("roomId")

    if message_type == "text":
        question = message["text"]
        if source_type in ("group", "room"):
            mentions = _self_mentions(message)
            question = (
                _strip_mentions(question, mentions) if mentions
                else _strip_text_mention_trigger(question)
            )

        if _is_celebration_message(question):
            logger.info(f"[{channel_id}] Ignored celebration/deal-closed message: {question[:50]}")
            return

        # 每則訊息只解析一次，並貫穿傳遞給下面的每一次回覆，
        # 讓忙碌的群組聊天能一眼看出每個答案屬於誰的問題。
        # 在 1:1 聊天或 LINE 未提供發訊者 id 時為 None/None。
        mention_user_id, mention_display_name = await _resolve_mention(
            source_type, sender_user_id, group_id, room_id, access_token
        )

        if source_type in ("group", "room") and not question:
            # 移除 mention 之後沒有剩下任何問題文字——
            # 這裡刻意保持通用回覆，不需要指定廠商名稱。
            await reply_text(
                reply_token, access_token, "請問想問什麼問題呢？",
                mention_user_id, mention_display_name,
            )
            return

        logger.info(f"[{channel_id}] ({source_type}) Q: {question[:50]}")

        if _is_update_kb_request(question):
            await reply_text(
                reply_token, access_token, "請上傳更新後的資料",
                mention_user_id, mention_display_name,
            )
            return

        restricted_topic = classify_restricted_topic(question)
        if restricted_topic == "authorization_promise":
            await reply_text(
                reply_token, access_token, _RESTRICTED_TOPIC_REPLY,
                mention_user_id, mention_display_name,
            )
            return
        if restricted_topic == "purchase":
            await _show_working_indicator(source_type, target_id, access_token, "⏳正在查詢中，請稍後")

            await _purchase_intent_ask_and_reply(
                channel_id, reply_token, access_token, question,
                sender_user_id, group_id, room_id,
                mention_user_id, mention_display_name,
            )
            return

        # 群組/聊天室在更早就已經先做過車輛相關性判斷，沒過關的訊息
        # 根本不會執行到這裡；但 1:1 聊天完全沒有經過那一關（見上面
        # 的說明：1:1 不需要 @mention 也不需要車輛相關性判斷就會被
        # 處理），導致跟車輛完全無關的問題（例如問颱風動態）會被直接
        # 轉給 NotebookLM——NotebookLM 有時候會為了「幫忙」而觸發它
        # 自己的網路搜尋功能，回答裡夾帶只有網頁版看得懂的 UI 元件
        # 描述，在 LINE 上會變成一大坨亂碼。1:1 這裡也要做同樣的
        # 把關，跟知識庫無關的問題一律當閒聊回絕，不要送進 NotebookLM。
        #
        # 但如果這個使用者最近才剛跟機器人聊過車（見
        # `_has_recent_conversation`），代表現在仍在同一輪對話裡
        # ——追問句常常不會重複任何車輛關鍵字，這種情況下不套用
        # 嚴格的關鍵字判斷，讓對話能自然延續下去。
        if (
            source_type == "user"
            and not _is_car_related_question(question)
            and not await _has_recent_conversation(channel_id, sender_user_id)
        ):
            # 這句話本身沒有踩中任何關鍵字，但還是要記錄「剛剛嘗試發問
            # 過」——讓使用者接下來幾句字面上再模糊的追問，能在
            # `_RECENT_ATTEMPT_WINDOW` 內透過 `_has_recent_conversation`
            # 得到寬限，不會被同一道關鍵字檢查連環打槍到底（見該函式
            # 案例 2 的說明）。
            await _touch_recent_attempt(channel_id, sender_user_id)
            await reply_text(
                reply_token, access_token, _OFF_TOPIC_REPLY,
                mention_user_id, mention_display_name,
            )
            return

        await _show_working_indicator(source_type, target_id, access_token, "⏳正在查詢中，請稍後")

        await _ask_and_reply(
            channel_id, reply_token, access_token, question,
            sender_user_id, group_id, room_id,
            mention_user_id, mention_display_name,
        )


async def _ask_and_reply(
    channel_id: str,
    reply_token: str,
    access_token: str,
    question: str,
    sender_user_id: str | None = None,
    group_id: str | None = None,
    room_id: str | None = None,
    mention_user_id: str | None = None,
    mention_display_name: str | None = None,
):
    """回答一個問題，並透過 reply token（而非 push）送出——
    reply 訊息不像 push 訊息那樣會被計入帳號每月的 LINE 訊息額度。
    這會讓 webhook handler 阻塞至 NotebookLM 查詢完成為止，
    所以非常慢的查詢有 reply token 過期的風險；這是為了不依賴
    push 而接受的取捨。

    當答案依廠商分則後超過 5 則（LINE 一次 reply 呼叫的訊息數上限）
    時，前 5 則仍用免額度的 reply 送出，其餘的才改用 push——寧可
    多花一點額度，也不要讓超過第 5 家的廠商資訊直接消失、使用者
    完全看不到（這是真實發生過的情況：8 家廠商的總覽拆成一家一則
    之後，只用 reply 的話後面 3 家會被悄悄截掉）。"""
    target_id = group_id or room_id or sender_user_id

    try:
        messages = await ask_question(channel_id, question, sender_user_id)
    except Exception as e:
        # ask_question 本身就失敗了——這通常很快就會發生（例如筆記本未
        # 綁定、API 直接回錯），reply_token 這時候幾乎一定還沒過期，
        # 用它回覆錯誤訊息是安全的。
        logger.error(f"[{channel_id}] Error: {e}")
        try:
            await reply_text(
                reply_token, access_token, "⚠️ 系統發生錯誤，請稍後再試。",
                mention_user_id, mention_display_name,
            )
        except Exception as reply_error:
            logger.error(f"[{channel_id}] Fallback reply also failed: {reply_error}")
            await notify_admin(
                f"silent:{channel_id}",
                f"⚠️ 頻道 {channel_id} 的使用者完全沒收到任何回覆"
                f"（原始錯誤：{e}；fallback 回覆也失敗：{reply_error}）",
            )
        return

    if not messages:
        messages = ["⚠️ 沒有取得回覆內容，請稍後再試。"]
    reply_messages, overflow_messages = messages[:5], messages[5:]

    try:
        await reply_text(reply_token, access_token, reply_messages, mention_user_id, mention_display_name)
    except Exception as reply_error:
        # ask_question 已經成功拿到答案了——這裡失敗幾乎都是因為查詢本身
        # 耗時太久（NotebookLM 對跨多家廠商的問題常需要 3-5 分鐘），
        # reply_token 早就過期。用同一個 token 再重試一次 reply 沒有意義
        # （token 本來就是一次性、而且已經失效），過去這樣做的結果就是
        # 使用者完全收不到任何東西、連錯誤訊息都沒有（真實發生過的
        # 案例）。改用 push 才有機會把答案送到使用者手上——這則答案
        # 已經生成好了，不該平白浪費掉。
        logger.error(
            f"[{channel_id}] reply_text failed (likely an expired reply_token after "
            f"a slow query): {reply_error}"
        )
        if not target_id:
            logger.warning(f"[{channel_id}] No usable target id for push fallback after reply failure")
            await notify_admin(
                f"silent:{channel_id}",
                f"⚠️ 頻道 {channel_id} 的使用者完全沒收到任何回覆"
                f"（reply 失敗：{reply_error}；且沒有可用的 target id 可以 push）",
            )
            return
        try:
            for msg in reply_messages:
                await push_text(target_id, access_token, msg)
        except Exception as push_error:
            logger.error(f"[{channel_id}] Push fallback also failed: {push_error}")
            await notify_admin(
                f"silent:{channel_id}",
                f"⚠️ 頻道 {channel_id} 的使用者完全沒收到任何回覆"
                f"（reply 失敗：{reply_error}；push fallback 也失敗：{push_error}）",
            )
            return

    if overflow_messages:
        if target_id:
            for extra in overflow_messages:
                try:
                    await push_text(target_id, access_token, extra)
                except Exception as push_error:
                    # 主要的回答（前 5 則）已經送出去了——這裡失敗
                    # 只代表超過 5 則之後的部分沒送到，不該讓使用者
                    # 看到「系統發生錯誤」這種好像整個查詢都失敗的
                    # 訊息，記錄下來讓管理員知道即可。
                    logger.error(
                        f"[{channel_id}] Failed to push overflow message "
                        f"(beyond LINE's 5-message reply limit): {push_error}"
                    )
        else:
            logger.warning(
                f"[{channel_id}] {len(overflow_messages)} overflow message(s) "
                "dropped: no usable target id for push fallback"
            )


async def _purchase_intent_ask_and_reply(
    channel_id: str,
    reply_token: str,
    access_token: str,
    question: str,
    sender_user_id: str | None,
    group_id: str | None = None,
    room_id: str | None = None,
    mention_user_id: str | None = None,
    mention_display_name: str | None = None,
):
    """處理一個購車意圖問題：拒絕討論交易本身，但仍會查詢
    符合條件的是哪一家廠商的車，並回傳該廠商的聯絡資訊
    （con_info），而不是回傳一個寫死的電話號碼。

    這個查詢是一個獨立的 NotebookLM 問題（不依附於使用者自己的
    對話串），所以這段 meta-instruction 不會外洩進他們持續進行中
    的聊天紀錄裡。基於與 ``_ask_and_reply`` 相同的額度考量，
    透過 reply（而非 push）送出。
    """
    reply = _purchase_intent_reply()
    try:
        contact_query = _CONTACT_INFO_QUERY_TEMPLATE.format(question=question)
        messages = await ask_question(channel_id, contact_query, line_user_id=None)
        if any(m.startswith("⚠️") for m in messages):
            # 這是真正的 ask_question 失敗（筆記本未綁定、API 錯誤等）
            # ——而不是「沒有比對到廠商」。仍然給使用者禮貌的通用回覆。
            logger.error(f"[{channel_id}] Contact info lookup returned an error: {messages}")
            await reply_text(reply_token, access_token, reply, mention_user_id, mention_display_name)
        else:
            vendor_messages = [m for m in messages if m.startswith("【")]
            if vendor_messages:
                intro = f"{_PURCHASE_INTENT_PREFIX}\n\n{_PURCHASE_INTENT_CONTACT_LEAD_IN}"
                reply_messages = [intro] + vendor_messages
                await reply_text(reply_token, access_token, reply_messages, mention_user_id, mention_display_name)
            else:
                await reply_text(reply_token, access_token, reply, mention_user_id, mention_display_name)
    except Exception as e:
        logger.error(f"[{channel_id}] Error looking up vendor contact info: {e}")
        try:
            await reply_text(reply_token, access_token, reply, mention_user_id, mention_display_name)
        except Exception as reply_error:
            logger.error(f"[{channel_id}] Fallback reply also failed: {reply_error}")
            await notify_admin(
                f"silent:{channel_id}",
                f"⚠️ 頻道 {channel_id} 的使用者完全沒收到任何回覆"
                f"（原始錯誤：{e}；fallback 回覆也失敗：{reply_error}）",
            )


async def _process_one_upload(
    channel_id: str, access_token: str, file_id: str | None, file_name: str
) -> str:
    """下載一個 LINE 檔案附件並更新知識庫，回傳結果訊息。

    失敗時回傳可以直接顯示給使用者看的 ⚠️ 訊息，不往外拋出例外，
    這樣不管是單一檔案上傳（_upload_and_reply）還是批次上傳
    （_handle_file_batch）都能安全地收集每個檔案各自的結果，
    一個檔案失敗不會中斷其他檔案的處理。"""
    try:
        if not file_id:
            raise ValueError("缺少 LINE 附件 ID")

        file_bytes = await download_content(file_id, access_token)
        return await update_knowledge_base(channel_id, file_bytes, file_name)
    except Exception as e:
        logger.error(f"[{channel_id}] Upload error for {file_name}: {e}")
        return f"⚠️ 更新知識庫失敗：{e}"


async def _upload_and_reply(
    channel_id: str,
    reply_token: str,
    access_token: str,
    file_id: str | None,
    file_name: str,
):
    """更新知識庫，並透過 reply（而非 push）確認完成——與
    ``_ask_and_reply`` 相同的額度考量。"""
    message = await _process_one_upload(channel_id, access_token, file_id, file_name)
    try:
        await reply_text(reply_token, access_token, message)
    except Exception as reply_error:
        logger.error(f"[{channel_id}] Fallback reply also failed: {reply_error}")
        await notify_admin(
            f"silent:{channel_id}",
            f"⚠️ 頻道 {channel_id} 的檔案上傳者完全沒收到任何回覆"
            f"（fallback 回覆失敗：{reply_error}）",
        )


_UPLOAD_SUCCESS_RE = re.compile(r"已(?:新增檔案|覆蓋同名舊檔案並上傳)：(.+)$")
_MAX_VENDOR_NAMES_SHOWN = 5


def _summarize_upload_results(results: list[str]) -> str:
    """把多個檔案各自的更新結果，合併成一則訊息：成功的只列出
    廠商名稱（最多顯示前 5 個，其餘用「...等N份」帶過，避免一次
    上傳十幾家廠商的資料時洗版），失敗的維持原本完整的錯誤訊息，
    讓使用者知道哪些檔案需要重新上傳。"""
    vendors = []
    failures = []
    for r in results:
        match = _UPLOAD_SUCCESS_RE.search(r)
        if match:
            vendors.append(vendor_from_title(match.group(1)))
        else:
            failures.append(r)

    parts = []
    if vendors:
        shown = "、".join(vendors[:_MAX_VENDOR_NAMES_SHOWN])
        if len(vendors) > _MAX_VENDOR_NAMES_SHOWN:
            shown += f"...等{len(vendors)}份"
        parts.append(f"✅ 知識庫已更新：{shown}")
    if failures:
        parts.append("\n".join(failures))

    return "\n\n".join(parts) if parts else "⚠️ 沒有成功更新任何檔案。"


async def _handle_file_batch(channel_id: str, access_token: str, file_events: list[dict]) -> None:
    """一次處理同一個 webhook 請求裡收到的所有檔案上傳事件。

    逐一分開回覆的話，一次選取多個檔案上傳會洗出好幾則「正在更新
    知識庫」跟好幾則「已更新」訊息；這裡改成只顯示一次處理中提示，
    全部上傳完才合併成一則通知回覆。每個 file event 都各自帶有
    只能用一次的 reply_token，這裡只用其中一個（最後一個）來回覆
    合併後的結果，其餘的就讓它自然過期即可，不會有副作用。"""
    first_source = file_events[0]["source"]
    source_type = first_source.get("type")
    target_id = (
        first_source.get("groupId") or first_source.get("roomId") or first_source.get("userId")
    )
    if target_id:
        await _show_working_indicator(source_type, target_id, access_token, "⏳正在更新知識庫，請稍後")

    results = []
    for event in file_events:
        message = event["message"]
        file_name = message.get("fileName") or "uploaded_file"
        file_id = message.get("id")
        results.append(await _process_one_upload(channel_id, access_token, file_id, file_name))

    summary = _summarize_upload_results(results)
    reply_token = file_events[-1]["replyToken"]
    try:
        await reply_text(reply_token, access_token, summary)
    except Exception as reply_error:
        logger.error(f"[{channel_id}] Fallback reply also failed: {reply_error}")
        await notify_admin(
            f"silent:{channel_id}",
            f"⚠️ 頻道 {channel_id} 的檔案上傳者完全沒收到任何回覆"
            f"（fallback 回覆失敗：{reply_error}）",
        )