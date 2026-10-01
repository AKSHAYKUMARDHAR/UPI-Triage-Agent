"""Stage 2: pull the payee out of a messy narration.

Narration formats in the synthetic data (see data/generator.py):
  UPI/DR/<ref>/<NAME>/<BANK>/<vpa>/<note>
  UPI-<NAME>-<vpa>-<BANK><ifsc>-<ref>-<note>
  UPI/<ref>/<note>/<vpa>/<BANK>
Plus non-UPI rails that carry a counterparty name:
  IMPS-<ref>-<NAME>-<note>
  NEFT CR-<IFSC>-<NAME>-<note>
Real bank exports add more, handled by a tolerant token parser: SBI's "TO TRANSFER-" prefix,
Axis "UPI/P2M/<ref>/<NAME>/..." with no VPA, Kotak "UPI/<NAME>/<ref>/<note>", card
"POS 4386XXXXXX1234 <MERCHANT>", "ACH D- TP ACH <MANDATE>", "BIL/ONL/<ref>/<BILLER>",
"MMT/IMPS/<ref>/<NAME>/<BANK>", "NEFT*<IFSC>*<ref>*<NAME>".

Returns the VPA handle, payee name and free-text note when present, and flags
person-to-person payments. The note is kept because it decides hard cases such as
P2P rent ('rent', 'maintenance+rent'). is_self_transfer() spots money moved between the
account holder's own accounts (a "self" note, or a payee matching OWN_NAMES in .env).
"""
import os
import re
from typing import Optional, TypedDict


class Extracted(TypedDict, total=False):
    vpa: str
    payee: str
    note: str
    looks_like_person: bool


# '-' is legal in a VPA but is also the field separator in 'UPI-<NAME>-<vpa>-...', so leave it out
# there. Between '/' separators it is unambiguous ('paytm-12345@paytm').
VPA_RE = re.compile(r"[A-Za-z0-9._]{2,}@[A-Za-z]{2,}")
VPA_SLASH_RE = re.compile(r"[A-Za-z0-9._-]{2,}@[A-Za-z]{2,}")

# Some banks put the rail before the real narration: "TO TRANSFER-UPI/DR/..." (SBI).
PREFIX_RE = re.compile(r"^\s*(?:(?:TO|BY)\s+TRANSFER|TRANSFER\s+(?:TO|FROM)|TRF\s+(?:TO|FROM))\s*[-:/]?\s*", re.I)

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

# Words that name the payment rail, not the payee ("POS", "P2M", "ACH", "ME DC SI").
RAIL_CODES = {"UPI", "DR", "CR", "P2M", "P2A", "P2P", "IMPS", "NEFT", "RTGS", "MMT", "ACH", "NACH", "ECS",
              "POS", "PCD", "VPS", "IPS", "MPS", "VIN", "ECOM", "PUR", "PRCH", "PURCHASE", "BIL", "ONL", "BBPS",
              "ME", "DC", "SI", "TP", "INB", "MOB", "IB", "NB", "TRF", "D", "C", "INR", "RS", "REF", "TXN",
              "CARD", "DEBIT", "CREDIT"}
MERCHANT_RAILS = {"P2M", "POS", "PCD", "VPS", "IPS", "MPS", "VIN", "ECOM", "PUR", "PRCH", "PURCHASE", "ME",
                  "DC", "CARD", "ACH", "NACH", "ECS", "BIL", "ONL", "BBPS"}
BANK_RE = re.compile(
    r"^(?:STATE BANK OF INDIA|BANK OF (?:BARODA|INDIA|MAHARASHTRA)|(?:HDFC|ICICI|AXIS|KOTAK(?: MAHINDRA)?|YES|"
    r"IDFC(?: FIRST)?|INDUSIND|FEDERAL|CANARA|UNION|IDBI|RBL|PUNJAB NATIONAL|PAYTM PAYMENTS|AIRTEL PAYMENTS|"
    r"AU SMALL FINANCE)(?: BANK)?(?: LTD| LIMITED)?|SBI|SBIN|UTIB|KKBK|YESB|PUNB|PNB|BARB|BOB|CNRB|IDFB|INDB|"
    r"FDRL|UBIN|IBKL|RATN|PYTM|AIRP|AUBL|BKID|MAHB)$", re.I)
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$", re.I)
# Reference numbers, letter-prefixed refs (N123456789), masked cards (4386XXXXXX1234), dates (01SEP26).
NOISE_WORD_RE = re.compile(r"^(?:\d+|[A-Z]{1,6}\d{5,}|[\dX*]*X[\dX*]*\d{3,}|[\dX*]{8,}|\d{1,2}[A-Z]{3}\d{2,4})$",
                           re.I)
