from services.text_formatter import build_answer_messages


def test_single_vendor_stays_one_message_with_shared_sources():
    answer = "McLaren 750S 的最大馬力是 750 匹 [1]。加速需要 2.8 秒 [2]。"
    source_map = {1: "McLaren_型錄.md", 2: "McLaren_型錄.md"}

    messages = build_answer_messages(answer, source_map)

    assert len(messages) == 2
    assert messages[0] == answer
    assert "【" not in messages[0]  # no vendor header for the single-vendor case
    assert "[1] McLaren_型錄.md" in messages[1]
    assert "[2] McLaren_型錄.md" in messages[1]


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
    assert "[1] McLaren_型錄.md" in mclaren_msg
    assert "BMW" not in mclaren_msg.split("參考來源")[0]  # McLaren body excludes BMW content

    assert bmw_msg.startswith("【BMW】")
    assert "510 匹" in bmw_msg
    assert "[2] BMW_型錄.md" in bmw_msg


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


def test_unprefixed_filename_becomes_unclassified_vendor():
    answer = "車輛的保固期是 3 年 [1]。\n\n特殊活動的折扣期限是本月底 [2]。"
    source_map = {1: "McLaren_型錄.md", 2: "notes.md"}

    messages = build_answer_messages(answer, source_map)

    assert any(m.startswith("【未分類】") for m in messages)
    assert any(m.startswith("【McLaren】") for m in messages)
