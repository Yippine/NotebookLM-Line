import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LI_OPEN_RE = re.compile(r"<li>", re.IGNORECASE)
_LIST_TAG_RE = re.compile(r"</?(?:ul|ol|li)>", re.IGNORECASE)
_LEADING_BULLET_RE = re.compile(r"^(?:[•\-*]|\d+[.\)])\s+")


def _split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _clean_table_cell(cell: str) -> str:
    """整理表格儲存格內容：把 <br> 與 <li> 轉成縮排的接續行，
    其餘 <ul>/<ol>/</li> 標籤直接移除。子項目用「◦」而不是「•」——
    「•」是 `_BULLET_LINE_RE` 認得的項目符號字元，用同一個字元會讓
    這一行被依廠商分則邏輯當成獨立的項目符號原子處理，這正是要避免
    的（見 `_convert_markdown_tables` 的說明）。

    模型有時候不會用 <li> 標籤，而是直接在儲存格內用文字型的
    「•」「-」或「1.」條列多個子項目、中間用 <br> 換行——這是實際
    發生過的真實情況。<br> 轉成真正的換行之後，這些行如果原封不動
    保留原本的項目符號字元，一樣會被外層邏輯誤判成獨立原子、導致
    整張表格被拆散，所以要逐行檢查、把任何會被 `_BULLET_LINE_RE`
    比對到的開頭字元也一併換成「◦」。"""
    cell = _LI_OPEN_RE.sub("\n    ◦ ", cell)
    cell = _BR_TAG_RE.sub("\n    ", cell)
    cell = _LIST_TAG_RE.sub("", cell)
    lines = []
    for line in cell.split("\n"):
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        stripped = _LEADING_BULLET_RE.sub("◦ ", stripped)
        lines.append(indent + stripped)
    return "\n".join(lines).strip()


