"""Preview how a statement export will be read. Runs locally: nothing is sent anywhere.

    python -m ingest statement.xls                  # columns found, row count, first rows
    python -m ingest statement.xls --out rows.csv   # write the pipeline's CSV
    python -m ingest statement.xls --label-sheet    # private/<name>_labels.csv to label by hand,
                                                    # then score the system against your own data

Real statements hold personal data: keep them, and anything made from them, out of git
(data/inbox/ and private/ are gitignored).
"""
import argparse
import csv
from pathlib import Path

from ingest.statement import StatementError, _find_header, parse_statement, read_grids

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--out", help="write the parsed rows as the pipeline's CSV")
    ap.add_argument("--label-sheet", nargs="?", const="", default=None,
                    help="write a CSV with an empty label column (default: private/<name>_labels.csv)")
    args = ap.parse_args()

    path = Path(args.file)
    data = path.read_bytes()
    try:
        for grid in read_grids(data, path.name):
            if found := _find_header(grid):
                header, cols = found
                print(f"header on row {header + 1}: " + ", ".join(
                    f"{field}={grid[header][j]!r}" for field, j in sorted(cols.items(), key=lambda kv: kv[1])))
                break
        rows = parse_statement(data, path.name)
    except StatementError as e:
        raise SystemExit(f"cannot read {path.name}: {e}")

    debits = sum(r["debit"] or 0 for r in rows)
    credits = sum(r["credit"] or 0 for r in rows)
    print(f"{len(rows)} transactions, {rows[0]['date']} to {rows[-1]['date']}, "
          f"debits {debits:,.2f}, credits {credits:,.2f}")
    for r in rows[:5]:
        amount = f"-{r['debit']:,.2f}" if r["debit"] else f"+{r['credit']:,.2f}"
        print(f"  {r['txn_id']}  {r['date']}  {amount:>12}  {r['narration'][:70]}")

    if args.out:
        cols_out = ["txn_id", "statement_id", "date", "narration", "debit", "credit"]
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols_out, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {args.out}")
    if args.label_sheet is not None:
        out = Path(args.label_sheet or ROOT / "private" / f"{path.stem}_labels.csv")
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["txn_id", "date", "narration", "debit", "credit", "label", "is_hard"])
            for r in rows:
                w.writerow([r["txn_id"], r["date"], r["narration"], r["debit"] or "", r["credit"] or "", "", ""])
        print(f"wrote {out}: fill in 'label' (one of data/taxonomy.json), then e.g.\n"
              f"  AGENT_ENABLED=false python -m eval.predict_agent --version A --gold {out.as_posix()}")


if __name__ == "__main__":
    main()
