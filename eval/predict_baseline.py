"""Run the baseline categorizer over the golden set and write predictions.

Usage: python -m eval.predict_baseline [--gate 0.84]

Only the narration is passed to the categorizer. Rows below the gate are marked
routed_to_review=1 (Version A has no agent, so uncertain rows go straight to review).
"""
import argparse
import csv
import time
from pathlib import Path

from categorizer import categorize

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", type=float, default=0.84)
    ap.add_argument("--gold", default=str(ROOT / "data" / "golden_set.csv"),
                    help="labelled set to predict on, e.g. data/holdout_set.csv")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    stem = Path(args.gold).stem
    args.out = args.out or str(ROOT / "eval" / ("predictions_baseline.csv" if stem == "golden_set"
                                                 else f"predictions_baseline_{stem.removesuffix('_set')}.csv"))

    with open(args.gold, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    t0, out = time.perf_counter(), []
    for r in rows:
        p = categorize(r["narration"])
        out.append({"txn_id": r["txn_id"], "predicted": p.category, "confidence": round(p.confidence, 4),
                    "routed_to_review": int(p.confidence < args.gate), "stage": p.stage, "reason": p.reason})
    elapsed = time.perf_counter() - t0

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    print(f"wrote {args.out} ({len(out)} rows, {elapsed / len(out) * 100:.2f}s per 100 rows)")


if __name__ == "__main__":
    main()
