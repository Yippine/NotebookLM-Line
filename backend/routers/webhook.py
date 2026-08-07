import asyncio
import hashlib
import hmac
import base64
import logging
import re
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Request, HTTPException
import aiosqlite
from config import settings
from database import DB
from services import google_log_service
from services.alert_service import notify_admin
from services.nlm_service import ask_question, update_knowledge_base
from services.line_service import (
    show_loading,
    reply_text,
    push_text,
    download_content,
    get_display_name,
)

router = APIRouter(tags=["webhook"])
logger = logging.getLogger(__name__)

_BEIJING_TZ = timezone(timedelta(hours=8))

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
)


def _is_car_related_question(text: str) -> bool:
    """檢查文字是否合理像是一個購車諮詢問題——這是用來判斷機器人
    在群組/聊天室中是否該回應（取代硬性要求 @mention）的判斷門檻。

    購買／授權限制類用語（例如「多少錢」、「報價」）本身通常不會
    包含字面上的汽車名詞，但在這個機器人的經銷商諮詢情境中，
    這些本質上就是與車相關的——因此這些類別也一併納入判斷，
    而不只是單純的關鍵字清單。
    """
    if any(keyword in text for keyword in _CAR_RELATED_KEYWORDS):
        return True
    return classify_restricted_topic(text) is not None


_UPDATE_KB_KEYWORDS = ("更新知識庫", "更新資料庫", "更新資料")


