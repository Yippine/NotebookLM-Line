from services.text_formatter import (
    build_answer_messages,
    format_for_line,
    _correct_vendor_citation_mismatches,
)

# 這個筆記本裡全部 8 家廠商——在 `nlm_service.ask_question` 的真實流程中，
# 這是從整個筆記本的來源檔名反推出來的，不只是這則答案實際引用到的
# 那幾個，所以測試也要模擬同樣「範圍比 source_map 更大」的情況。
_ALL_VENDORS = {
    "中信汽車商行", "中彰投汽車有限公司", "力彰汽車商行", "匯新中古汽車有限公司",
    "卡司汽車", "安心汽車", "正峰汽車商行", "永春中古汽車有限公司",
}


def test_prose_vendor_name_that_contradicts_its_own_citation_gets_corrected():
    # 重現實際發生過的真實案例：文字說「由『正峰汽車商行』提供」，但緊接在
    # 後面的引用編號 [1] 實際對應的來源檔案卻是「永春中古汽車有限公司」
    # ——使用者確認過正峰根本沒有這台車，代表引用編號才是可信的，文字
    # 上寫的廠商名稱是模型記錯了。這則答案本身完全沒有引用到「正峰汽車
    # 商行」的來源，所以一定要靠 known_vendors（筆記本全部廠商）才辨識
    # 得出「正峰汽車商行」是一個寫錯地方的已知廠商名稱。
    answer = (
        "目前在庫車輛中，符合「50 萬以下、2023 年以前的 Lexus」條件的僅有一台，"
        "由「正峰汽車商行」提供 [1]。"
    )
    source_map = {1: "永春中古汽車有限公司_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map, _ALL_VENDORS)

    assert len(messages) == 1
    assert "永春中古汽車有限公司" in messages[0]
    assert "正峰汽車商行" not in messages[0]


def test_multi_vendor_summary_table_corrects_every_mismatched_row_independently():
    # 重現實際發生過的真實案例：一張 5 家廠商的統計表裡，有 3 行的廠商
    # 名稱跟自己那一行的引用編號實際對應的來源對不上，其餘 2 行是對的
    # ——每一行要各自獨立判斷、獨立訂正，不能只抓第一個錯的就停。
    answer = (
        "| 廠商名稱 | 符合條件的台數 | 最低建議售價 |\n"
        "| :--- | :---: | :---: |\n"
        "| **力彰汽車商行** [1] | 4 台 [1] | 29.8 萬元 [1] |\n"
        "| **永春中古汽車有限公司** [2] | 3 台 [2] | 19.8 萬元 [2] |\n"
        "| **匯新中古汽車有限公司** [3] | 2 台 [3] | 18.8 萬元 [3] |\n"
        "| **安心汽車** [4] | 1 台 [4] | 25.8 萬元 [4] |\n"
        "| **中彰投汽車有限公司** [5] | 1 台 [5] | 56.8 萬元 [5] |\n"
    )
    source_map = {
        1: "中彰投汽車有限公司_20260811.md",  # 文字寫「力彰汽車商行」，錯
        2: "永春中古汽車有限公司_20260811.md",  # 文字寫「永春中古汽車有限公司」，對
        3: "正峰汽車商行_20260811.md",  # 文字寫「匯新中古汽車有限公司」，錯
        4: "安心汽車_20260811.md",  # 文字寫「安心汽車」，對
        5: "匯新中古汽車有限公司_20260811.md",  # 文字寫「中彰投汽車有限公司」，錯
    }

    corrected = _correct_vendor_citation_mismatches(format_for_line(answer), source_map, _ALL_VENDORS)

    # 錯誤的廠商名稱完全不該再出現在訂正後的文字裡。
    assert "力彰汽車商行" not in corrected
    # 三個錯誤的欄位都各自訂正成引用編號真正對應的廠商。
    assert "【中彰投汽車有限公司" in corrected
    assert "【正峰汽車商行" in corrected
    assert "【匯新中古汽車有限公司" in corrected
    # 本來就對的兩行不受影響、沒有被誤改。
    assert "【永春中古汽車有限公司" in corrected
    assert "【安心汽車" in corrected


def test_ambiguous_multi_vendor_citation_is_left_untouched():
    # 一句話同時引用多個不同廠商時（真正的跨廠商比較句），沒有單一個
    # 「正確答案」可以拿來訂正，必須原封不動保留，不能亂猜。
    answer = "力彰汽車商行與安心汽車的車款數量相近 [1, 2]。"
    source_map = {1: "力彰汽車商行_20260811.md", 2: "安心汽車_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map, _ALL_VENDORS)

    assert "力彰汽車商行與安心汽車的車款數量相近" in messages[0]


def test_correctly_matching_vendor_name_is_left_unchanged():
    answer = "力彰汽車商行目前有 4 台現車 [1]。"
    source_map = {1: "力彰汽車商行_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map, _ALL_VENDORS)

    assert "力彰汽車商行" in messages[0]


def test_mismatch_is_not_confused_with_a_closer_correct_mention_in_the_same_sentence():
    # 一句話裡先提到廠商 A（跟引用編號沒有直接關係），再提到廠商 B、緊接著
    # 才是引用編號——訂正邏輯必須抓「離引用編號最近」的那個廠商名稱
    # （B），而不是句子裡「最先出現」的那個（A），否則會把本來對的
    # B 誤改成 A。
    answer = "永春中古汽車有限公司目前沒有現車，正峰汽車商行則有一台 [1]。"
    source_map = {1: "正峰汽車商行_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map, _ALL_VENDORS)

    assert "正峰汽車商行則有一台" in messages[0]
    assert "永春中古汽車有限公司目前沒有現車" in messages[0]


def test_without_known_vendors_falls_back_to_vendors_present_in_source_map():
    # 沒有另外傳入 known_vendors 時（例如既有呼叫端還沒更新），退回只用
    # source_map 反推——至少「兩個都是這則答案有引用到的廠商、只是引用
    # 編號對到錯的那個」這種情況還是修得到，向下相容既有行為。
    answer = "永春中古汽車有限公司的現車比較多 [1]。"
    source_map = {1: "正峰汽車商行_20260811.md", 2: "永春中古汽車有限公司_20260811.md"}

    messages = build_answer_messages(format_for_line(answer), source_map)

    assert "正峰汽車商行的現車比較多" in messages[0]
