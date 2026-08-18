import json
import logging
import os
import asyncio
import mimetypes
import re
import tempfile
import time
from pathlib import Path
from notebooklm import NotebookLMClient, ChatGoal
from markitdown import MarkItDownException
from services.alert_service import notify_admin
from services.crypto_service import encrypt, decrypt
from services.doc_converter import convert_to_markdown
from services.text_formatter import vendor_from_title
from database import DB
import aiosqlite

logger = logging.getLogger(__name__)

# 避免並行覆寫環境變數的鎖
_nlm_lock = asyncio.Lock()

# 重複使用的 NotebookLMClient session，以 channel_id 為 key。
# ask_question 會在每一則 LINE 訊息時被呼叫，而透過 _run_with_auth
# 開啟一個 session 會建立一個全新的 httpx.AsyncClient（連線池是冷的，
# 所以第一次 RPC 要付出一次全新的 TCP/TLS 交握成本），關閉時又要把
# cookie jar 寫回磁碟——如果同一個 channel 過一會兒又要問下一個
# 問題，這些都是純粹的額外開銷。只有這條高頻的提問路徑會用到這個
# 快取；不常發生的管理操作（上傳、設定、列表）仍然走 _run_with_auth
# 每次呼叫都開啟再關閉的路徑，因為那邊多一次交握相對於這些操作
# 本身的延遲來說可以忽略不計，而且把快取的作用範圍限縮得窄一點，
# 也能限制快取 bug 一旦出現時的影響範圍。
_client_cache: dict[str, tuple] = {}
_client_cache_lock = asyncio.Lock()


async def _get_cached_client(channel_id: str, auth_json: dict):
    """回傳此 channel 已快取、已開啟的 NotebookLMClient，
    首次使用時才建立。"""
    async with _client_cache_lock:
        cached = _client_cache.get(channel_id)
        if cached is not None:
            return cached[1]

        client_ctx = NotebookLMClient.from_storage()
        async with _nlm_lock:
            old = os.environ.get("NOTEBOOKLM_AUTH_JSON")
            os.environ["NOTEBOOKLM_AUTH_JSON"] = json.dumps(auth_json)
            try:
                client = await client_ctx.__aenter__()
            finally:
                if old is None:
                    os.environ.pop("NOTEBOOKLM_AUTH_JSON", None)
                else:
                    os.environ["NOTEBOOKLM_AUTH_JSON"] = old

        _client_cache[channel_id] = (client_ctx, client)
        return client


async def _invalidate_client(channel_id: str) -> None:
    """捨棄並關閉某個 channel 已快取的 session，例如因為它已經
    失效，或是因為該 channel 的認證資訊剛被 bind_nlm 取代。"""
    async with _client_cache_lock:
        cached = _client_cache.pop(channel_id, None)
    if cached is not None:
        client_ctx, _ = cached
        try:
            await client_ctx.__aexit__(None, None, None)
        except Exception:
            logger.warning("Failed to close stale NotebookLM session for channel %s", channel_id)


async def aclose_all_clients() -> None:
    """關閉所有已快取的 NotebookLM session。應在應用程式關閉時呼叫。"""
    async with _client_cache_lock:
        cached_items = list(_client_cache.items())
        _client_cache.clear()
    for channel_id, (client_ctx, _) in cached_items:
        try:
            await client_ctx.__aexit__(None, None, None)
        except Exception:
            logger.warning("Failed to close NotebookLM session for channel %s", channel_id)

# client.sources.list(notebook_id)結果的快取，以 notebook_id 為 key。
# ask_question 幾乎每一個問題都需要 id->title 對照表來標註引用來源，
# 但來源實際上只會透過 replace_sources_for_notebook 改變（它會讓
# 對應的快取項目失效）——所以每一個問題都重新抓一次完整清單，
# 大多數時候都是純粹多餘的一次 API 往返。
_sources_cache: dict[str, list] = {}


def _invalidate_sources_cache(notebook_id: str) -> None:
    _sources_cache.pop(notebook_id, None)


# 針對「完全相同問題文字」的答案快取，以 (channel_id, 問題文字)
# 為 key。同一個問題重問 NotebookLM，生成時間本身就有很大的變異
# （實測同一句話問兩次，一次 191 秒、一次 48 秒），群組裡又常常有
# 好幾個人各自問到類似或完全相同的問題（例如「你們家有現車嗎」）
# ——與其每次都重新付出這個生成成本，一段時間內完全相同的問題直接
# 回傳上一次的答案。只在「這次提問沒有延續任何既有對話」時才使用
# 快取（見 ask_question 裡 conversation_id is None 的判斷）：一旦是
# 延續某個使用者自己對話串的追問，答案就跟那個人前面問過什麼有關，
# 不能被別人的、或這個人自己更早、不相干的快取答案取代。
#
# TTL 拉到 30 分鐘（原本 5 分鐘）：知識庫內容不會在這個時間尺度內
# 變動（更新知識庫是明確的上傳動作，不是背景自動變化），拉長快取
# 命中窗口純粹是換取更多次重複問題不必再等一次生成，沒有額外的
# 資料新鮮度代價。
_ANSWER_CACHE_TTL_SECONDS = 1800
_answer_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}

