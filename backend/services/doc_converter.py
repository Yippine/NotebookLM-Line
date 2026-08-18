import io
import logging
import re
from pathlib import Path

from markitdown import MarkItDown

logger = logging.getLogger(__name__)

_converter = MarkItDown()


# PDF/Word 轉檔後，頁首頁尾、頁碼這類每一頁都會重複出現一次的內容，
# 會在 Markdown 裡變成散落在正文中間的重複雜訊行——不但無助於
# NotebookLM 找答案，還會白白吃掉它要處理的 token，拖慢生成速度。
#
# 這裡刻意只砍「結構上不可能是真實車輛資料」的內容，不做「這一行
# 出現很多次就當作雜訊砍掉」這種通用的頻率判斷——後者風險太高：
# 同一份型錄裡不同車輛剛好都寫著「顏色：白色」也會重複出現多次，
# 誤判成頁首頁尾砍掉的話，就是真實車輛資料被無聲刪除，比拖慢速度
# 嚴重得多。只處理以下兩種能明確辨識、不會跟車輛資料混淆的樣式：
#
# 1. 單獨成行、整行就是頁碼本身的行（例如「第 3 頁」「3 / 10」
#    「Page 3 of 10」）——這種行不含任何車輛相關資訊，砍掉零風險。
# 2. 同一行文字在整份文件裡逐字重複出現 3 次以上、且同時符合
#    「看起來像聯絡資訊/版權宣告」樣式（含電話、網址、地址、©、
#    Copyright 等關鍵字）的行——只保留第一次出現、之後的重複才砍掉
#    （不是整段拿掉），即使誤判也只是留一份、不會真的遺失資訊，
#    比完全刪除安全。兩個條件都要同時成立才會處理，避免誤刪剛好
#    重複的真實資料。
_PAGE_NUMBER_LINE_RE = re.compile(
    r"^\s*(?:[-—]\s*)?"
    r"(?:第\s*\d+\s*頁(?:\s*[/,，]?\s*(?:共\s*)?\d+\s*頁)?"
    r"|\d+\s*/\s*\d+"
    r"|page\s+\d+(?:\s+of\s+\d+)?)"
    r"(?:\s*[-—])?\s*$",
    re.IGNORECASE,
)
_FOOTER_LIKE_RE = re.compile(
    r"(https?://|www\.|©|copyright|版權所有|電話[:：]|tel[:：.]|地址[:：])",
    re.IGNORECASE,
)


def _strip_pagination_artifacts(text: str) -> str:
    lines = text.split("\n")

    counts: dict[str, int] = {}
    for line in lines:
        stripped = line.strip()
        if stripped:
            counts[stripped] = counts.get(stripped, 0) + 1

    kept_footer_lines: set[str] = set()
    out: list[str] = []
    removed = 0
    for line in lines:
        stripped = line.strip()
        if stripped and _PAGE_NUMBER_LINE_RE.match(stripped):
            removed += 1
            continue
        if stripped and counts.get(stripped, 0) >= 3 and _FOOTER_LIKE_RE.search(stripped):
            if stripped in kept_footer_lines:
                removed += 1
                continue
            kept_footer_lines.add(stripped)
        out.append(line)

    if removed:
        logger.info(f"convert_to_markdown: 已移除 {removed} 行疑似頁首頁尾/頁碼的重複雜訊")

    result = "\n".join(out)
    # 移除的行留下的多餘空行一併收合，避免砍完之後留一堆空白段落。
    return re.sub(r"\n{3,}", "\n\n", result)


def convert_to_markdown(file_bytes: bytes, file_name: str) -> tuple[bytes, str]:
    """將上傳的文件轉換為 Markdown 文字。

    回傳 (markdown_bytes, markdown_file_name)。若格式不受支援或
    內容無法解析，會拋出 ``markitdown.MarkItDownException``。
    """
    extension = Path(file_name).suffix or None
    result = _converter.convert_stream(io.BytesIO(file_bytes), file_extension=extension)
    markdown_text = _strip_pagination_artifacts(result.markdown)
    markdown_name = Path(file_name).stem + ".md"
    return markdown_text.encode("utf-8"), markdown_name
