"""Stage 3: SBERT semantic fallback.

Embeds a handful of example phrases per category once, then classifies the extracted
payee text by nearest-example cosine similarity. Confidence blends the top similarity
with the margin over the runner-up category, so an ambiguous name ('Sri Ram') scores
low even when its best similarity is decent.

The examples are hand-written generic phrases. They deliberately do NOT come from
data/merchants.csv: that file is the RAG directory, and using it here would leak
Version C's knowledge into Version A.

Model bake-off (Version A tuning): set SBERT_MODEL to compare MiniLM vs E5 vs BGE, e.g.
    SBERT_MODEL=BAAI/bge-small-en-v1.5 python -m eval.predict_baseline
and record accuracy AND latency.
"""
import os
import re
from functools import lru_cache

from categorizer import Prediction

MODEL_NAME = os.getenv("SBERT_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
CONF_CAP = 0.92  # a name-only guess never gets rule-level confidence

# P2P Transfer, Salary & Income and Cash Withdrawal are decided by structure (person VPA,
# bank rail), not by what a payee name means, so they have no examples here.
EXAMPLES = {
    "Food Delivery": ["online food delivery app", "food order delivery", "order food online"],
    "Groceries": ["kirana store", "provision store", "general store groceries", "supermarket",
                  "vegetables and fruits shop", "daily essentials rice dal oil", "grocery delivery"],
    "Dining": ["restaurant", "veg hotel meals tiffin", "biryani restaurant", "cafe tea coffee snacks",
               "bakery and sweets", "dhaba", "fast food outlet", "chai stall"],
    "Fuel": ["petrol pump", "fuel station", "diesel filling station", "petroleum"],
    "Utilities": ["electricity bill", "power distribution company", "water supply bill",
                  "lpg cooking gas cylinder", "gas agency"],
    "Mobile & Internet": ["mobile recharge", "prepaid postpaid telecom", "broadband internet", "fibernet wifi"],
    "Rent": ["house rent", "flat rent payment to landlord", "rental housing", "pg accommodation rent"],
    "Shopping": ["online shopping marketplace", "electronics store", "clothing and fashion store",
                 "sports goods store", "eyewear optician", "home services cleaning salon repair",
                 "furniture and home decor", "footwear shop"],
    "Travel": ["cab ride", "auto rickshaw", "bike taxi", "bus ticket booking", "train ticket",
               "flight booking", "vehicle repair garage", "toll and parking"],
    "Entertainment & Subscriptions": ["movie tickets", "streaming subscription", "music subscription",
                                      "gym fitness membership", "gaming", "concert and events"],
    "Health & Pharmacy": ["pharmacy", "medical store chemist", "medicines", "hospital", "clinic doctor",
                          "diagnostic lab test", "dental clinic"],
    "Education": ["school fees", "coaching institute", "tuition classes", "college fees",
                  "online learning course", "exam preparation academy"],
    "EMI & Loans": ["loan emi", "credit card bill payment", "home loan repayment", "consumer finance emi"],
    "Investments": ["mutual fund sip", "stock broking", "life insurance premium", "fixed deposit", "gold investment"],
    "Bank Charges": ["bank service charges", "annual card fee"],
}

# Tokens in VPAs that say nothing about the category ('swiggy.order', 'decathlon.in').
VPA_NOISE = {"in", "india", "pay", "payment", "payments", "upi", "official", "online"}


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer  # heavy import: only when needed

    return SentenceTransformer(MODEL_NAME)


def embed(texts):
    """Unit-normalised embeddings with the shared model (also used by the RAG index)."""
    return _model().encode(texts, normalize_embeddings=True, show_progress_bar=False)


@lru_cache(maxsize=1)
def _example_matrix():
    labels = [cat for cat, phrases in EXAMPLES.items() for _ in phrases]
    phrases = [p for ps in EXAMPLES.values() for p in ps]
    return labels, embed(phrases)


def payee_text(payee: str = "", vpa: str = "") -> str:
    """Build the text to embed: payee name plus the VPA local part split into words."""
    local = vpa.split("@")[0] if vpa else ""
    vpa_words = [w for w in re.split(r"[._\-\d]+", local.lower()) if w and w not in VPA_NOISE]
    parts = [payee.strip()] if payee.strip() else []
    vpa_text = " ".join(vpa_words)
    if vpa_text and vpa_text.replace(" ", "") not in payee.lower().replace(" ", ""):
        parts.append(vpa_text)
    return " ".join(parts).lower()


def _confidence(top: float, margin: float) -> float:
    sim_part = min(max((top - 0.25) / 0.45, 0.0), 1.0)   # cosine 0.25 -> 0, 0.70 -> 1
    margin_part = min(max(margin / 0.15, 0.0), 1.0)      # runner-up 0.15 behind -> 1
    return round(min(CONF_CAP, 0.6 * sim_part + 0.4 * margin_part), 4)


@lru_cache(maxsize=4096)  # statements repeat payees a lot; skip re-embedding them
def classify(text: str) -> Prediction:
    text = (text or "").strip()
    if not text:
        return Prediction("Uncategorized", 0.0, "semantic: nothing to embed", "semantic")
    labels, matrix = _example_matrix()
    sims = matrix @ embed(text)
    best = {}
    for label, s in zip(labels, sims):
        best[label] = max(best.get(label, -1.0), float(s))
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    (cat, top), (runner, second) = ranked[0], ranked[1]
    conf = _confidence(top, top - second)
    return Prediction(cat, conf, f"semantic: '{text}' ~ {cat} (cos {top:.2f}, next {runner} {second:.2f})", "semantic")
