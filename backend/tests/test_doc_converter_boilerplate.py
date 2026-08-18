from services.doc_converter import _strip_pagination_artifacts


def test_strips_standalone_page_number_lines():
    text = (
        "車輛規格內容第一頁\n"
        "第 1 頁\n"
        "更多規格內容\n"
        "第 2 頁 共 10 頁\n"
        "3 / 10\n"
        "Page 4 of 10\n"
        "結尾內容"
    )

    result = _strip_pagination_artifacts(text)

    assert "第 1 頁" not in result
    assert "第 2 頁 共 10 頁" not in result
    assert "3 / 10" not in result
    assert "Page 4 of 10" not in result
    assert "車輛規格內容第一頁" in result
    assert "更多規格內容" in result
    assert "結尾內容" in result


def test_dedupes_repeated_footer_like_lines_but_keeps_one_copy():
    footer = "○○汽車股份有限公司 電話：02-1234-5678 www.example.com"
    text = "\n".join(
        [
            "第一頁內容",
            footer,
            "第二頁內容",
            footer,
            "第三頁內容",
            footer,
        ]
    )

    result = _strip_pagination_artifacts(text)

    # 只留一份，不是整段刪掉——即使誤判也不會真的遺失資訊。
    assert result.count(footer) == 1
    assert "第一頁內容" in result
    assert "第二頁內容" in result
    assert "第三頁內容" in result


def test_does_not_touch_repeated_vehicle_data_without_footer_like_pattern():
    # 「顏色：白色」這種可能在不同車輛紀錄裡剛好重複出現的真實資料，
    # 不含電話/網址/版權關鍵字，即使重複超過 3 次也不該被砍掉或去重。
    text = "\n".join(
        [
            "車型：Altis\n顏色：白色",
            "車型：Corolla Cross\n顏色：白色",
            "車型：Camry\n顏色：白色",
            "車型：RAV4\n顏色：白色",
        ]
    )

    result = _strip_pagination_artifacts(text)

    assert result.count("顏色：白色") == 4


def test_line_that_merely_contains_digits_is_not_mistaken_for_a_page_number():
    text = "車輛里程：35000 公里\n售價：850000 元"

    result = _strip_pagination_artifacts(text)

    assert "車輛里程：35000 公里" in result
    assert "售價：850000 元" in result
