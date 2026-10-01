"""Stage 1: deterministic rules.

Two kinds of rule:
  - Bank-rail patterns for non-UPI rows (salary credits, ATM withdrawals, bank charges).
  - A brand map for well-known national merchants, matched on the VPA handle first and
    then on the payee name, tolerant of truncation ('ZEROD', 'HDFC H') and one-letter
    typos ('ZETO', 'PRCTO').

The brand map deliberately covers big brands only. Small local merchants are left to
SBERT and, later, the RAG merchant lookup; that split is what the B vs C eval measures.

Every hit carries a rule id in `reason`, so coverage per rule can be counted:
    python -m categorizer.rules data/statements.csv
Design question for the interview: how many rules before maintenance cost beats the
accuracy gain? The coverage report answers it with data.
"""
import re
import sys
from collections import Counter
from difflib import SequenceMatcher
from typing import Optional

from categorizer import Prediction
from categorizer.extract import merchant as extract_merchant
from categorizer.extract import strip_prefix

CONF_BANK = 0.97        # bank-rail patterns are unambiguous
CONF_VPA = 0.97         # brand VPA handle: the strongest UPI signal
CONF_NAME = 0.95        # exact brand name
CONF_NAME_FUZZY = 0.88  # truncated / misspelt brand name

# (rule id, category, compiled pattern). Only applied to non-UPI narrations.
BANK_RULES = [
    ("salary", "Salary & Income", re.compile(r"\b(SALARY|SAL CR|PAYROLL)\b", re.I)),
    ("interest", "Salary & Income",
     re.compile(r"\b(INT\.?\s?(PD|CR)|INTEREST (CR|CREDIT|CREDITED|PAID)|CREDIT INTEREST|SB INTEREST)\b", re.I)),
    ("atm", "Cash Withdrawal", re.compile(r"^(ATW|NWD|EAW|ATM)[-/ ]|\b(ATM WDL|CASH WDL|CASH WITHDRAWAL)\b", re.I)),
    ("charges", "Bank Charges",
     re.compile(r"\b(CHARGES?|CHRGS?|ANNUAL FEE|SERVICE FEE|MIN(IMUM)? BAL|NON[- ]MAINT|GST ON)\b", re.I)),
    ("emi_nach", "EMI & Loans", re.compile(r"\b(NACH|ACH)\b.*\b(EMI|LOAN)\b|\bEMI\b", re.I)),
]

# category -> list of (VPA local-part prefixes, brand names). National head brands only.
BRANDS = {
    "Food Delivery": [(("swiggy",), ("swiggy",)), (("zomato",), ("zomato",))],
    "Groceries": [(("blinkit", "grofers"), ("blinkit", "grofers")), (("zepto",), ("zepto",)),
                  (("bigbasket",), ("bigbasket", "big basket")), (("avenuesupermarts", "dmart"), ("dmart", "d mart"))],
    "Fuel": [(("iocl", "indianoil"), ("indian oil", "iocl")), (("hpcl",), ("hp petrol", "hpcl", "hindustan petroleum")),
             (("bpcl",), ("bharat petroleum", "bpcl"))],
    "Utilities": [(("bescom",), ("bescom",)), (("bwssb",), ("bwssb",))],
    "Mobile & Internet": [(("jio",), ("jio", "reliance jio")), (("airtel",), ("airtel", "bharti airtel")),
                          (("actfibernet",), ("act fibernet",))],
    "Shopping": [(("amazon",), ("amazon",)), (("flipkart",), ("flipkart",)), (("myntra",), ("myntra",))],
    "Travel": [(("uber",), ("uber",)), (("olacabs",), ("ola", "ola cabs")), (("irctc",), ("irctc",)),
               (("makemytrip",), ("makemytrip", "make my trip"))],
    "Entertainment & Subscriptions": [(("netflix",), ("netflix",)), (("spotify",), ("spotify",)),
                                      (("bookmyshow",), ("bookmyshow", "book my show"))],
    "Health & Pharmacy": [(("apollopharmacy", "apollo"), ("apollo pharmacy", "apollo")),
                          (("pharmeasy",), ("pharmeasy",)), (("practo",), ("practo",))],
    "Education": [(("unacademy",), ("unacademy",)), (("byjus",), ("byjus", "byju's"))],
    "EMI & Loans": [(("bajajfinserv", "bajajfin"), ("bajaj finserv", "bajaj finance")),
                    (("hdfcltd", "hdfchomeloan"), ("hdfc home loan", "hdfc ltd"))],
    "Investments": [(("zerodha",), ("zerodha",)), (("groww",), ("groww",))],
    "Dining": [(("tatastarbucks", "starbucks"), ("starbucks", "tata starbucks")),
               (("mcdonalds",), ("mcdonald's", "mcdonalds"))],
}

