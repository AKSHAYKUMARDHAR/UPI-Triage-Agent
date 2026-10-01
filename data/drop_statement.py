"""Drop one statement from data/statements.csv into data/inbox/ to trigger the n8n workflow.

Usage: python data/drop_statement.py S003
"""
import csv
import sys
from pathlib import Path

HERE = Path(__file__).parent


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "S001"
    with open(HERE / "statements.csv", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if r["statement_id"] == sid]
        cols = reader.fieldnames
    if not rows:
        raise SystemExit(f"no rows for statement {sid}")
    (HERE / "inbox").mkdir(exist_ok=True)
    tmp, out = HERE / f".{sid}.tmp", HERE / "inbox" / f"{sid}.csv"  # tmp outside the watched folder
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(out)  # atomic rename, so the watcher never sees a half-written file
    print(f"dropped {len(rows)} rows -> {out}")


if __name__ == "__main__":
    main()
