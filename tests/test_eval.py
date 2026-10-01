"""Scorer metrics, including the held-out set's expect_review measures."""
import csv
from pathlib import Path

from eval.run_eval import by_purpose, score

ROOT = Path(__file__).resolve().parent.parent


def _g(label, expect="0", purpose="unseen_brand", hard="0"):
    return {"label": label, "expect_review": expect, "purpose": purpose, "is_hard": hard}


def _p(pred, routed, conf=0.5):
    return {"predicted": pred, "routed_to_review": "1" if routed else "0", "confidence": str(conf)}


def test_unknown_metrics():
    gold = {"a": _g("Shopping", "1", "cryptic"), "b": _g("Groceries", "1", "cryptic"), "c": _g("Fuel")}
    pred = {"a": _p("Shopping", True), "b": _p("Dining", False, 0.9), "c": _p("Fuel", False, 0.95)}
    s = score(gold, pred)
    assert s["unknown_flag_rate"] == 0.5          # a routed, b auto-posted
    assert s["unknown_wrong_auto"] == 0.5         # b auto-posted with the wrong category
    assert s["precision_automated"] == 0.5        # b wrong, c right


def test_golden_set_has_no_unknown_metrics():
    gold = {"a": {"label": "Fuel", "is_hard": "0"}}
    assert "unknown_flag_rate" not in score(gold, {"a": _p("Fuel", False)})


def test_by_purpose():
    gold = {"a": _g("Rent", purpose="person_note"), "b": _g("P2P Transfer", purpose="person_note")}
    pred = {"a": _p("Rent", False), "b": _p("Rent", True)}
    fam = by_purpose(gold, pred)["person_note"]
    assert fam == {"rows": 2, "accuracy": 0.5, "automation": 0.5}


def test_holdout_labels_are_in_taxonomy():
    import json

    tax = set(json.loads((ROOT / "data" / "taxonomy.json").read_text())["categories"])
    with open(ROOT / "data" / "holdout_set.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows and all(r["label"] in tax for r in rows)
