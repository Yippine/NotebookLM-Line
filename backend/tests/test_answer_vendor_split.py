from services.text_formatter import build_answer_messages, count_unclassified_drops


def test_single_vendor_gets_one_labeled_message_with_no_citation_markers():
    answer = "McLaren 750S 的最大馬力是 750 匹 [1]。加速需要 2.8 秒 [2]。"
    source_map = {1: "McLaren_型錄.md", 2: "McLaren_型錄.md"}

    messages = build_answer_messages(answer, source_map)

    assert len(messages) == 1
    assert messages[0].startswith("【McLaren】")
    assert "750 匹" in messages[0]
    assert "[1]" not in messages[0] and "[2]" not in messages[0]


def test_no_citations_returns_plain_single_message():
    answer = "很抱歉，目前知識庫沒有相關資料。"

    messages = build_answer_messages(answer, {})

    assert messages == [answer]


def test_multi_vendor_splits_into_one_message_per_vendor():
    answer = (
        "根據您的需求，以下是相關資訊：\n\n"
        "McLaren 750S 的最大馬力是 750 匹 [1]。\n\n"
        "BMW M4 的最大馬力是 510 匹 [2]。"
    )
    source_map = {1: "McLaren_型錄.md", 2: "BMW_型錄.md"}

    messages = build_answer_messages(answer, source_map)

    assert len(messages) == 3

    lead_msg, mclaren_msg, bmw_msg = messages
    # 整個回答的開場白會變成自己獨立的、不帶標籤的開頭訊息，
    # 而不是被併入廠商 1 的訊息泡泡中。
    assert lead_msg == "根據您的需求，以下是相關資訊："

    assert mclaren_msg.startswith("【McLaren】")
    assert "根據您的需求" not in mclaren_msg
    assert "750 匹" in mclaren_msg
    assert "BMW" not in mclaren_msg  # McLaren 的訊息不應包含 BMW 的內容
    assert "[1]" not in mclaren_msg

    assert bmw_msg.startswith("【BMW】")
    assert "510 匹" in bmw_msg
    assert "[2]" not in bmw_msg


def test_hyphen_range_citation_is_stripped_and_treated_as_multi_vendor():
    # NotebookLM 有時會用 "[1-3]" 這種連字號範圍來標記一次引用多個
    # 來源的句子，而不是逐一列出 "[1, 2, 3]"。這種格式也該被辨識並
    # 從呈現給使用者的文字中移除。
    answer = (
        "目前共有三家廠商的資訊 [1-3]。\n\n"
        "McLaren 750S 的最大馬力是 750 匹 [1]。\n\n"
        "BMW M4 的最大馬力是 510 匹 [2]。"
    )
    source_map = {1: "McLaren_型錄.md", 2: "BMW_型錄.md", 3: "Audi_型錄.md"}

    messages = build_answer_messages(answer, source_map)

    assert not any("[1-3]" in m for m in messages)
    assert not any("1-3" in m for m in messages)


def test_mixed_citation_paragraph_merges_into_preceding_vendor_group():
    answer = (
        "McLaren 750S 的保固是 3 年 [1]。\n\n"
        "兩者的保固政策相似 [1, 2]。\n\n"
        "BMW M4 的保固是 2 年 [2]。"
    )
    source_map = {1: "McLaren_型錄.md", 2: "BMW_型錄.md"}

    messages = build_answer_messages(answer, source_map)

    # 這個混合引用的中間段落無法再進一步切分——它會併入前面的
    # 廠商分組，而不是自己單獨變成一則不帶標籤的訊息
    assert len(messages) == 2
    assert "兩者的保固政策相似" in messages[0]
    assert "[1, 2]" not in messages[0]


def test_uncited_lead_in_before_multi_vendor_comparison_stays_in_place():
    """回歸測試：重現真實發生過的案例——緊接在『多廠商比較』內容之前、
    自己沒有引用的鋪陳句（例如「這兩款車型的核心差異如下：」），
    必須維持在它鋪陳的那段比較內容*前面*，不能被推到訊息最後面、
    變成一行掛在結尾、後面卻沒接著任何內容的孤兒句子。

    根本原因：`_split_by_vendor` 裡引用了多個廠商的原子，會直接
    `buckets[last_vendor].append(atom)` 接到目前的廠商分桶，卻沒有
    先把暫存的 `pending`（這句沒有引用的鋪陳句）一併 flush 進去——
    於是鋪陳句一路被延後到 for 迴圈結束、`pending` 才整批被接到
    分桶最後面，順序因此被打亂。"""
    answer = (
        "McLaren 750S 的保固是 3 年 [1]。\n\n"
        "中彰投汽車有限公司提供的 C250 保固是 2 年 [2]。\n\n"
        "這兩款車型的核心差異如下：\n\n"
        "- 保固差異：McLaren 3 年，中彰投 2 年 [1, 2]。\n"
        "- 售價差異：McLaren 較高，中彰投較低 [1, 2]。"
    )
    source_map = {1: "McLaren_型錄.md", 2: "中彰投汽車有限公司_型錄.md"}

    messages = build_answer_messages(answer, source_map)

    last_msg = messages[-1]
    lead_pos = last_msg.index("這兩款車型的核心差異如下")
    bullet_pos = last_msg.index("保固差異")
    assert lead_pos < bullet_pos, "鋪陳句應該出現在它介紹的比較內容之前，而不是被推到最後"