VPA_INDEX = [(prefix, cat) for cat, brands in BRANDS.items() for prefixes, _ in brands for prefix in prefixes]
NAME_INDEX = [(re.sub(r"[^a-z0-9]", "", n), n, cat) for cat, brands in BRANDS.items()
              for _, names in brands for n in names]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _match_vpa(vpa: str) -> Optional[Prediction]:
    local = vpa.lower().split("@")[0]
    for prefix, cat in VPA_INDEX:
        if local.startswith(prefix):
            return Prediction(cat, CONF_VPA, f"rules: brand VPA '{vpa}' -> {cat}", "rules")
    return None


def _match_name(payee: str) -> Optional[Prediction]:
    p = _norm(payee)
    if len(p) < 3:
        return None
    best = None
    for alias, raw, cat in NAME_INDEX:
        if p == alias or (len(alias) >= 4 and p.startswith(alias)):
            return Prediction(cat, CONF_NAME, f"rules: brand name '{payee}' ~ '{raw}' -> {cat}", "rules")
        # Truncated by the bank's column width: 'ZEROD' -> 'zerodha'. Needs 4+ chars to avoid noise.
        if len(p) >= 4 and alias.startswith(p):
            best = best or (cat, raw, "truncated")
        # One dropped or swapped letter: 'ZETO' -> 'zepto', 'MCDONAD'S' -> 'mcdonalds'.
        elif len(p) >= 4 and len(alias) >= 5 and SequenceMatcher(None, p, alias).ratio() >= 0.85:
            best = best or (cat, raw, "fuzzy")
    if best:
        cat, raw, how = best
        return Prediction(cat, CONF_NAME_FUZZY, f"rules: {how} brand name '{payee}' ~ '{raw}' -> {cat}", "rules")
    return None


def match(narration: str) -> Optional[Prediction]:
    if not narration:
        return None
    if not re.match(r"^\s*UPI", strip_prefix(narration), re.I):  # "TO TRANSFER-UPI/..." is still UPI
        for rule_id, cat, pat in BANK_RULES:
            if pat.search(narration):
                return Prediction(cat, CONF_BANK, f"rules: bank pattern '{rule_id}' -> {cat}", "rules")

    ext = extract_merchant(narration) or {}
    if ext.get("vpa") and (hit := _match_vpa(ext["vpa"])):
        return hit
    if ext.get("looks_like_person"):
        return None  # a person called 'Ola' is still a person; leave P2P to the extract stage
    if ext.get("payee") and (hit := _match_name(ext["payee"])):
        return hit
    return None


def coverage(narrations) -> Counter:
    """Hits per rule id (bank pattern or brand) over an unlabelled list of narrations."""
    hits = Counter()
    for n in narrations:
        p = match(n)
        if p is None:
            hits["<no rule>"] += 1
        else:
            hits[p.reason.split("->")[0].split("'")[1] if "bank pattern" in p.reason else p.category] += 1
    return hits


if __name__ == "__main__":
    import csv

    path = sys.argv[1] if len(sys.argv) > 1 else "data/statements.csv"
    with open(path, newline="", encoding="utf-8") as f:
        narrations = [r["narration"] for r in csv.DictReader(f)]
    for rule, n in coverage(narrations).most_common():
        print(f"{n:>5}  {n / len(narrations):6.1%}  {rule}")
