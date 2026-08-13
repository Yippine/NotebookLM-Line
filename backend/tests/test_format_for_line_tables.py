from services.text_formatter import build_answer_messages, format_for_line


def test_markdown_table_is_converted_without_pipes_or_html_tags():
    # 重現實際在 LINE 上看到的真實案例：NotebookLM 用表格回答「比較兩台車」，
    # 儲存格裡還夾雜 <br> 與 <ul><li> 標籤。
    text = (
        "Altis 與 Corolla Cross 規格與配備比較對照表\n\n"
        "| 比較項目 | Toyota Corolla Altis | Toyota Corolla Cross |\n"
        "| :--- | :--- | :--- |\n"
        "| 車身型式 | 主要是 4門5座 房車<br>(另有一款 2023 年式登記為 5門5座) | 均為 5門5座 跨界休旅車 |\n"
        "| 排氣量 | 主要是 1.8L | 均為 1.8L |\n"
        "| 安全配備 | <ul><li>ACC 跟車</li><li>倒車雷達</li></ul> | <ul><li>環景系統</li></ul> |\n\n"
        "---\n\n"
        "配備與規格重點解析"
    )

    result = format_for_line(text)

    # 原始表格語法與 HTML 標籤完全不該殘留——這是這個 bug 的核心症狀。
    assert "|" not in result
    assert "<br>" not in result
    assert ":---" not in result
    assert "<ul>" not in result and "<li>" not in result and "</li>" not in result

    # 內容本身要保留，只是換了呈現方式。
    assert "【車身型式】" in result
    assert "Toyota Corolla Altis：主要是 4門5座 房車" in result
    assert "(另有一款 2023 年式登記為 5門5座)" in result
    assert "Toyota Corolla Cross：均為 5門5座 跨界休旅車" in result
    assert "【排氣量】" in result
    assert "Toyota Corolla Altis：主要是 1.8L" in result
    assert "◦ ACC 跟車" in result
    assert "◦ 倒車雷達" in result

    # 表格前後的一般文字不受影響。
    assert "Altis 與 Corolla Cross 規格與配備比較對照表" in result
    assert "配備與規格重點解析" in result


