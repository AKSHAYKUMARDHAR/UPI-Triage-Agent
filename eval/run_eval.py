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

Held-out sets (data/holdout_set.csv) add two columns, and these metrics when present:
  expect_review          1 = the narration does not contain enough evidence to decide;
                         the right behaviour is review, whatever the label says
  unknown_flag_rate      share of expect_review rows routed to review (higher is better)
  unknown_wrong_auto     share of expect_review rows auto-posted with a wrong category
                         (the failure the gate exists to prevent; lower is better)
  purpose                row family (unseen_brand, cryptic, injection, ...): accuracy and
                         automation are also reported per family
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
    out = {
        "rows": n,
        "accuracy": correct / n,
        "automation_rate": auto / n,
        "precision_automated": (auto_correct / auto) if auto else 0.0,
        "review_rate": 1 - auto / n,
        "hard_accuracy": (hard_correct / hard) if hard else 0.0,
        "top_errors": errors.most_common(8),
    }
    unknown = [(g, pred[t]) for t, g in gold.items() if g.get("expect_review") == "1" and t in pred]
    if unknown:
        def routed(p):
            return (float(p["confidence"]) < gate) if gate is not None else p["routed_to_review"] == "1"
        out["unknown_flag_rate"] = sum(routed(p) for _, p in unknown) / len(unknown)
        out["unknown_wrong_auto"] = sum(not routed(p) and p["predicted"] != g["label"] for g, p in unknown) / len(unknown)
    return out


def by_purpose(gold, pred):
    """Accuracy and automation per row family, for held-out sets that carry a `purpose` column."""
    groups = {}
    for t, g in gold.items():
        if g.get("purpose") and t in pred:
            groups.setdefault(g["purpose"], []).append((g, pred[t]))
    return {k: {"rows": len(v),
                "accuracy": sum(p["predicted"] == g["label"] for g, p in v) / len(v),
                "automation": sum(p["routed_to_review"] != "1" for _, p in v) / len(v)}
            for k, v in sorted(groups.items())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("predictions")
    ap.add_argument("--gold", default=str(ROOT / "data" / "golden_set.csv"))
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    gold, pred = load(args.gold), load(args.predictions)
    s = score(gold, pred)
    print(f"rows                 {s['rows']}")
    for k in ("accuracy", "automation_rate", "precision_automated", "review_rate", "hard_accuracy",
              "unknown_flag_rate", "unknown_wrong_auto"):
        if k in s:
            print(f"{k:<21}{s[k]:.1%}")
    families = by_purpose(gold, pred)
    if families:
        print("by family            rows  accuracy  automation")
        for k, v in families.items():
            print(f"  {k:<18} {v['rows']:>4}  {v['accuracy']:>8.1%}  {v['automation']:>10.1%}")
    for t, g in gold.items():
        if g.get("purpose") == "injection" and t in pred:
            p = pred[t]
            print(f"injection {t}: predicted {p['predicted']} (label {g['label']}), "
                  f"{'routed to review' if p['routed_to_review'] == '1' else 'AUTO-POSTED'}")

    extra = [r for r in pred.values() if r.get("cost_usd")]
    if extra:
        cost = sum(float(r["cost_usd"]) for r in extra) / len(pred) * 100
        # Agent rows run in parallel, so per-row latencies overlap: summing them is not wall time.
        # Report the per-row distribution here; wall time per run is in eval/results/runs.jsonl.
        lats = sorted(float(r.get("latency_s") or 0) for r in pred.values())
        p95 = lats[int(0.95 * (len(lats) - 1))]
        errs = sum(int(r.get("tool_errors") or 0) for r in pred.values())
        print(f"cost per 100 rows    ${cost:.4f}\n"
              f"row latency          mean {sum(lats) / len(lats):.2f}s, p95 {p95:.2f}s (wall time: runs.jsonl)\n"
              f"tool-call errors     {errs}")

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
