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

    assert len(messages) == 2

    mclaren_msg, bmw_msg = messages
    assert mclaren_msg.startswith("【McLaren】")
    assert "根據您的需求" in mclaren_msg  # unattributed leading paragraph merges forward
    assert "750 匹" in mclaren_msg
    assert "BMW" not in mclaren_msg  # McLaren message excludes BMW content
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

    # the mixed-citation middle paragraph can't be split further — it merges
    # into whichever vendor group precedes it rather than becoming its own
    # unlabeled message
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
    assert "廠牌" in messages[0]  # the fallback apology, not silently dropped


def test_interleaved_bullet_list_still_splits_cleanly_by_vendor():
    """Reproduces the real failure: one bulleted list whose items cite
    different vendors line by line (sorted by year, not grouped by dealer),
    with no blank lines between bullets to split the paragraph on."""
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

    assert len(messages) == 2
    sum_msg, e8891_msg = messages
    assert sum_msg.startswith("【SUM】")
    assert "2024" in sum_msg and "2023" in sum_msg and "2018" in sum_msg
    assert "2020" not in sum_msg and "2015" not in sum_msg

    assert e8891_msg.startswith("【8891】")
    assert "2020" in e8891_msg and "2015" in e8891_msg
    assert "2024" not in e8891_msg


def test_vendor_date_filename_with_no_separator():
    """New convention: {廠商}{西元年月日}.md, e.g. McLaren20260715.md."""
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
