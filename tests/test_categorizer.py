"""Fast tests for extraction, rules and guardrails (no model download needed)."""
import pytest

from agent.guardrails import MAX_TOOL_CALLS, validate
from categorizer import categorize, extract, rules

TAXONOMY = ["Groceries", "P2P Transfer", "Rent"]


@pytest.mark.parametrize("narration, expected", [
    ("UPI/DR/125836504909/LAKSHMI BORA/ICICI/lakshmi.bor31@okaxis/movie",
     {"vpa": "lakshmi.bor31@okaxis", "payee": "LAKSHMI BORA", "note": "movie", "looks_like_person": True}),
    ("UPI-APOLLO PHARMACY-apollopharmacy@icici-AXIS4784-274020379980-Payment from Ph",
     {"vpa": "apollopharmacy@icici", "payee": "APOLLO PHARMACY", "looks_like_person": False}),
    ("UPI/324723016753/trip share/vikram.pat16@oksbi/HDFC",
     {"vpa": "vikram.pat16@oksbi", "note": "trip share", "looks_like_person": True}),
    ("IMPS-531179962046-SNEHA NAIR-REFUND", {"payee": "SNEHA NAIR", "note": "REFUND", "looks_like_person": True}),
])
def test_extract_formats(narration, expected):
    assert extract.merchant(narration) == expected


def test_brand_vpa_is_not_a_person():
    assert extract.merchant("UPI/1/x/swiggy.order@icici/HDFC")["looks_like_person"] is False


def test_local_merchant_on_personal_handle_is_not_a_person():
    ext = extract.merchant("UPI/DR/1/SRI LAKSHMI PROVISIO/ICICI/lakshmiprovisions@okaxis/Payment from Ph")
    assert ext["looks_like_person"] is False and "note" not in ext


@pytest.mark.parametrize("narration, category", [
    ("NEFT CR-ICICI0000634-ACME TECHNOLOGIES PVT LTD-SALARY OCT", "Salary & Income"),
    ("ATW-486424XXXXXX3320-S1ANBL465-BANGALORE", "Cash Withdrawal"),
    ("DEBIT CARD ANNUAL FEE", "Bank Charges"),
    ("UPI-ZEROD-zerodha.broking@icici-ICICI1348-523445855539-Payment from Ph", "Investments"),
    ("UPI-ZETO-qrpay@ybl-SBI1-2-x", "Groceries"),             # one-letter typo, unknown VPA
    ("UPI/DR/1/HP PE/SBI/zz@ybl/x", "Fuel"),                   # truncated name
])
def test_rules_hit(narration, category):
    assert rules.match(narration).category == category


@pytest.mark.parametrize("narration", [
    "UPI-AMIT SHARMA-amit.sha12@okaxis-S1-2-loan return",       # 'loan' note must not trigger EMI
    "UPI/DR/1/SAI RAM MEDICALS/HDFC/sairammedicals@okicici/x",   # local merchant: not a rule's job
])
def test_rules_miss(narration):
    assert rules.match(narration) is None


def test_guardrails():
    ok = {"category": "Rent", "confidence": 0.9, "reason": "rent note", "tool_calls": []}
    assert validate(ok, TAXONOMY) == []
    assert validate({**ok, "category": "Housing"}, TAXONOMY) == ["invalid_category"]
    assert validate({**ok, "confidence": 1.5}, TAXONOMY) == ["bad_confidence"]
    assert validate({**ok, "confidence": True}, TAXONOMY) == ["bad_confidence"]
    assert validate({**ok, "reason": " "}, TAXONOMY) == ["empty_reason"]
    assert validate({**ok, "tool_calls": [{}] * (MAX_TOOL_CALLS + 1)}, TAXONOMY) == ["max_tool_calls"]


@pytest.mark.parametrize("narration, category, above_gate", [
    ("UPI/1/dinner/rahul.sha12@okaxis/HDFC", "P2P Transfer", True),         # social note: really P2P
    ("UPI/1/house rent/rahul.sha12@okaxis/HDFC", "Rent", True),
    ("UPI/1/school fees jan/rahul.sha12@okaxis/HDFC", "P2P Transfer", False),  # note the rules cannot read
    ("UPI/1/Payment from Ph/rahul.sha12@okaxis/HDFC", "P2P Transfer", False),  # no note at all
])
def test_person_notes_only_auto_post_what_the_rules_understand(narration, category, above_gate):
    p = categorize(narration)
    assert p.category == category and (p.confidence >= 0.84) == above_gate
