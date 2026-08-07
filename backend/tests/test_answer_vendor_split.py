from services.text_formatter import build_answer_messages


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
