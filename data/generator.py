"""Generate synthetic UPI bank statements and a draft golden set.

Outputs
  data/statements.csv   ~1,000 rows across several statements (for demos and n8n)
  data/golden_set.csv   ~250 rows with a draft `label` column and an `is_hard` flag

The labels are a DRAFT. Hand-review golden_set.csv before trusting any eval number:
fix wrong labels, and note why hard rows are hard. That review is what makes it golden.

All data is synthetic. No real customer or employer data is used.
"""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
random.seed(42)

BANKS = ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "YESB"]
FIRST = ["Rahul", "Priya", "Arjun", "Sneha", "Vikram", "Ananya", "Rohit", "Kavya", "Aditya", "Meera",
         "Karthik", "Divya", "Suresh", "Lakshmi", "Imran", "Fatima", "Joseph", "Neha", "Amit", "Pooja"]
LAST = ["Sharma", "Reddy", "Iyer", "Nair", "Das", "Gupta", "Khan", "Rao", "Patel", "Singh", "Bora", "Dutta"]
P2P_NOTES = ["", "", "", "dinner", "trip share", "thanks", "movie", "gift", "loan return", "cab"]
RENT_NOTES = ["rent", "rent oct", "house rent", "Rent for flat", "maintenance+rent"]


def load_merchants():
    with open(HERE / "merchants.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def ref():
    return str(random.randint(10**11, 10**12 - 1))


def typo(s):
    """Introduce a realistic narration glitch: truncation, missing letter or case noise."""
    r = random.random()
    if r < 0.33 and len(s) > 6:
        return s[: random.randint(5, len(s) - 1)]
    if r < 0.66 and len(s) > 4:
        i = random.randint(1, len(s) - 2)
        return s[:i] + s[i + 1:]
    return s.swapcase()


def upi_narration(name, vpa, note=""):
    # Mimics common Indian bank formats: UPI/DR/<ref>/<NAME>/<BANK>/<vpa>/<note>
    fmt = random.choice([
        "UPI/DR/{ref}/{name}/{bank}/{vpa}/{note}",
        "UPI-{name}-{vpa}-{bank}{ifsc}-{ref}-{note}",
        "UPI/{ref}/{note}/{vpa}/{bank}",
    ])
    return fmt.format(ref=ref(), name=name.upper()[:20], bank=random.choice(BANKS),
                      vpa=vpa, note=note or "Payment from Ph", ifsc=random.randint(1000, 9999)).strip("/-")


def merchant_txn(m, hard=False):
    name, vpa = m["name"], m["vpa"]
    if hard:
        name = typo(name)
    amount = round(random.uniform(40, 4500), 2)
    if m["category"] in ("Rent", "EMI & Loans", "Investments"):
        amount = round(random.uniform(3000, 30000), 0)
    return upi_narration(name, vpa), m["category"], name, amount, "debit"


def p2p_txn():
    first, last = random.choice(FIRST), random.choice(LAST)
    vpa = f"{first.lower()}.{last.lower()[:3]}{random.randint(1, 99)}@ok{random.choice(['axis', 'icici', 'hdfcbank', 'sbi'])}"
    if random.random() < 0.2:  # P2P that is really rent: hard case
        note = random.choice(RENT_NOTES)
        return upi_narration(f"{first} {last}", vpa, note), "Rent", f"{first} {last}", round(random.uniform(8000, 35000), 0), "debit", True
    note = random.choice(P2P_NOTES)
    return upi_narration(f"{first} {last}", vpa, note), "P2P Transfer", f"{first} {last}", round(random.uniform(50, 5000), 0), "debit", bool(note == "")


def other_txn():
    kind = random.choice(["salary", "atm", "charges", "neft_in"])
    if kind == "salary":
        return f"NEFT CR-{random.choice(BANKS)}0000{random.randint(100, 999)}-ACME TECHNOLOGIES PVT LTD-SALARY OCT", "Salary & Income", "ACME", round(random.uniform(40000, 120000), 0), "credit", False
    if kind == "atm":
        return f"ATW-{random.randint(400000, 499999)}XXXXXX{random.randint(1000, 9999)}-S1ANBL{random.randint(100, 999)}-BANGALORE", "Cash Withdrawal", "ATM", float(random.choice([500, 1000, 2000, 5000])), "debit", False
    if kind == "charges":
        return random.choice(["SMS CHARGES FOR QTR", "DEBIT CARD ANNUAL FEE", "CHQ BOOK ISSUE CHARGES"]), "Bank Charges", "BANK", round(random.uniform(15, 750), 2), "debit", False
    return f"IMPS-{ref()}-{random.choice(FIRST).upper()} {random.choice(LAST).upper()}-REFUND", "P2P Transfer", "IMPS", round(random.uniform(100, 3000), 0), "credit", True


def make_rows(n, hard_share, merchants):
    rows, start = [], date(2026, 7, 1)
    head = [m for m in merchants if m["tier"] == "head"]
    tail = [m for m in merchants if m["tier"] == "tail"]
    for i in range(n):
        r = random.random()
        if r < 0.55:
            use_tail = random.random() < 0.35
            m = random.choice(tail if use_tail else head)
            hard = use_tail or random.random() < hard_share
            narration, label, merchant, amount, side = merchant_txn(m, hard=hard and not use_tail)
            is_hard = hard
        elif r < 0.85:
            narration, label, merchant, amount, side, is_hard = p2p_txn()
        else:
            narration, label, merchant, amount, side, is_hard = other_txn()
        rows.append({
            "txn_id": f"T{i:05d}",
            "statement_id": f"S{i // 60:03d}",
            "date": (start + timedelta(days=random.randint(0, 91))).isoformat(),
            "narration": narration,
            "debit": amount if side == "debit" else "",
            "credit": amount if side == "credit" else "",
            "label": label,
            "is_hard": int(is_hard),
            "merchant_hint": merchant,
        })
    return rows


def write(path, rows, cols):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    merchants = load_merchants()
    statements = make_rows(1000, hard_share=0.15, merchants=merchants)
    write(HERE / "statements.csv", statements, ["txn_id", "statement_id", "date", "narration", "debit", "credit"])
    golden = make_rows(250, hard_share=0.30, merchants=merchants)
    for r in golden:
        r["txn_id"] = "G" + r["txn_id"][1:]
    write(HERE / "golden_set.csv", golden, ["txn_id", "date", "narration", "debit", "credit", "label", "is_hard", "merchant_hint"])
    (HERE / "inbox").mkdir(exist_ok=True)
    hard = sum(r["is_hard"] for r in golden)
    print(f"statements.csv: {len(statements)} rows | golden_set.csv: {len(golden)} rows ({hard} hard)")
    print("Next: hand-review data/golden_set.csv labels before trusting eval numbers.")
