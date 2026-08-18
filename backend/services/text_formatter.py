import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# NotebookLM 在使用者觸發它的網路搜尋功能（例如問跟車輛完全無關的
# 天氣、新聞）時，回答裡有時會附帶一段 `<a2ui-json>...</a2ui-json>`
# ——這是它網頁版用來顯示「可匯入來源卡片」的內部 UI 元件描述
# （真實發生過的案例：一大包含 base64 下載網址的原始 JSON），
# 對 LINE 純文字訊息完全沒有意義，原封不動送出去只會是一大坨
# 看不懂的亂碼，必須整段拿掉。
_A2UI_JSON_RE = re.compile(r"<a2ui-json>.*?</a2ui-json>", re.DOTALL | re.IGNORECASE)
_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LI_OPEN_RE = re.compile(r"<li>", re.IGNORECASE)
_LIST_TAG_RE = re.compile(r"</?(?:ul|ol|li)>", re.IGNORECASE)
_LEADING_BULLET_RE = re.compile(r"^(?:[•\-*]|\d+[.\)])\s+")

# 表格轉換時，列與列之間的預設分隔——見 `_convert_markdown_tables`
# 與 `_promote_unambiguous_row_separators_to_paragraph_breaks` 的說明。
#
# 用兩種不同的分隔字串記住「這些列在轉換當下，第一欄本來就已經是
# 逐列變化的真實實體（例如廠商名稱、車輛型號），還是通用、會重複
# 出現的比較項目（例如「年份」「顏色」這種欄名）」——這個區分只有
# `_convert_markdown_tables` 當下知道（`collapse_first_col` 是否
# 成立），之後 `_promote_unambiguous_row_separators_to_paragraph_
# breaks` 沒辦法單靠文字內容重新推算出來，所以用不同的分隔字串
# 直接把這個資訊帶過去：已經是逐列變化實體的表格，永遠不該再被
# 誤判成「其實該逐欄分組」，否則會把已經正確的「一列一台車」拆散
# 成「一欄一個規格項目」。
_ROW_SEPARATOR = "－－－－－"
_ENTITY_ROW_SEPARATOR = "－－－●－－－"