def _is_update_kb_request(text: str) -> bool:
    """檢查（已去除 mention 的）文字是否在要求更新知識庫。"""
    return any(keyword in text for keyword in _UPDATE_KB_KEYWORDS)


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
async def webhook(channel_id: str, request: Request):
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

    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue

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
                continue

        # push 回覆的目標對象：若有群組/聊天室則優先使用，否則使用該使用者。
        target_id = source.get("groupId") or source.get("roomId") or source.get("userId")
        if not target_id:
            # 匿名群組成員（尚未加官方帳號為好友），沒有可用的目標對象——跳過。
            logger.info(f"[{channel_id}] Skipped: no usable target id in source={source}")
            continue

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
                continue

            logger.info(f"[{channel_id}] ({source_type}) Q: {question[:50]}")

            if _is_update_kb_request(question):
                await reply_text(
                    reply_token, access_token, "請上傳更新後的資料",
                    mention_user_id, mention_display_name,
                )
                continue

            restricted_topic = classify_restricted_topic(question)
            if restricted_topic == "authorization_promise":
                await reply_text(
                    reply_token, access_token, _RESTRICTED_TOPIC_REPLY,
                    mention_user_id, mention_display_name,
                )
                await _record_interaction(
                    channel_id, group_id, room_id, sender_user_id, access_token,
                    question, _RESTRICTED_TOPIC_REPLY, "需人工聯絡",
                    display_name=mention_display_name,
                )
                continue
            if restricted_topic == "purchase":
                await _show_working_indicator(source_type, target_id, access_token, "⏳正在查詢中，請稍後")

                await _purchase_intent_ask_and_reply(
                    channel_id, reply_token, access_token, question,
                    sender_user_id, group_id, room_id,
                    mention_user_id, mention_display_name,
                )
                continue

            await _show_working_indicator(source_type, target_id, access_token, "⏳正在查詢中，請稍後")

            await _ask_and_reply(
                channel_id, reply_token, access_token, question,
                sender_user_id, group_id, room_id,
                mention_user_id, mention_display_name,
            )
            continue

        if message_type == "file":
            file_name = message.get("fileName") or "uploaded_file"
            file_id = message.get("id")

            await _show_working_indicator(source_type, target_id, access_token, "⏳正在更新知識庫，請稍後")

            await _upload_and_reply(
                channel_id, reply_token, access_token, file_id, file_name,
            )

    return {"status": "ok"}


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
    push 而接受的取捨。"""
    status = "已完成"
    answer_text = ""
    try:
        messages = await ask_question(channel_id, question, sender_user_id)
        if not messages:
            messages = ["⚠️ 沒有取得回覆內容，請稍後再試。"]
        answer_text = "\n".join(messages)
        if any(msg.startswith("⚠️") for msg in messages):
            status = "異常"
        await reply_text(reply_token, access_token, messages, mention_user_id, mention_display_name)
    except Exception as e:
        logger.error(f"[{channel_id}] Error: {e}")
        status = "異常"
        answer_text = f"系統發生錯誤：{e}"
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

    await _record_interaction(
        channel_id, group_id, room_id, sender_user_id, access_token,
        question, answer_text, status, display_name=mention_display_name,
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
    status = "需人工聯絡"
    try:
        contact_query = _CONTACT_INFO_QUERY_TEMPLATE.format(question=question)
        messages = await ask_question(channel_id, contact_query, line_user_id=None)
        if any(m.startswith("⚠️") for m in messages):
            # 這是真正的 ask_question 失敗（筆記本未綁定、API 錯誤等）
            # ——而不是「沒有比對到廠商」。仍然給使用者禮貌的通用
            # 回覆，但另外標記狀態，避免在追蹤紀錄中看起來與正常的
            # 無比對結果一模一樣。
            logger.error(f"[{channel_id}] Contact info lookup returned an error: {messages}")
            status = "異常"
            await reply_text(reply_token, access_token, reply, mention_user_id, mention_display_name)
        else:
            vendor_messages = [m for m in messages if m.startswith("【")]
            if vendor_messages:
                intro = f"{_PURCHASE_INTENT_PREFIX}\n\n{_PURCHASE_INTENT_CONTACT_LEAD_IN}"
                reply_messages = [intro] + vendor_messages
                await reply_text(reply_token, access_token, reply_messages, mention_user_id, mention_display_name)
                reply = "\n".join(reply_messages)
            else:
                await reply_text(reply_token, access_token, reply, mention_user_id, mention_display_name)
    except Exception as e:
        logger.error(f"[{channel_id}] Error looking up vendor contact info: {e}")
        status = "異常"
        try:
            await reply_text(reply_token, access_token, reply, mention_user_id, mention_display_name)
        except Exception as reply_error:
            logger.error(f"[{channel_id}] Fallback reply also failed: {reply_error}")
            await notify_admin(
                f"silent:{channel_id}",
                f"⚠️ 頻道 {channel_id} 的使用者完全沒收到任何回覆"
                f"（原始錯誤：{e}；fallback 回覆也失敗：{reply_error}）",
            )

    await _record_interaction(
        channel_id, group_id, room_id, sender_user_id, access_token,
        question, reply, status, display_name=mention_display_name,
    )


async def _record_interaction(
    channel_id: str,
    group_id: str | None,
    room_id: str | None,
    sender_user_id: str | None,
    access_token: str,
    question: str,
    answer: str,
    status: str,
    display_name: str | None = None,
):
    """將一次問答記錄成使用者 Drive 資料夾中的文字檔，並在追蹤
    Sheet 中新增一列。盡力而為：這裡的失敗永遠不會影響 LINE
    對話，只會記錄在伺服器端。

    ``display_name`` 可以傳入已經解析好的值（例如群組/聊天室
    在建立回覆的 @mention 時已經查過一次），藉此省去重複查詢
    個人資料；1:1 聊天事先沒有解析過，所以會在這裡重新查詢。

    由呼叫端 await（而不是 fire-and-forget），讓它仍屬於進行中
    webhook 請求的一部分——`docker compose up -d` 重啟時
    （例如 tunnel watchdog 更換 tunnel 時觸發）會送出 SIGTERM，
    並給進行中的請求一段寬限期完成；孤兒背景任務不在這個
    保護範圍內，會在寫入途中直接被砍掉，導致紀錄悄悄遺失。
    """
    if not sender_user_id:
        return  # 沒有穩定的身分識別，就無法建立個人專屬資料夾

    try:
        if display_name is None:
            display_name = await get_display_name(access_token, sender_user_id, group_id, room_id)
        folder_id, folder_link = await google_log_service.get_or_create_user_folder(
            channel_id, sender_user_id, display_name
        )
        now = datetime.now(_BEIJING_TZ)
        # 兩者彼此獨立——都只需要上一步得到的 folder_id/folder_link，
        # 誰都不依賴另一個的結果——所以並行執行它們，而不是在使用者
        # 已經收到回覆之後，還要依序等待兩次 Google API 往返。
        # 使用 return_exceptions=True，讓其中一個失敗時，另一個仍能
        # 在這個協程回傳前完成——若用預設的 return_exceptions=False，
        # gather() 會在第一個 awaitable 失敗時立刻拋出例外，讓另一個
        # 變成孤兒任務繼續執行，這正是本函式文件中提到要避免的
        # 「在 SIGTERM 時寫入途中被砍掉」的風險。
        results = await asyncio.gather(
            google_log_service.save_text_record(folder_id, question, answer, now),
            google_log_service.append_sheet_row(now, display_name, folder_link, status),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
    except Exception as e:
        logger.error(f"[{channel_id}] Failed to record interaction to Google: {e}")
        await notify_admin(
            "google_log",
            f"⚠️ Google 紀錄寫入失敗（頻道 {channel_id}）：{e}",
        )


async def _upload_and_reply(
    channel_id: str,
    reply_token: str,
    access_token: str,
    file_id: str | None,
    file_name: str,
):
    """更新知識庫，並透過 reply（而非 push）確認完成——與
    ``_ask_and_reply`` 相同的額度考量。"""
    try:
        if not file_id:
            raise ValueError("缺少 LINE 附件 ID")

        file_bytes = await download_content(file_id, access_token)
        message = await update_knowledge_base(channel_id, file_bytes, file_name)
        await reply_text(reply_token, access_token, message)
    except Exception as e:
        logger.error(f"[{channel_id}] Upload error: {e}")
        try:
            await reply_text(reply_token, access_token, f"⚠️ 更新知識庫失敗：{e}")
        except Exception as reply_error:
            logger.error(f"[{channel_id}] Fallback reply also failed: {reply_error}")
            await notify_admin(
                f"silent:{channel_id}",
                f"⚠️ 頻道 {channel_id} 的檔案上傳者完全沒收到任何回覆"
                f"（原始錯誤：{e}；fallback 回覆也失敗：{reply_error}）",
            )