# 快取 key 只比對「拿掉標點符號、空白，以及句尾語氣助詞之後」的問題
# 文字，而不是逐字比對——群組裡常有好幾個人問法幾乎一樣、只差在
# 標點或語氣（「你們家有現車嗎」「你們家有現車嗎？」「你們家有 現車
# 嗎」），逐字比對會讓這些其實同一個問題的變體，各自重新付出一次
# 生成成本。
#
# 刻意不處理會改變句型結構的差異（例如「有沒有」vs「有」、「可以」
# vs「能不能」）——這裡的目標只是消掉純粹的標點/語氣雜訊，不是做
# 語意層級的問題等價判斷；貿然合併句型不同的問法，一旦誤判就會讓
# 使用者收到答非所問的快取答案，比多等一次生成更糟。
_CACHE_KEY_PUNCTUATION_RE = re.compile(
    r"[，。！？、；：「」『』（）()\[\]【】～~,.;:!?\"'`\-_\s]+"
)
_CACHE_KEY_TRAILING_PARTICLES = ("嗎", "呢", "啊", "呀", "喔", "唄", "吧", "囉", "嘛", "耶", "捏")


def _normalize_question_for_cache_key(question: str) -> str:
    normalized = _CACHE_KEY_PUNCTUATION_RE.sub("", question)
    trimmed = True
    while trimmed:
        trimmed = False
        for particle in _CACHE_KEY_TRAILING_PARTICLES:
            if normalized.endswith(particle):
                normalized = normalized[: -len(particle)]
                trimmed = True
    return normalized


def _get_cached_answer(channel_id: str, question: str) -> list[str] | None:
    key = (channel_id, _normalize_question_for_cache_key(question))
    cached = _answer_cache.get(key)
    if cached is None:
        return None
    expires_at, messages = cached
    if time.time() >= expires_at:
        _answer_cache.pop(key, None)
        return None
    return messages


def _save_cached_answer(channel_id: str, question: str, messages: list[str]) -> None:
    key = (channel_id, _normalize_question_for_cache_key(question))
    _answer_cache[key] = (time.time() + _ANSWER_CACHE_TTL_SECONDS, messages)