def _split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _clean_table_header_cell(cell: str) -> str:
    """整理表格「欄位名稱」（表頭那一列的儲存格），攤平成單行文字，
    供 ``f"{col_name}：{cell}"`` 當欄位名稱前綴用。

    跟 `_clean_table_cell`（給*儲存格內容*用，允許 <br> 轉成真正的
    換行，呈現多行子項目）刻意不同——欄位名稱一定要維持單行：這裡
    產生的文字這時候還沒經過 `format_for_line` 的 `<br>` → 換行轉換，
    如果放著不處理，等那一步跑完，「欄位名稱：值」這一整行就會被
    從 <br> 的位置攔腰切成兩行，前半段（通常是車型名稱）沒有「：」、
    在逐列解析（`_parse_row_major_table_block`）時會被直接丟棄，只
    剩後半段被當成欄位名稱——如果兩欄的後半段文字剛好相同（例如
    同一家廠商的兩台車，後半段都只剩「（廠商 [1]）」），兩欄就會被
    誤判成同一欄，轉置時其中一欄的整排資料會被另一欄悄悄蓋掉、憑空
    消失（真實發生過的案例：同一家廠商有兩台車時，比較表轉置後只
    剩其中一台車的資料，使用者完全看不出少了什麼）。"""
    cell = _BR_TAG_RE.sub(" ", cell)
    cell = _LIST_TAG_RE.sub("", cell)
    cell = re.sub(r"\*\*(.+?)\*\*", r"\1", cell)
    cell = re.sub(r"\s+", " ", cell)
    return cell.strip()


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
            # 用不同的分隔字串記住這一點，供
            # `_promote_unambiguous_row_separators_to_paragraph_breaks`
            # 判斷要不要嘗試改成逐欄分組——`collapse_first_col` 成立時，
            # 每一列的標籤已經是逐列變化的真實實體（例如廠牌／車型），
            # 不該再被誤判成「其實該逐欄分組」，見該常數的說明。
            row_separator = _ENTITY_ROW_SEPARATOR if collapse_first_col else _ROW_SEPARATOR

            block: list[str] = []

            if single_data_col:
                # 只有一欄資料時，這一欄的欄位名稱（`header[1]`）有兩種
                # 可能：可能就是廠商名稱本身（例如「| 比較項目 | 正峰
                # 汽車商行 |」，這時候逐行重複印出來是雜訊，見上面的
                # 說明），也可能是這一欄實際代表的車型／車款名稱（例如
                # 「| 比較項目 | A180 1.3 運動版 |」，只有一家廠商符合
                # 條件、但表格本身其實是拿來對照某一款特定車型的規格）
                # ——這裡不預先判斷是哪一種，先把它當成一行「車型：」
                # 資訊留著；如果它剛好等於這則訊息最後解析出來的廠商
                # 名稱，`_redundant_vendor_value_line_re` 會在稍後自動
                # 把這行拿掉，兩種情況都能正確處理，不需要在這裡先
                # 判斷、也判斷不出來（這裡還不知道引用編號對應到哪個
                # 廠商）。
                #
                # 如果表格本身已經有一列的標籤就叫「車型」（或近似
                # 說法），代表車型資訊已經包含在表格內容裡了，這裡就
                # 不用再多加一行——不但是多餘的，這一欄的欄位名稱有時
                # 其實是模型記錯、寫成別家廠商名稱的幻覺內容（真實發生
                # 過的案例），硬是掛上「車型：」這個標籤反而會誤導；
                # 表格裡真正的「車型」那一列本身就更可靠。
                existing_row_labels = {_clean_table_cell(row[0]) for row in rows if row and row[0]}
                col_header = header[1] if len(header) > 1 else ""
                col_header = _clean_table_cell(col_header)
                if (
                    col_header
                    and _CITATION_GROUP_RE.sub("", col_header).strip()
                    and not existing_row_labels & {"車型", "車款", "型號"}
                ):
                    block.append(f"車型：{col_header}")
            for row_idx, row in enumerate(rows):
                if row_idx > 0 or block:
                    # 這一列前面已經有內容了（前一列，或是上面加的
                    # 「車型：」那一行）——一定要放分隔線把它們隔開，
                    # 否則兩段內容會被直接黏在同一行裡，稍後解析表格
                    # 結構時會被誤判成同一「列」貢獻了好幾個不同的
                    # 欄位名稱，讓本來不該轉置的表格被誤判成可以轉置
                    # （這是實際發生過的真實案例）。
                    # 每一列之間留一個視覺分隔——但預設刻意不是「空行」：
                    # `_atoms_in` 是依空行把文字切成不同段落，每個段落各自
                    # 變成獨立的原子；如果這張表其實是「比較表」（同一列
                    # 裡不同儲存格分別引用不同廠商，例如比較兩款車在不同
                    # 廠商的規格），用空行分隔會被拆成好幾個原子，內容
                    # 各自散落到不同廠商的訊息氣泡裡（這是這個表格轉換
                    # 機制一開始就要避免的下場）。所以這裡先一律用分隔線
                    # 頂著——它在視覺上有分隔效果，但不會被判斷成空行、
                    # 也不會被 `_BULLET_LINE_RE`（只認半形符號）誤判成
                    # 項目符號，此時整張表格仍是同一個原子。
                    #
                    # 之後 `_promote_unambiguous_row_separators_to_paragraph_
                    # breaks` 會在真正知道每個引用編號對應到哪個廠商之後
                    # （這裡還不知道），回頭判斷是否要把這裡先放的分隔線
                    # 換成真正的段落空行，讓內容各自拆成獨立的訊息氣泡；
                    # 見該函式的說明。
                    block.append(row_separator)

                row_label = (
                    _clean_table_cell(row[label_col])
                    if len(row) > label_col and row[label_col]
                    else None
                )
                # 標籤這裡刻意保留引用標記（不在這裡先清掉）——
                # `_correct_vendor_citation_mismatches` 需要標籤緊接著
                # 自己的引用編號才能找到、訂正寫錯的廠商名稱；標記
                # 本身會在 `_strip_citations` 那一步統一清掉，順便
                # 收掉因此留下的多餘空格（見該函式）。
                #
                # 只有一欄資料時（`single_data_col`），統一用「屬性：值」
                # 單行格式（跟多欄、或轉置後的格式一致），而不是
                # 「【屬性】」括號單獨一行、值另起一行——這樣才能讓後續
                # `_redundant_vendor_value_line_re` 正確辨識並拿掉「來源
                # 車商：某某廠商」這種整行剛好等於廠商名稱的重複欄位，
                # 而且跟其他情況的呈現方式一致，不會有兩套不同的格式。
                if not single_data_col and row_label:
                    block.append(f"【{row_label}】")

                for col_name, cell in zip(header[label_col + 1 :], row[label_col + 1 :]):
                    # 欄位名稱在這裡就要攤平成單行（見
                    # `_clean_table_header_cell` 的說明），不能沿用
                    # `_clean_table_cell` 那種允許 <br> 變成真正換行的
                    # 處理方式——欄位名稱要接在同一行的「：」前面，
                    # 一旦被換行斷成兩截，後面的逐列解析會把沒有「：」
                    # 的那一截整個丟棄，導致不同欄位可能撞名、其中一欄
                    # 資料被靜默蓋掉。
                    col_name = _clean_table_header_cell(col_name)
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
                    if single_data_col:
                        block.append(f"{row_label}：{cell}" if row_label else cell)
                    else:
                        block.append(f"{col_name}：{cell}")
            out.append("\n".join(block))
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def format_for_line(text: str) -> str:
    """將 NotebookLM 的 markdown 輸出轉換成適合 LINE 顯示的純文字。"""
    text = _A2UI_JSON_RE.sub("", text)
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


