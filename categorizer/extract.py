"""Stage 2: pull the payee out of a messy narration.

Narration formats in the synthetic data (see data/generator.py):
  UPI/DR/<ref>/<NAME>/<BANK>/<vpa>/<note>
  UPI-<NAME>-<vpa>-<BANK><ifsc>-<ref>-<note>
  UPI/<ref>/<note>/<vpa>/<BANK>
Plus non-UPI rails that carry a counterparty name:
  IMPS-<ref>-<NAME>-<note>
  NEFT CR-<IFSC>-<NAME>-<note>

Returns the VPA handle, payee name and free-text note when present, and flags
person-to-person payments. The note is kept because it decides hard cases such as
P2P rent ('rent', 'maintenance+rent').
"""
import re
from typing import Optional, TypedDict


class Extracted(TypedDict, total=False):
    vpa: str
    payee: str
    note: str
    looks_like_person: bool


# '-' is legal in a VPA but is also the field separator in 'UPI-<NAME>-<vpa>-...', so leave it out.
VPA_RE = re.compile(r"[A-Za-z0-9._]{2,}@[A-Za-z]{2,}")

# Default notes that UPI apps stamp on a payment. They carry no signal, so drop them.
# 'Payment from Ph' is 'Payment from PhonePe' cut off by the bank's column width.
FILLER_NOTES = ("payment from ph", "upi payment", "paid via", "sent using", "pay to", "na")

# Words that mark a payee as a business, not a person.
BUSINESS_WORDS = {
    "store", "stores", "general", "kirana", "provision", "provisions", "mart", "supermarket", "traders",
    "enterprises", "agency", "agencies", "hotel", "foods", "food", "restaurant", "cafe", "bakery", "sweets",
    "medical", "medicals", "pharmacy", "chemist", "hospital", "clinic", "diagnostics", "labs", "school",
    "college", "institute", "academy", "classes", "tuition", "works", "garage", "motors", "auto", "service",
    "services", "petroleum", "fuels", "filling", "station", "vegetables", "fruits", "fresh", "pvt", "ltd",
    "limited", "llp", "co", "company", "india", "digital", "electronics", "fashion", "textiles", "point",
    "centre", "center", "fit", "gym", "power", "gas", "bill", "pay", "rent", "premium", "card", "bank",
}

# Person-shaped VPA local parts: 'firstname.abc12', 'rahul1234', '9876543210'.
# Brands use 'word.word' too ('swiggy.order', 'iocl.fuel'), so the dotted form needs digits.
PERSON_VPA_RE = re.compile(r"^(?:[a-z]+[._][a-z]+\d{1,4}|[a-z]{3,}\d{2,4}|\d{10})$")
# 'rahul.sharma' with no digits is a person only on a personal (Google Pay) handle.
DOTTED_NAME_RE = re.compile(r"^[a-z]+[._][a-z]+$")
# Google Pay personal handles; small merchants use them too, so this alone is not proof.
PERSONAL_HANDLES = ("okaxis", "okicici", "oksbi", "okhdfcbank")


def _clean_note(note: str) -> str:
    note = note.strip(" /-")
    if not note or note.lower().startswith(FILLER_NOTES) or re.fullmatch(r"[\d\W]*", note):
        return ""
    return note


def _looks_like_name(payee: str) -> bool:
    words = re.findall(r"[A-Za-z]+", payee)
    return 2 <= len(words) <= 3 and not any(w.lower() in BUSINESS_WORDS for w in words)


def looks_like_person(vpa: str = "", payee: str = "") -> bool:
    """VPA shape decides when there is one; fall back to the payee name otherwise."""
    if vpa:
        local, _, handle = vpa.lower().partition("@")
        business_payee = any(w.lower() in BUSINESS_WORDS for w in re.findall(r"[A-Za-z]+", payee))
        if PERSON_VPA_RE.match(local):
            # A person-shaped VPA with an obviously commercial name is still a business.
            return not business_payee
        if DOTTED_NAME_RE.match(local) and handle in PERSONAL_HANDLES:
            return not business_payee and not any(w in BUSINESS_WORDS for w in re.split(r"[._]", local))
        return False
    return bool(payee) and _looks_like_name(payee)


def _parse_upi(narration: str) -> Optional[Extracted]:
    m = VPA_RE.search(narration)
    if not m:
        return None
    vpa = m.group(0).strip(".-_")
    sep = "/" if narration.upper().startswith("UPI/") else "-"
    before = narration[: m.start()].strip(sep).split(sep)
    after = narration[m.end():].strip(sep).split(sep)
    payee = note = ""

    if sep == "/":
        if len(before) >= 4 and before[1].upper() == "DR":
            # UPI/DR/<ref>/<NAME>/<BANK>/<vpa>/<note>
            payee, note = before[3], "/".join(after)
        elif len(before) >= 3:
            # UPI/<ref>/<note>/<vpa>/<BANK>
            note = "/".join(before[2:])
    else:
        # UPI-<NAME>-<vpa>-<BANK><ifsc>-<ref>-<note>; the name itself may contain '-'
        payee = "-".join(before[1:])
        note = "-".join(after[2:]) if len(after) > 2 else ""

    out: Extracted = {"vpa": vpa}
    if payee.strip():
        out["payee"] = payee.strip()
    if _clean_note(note):
        out["note"] = _clean_note(note)
    out["looks_like_person"] = looks_like_person(vpa, out.get("payee", ""))
    return out


def _parse_bank_transfer(narration: str) -> Optional[Extracted]:
    # IMPS-<ref>-<NAME>-<note>  |  NEFT CR-<IFSC>-<NAME>-<note>  |  RTGS ...
    m = re.match(r"^(?:IMPS|NEFT|RTGS)(?:\s+(?:CR|DR))?-([^-]+)-([^-]+)(?:-(.*))?$", narration.strip(), re.I)
    if not m:
        return None
    payee, note = m.group(2).strip(), _clean_note(m.group(3) or "")
    out: Extracted = {"payee": payee, "looks_like_person": looks_like_person(payee=payee)}
    if note:
        out["note"] = note
    return out


def merchant(narration: str) -> Optional[Extracted]:
    """Best-effort parse. Returns None when no counterparty can be found."""
    if not narration or not narration.strip():
        return None
    return _parse_upi(narration) or _parse_bank_transfer(narration)