# 這是在 NotebookLM 筆記本層級強制執行的優先指令（不只是
# routers/webhook.py 中 LINE 端的關鍵字過濾），讓模型本身即使在
# 問題沒有比對到關鍵字清單時，也會主動拒答這些主題。
RESTRICTED_TOPIC_CUSTOM_PROMPT = (
    "你是僅供諮詢用途的客服助理，以下規則優先於所有其他指示：\n"
    "只有當使用者問題的「主要目的」就是交易本身時才需要拒答，包括：詢問價格、報價、"
    "折扣、付款方式、訂金、簽約、下單、收購價；要求代為下單、收款或代購；詢問授權、"
    "加盟、經銷資格；要求你承諾或保證任何結果。\n"
    "以下情況即使句子中出現「買」「賣」「這台車」等字眼，仍然要當作一般諮詢正常回答，"
    "不可拒答：詢問車輛規格、配備、功能、性能等技術資訊；詢問是否有特定年份、車型、"
    "顏色的現車或庫存；詢問展示中心、服務據點、保固、保養等一般諮詢。\n"
    "只有在確定使用者的核心意圖就是交易本身時，才只回覆：「本機器人僅供諮詢用途，"
    "不可以回答有關交易、買賣、授權、承諾等事宜。」\n"
    "回答結尾不要主動加上任何邀請使用者繼續提問的建議句，包括但不限於"
    "「如果想進一步比較...歡迎直接提供具體的車型名稱重新提問」「如果想比較 A 與 B "
    "的差異，歡迎直接告訴我」這種提示使用者用具體車型、廠牌重新提問的句子，也不要用"
    "「需要我...嗎？」「要不要我幫你...？」這種只能回「好」或「不用」的是非題來問"
    "——這個機器人是在 LINE 群組裡運作，使用者若只回覆「好」這種不含任何車輛相關"
    "字詞的單字，系統不會辨識為有效訊息、也就不會真的傳到你這裡，等於使用者對著"
    "空氣回答，得不到任何後續回應；就算問句本身不是單純的是非題，這種結尾句也只是"
    "多餘的重複，使用者本來就可以隨時直接追問。回答在實際內容講完就結束，不要額外"
    "加一句總結、提醒或邀請使用者繼續提問的結尾句。\n"
    "當使用者要求比較多個車型、或整理跨多個廠商來源的資訊時，請一律使用 Markdown "
    "表格呈現（列標籤放比較項目，欄位放各車型/廠商），絕對不要用「1. 2. 3.」編號"
    "段落呈現——不管編號底下接的是條列項目，還是像「1. 車身結構與空間定位：」這種"
    "編號小標題搭配一般敘述句，兩種都不行：這個系統會依照每段內容引用的來源自動"
    "分派到不同廠商的訊息，編號段落只要提到某一家廠商的名稱、引用到該廠商的來源，"
    "就會被整段拆到獨立的訊息裡、跟其他編號段落斷開，導致這則回答被腰斬成好幾則"
    "訊息、讀者完全看不出這些編號段落本來是同一組比較分析（真實發生過的案例："
    "四點編號分析寫到第四點提到某家廠商，整段被單獨拆成一則訊息，跟前三點斷開）；"
    "Markdown 表格則會被完整保留、不會被拆散。\n"
    "表格的欄位名稱（廠商/車商那一列）一定要用來源中記載的真實全名"
    "（例如「中彰投汽車有限公司」「正峰汽車商行」），絕對不可以為了讓表格看起來"
    "簡潔，而簡化成「車商 A」「車商 B」「經銷商1」這種匿名化的代稱——這個機器人存在"
    "的目的就是要讓使用者知道哪一家真實的廠商有他要的車，用代稱會讓使用者完全無法"
    "知道要去哪裡找車，即使廠商全名很長、欄位變得比較寬也一樣要用全名。\n"
    "只要一則回答會需要同時引用 4 家以上廠商的來源資料，不論使用者的提問是完全"
    "沒有指定條件（例如「有哪些現車」），還是已經指定了廠牌、顏色、里程等條件、"
    "只是篩選結果剛好分散在 4 家以上廠商（例如「各家豐田車的價格差異」「我要跑很遠"
    "的、白色的」這類問題），都算數，以下是硬性規定，優先順序高於「盡可能完整回答"
    "使用者問題」這個一般傾向：\n"
    "但如果使用者問的是「比較兩個以上特定車型本身的規格、配備差異」（例如「Altis"
    "跟 Corolla Cross 差在哪」「這兩款車有什麼不同」），這條規則不適用——這種問題"
    "使用者要的是車型跟車型之間的差異，不是各廠商的庫存分佈，即使符合條件的來源"
    "剛好分散在 4 家以上廠商，也要正常呈現完整的規格比較表；但表格方向要跟前面"
    "「整理跨多個廠商來源的資訊」那種表格相反——改成列標籤放各車型（例如"
    "「Toyota Corolla Altis」「Toyota Corolla Cross」），欄位放比較項目，讓同一"
    "款車型的所有規格集中呈現在同一列裡，方便使用者一次看完某一款車的完整資訊，"
    "而不是按規格項目分組、把同一款車的資訊拆散到不同列裡、跟另一款車交錯呈現。"
    "不能因為廠商數量超過門檻，就簡化成只有「廠商＋台數」、完全"
    "沒有比較到規格差異的表格，那樣答非所問。\n"
    "另外，如果使用者提問時明確使用「比較」「比較一下」「對照」這類字眼，要求把"
    "『同一款車型』在不同廠商的現車放在一起比較（例如「幫我用表格比較一下 3 家以上"
    "廠商的 Altis 現車」），代表使用者要的是逐台現車的詳細規格並排呈現，不是單純"
    "想知道哪些廠商有貨——這種情況同樣不適用「廠商＋台數」簡化表格，要正常列出"
    "每一台現車的詳細規格（年份、顏色、里程、配備等），用 Markdown 表格呈現。\n"
    "以下這條硬性規定，只限制使用者「沒有」明確要求逐台比較細節、單純想先知道有"
    "哪些現車、分佈在哪些廠商的清單式查詢（例如「有哪些現車」「各家豐田車的價格"
    "差異」這種只是想先掌握大概分佈狀況的問題）：\n"
    "1. 只能用一個簡短表格列出「廠商名稱」加上「符合條件的台數」，最多再加一項"
    "最關鍵的重點欄位（例如平均或最低價格），表格總欄位數不可超過 3 欄。\n"
    "2. 絕對不要在同一則回答裡展開列出每一台車輛的完整規格明細，也不要每個廠商"
    "各自列出配備、里程、年份等多個維度的完整比較。\n"
    "3. 回答結尾一定要主動詢問使用者想先深入了解哪一家廠商或哪一款車，再針對"
    "縮小後的範圍提供詳細資訊，不要一次把所有細節都生成出來。\n"
    "使用者如果接著用模糊代名詞追問（例如「其他的呢」「還有嗎」「那呢」），且沒有"
    "明確換成新的車型、廠牌或條件，預設視為延續上一句話所指定的具體範圍繼續追問，"
    "不要當成一個全新、不限範圍的問題——例如上一句話是在比較 Altis 與 Corolla Cross"
    "、且因為廠商數量超過門檻只列出了其中幾家，使用者追問「其他的呢？」指的通常是"
    "「這個比較裡還沒列出來的其他廠商」，仍然限定在 Altis 與 Corolla Cross 這兩款"
    "車型，不是重新問一個不限車型的現車總覽。只有使用者明確提到新的車型、廠牌或"
    "條件時，才視為換了主題、改用新的範圍作答。\n"
    "會有這條規定，是因為這類跨 4 家以上廠商的完整表格需要逐一比對大量資料，"
    "生成常常要 3-5 分鐘以上，使用者在 LINE 上等待體驗會很差——寧可先給精簡總覽、"
    "之後再追問，也不要一次生成涵蓋所有廠商細節的完整報告。\n"
    "文字敘述中每提到一次廠商名稱（例如「由『某某汽車』提供」「僅有一台，"
    "由...」），都必須跟緊接在後面的引用編號 [n] 實際對應的來源一致，"
    "不可以憑印象把資料寫成另一家廠商——尤其是「符合條件的僅有一台」"
    "「唯一一台」這類只鎖定單一結果的回答，公布廠商名稱之前務必重新核對"
    "該筆資料真正引用的來源是哪一份文件，寧可多花一點時間核對，也不可以"
    "讓廠商名稱跟實際引用的來源不符——這種錯誤會讓使用者聯繫錯誤的車商，"
    "比回答慢一點嚴重得多。\n"
    "使用者的問題如果跟車輛、車商、購車完全無關（例如天氣、颱風、新聞、"
    "股票等主題），一律直接說明「目前的來源文件中沒有相關資訊」，並且"
    "提示使用者可以詢問車輛規格、配備、庫存等問題，除此之外不要做任何"
    "其他事情——絕對不要主動提議、或在使用者要求下真的去執行任何形式的"
    "網路搜尋（不論是回覆「搜尋 ...」這種引導語，還是真的搜尋後生成"
    "研究報告、匯入卡片等內容）。這個機器人只能透過純文字傳送到 LINE，"
    "網路搜尋功能產生的匯入卡片等內容無法正常顯示成文字，使用者只會看到"
    "一大段亂碼，比乾脆說「沒有相關資訊」體驗更差。\n"
    "回答一開始不要加上「...主要差異整理如下：」「以下為您整理...」"
    "「根據來源資料，為您整理如下：」這類單純預告內容即將出現、本身沒有"
    "任何實質資訊的開場白句子——直接以表格或結果內容開頭即可，使用者"
    "在意的是答案本身，不需要先被告知「答案接下來會出現」。\n"
    "使用者指定的條件（例如顏色、里程、變速系統等）沒有任何現車完全"
    "符合、只能提供最接近的替代選項時，開頭只需一句話點出「沒有完全"
    "符合條件的現車，最接近的如下：」，不要重述使用者原本開出的每一項"
    "篩選條件，也不要另外用一整句話解釋這台替代車輛具體是哪幾項規格"
    "對不上——這些差異使用者自己比對後面列出的規格明細就看得出來，"
    "不需要事先用文字複述一次。點出「沒有完全符合、最接近如下」之後，"
    "直接接續列出該替代車輛的規格明細即可。\n"
    "任何情境下，只要是在列出單一一台車輛的規格明細（不論是前述沒有完全"
    "符合條件時的最接近替代車輛，還是其他情境下列出的單一現車），一律"
    "用「欄位名稱：值」的格式、一個欄位一行，例如「車型：X-TRAIL」"
    "「年份：2016」「顏色：灰色」這樣緊密列出，欄位與欄位之間不要留"
    "空行；絕對不要把欄位名稱單獨包成「【欄位名稱】」自成一行、值再"
    "另起一行或空一行接續——「【　】」這種括號標題格式只保留給廠商"
    "名稱或車輛名稱這種區塊標題使用，不要用在個別規格欄位上。\n"
    "回答如果會依廠商分成好幾則訊息呈現，開頭也不要另外加一句總覽有"
    "哪些廠商、共幾家的開場白（例如「目前有福大汽車、中彰投汽車有限"
    "公司與安心汽車三家車商擁有 Nissan Kicks：」）——這句話雖然本身"
    "有實質資訊（廠商名稱、家數），但後面每一家廠商自己的內容前面都"
    "會自動帶上「該廠商名稱」當標題，等於這句總覽的資訊在後面會逐一"
    "重複出現一次，先講一次總覽並不會讓使用者更快知道答案，只是多一"
    "段要往下滑才看得到實際內容的文字。直接從第一家廠商的內容開始"
    "回答即可，不需要先列出所有廠商名稱或家數。"
)