def _date_from_title(title: str) -> str | None:
    """從來源檔名中擷取結尾的 8 位數西元年月日字串（見 `vendor_from_title`
    的檔名慣例說明），代表這份來源最後一次上傳／覆蓋的日期。

    因為同一家廠商的舊來源會在重新上傳時被整份取代（見
    `nlm_service.upload_file` 的覆蓋邏輯），檔名裡的日期等同於這家
    廠商資料的「更新日期」，供 `_split_by_vendor` 用來把最近更新的
    廠商排在回答訊息的最前面。不符合「結尾 8 位數字」慣例的檔名
    （例如舊式的 "McLaren_型錄.md"）回傳 None，代表無從得知更新
    日期，排序時會被視為最舊、排在有日期的廠商之後。"""
    stem = Path(title).stem
    match = _TRAILING_DATE_RE.search(stem)
    return match.group(0) if match else None


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


_QUOTED_TEXT_RE = re.compile(r"[「『][^」』]*[」』]")


def _citation_numbers_excluding_quoted(text: str) -> set[int]:
    """跟 `_citation_numbers_in` 一樣，但會先把「...」/『...』引號
    包起來的文字整段拿掉，才開始找引用編號，只給 `_split_by_vendor`
    判斷段落歸屬用。

    真實發生過的案例：回答結尾的通用建議句裡，模型舉了一個範例句
    給使用者參考（例如「例如想看『卡司汽車 [6] 的現車』」），卻把
    引用編號直接標在範例句裡——這個範例句本身不是在陳述卡司汽車的
    資料，只是示範使用者可以怎麼問，適用於所有廠商，不該因為裡面
    剛好舉了一個廠商當例子、又剛好帶了引用編號，就把整段結尾建議
    誤判成只跟那家廠商有關，被拆成獨立一則、還貼上錯的標題。"""
    return _citation_numbers_in(_QUOTED_TEXT_RE.sub("", text))


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
    # 保險措施：表格轉換時放入的列分隔線（`_ROW_SEPARATOR` /
    # `_ENTITY_ROW_SEPARATOR`）理想上都該在
    # `_promote_unambiguous_row_separators_to_paragraph_breaks` 被轉成真正的
    # 段落空行或逐欄展開，但某些表格形狀兩種轉換條件都不成立——例如「單一
    # 廠商、單一資料欄」（只有一台車符合條件、逐列列出它的各項屬性）：
    # 每一列都能唯一歸屬到同一家廠商，但因為只有一家、不構成「橫跨多家
    # 廠商」，不會被展開成段落；欄位名稱（屬性名稱）也各自只出現一次，
    # 不構成「同一欄位重複出現在多列」，轉置也不會採用。兩邊都不處理的
    # 結果，分隔線會原封不動留在文字裡，這裡是它在送到使用者手上之前
    # 最後一個必經的清理點——如果真的還殘留到這裡，代表已知沒有跨廠商
    # 拆散的風險（否則早就在前面被展開或轉置掉了），直接換成一般換行即可，
    # 不該讓使用者看到內部使用的「－－－－－」原始占位字元（真實發生過的
    # 案例：只有一家廠商符合條件時，回覆訊息裡出現一整排看不懂的
    # 「－－－－－」）。
    text = re.sub(rf"\n?(?:{re.escape(_ROW_SEPARATOR)}|{re.escape(_ENTITY_ROW_SEPARATOR)})\n?", "\n", text)
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


