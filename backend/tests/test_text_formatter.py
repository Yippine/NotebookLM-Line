from services.text_formatter import format_for_line


def test_format_for_line_removes_notebooklm_numeric_citations() -> None:
    answer = "第一段內容 [1, 2]。\n\n第二段內容[3]，保留一般的 [重要] 標記。"

    assert format_for_line(answer) == (
        "第一段內容。\n\n第二段內容，保留一般的 [重要] 標記。"
    )