async def configure_restricted_topic_guard(storage_state: dict, notebook_id: str) -> None:
    """將筆記本的自訂聊天人設設定為拒絕討論交易相關主題。"""

    async def _configure(client):
        await client.chat.configure(
            notebook_id,
            goal=ChatGoal.CUSTOM,
            custom_prompt=RESTRICTED_TOPIC_CUSTOM_PROMPT,
        )

    await _run_with_auth(storage_state, _configure)


async def _run_with_auth(auth_json: dict, coro_fn):
    """以指定的認證資訊執行一個 notebooklm-py 操作。

    NOTEBOOKLM_AUTH_JSON 是一個整個行程共用的環境變數，而
    notebooklm-py 只會在開啟 session 時（在 ``__aenter__`` 內部）
    讀取它一次——一旦開啟完成，認證資訊就會綁定在該 client
    實例上，之後不會再去讀取這個環境變數。所以只有這個交握
    步驟需要與其他 channel 在背後互相調換環境變數的行為做序列化；
    真正耗時的部分（提問、上傳來源等）都在鎖外執行，這樣不同
    channel 的請求才不會互相排隊卡住彼此。
    """
    client_ctx = NotebookLMClient.from_storage()
    async with _nlm_lock:
        old = os.environ.get("NOTEBOOKLM_AUTH_JSON")
        os.environ["NOTEBOOKLM_AUTH_JSON"] = json.dumps(auth_json)
        try:
            client = await client_ctx.__aenter__()
        finally:
            if old is None:
                os.environ.pop("NOTEBOOKLM_AUTH_JSON", None)
            else:
                os.environ["NOTEBOOKLM_AUTH_JSON"] = old

    try:
        return await coro_fn(client)
    finally:
        await client_ctx.__aexit__(None, None, None)