_REDUNDANT_VENDOR_PREFIX_RE_CACHE: dict[str, re.Pattern] = {}


def _redundant_vendor_prefix_re(vendor: str) -> re.Pattern:
    """比對「這段內容開頭剛好就是這家廠商自己的『【廠商】』標籤」
    的正規表示式，容許標籤後面還帶著尚未被 `_strip_citations` 清掉
    的引用編號（例如「【中彰投汽車有限公司 [1]】」）。

    比較表格轉成「一台車一個區塊」時，NotebookLM 有時會把廠商標籤
    寫成帶全形括號的「【（廠商 [1]）】」（例如車型欄位標題底下的
    「（卡司汽車 [1]）」），而不是單純的「【廠商】」——兩種寫法都要
    認得，括號兩側也要容許空白（NotebookLM 有時會在括號內側多留
    一個空格，例如「（中彰投汽車有限公司 ）」），不然這種帶括號的
    版本就會漏網，跟外層【廠商】標題重複顯示。"""
    pattern = _REDUNDANT_VENDOR_PREFIX_RE_CACHE.get(vendor)
    if pattern is None:
        escaped = re.escape(vendor)
        citation = r"(?:\s*\[[\d,\s\-]+\])?"
        pattern = re.compile(
            rf"^【\s*[（(]?\s*{escaped}{citation}\s*[）)]?\s*{citation}】\n*"
        )
        _REDUNDANT_VENDOR_PREFIX_RE_CACHE[vendor] = pattern
    return pattern


_REDUNDANT_VENDOR_VALUE_LINE_RE_CACHE: dict[str, re.Pattern] = {}


def _redundant_vendor_value_line_re(vendor: str) -> re.Pattern:
    """比對「欄位名稱：廠商名稱」這種整行的值剛好就是廠商名稱本身
    的行（例如模型自己在比較表裡加了一列「車商名稱」／「提供廠商」，
    轉置成一台車一個區塊之後，這一行的值會跟外層「【廠商】」標題
    重複）。不特別比對欄位名稱本身寫的是什麼（「車商名稱」「提供
    廠商」或其他說法），只要值剛好等於廠商名稱就視為重複，同樣
    容許值後面帶著尚未清掉的引用編號。"""
    pattern = _REDUNDANT_VENDOR_VALUE_LINE_RE_CACHE.get(vendor)
    if pattern is None:
        escaped = re.escape(vendor)
        pattern = re.compile(
            rf"^[^\n：]+：{escaped}(?:\s*\[[\d,\s\-]+\])?[ \t]*\n?", re.MULTILINE
        )
        _REDUNDANT_VENDOR_VALUE_LINE_RE_CACHE[vendor] = pattern
    return pattern


