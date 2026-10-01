"""Baseline categorizer: rules -> merchant/VPA extraction -> SBERT semantic fallback.

Contract (keep it stable: the MCP tool, the agent and the eval all depend on it):

    categorize(narration: str) -> Prediction

Only the narration is passed in. Never read golden-set columns such as `label` or
`merchant_hint` here, or the eval measures nothing.
"""
import re
from dataclasses import dataclass


@dataclass
class Prediction:
    category: str          # one of data/taxonomy.json categories, or "Uncategorized"
    confidence: float      # 0.0 - 1.0
    reason: str            # short, human-readable: which stage decided and why
    stage: str             # "rules" | "extract" | "semantic" | "none"


# Stage modules import Prediction from here, so import them after it is defined.
from categorizer import extract, rules, semantic  # noqa: E402

# Person-to-person: the free-text note is the only thing that separates rent from a loan to a friend.
RENT_NOTE = re.compile(r"\b(rent|rental|landlord|lease|pg|maintenance)\b", re.I)
REFUND_NOTE = re.compile(r"\b(refund|reversal|cashback|chargeback)\b", re.I)
# Notes that describe money shared between people: the payment really is a P2P transfer.
SOCIAL_NOTE = re.compile(
    r"\b(dinner|lunch|breakfast|snacks?|coffee|tea|drinks?|party|birthday|gift|thanks|thank you|treat|"
    r"trip|share|split|movie|cab|auto|taxi|loan|borrowed|owed?|return(ed)?|settle(ment)?|contribution|"
    r"wedding|festival|pocket money)\b", re.I)

CONF_P2P_NOTE = 0.90    # person + a note that says what it was for
CONF_P2P_BARE = 0.80    # person, no note: probably P2P, but undisclosed rent looks the same
CONF_P2P_UNREAD = 0.75  # a note the rules cannot read ("rnt", "tuition"): below the gate, so someone reads it
CONF_REFUND = 0.70      # money back from a person may belong to the original purchase's category


def _person(ext: extract.Extracted) -> Prediction:
    note, who = ext.get("note", ""), ext.get("payee") or ext.get("vpa", "")
    if note and RENT_NOTE.search(note):
        return Prediction("Rent", CONF_P2P_NOTE, f"extract: person '{who}' with rent note '{note}'", "extract")
    if note and REFUND_NOTE.search(note):
        return Prediction("P2P Transfer", CONF_REFUND, f"extract: refund from person '{who}' ('{note}')", "extract")
    if note and SOCIAL_NOTE.search(note):
        return Prediction("P2P Transfer", CONF_P2P_NOTE, f"extract: person '{who}', note '{note}'", "extract")
    if note:
        # The note says what the money was for, in words the rules do not know. That is evidence,
        # not noise: a confident P2P here would auto-post school fees or rent as a transfer.
        return Prediction("P2P Transfer", CONF_P2P_UNREAD,
                          f"extract: person '{who}', note '{note}' not recognised by the rules", "extract")
    return Prediction("P2P Transfer", CONF_P2P_BARE, f"extract: person '{who}', no note", "extract")


def categorize(narration: str) -> Prediction:
    """Run the stages in order and return the first answer that applies.

      1. rules.match       brand VPAs/names and bank-rail patterns (high confidence)
      2. extract.merchant  parse payee/VPA/note; person payees become P2P or Rent by note
      3. semantic.classify SBERT on the merchant name for everything rules did not know

    Low-confidence answers are still returned as the best guess; the gate in the eval /
    agent decides whether they go to review.
    """
    if hit := rules.match(narration):
        return hit
    ext = extract.merchant(narration)
    if not ext:
        return Prediction("Uncategorized", 0.0, "none: no rule hit and no payee found", "none")
    if ext.get("looks_like_person"):
        return _person(ext)
    text = semantic.payee_text(ext.get("payee", ""), ext.get("vpa", ""))
    return semantic.classify(text)