async def bind_nlm(channel_id: str, storage_state: dict):
    """加密並儲存 NLM 的 storage_state，然後取得第一個筆記本 ID。"""
    encrypted = encrypt(storage_state)

    notebooks = await list_notebooks(storage_state)
    notebook_id = notebooks[0]["id"] if notebooks else None

    if notebook_id:
        await configure_restricted_topic_guard(storage_state, notebook_id)

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE channels SET nlm_auth_json_encrypted=?, notebook_id=? WHERE channel_id=?",
            (encrypted, notebook_id, channel_id),
        )
        await db.commit()

    # 這個 channel 任何已快取的 session 都是用舊的認證資訊開啟的——
    # 把它捨棄掉，讓下一次 ask_question 用新的認證資訊重新開啟一個。
    await _invalidate_client(channel_id)

    return notebook_id, notebooks


async def list_notebooks(storage_state: dict) -> list[dict]:
    """回傳所有筆記本的 {id, title} 清單。"""
    async def _list(client):
        notebooks = await client.notebooks.list()
        return [{"id": nb.id, "title": nb.title} for nb in notebooks]

    return await _run_with_auth(storage_state, _list)


async def list_notebooks_for_channel(channel_id: str) -> list[dict]:
    """使用某個 channel 已儲存的 cookie 取得筆記本清單。"""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nlm_auth_json_encrypted FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        row = await cur.fetchone()

    if not row or not row["nlm_auth_json_encrypted"]:
        return []

    storage_state = decrypt(row["nlm_auth_json_encrypted"])
    return await list_notebooks(storage_state)


async def select_notebook(channel_id: str, notebook_id: str):
    """更新某個 channel 所選擇的筆記本。"""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nlm_auth_json_encrypted FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        row = await cur.fetchone()
        await db.execute(
            "UPDATE channels SET notebook_id=? WHERE channel_id=?",
            (notebook_id, channel_id),
        )
        await db.commit()

    if row and row["nlm_auth_json_encrypted"]:
        storage_state = decrypt(row["nlm_auth_json_encrypted"])
        await configure_restricted_topic_guard(storage_state, notebook_id)