def _convert_markdown_tables(text: str) -> str:
    """把 Markdown 表格轉成 LINE 純文字讀得懂的條列格式。

    LINE 的文字訊息完全不支援表格語法——但 NotebookLM 遇到「比較兩台車」
    這類問題時，很自然會選擇用表格回答（有時儲存格裡還會夾雜 <br>、
    <ul><li> 這些 HTML 標籤）。若原封不動送出去，使用者看到的只會是
    一堆 "|"、"<br>"、"<li>"，比不轉換更難讀（這是實際發生過的真實
    情況，不是假設）。

    轉換後的整張表格會保持成單一個不含空行的區塊，刻意不用「•」開頭
    ——「依廠商分則」那套邏輯（見 `_split_by_vendor`）是以項目符號為
    最小單位、各自依引用編號分配到不同廠商的訊息。一張比較表本來就
    是要把多個廠商的資料放在同一個地方對照，如果轉成逐行項目符號，
    等於是把它交給那套邏輯拆散——不同欄位（例如「Toyota Corolla
    Altis」跟「Toyota Corolla Cross」那兩格）引用到的廠商編號通常不同，
    拆完之後同一張表的標題跟內容會散落在不同廠商的訊息氣泡裡，比留著
    "|" 原始語法更難讀（這也是實際發生過的真實情況）。保持不含空行的
    單一區塊，會讓它被當成一個整體，不會被項目符號規則打散。
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if "|" in line and i + 1 < n and _TABLE_SEPARATOR_RE.match(lines[i + 1]):
            header = _split_table_row(line)
            i += 2
            rows: list[list[str]] = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_table_row(lines[i]))
                i += 1

            # 只有一個資料欄（例如篩選結果剛好只有一家廠商符合條件）時，
            # 不需要再把該欄位名稱（廠商名稱）當成每一行的前綴——這個
            # 表格本來就是拿來比較「多個」廠商用的格式，只有一欄時，
            # 每一行都重複同一個廠商名稱只是雜訊，且這則訊息外層本來
            # 就已經有 `_split_by_vendor` 加上的「【廠商】」標題了。
            single_data_col = len(header) == 2

            # 另一種真實發生過的情況：表格是「每一列一台車」的方向，
            # 第一欄放的是「廠商」，但因為篩選結果剛好只有一家廠商，
            # 每一列的第一欄其實都是同一個值——這種情況下把它逐列包成
            # 「【廠商名稱】」當標籤，只會不斷重複印出同一個廠商名稱，
            # 一樣是雜訊（外層訊息本來就已經有廠商標題了）。改用第二欄
            # 當作每一列真正有辨識度的標籤（通常是廠牌或車型）。只有
            # 「不只一列、且第一欄的值全部相同」時才觸發，避免誤判正常
            # 「每一列各是不同比較項目」的表格（那種第一欄本來就該逐列
            # 變化）。
            first_col_values = {_clean_table_cell(row[0]) for row in rows if row and row[0]}
            collapse_first_col = len(header) > 2 and len(rows) > 1 and len(first_col_values) == 1
            label_col = 1 if collapse_first_col else 0

            block: list[str] = []
            for row in rows:
                if len(row) > label_col and row[label_col]:
                    # 標籤這裡刻意保留引用標記（不在這裡先清掉）——
                    # `_correct_vendor_citation_mismatches` 需要標籤緊接著
                    # 自己的引用編號才能找到、訂正寫錯的廠商名稱；標記
                    # 本身會在 `_strip_citations` 那一步統一清掉，順便
                    # 收掉因此留下的多餘空格（見該函式）。
                    block.append(f"【{_clean_table_cell(row[label_col])}】")
                for col_name, cell in zip(header[label_col + 1 :], row[label_col + 1 :]):
                    cell = _clean_table_cell(cell)
                    if not cell:
                        continue
                    if not _CITATION_GROUP_RE.sub("", cell).strip():
                        # 這一欄的儲存格整個只有引用編號、沒有其他文字
                        # 內容（真實發生過：模型自己額外加了一欄「引用
                        # 來源」，值就是單純的 [1]）——直接印成「欄位
                        # 名稱：」不會有任何實際資訊，`_strip_citations`
                        # 把 [1] 清掉之後只會留下一行空蕩蕩的「引用
                        # 來源：」。但引用編號本身不能整個丟掉：
                        # `_split_by_vendor` 要靠它才能判斷這一整段
                        # 屬於哪個廠商，所以改成直接接在前一行後面，
                        # 讓引用編號還留在同一個原子裡、但不會多印出
                        # 一行沒有意義的欄位名稱。
                        if block:
                            block[-1] += " " + cell
                        else:
                            block.append(cell)
                        continue
                    block.append(cell if single_data_col else f"{col_name}：{cell}")
            out.append("\n".join(block))
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def format_for_line(text: str) -> str:
    """將 NotebookLM 的 markdown 輸出轉換成適合 LINE 顯示的純文字。"""
    text = _convert_markdown_tables(text)
    text = _BR_TAG_RE.sub("\n", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^\*\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"(?<!\*)\*(?!\*)", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_CITATION_GROUP_RE = re.compile(r"\[([\d,\s\-]+)\]")
_CITATION_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")
_BULLET_LINE_RE = re.compile(r"^(?:[•\-*]|\d+[.\)])\s+")


_TRAILING_DATE_RE = re.compile(r"\d{8}$")


def vendor_from_title(title: str) -> str:
    """從來源的檔名中擷取廠商名稱。

    目前的命名慣例是 `{廠商}{西元年月日}`，中間不加分隔符
    （例如 "McLaren20260715.md" 代表 2026-07-15）——廠商名稱就是
    結尾 8 位數日期之前的部分。舊式以 `{廠商}_...` 為前綴命名的
    檔案（例如 "McLaren_型錄.md"）也仍然支援。兩種樣式都不符合的
    檔名，就無法歸屬到任何廠商。
    """
    stem = Path(title).stem

    vendor, sep, _ = stem.partition("_")
    if sep:
        return vendor

    match = _TRAILING_DATE_RE.search(stem)
    if match:
        vendor = stem[: match.start()]
        if vendor:
            return vendor

    return "未分類"


def _citation_numbers_in(text: str) -> set[int]:
    numbers = set()
    for match in _CITATION_GROUP_RE.finditer(text):
        for piece in match.group(1).split(","):
            piece = piece.strip()
            if piece.isdigit():
                numbers.add(int(piece))
                continue
            range_match = _CITATION_RANGE_RE.match(piece)
            if range_match:
                start, end = int(range_match.group(1)), int(range_match.group(2))
                if start <= end:
                    numbers.update(range(start, end + 1))
    return numbers


def citation_numbers_in(text: str) -> set[int]:
    """回傳文字中出現過的所有引用編號（例如 "[1]"、"[1, 2]" 或
    "[1-3]" 裡的 1、2、3）。

    公開給 nlm_service 使用，讓它能在送出答案前，檢查 NotebookLM
    回傳的 references 是否漏掉了答案文字裡實際引用到的某個編號
    ——這種上游缺漏會讓 `build_answer_messages` 把查無廠商的段落
    誤併入前一個廠商的訊息裡。
    """
    return _citation_numbers_in(text)


def _strip_citations(text: str) -> str:
    """移除像 [1]、[1, 2] 或 [1-3] 這樣的引用標記——因為呈現給使用者
    的內容中沒有任何說明能解釋它們指的是什麼，留著只會是雜訊。"""
    text = _CITATION_GROUP_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    # 表格轉換時，「【標籤 [1]】」這種標籤緊跟著引用編號的寫法，
    # 引用編號被上面那行清掉之後會留下一個多餘的空格（變成
    # 「【標籤 】」）——這裡順手收掉，標籤本身不受影響。
    text = re.sub(r"[ \t]+】", "】", text)
    return text.strip()


def _atoms_in(text: str) -> list[tuple[str, str, int]]:
    """將文字切分成有序的 (kind, content, paragraph_index) 原子單位。

    每一個項目符號清單項目都會變成自己獨立的 'bullet' 原子，
    而不是黏在該段落其餘內容上——因為模型經常會用單一個項目符號
    清單來回答，其中每一行各自引用不同的廠商（例如依年份排序，
    而不是依廠商分組），所以段落層級的切分無法把它們區分開來。
    沒有任何項目符號的段落，則整段維持為單一個 'block' 原子。

    保留 ``paragraph_index``（記錄某個原子來自原文中哪一個以空行
    分隔的段落），是為了讓 `_split_by_vendor` 能區分「整個回答自己
    的開場段落」與「廠商 1 自己的介紹行」——這兩者都是在任何廠商
    確立之前出現、沒有引用的原子，但只有前者才是真正獨立的一個
    段落。
    """
    atoms: list[tuple[str, str, int]] = []
    paragraphs = [p for p in _PARAGRAPH_SPLIT_RE.split(text.strip()) if p.strip()]
    for para_idx, para in enumerate(paragraphs):
        lines = [line for line in para.split("\n") if line.strip()]
        lead: list[str] = []
        i = 0
        while i < len(lines) and not _BULLET_LINE_RE.match(lines[i].strip()):
            lead.append(lines[i])
            i += 1
        if lead:
            atoms.append(("block", "\n".join(lead), para_idx))
        while i < len(lines):
            atoms.append(("bullet", lines[i], para_idx))
            i += 1
    return atoms


def _render_atoms(atoms: list[tuple[str, str, int]]) -> str:
    """重新組合原子，讓連續的項目符號緊密排列，其餘內容則以空行
    隔開。"""
    parts: list[str] = []
    prev_kind = None
    for kind, content, _ in atoms:
        if parts:
            parts.append("\n" if kind == "bullet" and prev_kind == "bullet" else "\n\n")
        parts.append(content)
        prev_kind = kind
    return "".join(parts)


def _split_by_vendor(text: str, citation_vendor: dict[int, str]) -> list[str]:
    """依照每個原子引用的單一廠商，將回答內容分組——按廠商分桶，
    而不是只合併連續的片段——這樣即使一個項目符號清單中交錯著
    多個廠商的項目，最終每個廠商仍會整理成一則乾淨的訊息。

    有兩種原子無法單靠自身歸屬到某一個廠商，會分別處理：

    - 完全*沒有*引用來源的原子（例如某廠商自己沒有引用的介紹行，
      像「2. 聯絡資訊：...這家廠商所持有的車款包括：」，出現在
      該廠商自己有引用的項目符號之前）會先被暫存（pending），
      並歸入接下來出現的*下一個*廠商的分桶——把它接到它實際上
      介紹的那個廠商，而不是前一個廠商，這正是為什麼每家經銷商
      的說明都能各自維持在自己的訊息裡，而不會滲入前一家經銷商
      的訊息中。
    - 引用了*不只一個*廠商的原子（例如一句比較兩家經銷商的句子）
      無論怎麼歸屬都確實模稜兩可，所以會直接歸入目前「當前使用中」
      的廠商（也就是它之前最近出現的那個廠商）——因為它並不是在
      介紹某個「下一個」廠商。

    在*第一個廠商出現之前*完全沒有引用的內容，與上述兩種情況都
    不同：如果它來自比介紹廠商 1 那一段更早的段落（例如一句獨立的
    開場白，像「目前共有三家廠商擁有現車庫存：」），那它是整個回答
    的開場白，而不是廠商 1 自己的內容，會單獨變成一則不帶標籤的
    開頭訊息。而與廠商 1 自己第一個有引用的原子*同一段落*的內容
    （例如廠商 1 自己沒有引用的「1. 聯絡資訊：...」那一行，緊接在
    它第一個有引用的項目符號之前）則會留在廠商 1 裡——段落是否
    相鄰，正是區分這兩種情況的依據，因為若不看段落，這兩者
    表面上都只是「在任何廠商出現之前、沒有引用的原子」。
    """
    atoms = _atoms_in(text)

    buckets: dict[str, list[tuple[str, str, int]]] = {}
    order: list[str] = []
    pending: list[tuple[str, str, int]] = []
    last_vendor: str | None = None
    lead_atoms: list[tuple[str, str, int]] = []

    for atom in atoms:
        _, content, para_idx = atom
        cited_vendors = {
            citation_vendor[n] for n in _citation_numbers_in(content) if n in citation_vendor
        }

        if len(cited_vendors) == 1:
            vendor = next(iter(cited_vendors))
        elif len(cited_vendors) > 1 and last_vendor is not None:
            buckets[last_vendor].append(atom)
            continue
        else:
            pending.append(atom)
            continue

        if vendor not in buckets:
            buckets[vendor] = []
            order.append(vendor)
            if last_vendor is None:
                # 依段落邊界切分 pending：來自比這一段更早的段落的
                # 原子，屬於整個回答的開場白；來自這個廠商自己所在
                # 段落的原子（它自己沒有引用的介紹行）則留給它。
                lead_atoms = [a for a in pending if a[2] < para_idx]
                pending = [a for a in pending if a[2] >= para_idx]
        buckets[vendor].extend(pending)
        pending = []
        buckets[vendor].append(atom)
        last_vendor = vendor

    if not order:
        return [_render_atoms(pending)] if pending else []

    # 最後一個廠商之後的尾隨內容（後面沒有下一個廠商可以歸入了）
    # 會保留附加在最後一個廠商上，而不是被丟棄。
    if pending:
        buckets[last_vendor].extend(pending)

    messages = [_render_atoms(lead_atoms)] if lead_atoms else []
    messages.extend(f"【{vendor}】\n\n{_render_atoms(buckets[vendor])}" for vendor in order)
    return messages


_VENDOR_CITATION_GAP_RE_CACHE: dict[frozenset[str], re.Pattern] = {}


def _correct_vendor_citation_mismatches(
    text: str, source_map: dict[int, str], known_vendors: set[str] | None = None
) -> str:
    """修正 NotebookLM 已知會發生的一種幻覺：自由文字或表格儲存格裡寫的
    廠商名稱，跟緊接在後面的引用編號 [n] 實際對應的來源廠商對不上
    （例如文字寫「由力彰汽車商行提供 [1]」，但 [1] 實際引用的來源檔案
    卻是「中彰投汽車有限公司」的資料）——這是真實發生過的案例，而且
    同一則回答裡可能好幾行都有這個問題，不是單一個案。

    引用編號到底引用了哪一份來源文件，是 NotebookLM 自己的檢索結果，
    相對可信；廠商名稱則是模型另外用自由文字生成出來的，會有記錯、
    混淆兩家廠商的風險。這裡反過來拿引用編號查出的真正廠商，去訂正
    文字上寫錯的廠商名稱——只在「這個引用編號明確對應到單一廠商，
    且跟文字寫的不一樣」時才動手，引用了多個廠商（真正的跨廠商比較句）
    一律不碰，避免誤改。

    ``known_vendors`` 是「這個筆記本裡所有可能出現的廠商名稱」（來自
    全部來源檔名，不只是這則答案實際引用到的那幾個）——一定要用這個
    較大的範圍，而不是只從 `source_map`（這則答案引用到的來源）反推：
    真實發生過的案例是模型寫錯的廠商名稱剛好是「這則答案完全沒有
    引用到」的另一家廠商，若只看 `source_map`，連要辨識出「這是一個
    已知廠商名稱、只是寫錯地方了」都做不到。沒有提供時退回用
    `source_map` 反推，讓既有呼叫端與測試不用跟著修改。"""
    vendors_by_citation = {n: vendor_from_title(t) for n, t in source_map.items()}
    if known_vendors is None:
        known_vendors = set(vendors_by_citation.values())
    known_vendors_list = sorted(known_vendors - {"未分類"}, key=len, reverse=True)
    if not known_vendors_list:
        return text

    cache_key = frozenset(known_vendors_list)
    pattern = _VENDOR_CITATION_GAP_RE_CACHE.get(cache_key)
    if pattern is None:
        vendor_alt = "|".join(re.escape(v) for v in known_vendors_list)
        # 廠商名稱跟它自己的引用編號之間，實際上常常隔著動詞、標點
        # （例如「由『正峰汽車商行』提供，詳細規格資訊如下：[1]」），
        # 不能只允許緊貼在後面——但也不能無限制地往後找，不然會誤把
        # 一句話裡「最先出現」的廠商名稱，錯配到其實是在講「另一家、
        # 離引用編號更近」的廠商的引用編號上。用一個否定前瞻擋住中間
        # 出現任何一個已知廠商名稱的開頭字元，讓 regex 引擎在真的有
        # 兩個廠商名稱夾在中間時，只有離引用編號最近的那一個能配對
        # 成功；同時禁止跨越句號/問號/驚嘆號/換行，避免跨到不相干的
        # 另一句話。
        gap = rf"(?:(?!{vendor_alt}|[。！？\n\[])[\s\S]){{0,20}}"
        pattern = re.compile(rf"({vendor_alt}){gap}(\[[\d,\s\-]+\])")
        _VENDOR_CITATION_GAP_RE_CACHE[cache_key] = pattern

    def _replace(match: re.Match) -> str:
        mentioned_vendor, citation_part = match.group(1), match.group(2)
        cited = {
            vendors_by_citation[n]
            for n in _citation_numbers_in(citation_part)
            if n in vendors_by_citation
        }
        if len(cited) == 1:
            true_vendor = next(iter(cited))
            if true_vendor != mentioned_vendor:
                return true_vendor + match.group(0)[len(mentioned_vendor):]
        return match.group(0)

    return pattern.sub(_replace, text)


_UNCLASSIFIED_HEADER = "【未分類】"
_NO_VENDOR_MATCH_REPLY = "很抱歉，目前的資料無法明確對應到特定廠商，請提供更明確的條件（如廠牌、車型）以便查詢。"


def _split_and_classify(
    text: str, source_map: dict[int, str], known_vendors: set[str] | None = None
) -> tuple[list[str], int]:
    """`build_answer_messages` 與 `count_unclassified_drops` 共用的核心邏輯，
    回傳 (最終要送出的訊息清單, 因未分類而被捨棄的段落數)。"""
    if not text.strip():
        return [], 0

    text = _correct_vendor_citation_mismatches(text, source_map, known_vendors)

    cited_numbers = _citation_numbers_in(text) & source_map.keys()
    citation_vendor = {n: vendor_from_title(source_map[n]) for n in cited_numbers}

    parts = [_strip_citations(part) for part in _split_by_vendor(text, citation_vendor)]
    dropped = [part for part in parts if part.startswith(_UNCLASSIFIED_HEADER)]
    messages = [part for part in parts if not part.startswith(_UNCLASSIFIED_HEADER)]

    if dropped:
        # 這種捨棄目前使用者完全無感——收到的答案就只是「少了一段」，
        # 沒有任何跡象顯示曾經有內容被拿掉。這裡留一筆 log，是目前
        # 唯一能追溯「這件事發生過幾次」的地方；`count_unclassified_drops`
        # 讓呼叫端（`nlm_service.ask_question`）可以進一步決定要不要
        # 發管理員告警。
        logger.warning(
            f"build_answer_messages: dropped {len(dropped)}/{len(parts)} part(s) as "
            f"{_UNCLASSIFIED_HEADER}（來源檔名無法歸屬到任何廠商，內容未送給使用者）"
        )

    if not messages and parts:
        return [_NO_VENDOR_MATCH_REPLY], len(dropped)
    return messages, len(dropped)


def build_answer_messages(
    text: str, source_map: dict[int, str], known_vendors: set[str] | None = None
) -> list[str]:
    """為一則回答建立 LINE 訊息，每個廠商各一則。

    每個段落會被歸屬到它所引用的單一廠商（見 `vendor_from_title`），
    並加上「【廠商】」標題，讓使用者一眼就能看出哪些廠商有相符的
    資訊——不會另外附上來源清單。若有多個廠商都有相符資訊，
    則各自成一則訊息。沒有引用來源、或引用了不只一個廠商的
    段落，會合併進相鄰廠商的訊息，而不是自己單獨變成一則
    不帶標籤的訊息。

    來源檔名無法歸屬到任何廠商的內容（標記為「未分類」）會被捨棄
    而不送出——因為它無法連結到特定的經銷商，對使用者沒有用處。

    ``known_vendors``：見 `_correct_vendor_citation_mismatches` 的說明，
    傳入這個筆記本全部來源檔名反推出的廠商名稱，訂正效果才會涵蓋
    「模型寫錯的廠商名稱剛好沒被這則答案引用到」的情況。不傳的話
    只能訂正成「這則答案本身有引用到」的廠商名稱。
    """
    messages, _ = _split_and_classify(text, source_map, known_vendors)
    return messages


def count_unclassified_drops(
    text: str, source_map: dict[int, str], known_vendors: set[str] | None = None
) -> int:
    """回傳這則答案依廠商分則後，有多少段落因為來源檔名無法歸屬到
    任何廠商而被 `build_answer_messages` 捨棄、沒有送給使用者。

    給 `nlm_service.ask_question` 用來判斷是否要發出管理員告警——
    「未分類」的內容目前是靜默捨棄，使用者完全看不出來答案曾經
    被拿掉一段，只能靠這個訊號才會被發現。"""
    _, dropped = _split_and_classify(text, source_map, known_vendors)
    return dropped