def _collapse_consecutive_row_separators(text: str) -> str:
    """拿掉一整行剛好等於廠商名稱的欄位（見
    `_redundant_vendor_value_line_re`）之後，那一行原本左右兩側的
    分隔線就會直接相鄰、或是變成整段內容開頭／結尾——這裡把連續
    出現的分隔線收合成一條，並且拿掉出現在開頭或結尾的分隔線
    （沒有東西可以分隔了）。"""
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        if line == _ROW_SEPARATOR and out and out[-1] == _ROW_SEPARATOR:
            continue
        out.append(line)
    while out and out[0] == _ROW_SEPARATOR:
        out.pop(0)
    while out and out[-1] == _ROW_SEPARATOR:
        out.pop()
    return "\n".join(out)


def _split_by_vendor(
    text: str,
    citation_vendor: dict[int, str],
    citation_date: dict[int, str | None] | None = None,
) -> list[str]:
    """依照每個原子引用的單一廠商，將回答內容分組——按廠商分桶，
    而不是只合併連續的片段——這樣即使一個項目符號清單中交錯著
    多個廠商的項目，最終每個廠商仍會整理成一則乾淨的訊息。

    各廠商的訊息氣泡最終會依 ``citation_date``（見 `_date_from_title`）
    重新排序，最近更新的廠商排最前面；沒有日期資訊的廠商視為最舊，
    排在所有有日期的廠商之後，彼此之間則維持原本依文字中出現順序
    排列（Python 的 `sorted` 是穩定排序）。不影響開頭那則不帶廠商
    標籤的開場白訊息——它一律留在最前面。

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
            citation_vendor[n]
            for n in _citation_numbers_excluding_quoted(content)
            if n in citation_vendor
        }

        if len(cited_vendors) == 1:
            vendor = next(iter(cited_vendors))
        elif len(cited_vendors) > 1 and last_vendor is not None:
            # 這個原子本身確實無法單獨歸屬給某一家廠商，所以跟目前
            # 分桶邏輯一致，直接接到「當前使用中」的廠商——但在接
            # 它之前，要先把還在等待的 pending（例如一句還沒有引用、
            # 正要鋪陳接下來這段比較內容的句子，像「這兩款車型的
            # 核心差異如下：」）依原文順序先接進去，這個原子才接在
            # 後面。少了這一步，pending 只會留到 for 迴圈結束後才
            # 被整批接到分桶最後面（見下面「最後一個廠商之後的尾隨
            # 內容」那段），鋪陳句就會被推到它原本要介紹的內容*之後*
            # ——這是真實發生過的案例：使用者看到的訊息裡，「這兩款
            # 車型的核心差異如下：」變成孤零零掛在整則訊息的最後一行，
            # 後面卻沒接著任何比較內容。
            buckets[last_vendor].extend(pending)
            pending = []
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

    # 依廠商上次更新日期，把最近更新的廠商排到最前面。一家廠商可能
    # 被好幾個引用編號引用到（例如同一份來源被引用兩次），取其中
    # 最新的日期代表這家廠商；完全沒有日期資訊（`_date_from_title`
    # 回傳 None，例如舊式檔名）的廠商，key 用空字串墊底，會被排到
    # 所有有日期的廠商之後——彼此之間則因為 `sorted` 是穩定排序，
    # 維持原本依文字出現順序排列的相對順序，行為跟排序前一致。
    vendor_dates: dict[str, str] = {}
    for citation_number, vendor in citation_vendor.items():
        date = (citation_date or {}).get(citation_number)
        if date and date > vendor_dates.get(vendor, ""):
            vendor_dates[vendor] = date
    order = sorted(order, key=lambda v: vendor_dates.get(v, ""), reverse=True)

    def _build_vendor_message(vendor: str) -> str:
        rendered = _render_atoms(buckets[vendor])
        # 廠商總覽表那種「一列就是一家廠商」的表格，列本身的標籤
        # 內容剛好就是廠商名稱（見 `_convert_markdown_tables`），
        # 現在這種表格已經會拆成一家廠商一則訊息（見
        # `_promote_unambiguous_row_separators_to_paragraph_breaks`）
        # ——如果不處理，這裡再加一次「【廠商】」標題，廠商名稱就會
        # 連續重複印兩次。只有在內容本身「剛好就是以同一個廠商名稱
        # 開頭」時才去掉這個重複，其餘情況（例如列標籤是廠牌、車型
        # 這種跟廠商名稱不同的內容）不受影響，維持原本的雙層資訊。
        #
        # 這裡還沒有經過 `_strip_citations`（那一步在這個函式的呼叫端
        # 才會做），標籤後面可能還帶著引用編號（例如「【中彰投汽車
        # 有限公司 [1]】」），比對時要把這個可能存在的引用編號也一併
        # 算進去，不能只比對不含引用編號的字面全等。
        redundant_prefix_re = _redundant_vendor_prefix_re(vendor)
        rendered = redundant_prefix_re.sub("", rendered, count=1)

        # 另一種真實發生過的重複：模型自己在比較表裡多加了一列
        # 「車商名稱」／「提供廠商」來標示每台車屬於哪個廠商，轉置成
        # 一台車一個區塊之後，每個區塊底下都會各自重複出現一行內容
        # 剛好就是廠商名稱本身的欄位——這則訊息外層已經有「【廠商】」
        # 標題了，逐項重複同一個廠商名稱只是雜訊。不特別認欄位名稱
        # 寫的是什麼，只要某一行的值剛好等於廠商名稱就整行拿掉。
        rendered = _redundant_vendor_value_line_re(vendor).sub("", rendered)
        # 拿掉的那一行左右兩側可能各自留著一條分隔線——原本是用來隔開
        # 「這一行」跟前後內容的，這一行整個被拿掉之後，兩條分隔線會
        # 直接相鄰（或變成開頭／結尾），見該函式的說明。
        rendered = _collapse_consecutive_row_separators(rendered)

        return f"【{vendor}】\n\n{rendered}"

    messages = [_render_atoms(lead_atoms)] if lead_atoms else []
    messages.extend(_build_vendor_message(vendor) for vendor in order)
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


def _parse_row_major_table_block(rows: list[str]) -> list[tuple[str | None, list[tuple[str, str]]]]:
    """把 `_convert_markdown_tables` 產生的、以列為單位的區塊文字
    （已經用 `_ROW_SEPARATOR` 分開），逐列拆回 `(列標籤, [(欄位名稱, 內容), ...])`
    的結構化資料，供 `_try_transpose_table_block_by_column` 使用。"""
    parsed: list[tuple[str | None, list[tuple[str, str]]]] = []
    for row_text in rows:
        lines = [line for line in row_text.split("\n") if line.strip()]
        row_label: str | None = None
        pairs: list[tuple[str, str]] = []
        for line in lines:
            stripped = line.strip()
            if (
                not pairs
                and row_label is None
                and stripped.startswith("【")
                and stripped.endswith("】")
                and "：" not in stripped
            ):
                row_label = stripped[1:-1]
                continue
            if "：" in line:
                col_name, _, cell = line.partition("：")
                pairs.append((col_name.strip(), cell.strip()))
        parsed.append((row_label, pairs))
    return parsed


def _try_transpose_table_block_by_column(
    rows: list[str], citation_vendor: dict[int, str]
) -> list[str] | None:
    """`_promote_unambiguous_row_separators_to_paragraph_breaks` 的輔助
    函式：當「每一列」都無法唯一歸屬到一個廠商時（真正的比較表，
    不同廠商是分散在同一列裡的不同欄位，而不是逐列變化——例如同一
    款車在不同廠商的規格比較，廠商剛好就是各個資料欄），改試著看
    「每一欄」是否能唯一歸屬到一個廠商，如果可以，把資料從「以列
    為單位」轉置成「以欄為單位」，讓每個廠商各自的完整規格集中在
    同一個區塊裡，之後才能各自拆成獨立的訊息氣泡（真實發生過的
    使用者需求：比較同一款車在不同廠商的規格時，希望同一家廠商的
    資料出現在同一個氣泡裡，而不是逐項目交錯呈現）。

    解析不出一致的欄位結構、或欄位一樣無法唯一歸屬時，回傳 None，
    讓呼叫端維持原本以列為單位、合併成一則訊息的呈現方式——這是
    安全的預設值，不會因為轉置失敗而弄壞內容。

    只有一個資料欄的表格（`single_data_col`）轉換後，每一列本來就
    只剩一組「屬性：值」，經過這個函式解析會變成「每一列各自貢獻
    剛好一個獨有欄位名稱」——表面上符合「有多個欄位」的條件，但
    這些欄位其實完全不重複出現在別的列裡，轉置沒有任何意義（只是
    把同一批屬性重新包裝成一樣的樣子，還會讓每個屬性各自被拆成
    獨立的段落，反而更零碎）。真正值得轉置的表格，同一個欄位名稱
    必須在不只一列裡出現，所以額外要求至少有一個欄位名稱重複
    出現在兩列以上，才視為真正一致的欄位結構。"""
    parsed_rows = _parse_row_major_table_block(rows)

    col_names: list[str] = []
    col_occurrence_counts: dict[str, int] = {}
    for _, pairs in parsed_rows:
        for col_name, _ in pairs:
            if col_name not in col_names:
                col_names.append(col_name)
            col_occurrence_counts[col_name] = col_occurrence_counts.get(col_name, 0) + 1

    if len(col_names) < 2:
        return None
    if max(col_occurrence_counts.values(), default=0) < 2:
        return None

    col_vendor_sets = []
    for col_name in col_names:
        cells = [col_name] + [
            cell for _, pairs in parsed_rows for (cn, cell) in pairs if cn == col_name
        ]
        combined = " ".join(cells)
        vendors = {
            citation_vendor[n]
            for n in _citation_numbers_excluding_quoted(combined)
            if n in citation_vendor
        }
        col_vendor_sets.append(vendors)

    if not all(len(vendors) == 1 for vendors in col_vendor_sets):
        return None

    blocks = []
    for col_name in col_names:
        lines = [f"【{col_name}】"]
        for row_label, pairs in parsed_rows:
            cell = next((c for cn, c in pairs if cn == col_name), None)
            if not cell:
                continue
            lines.append(f"{row_label}：{cell}" if row_label else cell)
        blocks.append("\n".join(lines))
    return blocks


def _promote_unambiguous_row_separators_to_paragraph_breaks(
    text: str, citation_vendor: dict[int, str]
) -> str:
    """把「每一列都能唯一歸屬到剛好一個廠商」的表格區塊，從「分隔線
    分隔、維持同一個原子」升級成「空行分隔、各自獨立的段落」，讓
    `_split_by_vendor` 把它們拆成各自獨立的訊息——用來讓「廠商總覽表」
    這種一列就是一家廠商的表格，回到每家廠商各自一則訊息氣泡的呈現
    方式（`_convert_markdown_tables` 沒辦法在轉換當下就做這個判斷，
    因為那時候還不知道每個引用編號對應到哪個廠商，只能先用分隔線
    頂著，見該函式的說明）。

    兩種分隔字串分開處理（見 `_ENTITY_ROW_SEPARATOR` 的說明）：

    - `_ENTITY_ROW_SEPARATOR`：轉換當下就已經確定每一列的標籤是
      逐列變化的真實實體（例如廠牌／車型），只要每一列都能唯一
      歸屬到一個廠商就直接逐列展開，不會再嘗試逐欄分組——這種表格
      本來就已經是正確的方向，不該被誤判成「其實該逐欄分組」。

    - `_ROW_SEPARATOR`：如果「每一列」都能唯一歸屬、而且真的橫跨
      不只一家廠商（典型的廠商總覽表，一列就是一家廠商），逐列展開
      最有意義，直接採用。否則會改試著看「每一欄」是否能唯一歸屬到
      一個廠商（見 `_try_transpose_table_block_by_column`）——這裡
      刻意優先試欄，而不是只要逐列能唯一歸屬就直接採用：真實發生過
      的案例是模型自己先依廠商分好幾個小節、每個小節底下比較「同一
      家廠商的好幾台車」，這種表格逐列展開時，每一列（例如「年份」）
      雖然也都唯一歸屬到同一家廠商（因為整段本來就只有這一家），
      但這樣做完全沒有分開任何東西，呈現出來會變成「依規格項目
      分組」而不是使用者要的「依車輛分組、同一台車的完整資料集中
      在同一個區塊」。只有在「逐列能唯一歸屬、且真的橫跨多家廠商」
      時，逐列展開才是唯一有意義的選擇，這時才會直接採用，不繞去
      試逐欄。

    兩種方向都試過還是不行，分隔線才會原封不動保留，繼續合併成
    一則訊息，避免比較表的標題和內容被拆散到不同廠商的訊息氣泡裡。
    """
    if _ROW_SEPARATOR not in text and _ENTITY_ROW_SEPARATOR not in text:
        return text

    paragraphs = _PARAGRAPH_SPLIT_RE.split(text)
    out_paragraphs = []
    for para in paragraphs:
        if _ENTITY_ROW_SEPARATOR in para:
            rows = [row.strip("\n") for row in para.split(_ENTITY_ROW_SEPARATOR)]
            vendor_sets = [
                {
                    citation_vendor[n]
                    for n in _citation_numbers_excluding_quoted(row)
                    if n in citation_vendor
                }
                for row in rows
            ]
            if all(len(vendors) == 1 for vendors in vendor_sets):
                out_paragraphs.extend(rows)
            else:
                out_paragraphs.append(para)
            continue

        if _ROW_SEPARATOR not in para:
            out_paragraphs.append(para)
            continue

        rows = [row.strip("\n") for row in para.split(_ROW_SEPARATOR)]
        vendor_sets = [
            {
                citation_vendor[n]
                for n in _citation_numbers_excluding_quoted(row)
                if n in citation_vendor
            }
            for row in rows
        ]
        row_homogeneous = all(len(vendors) == 1 for vendors in vendor_sets)
        distinct_row_vendors = {v for vendors in vendor_sets for v in vendors}

        if row_homogeneous and len(distinct_row_vendors) > 1:
            out_paragraphs.extend(rows)
            continue

        transposed = _try_transpose_table_block_by_column(rows, citation_vendor)
        if transposed is not None:
            out_paragraphs.extend(transposed)
        else:
            # 逐列展開沒有意義（要嘛整段本來就只有一家廠商、逐列展開
            # 沒有分開任何東西，要嘛某一列橫跨多個廠商），逐欄轉置也
            # 失敗——兩種方向都試過還是不行，維持原本合併成一則、
            # 分隔線原封不動保留的呈現方式，不強行拆開。
            out_paragraphs.append(para)
    return "\n\n".join(out_paragraphs)


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
    citation_date = {n: _date_from_title(source_map[n]) for n in cited_numbers}

    text = _promote_unambiguous_row_separators_to_paragraph_breaks(text, citation_vendor)

    parts = [
        _strip_citations(part)
        for part in _split_by_vendor(text, citation_vendor, citation_date)
    ]
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