def test_unprefixed_filename_is_dropped_not_sent_unclassified():
    answer = "車輛的保固期是 3 年 [1]。\n\n特殊活動的折扣期限是本月底 [2]。"
    source_map = {1: "McLaren_型錄.md", 2: "notes.md"}

    messages = build_answer_messages(answer, source_map)

    assert not any(m.startswith("【未分類】") for m in messages)
    assert any(m.startswith("【McLaren】") for m in messages)


def test_all_unclassified_falls_back_to_apology_instead_of_silence():
    answer = "車輛的保固期是 3 年 [1]。\n\n特殊活動的折扣期限是本月底 [2]。"
    source_map = {1: "notes.md", 2: "misc.md"}

    messages = build_answer_messages(answer, source_map)

    assert len(messages) == 1
    assert "未分類" not in messages[0]
    assert "廠牌" in messages[0]  # 這是備援的致歉訊息，而不是被悄悄丟棄


def test_count_unclassified_drops_reports_how_much_content_was_discarded():
    """`count_unclassified_drops` 讓呼叫端能知道「未分類」捨棄了幾段——
    這種捨棄使用者完全無感，沒有這個訊號就不會被發現。"""
    answer = "車輛的保固期是 3 年 [1]。\n\n特殊活動的折扣期限是本月底 [2]。"

    # 一段可歸屬、一段不可歸屬（notes.md 沒有廠商前綴）——只丟棄那一段。
    mixed_source_map = {1: "McLaren_型錄.md", 2: "notes.md"}
    assert count_unclassified_drops(answer, mixed_source_map) == 1

    # 兩段都不可歸屬——兩者都會被歸到同一個「未分類」桶（因為
    # `vendor_from_title` 對兩個檔名都回傳同一個字串「未分類」），
    # 合併成一段被捨棄，而不是兩段各自獨立被丟棄。
    all_unclassified_source_map = {1: "notes.md", 2: "misc.md"}
    assert count_unclassified_drops(answer, all_unclassified_source_map) == 1

    # 完全沒有未分類內容時，不該誤報。
    clean_source_map = {1: "McLaren_型錄.md", 2: "BMW_型錄.md"}
    assert count_unclassified_drops(answer, clean_source_map) == 0


def test_interleaved_bullet_list_still_splits_cleanly_by_vendor():
    """重現真實發生過的失敗案例：單一個項目符號清單，其中每一行
    各自引用不同的廠商（依年份排序，而不是依經銷商分組），
    項目符號之間也沒有空行可用來切分段落。"""
    answer = (
        "根據提供的資料，車型為 Corolla Cross 的車輛共有以下幾款：\n\n"
        "• 2024 年：尊爵版，價格為 85.9 萬 [1]。\n"
        "• 2023 年：豪華版，價格為 71.1 萬 [1]。\n"
        "• 2020 年：尊爵版，價格為 97.4 萬 [2]。\n"
        "• 2018 年：入門型，價格為 68.7 萬 [1]。\n"
        "• 2015 年：兩台，尊爵版與豪華版 [2]。"
    )
    source_map = {1: "SUM_20260715.md", 2: "8891_20260715.md"}

    messages = build_answer_messages(answer, source_map)

    assert len(messages) == 3
    lead_msg, sum_msg, e8891_msg = messages
    assert "根據提供的資料" in lead_msg
    assert sum_msg.startswith("【SUM】")
    assert "2024" in sum_msg and "2023" in sum_msg and "2018" in sum_msg
    assert "2020" not in sum_msg and "2015" not in sum_msg

    assert e8891_msg.startswith("【8891】")
    assert "2020" in e8891_msg and "2015" in e8891_msg
    assert "2024" not in e8891_msg


def test_uncited_intro_line_attaches_to_the_vendor_it_introduces_not_the_prior_one():
    """回歸測試：每個經銷商自己的編號介紹行（本身沒有引用）必須
    留在*那個*經銷商的說明裡，而不能滲入前一個經銷商的訊息中——
    否則多個經銷商最終會共用同一個訊息泡泡，而不是各自擁有
    自己的訊息。"""
    answer = (
        "根據來源資料，目前有多家廠商擁有現車庫存：\n\n"
        "1. 聯絡資訊：(02)123-6797\n"
        "這家廠商擁有的車款最為豐富，包含：\n"
        "• 2024 年旗艦版 [1]。\n"
        "• 2020 年豪華版 [1]。\n\n"
        "2. 聯絡資訊：(02)123-7872\n"
        "這家廠商所持有的車款包括：\n"
        "• 2021 年旗艦版 [2]。\n\n"
        "3. 聯絡資訊：(02)435-5212\n"
        "這家廠商持有的車款包括：\n"
        "• 2016 年旗艦版 [3]。"
    )
    source_map = {1: "伊利安_20260720.md", 2: "jwincar_20260720.md", 3: "棋勝汽車_20260720.md"}

    messages = build_answer_messages(answer, source_map)

    assert len(messages) == 4
    lead, dealer1, dealer2, dealer3 = messages
    assert "根據來源資料" in lead

    assert dealer1.startswith("【伊利安】")
    assert "(02)123-6797" in dealer1
    assert "(02)123-7872" not in dealer1  # 經銷商 2 的介紹不應滲入這裡

    assert dealer2.startswith("【jwincar】")
    assert "(02)123-7872" in dealer2
    assert "(02)123-6797" not in dealer2
    assert "(02)435-5212" not in dealer2  # 經銷商 3 的介紹不應滲入這裡

    assert dealer3.startswith("【棋勝汽車】")
    assert "(02)435-5212" in dealer3


