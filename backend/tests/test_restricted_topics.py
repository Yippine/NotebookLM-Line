import re
from pathlib import Path

import pytest

from routers.webhook import (
    _is_purchase_intent,
    _is_restricted_topic,
    _purchase_intent_reply,
    classify_restricted_topic,
)
from config import settings

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "restricted_topic_cases.txt"

# A = 合理的諮詢 -> 不應該被擋下
# B/C/D = 交易／授權／承諾 -> 應該被擋下
CATEGORY_EXPECT_BLOCKED = {"A": False, "B": True, "C": True, "D": True}

_SECTION_RE = re.compile(r"^【([A-D])\.")
_CASE_RE = re.compile(r"^(\d{3})\.\s*(.+)$")


def _load_cases() -> list[pytest.param]:
    cases = []
    current_cat = None
    for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        section_match = _SECTION_RE.match(line)
        if section_match:
            current_cat = section_match.group(1)
            continue
        case_match = _CASE_RE.match(line)
        if case_match and current_cat:
            num, text = case_match.groups()
            cases.append(
                pytest.param(
                    text,
                    CATEGORY_EXPECT_BLOCKED[current_cat],
                    id=f"{current_cat}-{num}",
                )
            )
    return cases


def _load_cases_by_category(categories: tuple[str, ...]) -> list[pytest.param]:
    cases = []
    current_cat = None
    for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        section_match = _SECTION_RE.match(line)
        if section_match:
            current_cat = section_match.group(1)
            continue
        case_match = _CASE_RE.match(line)
        if case_match and current_cat in categories:
            num, text = case_match.groups()
            cases.append(pytest.param(text, id=f"{current_cat}-{num}"))
    return cases


@pytest.mark.parametrize("question, expected_blocked", _load_cases())
def test_restricted_topic_classification(question: str, expected_blocked: bool):
    assert _is_restricted_topic(question) == expected_blocked


@pytest.mark.parametrize("question", _load_cases_by_category(("B",)))
def test_purchase_category_routes_to_purchase_intent(question: str):
    """B 類（交易／買賣）案例必須導向廠商聯絡資訊的回覆，
    而不是通用的授權/承諾婉拒回覆。"""
    assert classify_restricted_topic(question) == "purchase"


@pytest.mark.parametrize("question", _load_cases_by_category(("C", "D")))
def test_authorization_promise_categories_route_to_generic_refusal(question: str):
    """C/D 類（授權／商標／承諾）案例必須維持通用的婉拒回覆，
    即使句子中也順帶提到了購買（例如「如果我買這個，你能保證……」
    是一個承諾問題，而不是購車詢問）——授權／承諾的優先權更高。"""
    assert classify_restricted_topic(question) == "authorization_promise"


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("這台車已經成交了嗎？", id="deal-closed"),
        pytest.param("請問這台車現在是不是待售狀態？", id="for-sale"),
    ],
)
def test_new_purchase_keywords(question: str):
    assert _is_purchase_intent(question) is True


def test_purchase_intent_reply_includes_dealer_contact_info():
    reply = _purchase_intent_reply()
    assert settings.dealer_contact_info in reply


@pytest.mark.parametrize(
    "question, expected_blocked",
    [
        # 混合意圖：買/賣只是拿來鋪陳一個單純資訊詢問——資訊詢問的
        # 部分應該被回答，而不是被一律擋下。
        pytest.param("我要買麥拉倫720s，請給我這種車型的資訊", False, id="buy-framing-then-info"),
        pytest.param("想賣掉我的舊車，可以先給我這台的規格資料嗎", False, id="sell-framing-then-spec"),
        # 同樣的鋪陳說法，但訊息其他地方若有真正的交易性尾段，
        # 即使資訊詢問子句被豁免，仍然應該被擋下。
        pytest.param("我要買720S，請問多少錢", True, id="buy-framing-then-price-still-blocked"),
        pytest.param("我要買這台車，請問你們接受轉帳嗎", True, id="buy-framing-then-payment-still-blocked"),
    ],
)
def test_mixed_intent_buy_framing(question: str, expected_blocked: bool):
    assert _is_restricted_topic(question) == expected_blocked