async def replace_sources_for_notebook(
    storage_state: dict,
    notebook_id: str,
    file_bytes: bytes,
    file_name: str,
    mime_type: str | None = None,
    title: str | None = None,
) -> str:
    """將檔案上傳到筆記本。

    若已存在同一家廠商（用檔名判斷）的來源，會先刪除它，再以新
    上傳的檔案取代。不同廠商的來源則不受影響，讓知識庫能持續累積
    檔案，而不是每次上傳都被清空重來。
    """

    async def _replace(client):
        existing_sources = await client.sources.list(notebook_id)
        uploaded_title = title or Path(file_name).name
        uploaded_vendor = vendor_from_title(uploaded_title)

        # 檔名慣例是 `{廠商}{日期}`，同一家廠商每天上傳的檔名都不
        # 一樣，不能用完整檔名逐字比對（那樣永遠對不上，舊檔案就
        # 會一直堆積、覆蓋機制形同虛設）。改用廠商名稱比對，忽略
        # 日期差異。檔名不符合「廠商+日期」慣例、抓不出廠商
        # （歸類為「未分類」）的來源，才退回用完整檔名逐字比對，
        # 避免彼此誤判成同一家廠商而被錯誤覆蓋。
        if uploaded_vendor != "未分類":
            duplicate_sources = [
                s for s in existing_sources
                if vendor_from_title(s.title) == uploaded_vendor
            ]
        else:
            duplicate_sources = [
                s for s in existing_sources if s.title == uploaded_title
            ]

        with tempfile.NamedTemporaryFile(
            suffix=Path(file_name).suffix or ".txt",
            delete=False,
        ) as tmp_file:
            tmp_file.write(file_bytes)
            temp_path = tmp_file.name

        try:
            source = await client.sources.add_file(
                notebook_id,
                temp_path,
                mime_type=mime_type,
                title=uploaded_title,
                wait=True,
                wait_timeout=180.0,
            )
            for source_item in duplicate_sources:
                await client.sources.delete(notebook_id, source_item.id)

            _invalidate_sources_cache(notebook_id)

            final_title = source.title or uploaded_title
            if duplicate_sources:
                return f"✅ 知識庫已更新。已覆蓋同名舊檔案並上傳：{final_title}"
            return f"✅ 知識庫已更新。已新增檔案：{final_title}"
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass

    return await _run_with_auth(storage_state, _replace)


async def update_knowledge_base(
    channel_id: str,
    file_bytes: bytes,
    file_name: str,
    title: str | None = None,
) -> str:
    """將上傳的檔案轉換為 Markdown，再上傳到該 channel 的筆記本。"""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nlm_auth_json_encrypted, notebook_id FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        row = await cur.fetchone()

    if not row or not row["nlm_auth_json_encrypted"] or not row["notebook_id"]:
        return "⚠️ NotebookLM 尚未綁定，請聯繫管理員完成設定。"

    try:
        markdown_bytes, markdown_name = convert_to_markdown(file_bytes, file_name)
    except MarkItDownException as e:
        return (
            f"⚠️ 檔案轉換失敗，無法上傳：{e}\n"
            "請確認檔案格式是否支援（PDF、Word、PowerPoint、Excel、純文字等）。"
        )

    storage_state = decrypt(row["nlm_auth_json_encrypted"])
    notebook_id = row["notebook_id"]

    try:
        return await replace_sources_for_notebook(
            storage_state,
            notebook_id,
            markdown_bytes,
            markdown_name,
            mime_type="text/markdown",
            title=title,
        )
    except Exception as e:
        return f"⚠️ 更新知識庫失敗：{e}"


async def _set_health_status(channel_id: str, status: str) -> None:
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE channels SET nlm_health_status=? WHERE channel_id=?",
            (status, channel_id),
        )
        await db.commit()


async def check_all_channels_health() -> None:
    """主動探測每個已綁定 channel 的 NotebookLM session，讓失效的
    cookie 能依排程被發現，而不是要等到某個學生剛好先問了問題
    （然後還得等有人注意到投訴）。每個 channel 都是獨立檢查的——
    某一個 channel 的 session 失效，不應該讓其餘 channel 的檢查
    也跟著停下來。

    通知只在狀態真正從 healthy（或從未檢查過）轉變成 expired 的
    那一刻發送一次——同一個 channel 持續壞著的話，接下來每半小時
    再檢查到同樣的失敗，不會重複告警，直到它恢復正常、之後又再次
    壞掉為止，才會發出下一次通知。

    這支排程只是保底：真正有學生在問問題的 channel，ask_question()
    一撞到認證失敗就會立刻把狀態標成 expired（見該函式），不需要
    等這裡的排程輪到它——這裡存在的意義是涵蓋「暫時沒人問問題」
    的 channel，讓它的失效不會一直沒被發現，直到半小時後才被這裡
    抓到為止。
    """
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT channel_id, nlm_auth_json_encrypted, nlm_health_status FROM channels "
            "WHERE nlm_auth_json_encrypted IS NOT NULL AND notebook_id IS NOT NULL"
        )
        rows = await cur.fetchall()

    for row in rows:
        channel_id = row["channel_id"]
        was_expired = row["nlm_health_status"] == "expired"
        try:
            storage_state = decrypt(row["nlm_auth_json_encrypted"])
            await list_notebooks(storage_state)
        except Exception as e:
            logger.warning(f"[{channel_id}] NotebookLM health check failed: {e}")
            await _set_health_status(channel_id, "expired")
            if not was_expired:
                # 剛剛才變成 expired（不是本來就已知壞掉、還沒修好）——
                # 這是真正值得發一次通知的「狀態轉換」瞬間。
                await notify_admin(
                    f"nlm-health:{channel_id}",
                    f"⚠️ 定期健康檢查發現頻道 {channel_id} 的 NotebookLM 登入已失效：{e}\n"
                    "請重新 notebooklm login 並更新綁定，目前還沒有學員回報，及早處理可避免影響到他們。",
                    # 這裡已經用 DB 裡的 healthy/expired 狀態轉換做過精確的
                    # 防重複判斷了，不需要再疊加 notify_admin 自己那套以
                    # 時間為準的冷卻機制——否則短時間內真的又壞一次時，
                    # 會被那個冷卻機制誤擋下來。
                    cooldown_seconds=0,
                )
        else:
            if was_expired:
                await _set_health_status(channel_id, "healthy")


