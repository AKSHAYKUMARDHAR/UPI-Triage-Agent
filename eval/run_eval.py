"""Score a predictions file against the golden set.

Usage: python -m eval.run_eval eval/predictions_baseline.csv [--sweep]

Predictions CSV columns: txn_id, predicted, confidence, routed_to_review
(optional: tool_errors, cost_usd, latency_s)

Metrics
  accuracy               predicted == label over all rows (review rows count as wrong
                         unless the suggestion was right; this is the strict view)
  automation_rate        share of rows NOT routed to review
  precision_automated    accuracy on the rows the system handled by itself
  review_rate            share routed to review
  hard_accuracy          accuracy on is_hard rows only
  --sweep                precision/automation trade-off across confidence gates
"""
import argparse
import csv
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {r["txn_id"]: r for r in csv.DictReader(f)}


def score(gold, pred, gate=None):
    n = len(gold)
    correct = auto = auto_correct = hard = hard_correct = 0
    errors = Counter()
    for tid, g in gold.items():
        p = pred.get(tid)
        if p is None:
            errors["missing_prediction"] += 1
            continue
        ok = p["predicted"] == g["label"]
        routed = (float(p["confidence"]) < gate) if gate is not None else p["routed_to_review"] == "1"
        correct += ok
        if not routed:
            auto += 1
            auto_correct += ok
        if g.get("is_hard") == "1":
            hard += 1
            hard_correct += ok
        if not ok:
            errors[f"{g['label']} -> {p['predicted']}"] += 1
    return {
        "rows": n,
        "accuracy": correct / n,
        "automation_rate": auto / n,
        "precision_automated": (auto_correct / auto) if auto else 0.0,
        "review_rate": 1 - auto / n,
        "hard_accuracy": (hard_correct / hard) if hard else 0.0,
        "top_errors": errors.most_common(8),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("predictions")
    ap.add_argument("--gold", default=str(ROOT / "data" / "golden_set.csv"))
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    gold, pred = load(args.gold), load(args.predictions)
    s = score(gold, pred)
    print(f"rows                 {s['rows']}")
    for k in ("accuracy", "automation_rate", "precision_automated", "review_rate", "hard_accuracy"):
        print(f"{k:<21}{s[k]:.1%}")

    extra = [r for r in pred.values() if r.get("cost_usd")]
    if extra:
        cost = sum(float(r["cost_usd"]) for r in extra) / len(pred) * 100
        lat = sum(float(r.get("latency_s") or 0) for r in pred.values()) / len(pred) * 100
        errs = sum(int(r.get("tool_errors") or 0) for r in pred.values())
        print(f"cost per 100 rows    ${cost:.4f}\nlatency per 100 rows {lat:.1f}s\ntool-call errors     {errs}")

    print("top errors (label -> predicted):")
    for e, c in s["top_errors"]:
        print(f"  {c:>3}  {e}")

    if args.sweep:
        print("\ngate   automation  precision_automated")
        for g in [0.5, 0.6, 0.7, 0.75, 0.8, 0.84, 0.88, 0.92, 0.95]:
            t = score(gold, pred, gate=g)
            print(f"{g:<6} {t['automation_rate']:>9.1%}  {t['precision_automated']:>10.1%}")


if __name__ == "__main__":
    main()
