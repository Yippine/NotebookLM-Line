from routers.webhook import _is_car_related_question


def test_plain_car_keyword_triggers():
    assert _is_car_related_question("這台車的油耗如何") is True
    assert _is_car_related_question("有沒有這款的現車") is True
    assert _is_car_related_question("內裝配備有哪些") is True


def test_purchase_phrasing_without_car_noun_still_triggers():
    """在這個經銷商機器人的情境下，價格／購買用語本質上就是與車
    相關的，即使沒有出現字面上的「車」字。"""
    assert _is_car_related_question("多少錢") is True
    assert _is_car_related_question("可以議價嗎") is True


def test_authorization_phrasing_without_car_noun_still_triggers():
    assert _is_car_related_question("可以保證嗎") is True


def test_unrelated_chitchat_does_not_trigger():
    assert _is_car_related_question("午餐吃了嗎") is False
    assert _is_car_related_question("哈囉大家好") is False
    assert _is_car_related_question("今天天氣真好") is False


def test_empty_string_does_not_trigger():
    assert _is_car_related_question("") is False


def test_english_model_name_with_trim_level_triggers():
    """回歸測試：一則指名英文品牌／車型加上配置等級的訊息
    （例如「Nissan X-Trail2019入門型」）沒有任何中文的汽車名詞
    （像「車」），但毫無疑問是一個購車問題。"""
    assert _is_car_related_question("誰家有Nissan X-Trail2019入門型？") is True


def test_bare_brand_name_triggers():
    assert _is_car_related_question("Toyota有推薦的嗎") is True
    assert _is_car_related_question("賓士的保固怎麼算") is True
