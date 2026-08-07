import re
from pathlib import Path


def format_for_line(text: str) -> str:
    """將 NotebookLM 的 markdown 輸出轉換成適合 LINE 顯示的純文字。"""
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^\*\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"(?<!\*)\*(?!\*)", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_CITATION_GROUP_RE = re.compile(r"\[([\d,\s]+)\]")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")
_BULLET_LINE_RE = re.compile(r"^(?:[•\-*]|\d+[.\)])\s+")


_TRAILING_DATE_RE = re.compile(r"\d{8}$")


def _vendor_from_title(title: str) -> str:
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
    return numbers


def _strip_citations(text: str) -> str:
    """移除像 [1] 或 [1, 2] 這樣的引用標記——因為呈現給使用者的內容
    中沒有任何說明能解釋它們指的是什麼，留著只會是雜訊。"""
    text = _CITATION_GROUP_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
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


_UNCLASSIFIED_HEADER = "【未分類】"
_NO_VENDOR_MATCH_REPLY = "很抱歉，目前的資料無法明確對應到特定廠商，請提供更明確的條件（如廠牌、車型）以便查詢。"


def build_answer_messages(text: str, source_map: dict[int, str]) -> list[str]:
    """為一則回答建立 LINE 訊息，每個廠商各一則。

    每個段落會被歸屬到它所引用的單一廠商（見 `_vendor_from_title`），
    並加上「【廠商】」標題，讓使用者一眼就能看出哪些廠商有相符的
    資訊——不會另外附上來源清單。若有多個廠商都有相符資訊，
    則各自成一則訊息。沒有引用來源、或引用了不只一個廠商的
    段落，會合併進相鄰廠商的訊息，而不是自己單獨變成一則
    不帶標籤的訊息。

    來源檔名無法歸屬到任何廠商的內容（標記為「未分類」）會被捨棄
    而不送出——因為它無法連結到特定的經銷商，對使用者沒有用處。
    """
    if not text.strip():
        return []

    cited_numbers = _citation_numbers_in(text) & source_map.keys()
    citation_vendor = {n: _vendor_from_title(source_map[n]) for n in cited_numbers}

    parts = [_strip_citations(part) for part in _split_by_vendor(text, citation_vendor)]
    messages = [part for part in parts if not part.startswith(_UNCLASSIFIED_HEADER)]

    if not messages and parts:
        return [_NO_VENDOR_MATCH_REPLY]
    return messages
