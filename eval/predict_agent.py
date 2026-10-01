"""Run the full triage pipeline (baseline -> gate -> agent) on the golden set.

Usage:
  python -m eval.predict_agent --version C              # A + agent + RAG
  python -m eval.predict_agent --version B              # A + agent, no RAG
  python -m eval.predict_agent --version A              # baseline through the same pipeline (no LLM)
  python -m eval.predict_agent --version C --limit 20   # cheap smoke run first

Only txn_id, narration and amount reach the pipeline. `label` and `merchant_hint` are read
by the scorer afterwards, never by the agent.

Writes eval/predictions_<version>.csv (for eval/run_eval.py), the full per-row audit trail to
eval/results/decisions_<version>.jsonl, and appends a run manifest (model, prompt version,
gate, metrics, cost, wall time) to eval/results/runs.jsonl.
"""
import argparse
import asyncio
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from agent import loop
from eval.run_eval import score

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"
INPUT_COLS = ("txn_id", "narration", "debit", "credit")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", choices=["A", "B", "C"], required=True)
    ap.add_argument("--limit", type=int, default=0, help="only the first N rows (smoke test)")
    ap.add_argument("--gold", default=str(ROOT / "data" / "golden_set.csv"))
    args = ap.parse_args()

    use_agent, use_rag = {"A": (False, False), "B": (True, False), "C": (True, True)}[args.version]
    if use_agent and not loop.has_llm_credentials():
        raise SystemExit("Version B/C needs an LLM key: ANTHROPIC_API_KEY or (free) GEMINI_API_KEY in .env")

    with open(args.gold, newline="", encoding="utf-8") as f:
        gold_rows = list(csv.DictReader(f))
    if args.limit:
        gold_rows = gold_rows[: args.limit]
    rows = [{k: r[k] for k in INPUT_COLS} for r in gold_rows]  # the only columns the agent sees

    t0 = time.perf_counter()
    decisions = asyncio.run(loop.triage_async(rows, use_agent=use_agent, use_rag=use_rag))
    wall = time.perf_counter() - t0

    suffix = args.version + (f"_limit{args.limit}" if args.limit else "")
    out_csv = ROOT / "eval" / f"predictions_{suffix}.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["txn_id", "predicted", "confidence", "routed_to_review", "stage", "decided_by", "reason",
                    "tool_errors", "cost_usd", "latency_s"])
        for d in decisions:
            conf = d["confidence"] if isinstance(d["confidence"], (int, float)) else 0.0
            w.writerow([d["txn_id"], d["category"], round(conf, 4), int(d["routed_to_review"]), d["stage"],
                        d["decided_by"], d["reason"], d["tool_errors"], d["cost_usd"], d["latency_s"]])

    RESULTS.mkdir(exist_ok=True)
    with open(RESULTS / f"decisions_{suffix}.jsonl", "w", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")

    gold = {r["txn_id"]: r for r in gold_rows}
    with open(out_csv, newline="", encoding="utf-8") as f:
        metrics = score(gold, {r["txn_id"]: r for r in csv.DictReader(f)})
    agent_rows = [d for d in decisions if d["decided_by"] == "agent"]
    manifest = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "version": args.version,
        "rows": len(rows), "gate": loop.CONFIDENCE_GATE,
        "provider": loop.PROVIDER if use_agent else None, "model": loop.MODEL if use_agent else None, "effort": loop.EFFORT if use_agent else None,
        "prompt_version": loop.PROMPT_VERSION if use_agent else None, "transport": loop.TOOLS_TRANSPORT,
        "agent_rows": len(agent_rows),
        "cost_usd": round(sum(d["cost_usd"] for d in decisions), 4),
        "cost_per_100_rows": round(sum(d["cost_usd"] for d in decisions) / len(rows) * 100, 4),
        "wall_s": round(wall, 1), "wall_per_100_rows_s": round(wall / len(rows) * 100, 2),
        "tool_errors": sum(d["tool_errors"] for d in decisions),
        **{k: round(v, 4) for k, v in metrics.items() if isinstance(v, float)},
    }
    with open(RESULTS / "runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(manifest) + "\n")

    print(f"wrote {out_csv.relative_to(ROOT)} ({len(rows)} rows, {len(agent_rows)} sent to the agent, "
          f"{wall:.1f}s wall, ${manifest['cost_usd']:.4f})")
    llm_errors = sum("llm_error" in d["violations"] for d in decisions)
    if llm_errors:
        print(f"WARNING: {llm_errors} rows hit LLM errors (quota or rate limit?) and were routed to review; "
              f"see eval/results/decisions_{suffix}.jsonl. Re-run later for clean numbers.")
    print(f"score it: python -m eval.run_eval {out_csv.relative_to(ROOT).as_posix()} --sweep")


if __name__ == "__main__":
    main()
