"""Narrations in real banks' formats (made up, never real data), privacy redaction, file upload."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent import loop
from agent.redact import redact
from categorizer import categorize
from tests.test_agent_loop import FakeClaude


@pytest.mark.parametrize("narration, category", [
    ("TO TRANSFER-UPI/DR/412345678901/SWIGGY/YESB/swiggy8@ybl/Paymen--", "Food Delivery"),       # SBI prefix
    ("UPI/P2M/412345678901/ZOMATO LIMITED/Pay/HDFC BANK", "Food Delivery"),                      # Axis, no VPA
    ("UPI-ZOMATO-ZOMATO-ORDER@PAYTM-PYTM0123456-412345678901-PAYMENT", "Food Delivery"),         # hyphen in VPA
    ("POS 438628XXXXXX1234 AMAZON PAY INDIA", "Shopping"),                                       # debit card
    ("ME DC SI 4386XXXXXXXX1234 NETFLIX", "Entertainment & Subscriptions"),                      # card mandate
    ("UPI/RAHUL SHARMA/412345678901/rent sept", "Rent"),                                         # Kotak, no VPA
    ("UPI/P2A/412345678905/RAHUL SHARMA/rent/HDFC BANK", "Rent"),                                # Axis P2A
    ("IMPS/P2A/412345678901/RAVI KUMAR/HDFC/self", "Self Transfer"),                           # own account
    ("ACH D- TP ACH ZERODHA BROKING-1234567", "Investments"),                                    # NACH mandate
    ("NACH-DR-BAJAJ FINANCE LTD-12345", "EMI & Loans"),
    ("BIL/ONL/000123456/BESCOM/202609101234", "Utilities"),                                      # ICICI bill pay
    ("CREDIT INTEREST", "Salary & Income"),
])
def test_real_bank_formats(narration, category):
    assert categorize(narration).category == category


@pytest.mark.parametrize("narration", [
    "MMT/IMPS/412345678904/RAHUL SHARMA/HDFC Bank",
    "NEFT*HDFC0000123*N123456789*RAHUL SHARMA--",
])
def test_person_transfers_without_a_vpa(narration):
    p = categorize(narration)
    assert p.category == "P2P Transfer" and p.stage == "extract"


def test_self_transfer_by_own_name(monkeypatch):
    narration = "MMT/IMPS/412345678904/MR RAVI KUMAR/HDFC Bank"
    assert categorize(narration).category == "P2P Transfer"
    monkeypatch.setenv("OWN_NAMES", "Ravi Kumar Sharma")
    assert categorize(narration).category == "Self Transfer"
    monkeypatch.setenv("OWN_NAMES", "Kumar Sharma")       # a relative sharing a surname is not "self"
    assert categorize(narration).category == "P2P Transfer"


def test_redaction_masks_people_numbers_and_keeps_merchants(monkeypatch):
    monkeypatch.setenv("OWN_NAMES", "Ravi Kumar Sharma")
    (n,) = redact("UPI/DR/412345678901/MOHAN K/SBIN/9876543210@ybl/school fees")
    assert "MOHAN" not in n and "9876543210" not in n and "412345678901" not in n
    assert "[PERSON]" in n and "[PHONE]@ybl" in n and "school fees" in n
    (n,) = redact("UPI/1/house rent/kavya.nai21@okaxis/HDFC")
    assert "kavya" not in n and "[PERSON]@okaxis" in n
    (n,) = redact("POS 438628XXXXXX1234 AMAZON PAY INDIA")
    assert n == "POS [CARD] AMAZON PAY INDIA"
    (n, reason) = redact("IMPS/P2A/412345678901/RAVI KUMAR/HDFC/rent", "extract: person 'RAVI KUMAR'")
    assert "RAVI" not in n + reason and "[ME]" in n


def test_llm_never_sees_people_or_phone_numbers():
    row = {"txn_id": "R1", "debit": "18000", "credit": "",
           "narration": "UPI/DR/412345678901/MOHAN K/SBIN/9876543210@ybl/school fees"}
    fake = FakeClaude(final=("submit_decision", {"category": "Education", "confidence": 0.9, "reason": "fees"}))
    (d,) = asyncio.run(loop.triage_async([row], llm=fake, transport="inproc"))
    sent = json.dumps([r["messages"] for r in fake.requests], default=str)
    assert "MOHAN" not in sent and "9876543210" not in sent and "school fees" in sent
    assert d["privacy"] == "redact" and "[PHONE]" in d["llm_input"]


def test_upload_a_bank_export_end_to_end():
    from api.main import app
    from tests.test_ingest import AXIS_CSV

    client = TestClient(app)
    r = client.post("/triage/file", files={"file": ("axis_sep.csv", AXIS_CSV.encode(), "text/csv")},
                    data={"use_agent": "false"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parsed_rows"] == body["processed"] == 2 and body["statement_id"] == "axis_sep"
    assert body["persisted"] is False                     # tests never touch the demo database
    bad = client.post("/triage/file", files={"file": ("s.pdf", b"%PDF-1.7", "application/pdf")})
    assert bad.status_code == 422 and "PDF" in bad.json()["detail"]
