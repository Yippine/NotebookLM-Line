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


def test_single_data_column_header_that_is_a_car_model_is_preserved_as_a_line():
    # 重現實際發生過的真實案例：篩選結果只有一台車符合條件，模型用
    # 「比較項目 | 車型名稱」這種只有一欄的表格回答，這一欄的欄位
    # 名稱本身就是車型（不是廠商名稱），而且表格裡沒有另外一列已經
    # 記載車型資訊——這種情況下這個車型名稱不能整個遺失，要保留
    # 成一行「車型：」資訊。
    answer = (
        "| 比較項目 | A180 1.3 運動版 原版件 23P |\n"
        "| :--- | :--- |\n"
        "| 來源車商 | 正峰汽車商行 [6] |\n"
        "| 年份 | 2022 [6] |\n"
        "| 建議售價 | 112.8萬 [6] |\n"
    )
    source_map = {6: "正峰汽車商行_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map)

    assert len(messages) == 1
    msg = messages[0]
    assert msg.count("正峰汽車商行") == 1  # 只有外層標題那一次，「來源車商」那行被判斷成重複拿掉了
    assert "－－－－－\n－－－－－" not in msg  # 拿掉那一行後，前後的分隔線不該疊成連續兩條
    assert "車型：A180 1.3 運動版 原版件 23P" in msg
    assert "年份：2022" in msg
    assert "建議售價：112.8萬" in msg
    assert "來源車商" not in msg


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
    # 「重複值」而收合掉（`_convert_markdown_tables` 的 collapse_first_col
    # 這一關）。同一家廠商比較兩款車時，後續會依欄（車型）轉置分組，
    # 見 `test_same_model_compared_across_vendors_transposes_into_one_bubble_per_vendor`
    # 同樣的邏輯——即使只有一家廠商，依車型分組也比依規格項目分組更
    # 有意義。
    answer = (
        "| 比較項目 | Toyota Corolla Altis | Toyota Corolla Cross |\n"
        "| :--- | :--- | :--- |\n"
        "| 排氣量 | 1.8L [1] | 1.8L [1] |\n"
        "| 引擎燃料 | 汽油 [1] | 汽油 [1] |\n"
    )
    source_map = {1: "中信汽車商行_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map)

    assert len(messages) == 1
    msg = messages[0]
    assert msg.startswith("【中信汽車商行】")
    assert "【Toyota Corolla Altis】" in msg
    assert "【Toyota Corolla Cross】" in msg
    assert "排氣量：1.8L" in msg
    assert "引擎燃料：汽油" in msg


def test_single_data_column_table_does_not_repeat_the_vendor_name_on_every_row():
    # 重現實際發生過的真實案例：篩選結果只有一台車、一家廠商符合條件時，
    # 模型仍然用「比較表」格式回答，只是只剩一欄資料。轉換後不該在每一行
    # 都重複印出同一個廠商名稱當前綴——這則訊息外層本來就已經有
    # `_split_by_vendor` 加上的「【廠商】」標題了，逐行再重複一次只是雜訊。
    # 格式統一用「屬性：值」單行呈現（不再是「【屬性】」括號單獨一行、
    # 值另起一行），跟多欄／轉置後的格式一致。
    answer = (
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
    assert "廠牌：LEXUS" in msg
    assert "車型：RX" in msg
    assert "建議售價：44.8萬" in msg
    # 欄位名稱（廠商名稱）不該被當成每一行的前綴重複出現。
    assert "正峰汽車商行：" not in msg
    # 這一欄的欄位名稱本身剛好是（寫錯的）廠商名稱，而表格裡已經有
    # 「車型」這一列了，不該額外多加一行把它當成車型資訊。
    assert "正峰汽車商行" not in msg


def test_citation_inside_a_quoted_example_in_the_closing_suggestion_does_not_mislabel_it():
    # 重現實際發生過的真實案例：廠商總覽表格後面，結尾的通用建議句
    # 舉了一個範例句給使用者參考（「例如想看『卡司汽車 [6] 的現車』」），
    # 模型卻把引用編號直接標在範例句裡。這個範例句本身適用於所有
    # 廠商，不該因為裡面剛好舉了卡司汽車當例子、又剛好帶了引用編號，
    # 就把整段結尾建議判斷成「只跟卡司汽車有關」而貼上錯誤的標題
    # ——結尾建議沒有自己的引用來源，應該是附加在最後一家廠商
    # （卡司汽車，表格裡最後一列）的訊息後面，而不是自成一則。
    answer = (
        "目前在庫現車分布於 **8 家車商**，以下為您整理各車商的現車總台數與最低建議售價總覽：\n\n"
        "| 廠商名稱 | 符合條件台數 | 最低建議售價 |\n"
        "| :--- | :---: | :---: |\n"
        "| **中彰投汽車有限公司** [1] | 42 台 [1] | 4.8 萬 [2] |\n"
        "| **卡司汽車** [6] | 17 台 [6] | 12.8 萬 [7] |\n\n"
        "如果想先深入了解哪一家廠商的現車清單，或是想尋找特定車型、年份、"
        "顏色的現車（例如想看**「卡司汽車 [6] 的現車」**，或是比較"
        "**「Altis 與 Focus 的規格差異」**），歡迎隨時用具體的車商、"
        "車型重新提問，我會為您提供詳細的車輛資訊。"
    )
    source_map = {
        1: "中彰投汽車有限公司_20260811.md", 2: "中彰投汽車有限公司_20260811.md",
        6: "卡司汽車_20260811.md", 7: "卡司汽車_20260811.md",
    }

    messages = build_answer_messages(format_for_line(answer), source_map)

    # 開場白（沒有引用、在任何廠商之前出現）自己獨立成一則不帶標籤
    # 的開頭訊息，接著是兩家廠商各自一則——結尾建議沒有變成第 4 則
    # 貼錯標籤的孤兒訊息，而是接在最後一家廠商（卡司汽車）後面。
    assert len(messages) == 3
    assert messages[1].startswith("【中彰投汽車有限公司】")
    assert messages[2].startswith("【卡司汽車】")
    assert "如果想先深入了解哪一家廠商的現車清單" in messages[2]


def test_multi_row_vendor_overview_table_splits_into_one_message_per_vendor():
    # 重現實際發生過的真實案例＋使用者明確要求的呈現方式：廠商總覽
    # 表格裡每一列都能唯一歸屬到剛好一家廠商時（不是同一列橫跨多家
    # 廠商的比較表），應該拆成一家廠商一則訊息、各自獨立的泡泡，
    # 而不是全部擠在同一則訊息裡。
    answer = (
        "| 廠商名稱 | 符合條件台數 | 最低建議售價 |\n"
        "| :--- | :---: | :---: |\n"
        "| **中彰投汽車有限公司** [1] | 42 台 [1] | 4.8 萬 [1] |\n"
        "| **中信汽車商行** [2] | 6 台 [2] | 25.8 萬 [2] |\n"
        "| **力彰汽車商行** [3] | 7 台 [3] | 16.8 萬 [3] |\n"
        "| **匯新中古汽車有限公司** [4] | 27 台 [4] | 22.8 萬 [4] |\n"
        "| **卡司汽車** [5] | 17 台 [5] | 12.8 萬 [5] |\n"
    )
    source_map = {
        1: "中彰投汽車有限公司_20260811.md", 2: "中信汽車商行_20260811.md",
        3: "力彰汽車商行_20260811.md", 4: "匯新中古汽車有限公司_20260811.md",
        5: "卡司汽車_20260811.md",
    }

    messages = build_answer_messages(format_for_line(answer), source_map)

    assert len(messages) == 5  # 一家廠商一則
    for message, vendor_file in zip(messages, source_map.values()):
        vendor = vendor_file.split("_")[0]
        assert message.startswith(f"【{vendor}】")
        # 重現實際發生過的真實案例：這種表格的列標籤內容剛好就是
        # 廠商名稱本身，不該跟 `_split_by_vendor` 外層加的「【廠商】」
        # 標題重複疊加、印兩次。
        assert message.count(f"【{vendor}】") == 1
    assert "－－－－－" not in "".join(messages)  # 分隔線已經升級成真正的段落，不留殘餘
    assert "42 台" in messages[0]
    assert "12.8 萬" in messages[4]


def test_genuine_comparison_table_with_multi_vendor_rows_still_stays_merged():
    # 對照組：確保「每一列橫跨多家廠商」的真正比較表，不會被誤判成
    # 「每一列唯一歸屬到一家廠商」而被拆散——這正是當初要用分隔線
    # （而不是空行）的原因，見 `_convert_markdown_tables` 的說明。
    answer = (
        "以下為比較表：\n\n"
        "| 項目 | Toyota Corolla Altis | Toyota Corolla Cross |\n"
        "| :--- | :--- | :--- |\n"
        "| 排氣量 | 包含 1.8L、2.0L [1-3] | 皆為 1.8L [2, 3] |\n"
        "| 引擎燃料 | 汽油、油電混合 [1-3] | 皆為汽油 [2, 3] |\n\n"
        "正峰汽車商行的車輛規格如下 [2]。\n\n"
        "永春中古汽車有限公司的車輛規格如下 [3]。"
    )
    source_map = {
        1: "中彰投汽車有限公司_20260811.md",
        2: "正峰汽車商行_20260811.md",
        3: "永春中古汽車有限公司_20260811.md",
    }

    messages = build_answer_messages(format_for_line(answer), source_map)

    table_messages = [m for m in messages if "【排氣量】" in m]
    assert len(table_messages) == 1
    assert "【引擎燃料】" in table_messages[0]


def test_same_model_compared_across_vendors_transposes_into_one_bubble_per_vendor():
    # 重現實際發生過的真實案例＋使用者明確要求的呈現方式：使用者問
    # 同一款車在不同廠商的規格比較（例如「Nissan Kicks」剛好 2 家
    # 廠商都有），模型很自然會用「比較項目」當第一欄、每家廠商各佔
    # 一欄的表格回答。這種情況下每一欄（不是每一列）才唯一對應到
    # 一家廠商，應該轉置成「以廠商為單位」，讓同一家廠商的完整規格
    # 集中在同一個氣泡裡，而不是逐項目交錯呈現在同一則訊息裡。
    answer = (
        "目前在庫車輛中共有 **2 台 Nissan Kicks**，分別由 **中彰投汽車有限公司** [1] 與 "
        "**安心汽車** [2] 提供。以下為您整理這兩部車輛的規格與售價比較：\n\n"
        "| 比較項目 | 中彰投汽車有限公司 [1] | 安心汽車 [2] |\n"
        "| :--- | :--- | :--- |\n"
        "| **車型年份** | 2020 年 [1] | 2025 年 [2] |\n"
        "| **建議售價** | 32.8 萬 [1] | 62.8 萬 [2] |\n\n"
        "這兩台車皆為白色車款 [1, 2]。"
    )
    source_map = {1: "中彰投汽車有限公司_20260811.md", 2: "安心汽車_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map)

    # 開場白自己一則，接著兩家廠商各自一則——不是逐項目交錯的單一表格。
    assert len(messages) == 3
    assert messages[1].startswith("【中彰投汽車有限公司】")
    assert "車型年份：2020 年" in messages[1]
    assert "建議售價：32.8 萬" in messages[1]
    assert messages[2].startswith("【安心汽車】")
    assert "車型年份：2025 年" in messages[2]
    assert "建議售價：62.8 萬" in messages[2]
    # 沒有任何一則訊息把兩家廠商的資料混在同一段裡。
    assert "安心汽車" not in messages[1]
    assert "中彰投汽車有限公司" not in messages[2]


def test_redundant_vendor_name_field_within_each_transposed_item_is_dropped():
    # 重現實際發生過的真實案例：問「10 台賓士」的完整比較表，模型
    # 自己在表格裡多加了一列「車商名稱」來標示每台車屬於哪家廠商。
    # 轉置成一台車一個區塊、再依廠商分則之後，每個區塊底下都會各自
    # 重複出現一行「車商名稱：xxx」——這則訊息外層已經有「【廠商】」
    # 標題了，逐項重複同一個廠商名稱只是雜訊，應該整行拿掉。
    answer = (
        "在目前的現有庫存中，共有 **3 台**賓士現車，分別由**中彰投汽車有限公司** [1, 2] 以及"
        "**卡司汽車** [3] 提供。以下為您整理所有賓士車款的規格比較：\n\n"
        "| 比較項目 | 車款 1 | 車款 2 | 車款 3 |\n"
        "| :--- | :--- | :--- | :--- |\n"
        "| **車商名稱** | **中彰投汽車有限公司** [1] | **中彰投汽車有限公司** [1] | **卡司汽車** [3] |\n"
        "| **車型** | E-Class E200 | C-Class C250 | A-Class A180 |\n"
        "| **建議售價** | 65.0萬 | 25.8萬 | 36.8萬 |\n"
    )
    source_map = {
        1: "中彰投汽車有限公司_20260811.md",
        3: "卡司汽車_20260811.md",
    }

    messages = build_answer_messages(format_for_line(answer), source_map)

    zhongzhang_msg = next(m for m in messages if m.startswith("【中彰投汽車有限公司】"))
    kasi_msg = next(m for m in messages if m.startswith("【卡司汽車】"))

    # 車型、售價這些真正有用的資訊都還在。
    assert "車型：E-Class E200" in zhongzhang_msg
    assert "車型：A-Class A180" in kasi_msg
    # 但「車商名稱：xxx」這種整行剛好等於外層廠商標題的內容被拿掉了
    # ——只留下最上面那個外層標題本身，不會逐項重複。
    assert zhongzhang_msg.count("中彰投汽車有限公司") == 1
    assert kasi_msg.count("卡司汽車") == 1
    assert "車商名稱" not in zhongzhang_msg
    assert "車商名稱" not in kasi_msg


def test_summary_heading_after_transposed_table_stays_before_its_own_bullets():
    # 重現實際發生過的真實案例（2026-08-17 log）：轉置表格之後，緊接著
    # 一段「這兩台車的主要差異在於：」開場句 + 每一點都同時引用兩家
    # 廠商的條列比較（例如「...為 1.6L [1]；而...為 1.8L [2]。」）。
    # 這句開場句自己沒有引用，而 `_split_by_vendor` 原本的 bug 是：
    # 「引用了不只一個廠商」的原子會直接接到目前分桶，卻沒有先把
    # 還在等待的開場句一併接進去——導致開場句被延後到所有條列比較
    # 之後，變成孤零零掛在訊息最後、後面卻接著跟它無關的建議句。
    answer = (
        "卡司汽車與中彰投汽車有限公司所提供的 Benz C-Class 車型詳細規格與差異如下表：\n\n"
        "| 比較項目 | 卡司汽車 | 中彰投汽車有限公司 |\n"
        "| :--- | :--- | :--- |\n"
        "| **廠牌** | BENZ [1] | Benz [2] |\n"
        "| **排氣量** | 1.6L [1] | 1.8L [2] |\n\n"
        "這兩台車的主要差異在於：\n"
        "- **排氣量差異**：**卡司汽車**的 C180 排氣量為 **1.6L** [1]；而**中彰投汽車有限公司**"
        "的 C250 排氣量較大，為 **1.8L** [2]。\n"
        "- **售價與配備**：C180 建議售價為 **26.8萬** [1]；C250 售價為 **25.8萬** [2]。\n\n"
        "如果想比較這兩款車與其他同級距車款（例如 C300 或 E200）的規格差異，歡迎隨時告訴我。"
    )
    source_map = {1: "卡司汽車_20260814.md", 2: "中彰投汽車有限公司_20260814.md"}

    messages = build_answer_messages(format_for_line(answer), source_map)

    # 開場白單獨一則、卡司汽車一則，最後中彰投的那一則裡帶著「這兩台車
    # 的主要差異在於：」開場句、緊接著它自己的條列比較、最後才是建議句
    # ——三者維持原文順序，而不是開場句被推到建議句前面孤立一行。
    zhongzhang_msg = next(m for m in messages if m.startswith("【中彰投汽車有限公司】"))
    heading_pos = zhongzhang_msg.index("這兩台車的主要差異在於")
    bullet_pos = zhongzhang_msg.index("排氣量差異")
    suggestion_pos = zhongzhang_msg.index("如果想比較")
    assert heading_pos < bullet_pos < suggestion_pos


def test_a2ui_json_block_from_notebooklm_web_search_is_stripped_entirely():
    # 重現實際發生過的真實案例：使用者問了跟車輛完全無關的問題（颱風
    # 動態），NotebookLM 觸發了它自己的網路搜尋功能，回答裡夾帶一段
    # `<a2ui-json>...</a2ui-json>`——這是它網頁版用來顯示「可匯入
    # 來源卡片」的內部 UI 元件描述，含一大包 JSON 跟 base64 下載網址，
    # 對 LINE 純文字訊息完全沒有意義，原封不動送出去只會是一大坨
    # 看不懂的亂碼，必須整段拿掉。
    answer = (
        "以下是為您搜尋到的颱風最新動態最新資訊：\n\n"
        "目前無颱風直接威脅台灣。\n\n"
        "<a2ui-json>\n"
        '[{"version": "v0.9", "createSurface": {"surfaceId": "source-import-typhoon"}},\n'
        '{"version": "v0.9", "updateComponents": {"surfaceId": "x", "components": '
        '[{"id": "root", "component": "SourceImportCard", "sources": [{"url": "https://example.com/a?token=abc123"}]}]}}]\n'
        "</a2ui-json>"
    )

    result = format_for_line(answer)

    assert "<a2ui-json>" not in result
    assert "SourceImportCard" not in result
    assert "example.com" not in result
    assert "目前無颱風直接威脅台灣" in result


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


def test_single_vendor_single_car_attribute_table_has_no_leaked_row_separator():
    # 重現實際在 LINE 上看到的真實案例：只有一家廠商、僅一台車符合條件時，
    # NotebookLM 把它的規格整理成「逐列一個屬性」的表格（第一欄是屬性
    # 名稱，第二欄固定是這台車，只有一個資料欄）。這種表格既不構成
    # 「橫跨多家廠商」（只有一家，不會被展開成段落），欄位名稱（各屬性）
    # 也都只出現一次（不構成「同一欄位重複出現在多列」，不會被轉置）
    # ——兩種轉換條件都不成立，內部用來占位的列分隔線
    # （`_ROW_SEPARATOR`，字面上是一整排「－」）過去會原封不動留在文字裡，
    # 直接送到使用者手上，變成一整排看不懂的「－－－－－」。
    text = (
        "目前在所有車商的庫存中，僅有一台 SUZUKI IGNIS，由三隻小豬車坊提供，"
        "該車輛的詳細規格與車況如下：\n\n"
        "| 比較項目 | SUZUKI IGNIS 詳細資訊 |\n"
        "| :--- | :--- |\n"
        "| 年份 | 2020 年 [1] |\n"
        "| 顏色 | 藍色 [1] |\n"
        "| 變速系統 | 手自排 [1] |\n"
        "| 排氣量 | 1.2L [1] |\n"
    )
    source_map = {1: "三隻小豬車坊20260101.md"}

    messages = build_answer_messages(format_for_line(text), source_map)

    for message in messages:
        assert "－" not in message

    vendor_message = next(m for m in messages if m.startswith("【三隻小豬車坊】"))
    assert (
        vendor_message == "【三隻小豬車坊】\n\n"
        "車型：SUZUKI IGNIS 詳細資訊\n"
        "年份：2020 年\n"
        "顏色：藍色\n"
        "變速系統：手自排\n"
        "排氣量：1.2L"
    )