SELF_NOTE_RE = re.compile(r"\b(self|to self|own (a ?c|acc|account)|self transfer|own transfer)\b", re.I)
HONORIFICS = {"MR", "MRS", "MS", "MISS", "SHRI", "SMT", "KUM", "DR"}


def strip_prefix(narration: str) -> str:
    return PREFIX_RE.sub("", narration or "", count=1)


def _is_filler(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith(FILLER_NOTES) or t in {"pay", "payment", "payments", "transfer", "upi", "remarks"}


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
    sep = "/" if narration.upper().startswith("UPI/") else "-"
    m = (VPA_SLASH_RE if sep == "/" else VPA_RE).search(narration)
    if not m:
        return None
    vpa = m.group(0).strip(".-_")
    before = narration[: m.start()].strip(sep).split(sep)
    after = narration[m.end():].strip(sep).split(sep)
    payee = note = ""

    if sep == "/":
        if len(before) >= 4 and before[1].upper() in ("DR", "CR", "P2M", "P2A", "P2P"):
            # UPI/DR/<ref>/<NAME>/<BANK>/<vpa>/<note>   (CR for money in; Axis writes P2M / P2A)
            payee = next((t for t in before[2:] if re.search(r"[A-Za-z]{2}", t) and not BANK_RE.match(t.strip())),
                         "")
            note = "/".join(after)
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


def _tokens(narration: str) -> tuple[list[str], set[str]]:
    """Split on the usual separators and drop rail codes, references, IFSCs and bank names."""
    rails: set[str] = set()
    out = []
    for raw in re.split(r"[/*|]|-|\s{2,}", narration):
        words = raw.split()
        # leading rail codes and numbers: "POS 4386XXXXXX1234 AMAZON PAY" -> "AMAZON PAY"
        while words and (words[0].upper().strip(".:") in RAIL_CODES or NOISE_WORD_RE.match(words[0])):
            if words[0].upper().strip(".:") in RAIL_CODES:
                rails.add(words[0].upper().strip(".:"))
            words.pop(0)
        while words and NOISE_WORD_RE.match(words[-1]):
            words.pop()
        token = " ".join(words)
        if token and not BANK_RE.match(token) and not IFSC_RE.match(token):
            out.append(token)
    return out, rails


def _parse_tokens(narration: str) -> Optional[Extracted]:
    """Any other format: the first word-like token is the payee, what follows is the note."""
    tokens, rails = _tokens(narration)
    names = [t for t in tokens if len(re.findall(r"[A-Za-z]", t)) >= 3 and not _is_filler(t)]
    if not names:
        return None
    payee = names[0]
    note = _clean_note(" ".join(t for t in tokens[tokens.index(payee) + 1:] if not _is_filler(t)))
    out: Extracted = {"payee": payee,
                      "looks_like_person": not (rails & MERCHANT_RAILS) and _looks_like_name(payee)}
    if note:
        out["note"] = note
    return out


def _name_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[A-Z]+", name.upper()) if w not in HONORIFICS]


def is_self_transfer(ext: Extracted) -> bool:
    """Money between the account holder's own accounts: not spending, not a P2P transfer."""
    if ext.get("looks_like_person") and SELF_NOTE_RE.search(ext.get("note", "")):
        return True
    payee = _name_words(ext.get("payee", ""))
    if len(payee) < 2:
        return False
    for own in filter(None, (n.strip() for n in os.getenv("OWN_NAMES", "").split(","))):
        words = _name_words(own)
        # same first name, and every payee word is part of the full name ("RAVI KUMAR" ~ "RAVI KUMAR SHARMA")
        if words and payee[0] == words[0] and set(payee) <= set(words):
            return True
    return False


def merchant(narration: str) -> Optional[Extracted]:
    """Best-effort parse. Returns None when no counterparty can be found."""
    if not narration or not narration.strip():
        return None
    narration = strip_prefix(narration)
    return _parse_upi(narration) or _parse_bank_transfer(narration) or _parse_tokens(narration)