async def _get_conversation_id(channel_id: str, line_user_id: str) -> str | None:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT conversation_id FROM user_conversations WHERE channel_id=? AND line_user_id=?",
            (channel_id, line_user_id),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def _save_conversation_id(channel_id: str, line_user_id: str, conversation_id: str) -> None:
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """
            INSERT INTO user_conversations (channel_id, line_user_id, conversation_id, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel_id, line_user_id)
            DO UPDATE SET conversation_id=excluded.conversation_id, updated_at=CURRENT_TIMESTAMP
            """,
            (channel_id, line_user_id, conversation_id),
        )
        await db.commit()


# notebooklm-py 在偵測到登入 session 已失效時，固定會拋出這個訊息的
# ValueError（見該套件 `_auth/refresh.py`），不是它自訂的例外類型，
# 所以只能比對訊息文字。cookie 失效目前還會不定期發生，在修好之前
# 不該讓使用者看到這種內部技術訊息。
_AUTH_FAILURE_SIGNATURE = "Authentication expired or invalid"
_AUTH_FAILURE_USER_REPLY = (
    "不好意思，系統目前登入狀態異常，暫時無法查詢車輛資訊，"
    "我們已經在處理中，請稍後再試一次 🙏"
)


async def ask_question(channel_id: str, question: str, line_user_id: str | None = None) -> list[str]:
    """載入該 channel 的 cookie，向 NotebookLM 提問，並回傳可直接
    用於 LINE 的答案訊息。

    當有提供 ``line_user_id`` 時，這個問題會延續該使用者在這個
    筆記本上自己的 NotebookLM 對話串（跨請求持久保存），讓後續
    追問能帶著先前的上下文。若沒有提供（例如 LINE 不會給匿名
    群組成員一個 id），則每個問題都會獨立提問。
    """
    from services.text_formatter import (
        format_for_line,
        build_answer_messages,
        citation_numbers_in,
        count_unclassified_drops,
        vendor_from_title,
    )

    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nlm_auth_json_encrypted, notebook_id, nlm_health_status "
            "FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        row = await cur.fetchone()

    if not row or not row["nlm_auth_json_encrypted"] or not row["notebook_id"]:
        return ["⚠️ NotebookLM 尚未綁定，請聯繫管理員完成設定。"]

    storage_state = decrypt(row["nlm_auth_json_encrypted"])
    notebook_id = row["notebook_id"]
    was_expired = row["nlm_health_status"] == "expired"

    conversation_id = (
        await _get_conversation_id(channel_id, line_user_id) if line_user_id else None
    )
    logger.info(
        f"[{channel_id}] user={line_user_id!r} 這次提問帶入的 conversation_id={conversation_id!r}"
    )

    if conversation_id is None:
        cached = _get_cached_answer(channel_id, question)
        if cached is not None:
            logger.info(f"[{channel_id}] 命中答案快取，不重新查詢 NotebookLM：{question!r}")
            return cached

    async def _ask(client):
        result = await client.chat.ask(notebook_id, question, conversation_id=conversation_id)
        logger.info(
            f"[{channel_id}] user={line_user_id!r} NotebookLM 回傳延續用的 "
            f"conversation_id={result.conversation_id!r}"
        )
        # 建立 source_id → title 的對照表
        source_map = {}
        if result.references:
            sources = _sources_cache.get(notebook_id)
            if sources is None:
                sources = await client.sources.list(notebook_id)
                _sources_cache[notebook_id] = sources
            id_to_title = {s.id: s.title for s in sources}
            for ref in result.references:
                if ref.citation_number is not None and ref.source_id in id_to_title:
                    source_map[ref.citation_number] = id_to_title[ref.source_id]
        logger.info(
            f"[{channel_id}] raw answer (for vendor-split debugging): {result.answer!r} "
            f"source_map={source_map!r}"
        )
        return result.answer, result.conversation_id, source_map

    try:
        try:
            client = await _get_cached_client(channel_id, storage_state)
            answer, new_conversation_id, source_map = await _ask(client)
        except Exception:
            # 快取的 session 可能已經失效（連線中斷、cookie 過期）——
            # 在把錯誤呈現給使用者之前，先捨棄它並用一個全新開啟的
            # session 重試一次。
            await _invalidate_client(channel_id)
            client = await _get_cached_client(channel_id, storage_state)
            answer, new_conversation_id, source_map = await _ask(client)

        # NotebookLM 的 references 偶爾會漏給答案文字裡實際用到的某個
        # 引用編號（source_map 缺一個 key，但文字裡仍出現對應的 [n]）。
        # 這種殘缺直接拿去做廠商分割，會讓那個編號所屬的整段內容因為
        # 「查不到廠商」而被誤併入前一個廠商的訊息裡。與其把這種殘缺
        # 答案送給使用者，先重問一次——同一個問題重問時偶爾成功、
        # 偶爾失敗，代表這是上游的偶發問題，不是每次都會發生，值得
        # 賭一次重試。只重試一次，重問後不論是否仍然殘缺都直接採用，
        # 避免無限重問。
        if citation_numbers_in(answer) - source_map.keys():
            try:
                answer, new_conversation_id, source_map = await _ask(client)
            except Exception:
                pass  # 重問失敗就沿用原本（雖然殘缺）的答案，好過整個問題失敗

        if line_user_id and new_conversation_id:
            await _save_conversation_id(channel_id, line_user_id, new_conversation_id)

        # 用「這個筆記本全部來源檔名」反推出的廠商名單，而不是只看
        # 這則答案實際引用到的那幾個——真實發生過模型寫錯的廠商名稱
        # 剛好是這則答案完全沒有引用到的另一家廠商，只看 source_map
        # 的話，連辨識出「這是一個已知廠商名稱、只是寫錯地方了」都
        # 做不到。sources_cache 通常已經有值（前面 _ask 只要 references
        # 非空就會填入）；萬一這次答案完全沒有引用來源，就退回空集合，
        # 訂正函式在沒有已知廠商名單時本來就是無害的無動作。
        all_sources = _sources_cache.get(notebook_id) or []
        known_vendors = {vendor_from_title(s.title) for s in all_sources}

        formatted = format_for_line(answer)
        dropped = count_unclassified_drops(formatted, source_map, known_vendors)
        if dropped:
            # 使用者收到的答案就只是「少了一段」，完全沒有跡象顯示曾經
            # 有內容被拿掉——常見原因是有新上傳的知識庫來源檔名不符合
            # 「{廠商}_{日期}」命名慣例，導致內容查不到廠商而被捨棄。
            # 沒有這則告警的話，這種資料品質問題只能靠使用者自己發現
            # 答案怪怪的、再回報出來才會被注意到。
            await notify_admin(
                f"unclassified-drop:{channel_id}",
                f"⚠️ 頻道 {channel_id} 的回答中有 {dropped} 段內容因來源檔名無法歸屬到"
                "任何廠商（未分類）而被捨棄、未送給使用者。常見原因是有新上傳的知識庫"
                "來源檔名不符合「{廠商}_{日期}」命名慣例，請檢查最近上傳的來源檔名。",
            )
        messages = build_answer_messages(formatted, source_map, known_vendors)
        if conversation_id is None:
            _save_cached_answer(channel_id, question, messages)
        if was_expired:
            # 這次提問成功了，代表 cookie 其實已經活過來了（可能是
            # 套件自動換發、也可能是有人剛好在別處重新登入）——立刻
            # 把狀態改回 healthy，不要留著 expired 讓 nlm_cookie_refresh.py
            # 的偵測閘門誤以為還在壞、白白再刷新一次。
            await _set_health_status(channel_id, "healthy")
        return messages
    except Exception as e:
        await notify_admin(
            f"nlm:{channel_id}",
            f"⚠️ NotebookLM 查詢失敗（頻道 {channel_id}）：{e}\n"
            "常見原因是登入 cookie 失效，請確認是否需要重新 notebooklm login。",
        )
        if _AUTH_FAILURE_SIGNATURE in str(e):
            # 立刻把這個 channel 標成 expired，不必等 check_all_channels_health()
            # 下一次排程（最多半小時後）才發現——這是使用者真的問問題時
            # 當場撞到的失敗，是最即時的偵測時機。scripts/nlm_cookie_refresh.py
            # 會頻繁檢查這個欄位，一旦看到 expired 就會立刻嘗試刷新，
            # 不用再等它自己那個排程週期。
            await _set_health_status(channel_id, "expired")
            # cookie 失效目前還會不定期發生（管理員通常幾分鐘內就會手動
            # 重新登入修復），但在修好之前，使用者不該看到
            # 「Authentication expired or invalid...Run 'notebooklm login'」
            # 這種內部技術訊息——用罐頭訊息頂著，管理員那邊還是照樣
            # 會收到上面完整的原始錯誤，不影響除錯。
            return [_AUTH_FAILURE_USER_REPLY]
        return [f"⚠️ 查詢失敗：{e}"]