def test_bare_bullet_characters_inside_a_cell_do_not_re_fragment_the_table():
    # 重現實際發生過的真實案例：模型沒有用 <li>，而是直接在儲存格裡
    # 用文字型的「•」搭配 <br> 條列多個子項目。<br> 轉成真正換行後，
    # 如果「•」原封不動保留，這些行會被誤判成獨立的項目符號原子，
    # 導致整張表格（含引用多家不同廠商的其他欄位）又被拆散。
    answer = (
        "| 項目 | 車商A | 車商B |\n"
        "| :--- | :--- | :--- |\n"
        "| 特色車款 | • KIA EV6<br>(2022 電動)<br>• BMW i3<br>(2017 油電) [1] | • Golf GTI<br>(2016) [2] |\n"
    )
    source_map = {1: "中信汽車商行_20260811.md", 2: "中彰投汽車有限公司_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map)

    assert len(messages) == 1
    assert "◦ KIA EV6" in messages[0]
    assert "◦ BMW i3" in messages[0]
    assert "◦ Golf GTI" in messages[0]
    # 沒有任何一行是以「•」開頭的獨立項目符號殘留。
    assert not any(line.strip().startswith("• ") for line in messages[0].splitlines())


def test_table_rows_use_a_different_bullet_glyph_than_the_vendor_split_bullet_marker():
    # 表格轉換絕對不能用「•」開頭——那是 `_split_by_vendor` 用來判斷
    # 「這是一個獨立項目符號原子」的字元，用了就會被那套邏輯逐行拆散。
    text = (
        "| 項目 | A | B |\n"
        "| :--- | :--- | :--- |\n"
        "| 排氣量 | 1.8L | 2.0L |\n"
    )

    result = format_for_line(text)

    for line in result.splitlines():
        assert not line.startswith("• "), f"表格轉換不該產生以「• 」開頭的行：{line!r}"


def test_multi_vendor_comparison_table_stays_intact_as_one_message_not_scrambled():
    # 這是真實發生過的案例：一張比較表裡，不同欄位引用不同廠商組合
    # （Altis 那格引用多家廠商，Cross 那格引用另一組），如果表格被拆成
    # 逐行項目符號，依廠商分則邏輯會把同一張表的標題和內容拆到不同
    # 廠商的訊息氣泡裡。整張表應該維持完整、不被拆散。
    answer = (
        "以下為比較表：\n\n"
        "| 項目 | Toyota Corolla Altis | Toyota Corolla Cross |\n"
        "| :--- | :--- | :--- |\n"
        "| 排氣量 | 包含 1.8L、2.0L [1-3] | 皆為 1.8L [2, 3] |\n"
        "| 引擎燃料 | 汽油、油電混合 [1-3] | 皆為汽油 [2, 3] |\n\n"
        "接下來是各車商的詳細清單：\n\n"
        "正峰汽車商行的車輛規格如下 [2]。\n\n"
        "永春中古汽車有限公司的車輛規格如下 [3]。"
    )
    source_map = {
        1: "中彰投汽車有限公司_20260811.md",
        2: "正峰汽車商行_20260811.md",
        3: "永春中古汽車有限公司_20260811.md",
    }

    messages = build_answer_messages(format_for_line(answer), source_map)

    # 表格的標題（【排氣量】【引擎燃料】）跟內容必須出現在同一則訊息裡，
    # 不會被拆到不同廠商的訊息中、也不會出現「標題在、內容不見了」。
    table_messages = [m for m in messages if "【排氣量】" in m]
    assert len(table_messages) == 1
    table_msg = table_messages[0]
    assert "【引擎燃料】" in table_msg
    assert "Toyota Corolla Altis：包含 1.8L、2.0L" in table_msg
    assert "Toyota Corolla Cross：皆為 1.8L" in table_msg
    assert "Toyota Corolla Altis：汽油、油電混合" in table_msg
    assert "Toyota Corolla Cross：皆為汽油" in table_msg


def test_repeated_identical_first_column_value_is_not_repeated_as_a_bracket_label_per_row():
    # 重現實際發生過的真實案例：表格是「每一列一台車」的方向，第一欄
    # 放「廠商」，但因為篩選結果剛好只有一家廠商符合條件，每一列的
    # 第一欄其實都是同一個值（「卡司汽車」）。逐列包成「【卡司汽車】」
    # 當標籤只是不斷重複同一個廠商名稱，外層訊息本來就已經有廠商
    # 標題了。應該改用第二欄（廠牌）當作每一列真正有辨識度的標籤。
    answer = (
        "| 廠商 | 廠牌 | 車型 | 建議售價 |\n"
        "| :--- | :--- | :--- | :--- |\n"
        "| 卡司汽車 [1] | Suzuki [1] | SWIFT [1] | 19.8萬 [1] |\n"
        "| 卡司汽車 [1] | MINI [1] | COUNTRYMAN [1] | 55.8萬 [1] |\n"
        "| 卡司汽車 [1] | TOYOTA [1] | YARIS [1] | 27.8萬 [1] |\n"
    )
    source_map = {1: "卡司汽車_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map)

    assert len(messages) == 1
    msg = messages[0]
    # 「【卡司汽車】」只該出現一次——那是 `_split_by_vendor` 幫整則
    # 訊息加的外層標題，不是表格逐列重複出來的雜訊。
    assert msg.count("【卡司汽車】") == 1
    # 改用廠牌當標籤，三台車都各自有自己的標籤，不是同一個廠商名稱。
    assert "【Suzuki】" in msg
    assert "【MINI】" in msg
    assert "【TOYOTA】" in msg
    assert "車型：SWIFT" in msg
    assert "建議售價：19.8萬" in msg
    assert "建議售價：19.8萬" in msg


def test_table_with_a_single_row_still_uses_the_first_column_as_the_label():
    # 只有一列時，不算「重複」，第一欄的值當標籤本來就沒問題，
    # 不該被「收合成第二欄」的邏輯誤觸發。
    answer = (
        "| 廠商 | 廠牌 | 車型 |\n"
        "| :--- | :--- | :--- |\n"
        "| 卡司汽車 [1] | Suzuki [1] | SWIFT [1] |\n"
    )
    source_map = {1: "卡司汽車_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map)

    assert "【卡司汽車】" in messages[0]
    assert "廠牌：Suzuki" in messages[0]


def test_a_column_that_is_purely_a_bare_citation_marker_does_not_leave_a_dangling_empty_label():
    # 重現實際發生過的真實案例：模型自己在表格最後多加了一欄「引用
    # 來源」，每一列的值就只是單純的 [1]/[2]/[3]，沒有其他文字。
    # 直接印成「引用來源：」的話，[1] 被 `_strip_citations` 清掉後
    # 只會留下一行空蕩蕩、沒有意義的「引用來源：」。
    answer = (
        "| 廠牌 | 車型 | 建議售價 | 引用來源 |\n"
        "| :--- | :--- | :--- | :--- |\n"
        "| Suzuki | SWIFT | 19.8 萬 | [1] |\n"
        "| MINI | COUNTRYMAN | 55.8 萬 | [1] |\n"
    )
    source_map = {1: "卡司汽車_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map)

    assert len(messages) == 1
    msg = messages[0]
    assert "引用來源" not in msg
    # 引用編號本身沒有整個被丟掉、廠商歸屬依然正確判斷得出來
    # ——訊息確實被貼上了「卡司汽車」這個廠商的標題。
    assert "【卡司汽車】" in msg
    assert "建議售價：19.8 萬" in msg
    assert "建議售價：55.8 萬" in msg


def test_genuinely_varying_first_column_is_unaffected_by_the_collapse_logic():
    # 第一欄本來就逐列變化（正常的「比較項目」表格）時，不該被誤判成
    # 「重複值」而收合掉。
    answer = (
        "| 比較項目 | Toyota Corolla Altis | Toyota Corolla Cross |\n"
        "| :--- | :--- | :--- |\n"
        "| 排氣量 | 1.8L [1] | 1.8L [1] |\n"
        "| 引擎燃料 | 汽油 [1] | 汽油 [1] |\n"
    )
    source_map = {1: "中信汽車商行_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map)

    assert "【排氣量】" in messages[0]
    assert "【引擎燃料】" in messages[0]
    assert "Toyota Corolla Altis：1.8L" in messages[0]


def test_single_data_column_table_does_not_repeat_the_vendor_name_on_every_row():
    # 重現實際發生過的真實案例：篩選結果只有一台車、一家廠商符合條件時，
    # 模型仍然用「比較表」格式回答，只是只剩一欄資料。轉換後不該在每一行
    # 都重複印出同一個廠商名稱當前綴——這則訊息外層本來就已經有
    # `_split_by_vendor` 加上的「【廠商】」標題了，逐行再重複一次只是雜訊。
    answer = (
        "目前僅有一台符合條件，由「正峰汽車商行」提供：[1]\n\n"
        "| 比較項目 | 正峰汽車商行 |\n"
        "| :--- | :--- |\n"
        "| 廠牌 | LEXUS [1] |\n"
        "| 車型 | RX [1] |\n"
        "| 建議售價 | 44.8萬 [1] |\n"
    )
    source_map = {1: "永春中古汽車有限公司_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map)

    assert len(messages) == 1
    msg = messages[0]
    assert "【永春中古汽車有限公司】" in msg
    assert "【廠牌】" in msg
    assert "LEXUS" in msg
    assert "【建議售價】" in msg
    assert "44.8萬" in msg
    # 欄位名稱（廠商名稱）不該被當成每一行的前綴重複出現。
    assert "正峰汽車商行：" not in msg


def test_br_tags_outside_table_become_newlines():
    text = "第一行內容<br>第二行內容<br/>第三行內容"

    result = format_for_line(text)

    assert "<br" not in result
    assert result == "第一行內容\n第二行內容\n第三行內容"


def test_non_table_content_is_unaffected_by_table_conversion():
    text = "一般的**粗體**文字，含有直線符號，例如「A|B」這種文字，不該被誤判成表格。"

    result = format_for_line(text)

    assert result == "一般的粗體文字，含有直線符號，例如「A|B」這種文字，不該被誤判成表格。"


def test_existing_heading_bold_and_bullet_behavior_still_works():
    text = "## 標題\n**重點**\n* 項目一\n* 項目二\n\n\n\n多餘空行"

    result = format_for_line(text)

    assert result == "標題\n重點\n• 項目一\n• 項目二\n\n多餘空行"
