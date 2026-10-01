"""Mask personal details before a narration reaches an LLM (PRIVACY_MODE=redact, the default).

To categorise a row the model needs the merchant, the payment rail and the note, not who you
paid. Masked:
  - people's names, when extraction recognises the payee as a person    -> [PERSON]
  - the account holder's own names (OWN_NAMES in .env)                    -> [ME]
  - UPI IDs built on a person's name or a phone number                    -> [PERSON]@okaxis, [PHONE]@ybl
  - phone numbers, card numbers, account / reference numbers, e-mails      -> [PHONE] [CARD] [NUM] [EMAIL]
Kept: merchant names (the signal) and amounts (they tell rent from a dinner split).

Best effort, not a guarantee: a name the extractor does not recognise as a person stays visible.
For real statements on a free tier, AGENT_ENABLED=false keeps every row on your machine.
"""
import os
import re

from categorizer import extract

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"(?<![\dA-Za-z])(?:\+?91[\s-]?)?[6-9]\d{9}(?![\dA-Za-z])")
CARD_RE = re.compile(r"(?<![\dA-Za-z])\d{0,6}[X*]{4,}\d{2,4}(?![\dA-Za-z])", re.I)
LONG_NUM_RE = re.compile(r"(?<![\dA-Za-z])\d{6,}(?![\dA-Za-z])")


def _own_names() -> list[str]:
    return [n.strip() for n in os.getenv("OWN_NAMES", "").split(",") if n.strip()]


def _replace(text: str, needle: str, token: str) -> str:
    if len(needle.strip()) < 3:
        return text
    return re.sub(re.escape(needle.strip()), token, text, flags=re.I)


def redact(narration: str, *extra: str) -> tuple[str, ...]:
    """Mask the narration and any extra texts (e.g. the baseline's reason) with the same rules."""
    texts = [narration, *extra]
    ext = extract.merchant(narration) or {}
    payee, vpa = ext.get("payee", ""), ext.get("vpa", "")

    swaps: list[tuple[str, str]] = []
    if extract.is_self_transfer(ext) and payee:
        swaps.append((payee, "[ME]"))
    elif ext.get("looks_like_person"):
        if payee:
            swaps.append((payee, "[PERSON]"))
        local = vpa.split("@")[0]
        if local and not PHONE_RE.fullmatch(local):
            swaps.append((local, "[PERSON]"))
    swaps += [(name, "[ME]") for name in _own_names()]

    out = []
    for text in texts:
        for needle, token in sorted(swaps, key=lambda s: -len(s[0])):  # longest first: full name before parts
            text = _replace(text, needle, token)
        text = EMAIL_RE.sub("[EMAIL]", text)
        text = PHONE_RE.sub("[PHONE]", text)
        text = CARD_RE.sub("[CARD]", text)
        text = LONG_NUM_RE.sub("[NUM]", text)
        out.append(text)
    return tuple(out)