def test_overall_intro_becomes_standalone_leading_message():
    """回歸測試：出現在任何廠商內容之前、為整個回答鋪陳的句子
    （例如「目前共有三家廠商擁有現車庫存：」）必須自己獨立成一則
    開頭訊息，而不能被黏在第一個廠商的「【廠商】」標題底下，
    好像它是那個廠商自己的文字一樣。"""
    answer = (
        "根據來源文件，目前共有三家廠商擁有 Honda CRV 的現車庫存，"
        "以下為您整理各家的車款資訊與聯絡方式：\n\n"
        "聯絡資訊：(02)123-6797 (Line ID: yilian@01)\n"
        "這家廠商提供的 CRV 車款年份涵蓋範圍最廣：\n"
        "• 2024 年 旗艦版：具備「里程少」與「剛做完大保養」的特色 [1]。"
    )
    source_map = {1: "伊利安_20260720.md"}

    messages = build_answer_messages(answer, source_map)

    assert len(messages) == 2
    lead_msg, vendor_msg = messages
    assert lead_msg == "根據來源文件，目前共有三家廠商擁有 Honda CRV 的現車庫存，以下為您整理各家的車款資訊與聯絡方式："
    assert vendor_msg.startswith("【伊利安】")
    assert "根據來源文件" not in vendor_msg


def test_vendor_date_filename_with_no_separator():
    """新的命名慣例：{廠商}{西元年月日}.md，例如 McLaren20260715.md。"""
    answer = (
        "McLaren 750S 的最大馬力是 750 匹 [1]。\n\n"
        "BMW M4 的最大馬力是 510 匹 [2]。"
    )
    source_map = {1: "McLaren20260715.md", 2: "BMW20260714.md"}

    messages = build_answer_messages(answer, source_map)

    assert len(messages) == 2
    mclaren_msg, bmw_msg = messages
    assert mclaren_msg.startswith("【McLaren】")
    assert "750 匹" in mclaren_msg
    assert bmw_msg.startswith("【BMW】")
    assert "510 匹" in bmw_msg


def test_vendor_messages_reordered_by_most_recent_update_first():
    """廠商訊息氣泡的出現順序，要依檔名裡的更新日期排序（最近更新的
    廠商排最前面），而不是依文字裡第一次提到的順序——即使文字裡先
    提到的是比較舊的廠商，訊息順序也要把它排到後面。"""
    answer = (
        "McLaren 750S 的最大馬力是 750 匹 [1]。\n\n"
        "BMW M4 的最大馬力是 510 匹 [2]。\n\n"
        "Audi RS6 的最大馬力是 600 匹 [3]。"
    )
    # 文字裡的提及順序是 McLaren → BMW → Audi，但更新日期最新的其實
    # 是 Audi（20260720），其次是 BMW（20260710），McLaren 最舊。
    source_map = {
        1: "McLaren20260701.md",
        2: "BMW20260710.md",
        3: "Audi20260720.md",
    }

    messages = build_answer_messages(answer, source_map)

    vendor_order = [m.split("】")[0].lstrip("【") for m in messages]
    assert vendor_order == ["Audi", "BMW", "McLaren"]


def test_vendor_messages_without_date_filename_sort_after_dated_vendors():
    """檔名不符合「結尾 8 位數日期」慣例、抓不出更新日期的廠商
    （例如舊式的 `{廠商}_...` 命名），視為最舊，排在所有有日期的
    廠商之後；彼此之間則維持原本依文字出現順序排列。"""
    answer = (
        "McLaren 750S 的最大馬力是 750 匹 [1]。\n\n"
        "BMW M4 的最大馬力是 510 匹 [2]。\n\n"
        "Audi RS6 的最大馬力是 600 匹 [3]。"
    )
    source_map = {
        1: "McLaren_型錄.md",  # 舊式命名，沒有日期
        2: "BMW20260710.md",
        3: "Audi_型錄.md",  # 舊式命名，沒有日期
    }

    messages = build_answer_messages(answer, source_map)

    vendor_order = [m.split("】")[0].lstrip("【") for m in messages]
    assert vendor_order == ["BMW", "McLaren", "Audi"]
