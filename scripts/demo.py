"""Prepare and drive the 2-minute demo (shot list: docs/DEMO.md).

Usage: python scripts/demo.py [--reset] [--no-browser]

1. checks Postgres and n8n are up (`docker compose up -d`)
2. starts the API on :8000 if it is not running, with the MCP server in-process so the demo
   is not waiting on model loads, and warms it up
3. opens the browser tabs the video uses (n8n, the review queue, the README)
4. waits for Enter, then drops data/demo_statement.csv into data/inbox/; n8n picks it up
5. waits until the triage lands in Postgres and prints the summary

--reset clears the results and review_queue tables first (they only hold demo data).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

API = "http://localhost:8000"
N8N = "http://localhost:5678"
REPO = "https://github.com/AKSHAYKUMARDHAR/UPI-Triage-Agent"


def http(method: str, url: str, body: dict | None = None, timeout: float = 5):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def up(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=3)
        return True
    except Exception:
        return False


def start_api() -> subprocess.Popen | None:
    if up(f"{API}/health"):
        print("API: already running on :8000")
        return None
    env = {**os.environ, "TOOLS_TRANSPORT": "inproc"}  # beats .env: load_dotenv never overrides
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(90):
        if up(f"{API}/health"):
            print(f"API: started on :8000 (pid {proc.pid})")
            return proc
        time.sleep(1)
    proc.terminate()
    raise SystemExit("API did not start; run `uvicorn api.main:app --port 8000` by hand to see the error")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="clear results and review_queue first")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--file", default=str(ROOT / "data" / "demo_statement.csv"))
    args = ap.parse_args()

    if not db.available():
        raise SystemExit("Postgres is not reachable: run `docker compose up -d` first")
    print("Postgres: up" + ("" if up(f"{N8N}/healthz") else "   (n8n is NOT up: the inbox trigger will not fire)"))

    proc = start_api()
    print("health:", http("GET", f"{API}/health"))
    print("warming up the categorizer (loads SBERT once)...")
    http("POST", f"{API}/triage", {"statement_id": "WARMUP", "use_agent": False,
                                   "rows": [{"txn_id": "W001", "narration": "UPI/1/x/swiggy.order@icici/HDFC"}]},
         timeout=180)
    with db.connect() as conn:
        conn.execute("DELETE FROM results WHERE statement_id = 'WARMUP'")
        if args.reset:
            conn.execute("TRUNCATE results, review_queue")
            print("cleared results and review_queue")

    if not args.no_browser:
        for url in (f"{N8N}/home/workflows", f"{API}/review", REPO):
            webbrowser.open(url)

    rows = sum(1 for _ in open(args.file, encoding="utf-8")) - 1
    input(f"\nReady. Start recording, then press Enter to drop {Path(args.file).name} ({rows} rows) into the inbox...")
    with db.connect() as conn:
        dropped_at = conn.execute("SELECT now()").fetchone()[0]
    tmp, out = ROOT / "data" / ".demo.tmp", ROOT / "data" / "inbox" / f"DEMO-{datetime.now():%H%M%S}.csv"
    shutil.copyfile(args.file, tmp)
    tmp.replace(out)  # atomic: the watcher never sees a half-written file
    print(f"dropped -> {out.relative_to(ROOT)}; n8n should pick it up within a few seconds")

    t0 = time.time()
    while time.time() - t0 < 600:
        with db.connect() as conn:
            n = conn.execute("SELECT count(*) FROM results WHERE statement_id = 'DEMO' AND created_at >= %s",
                             (dropped_at,)).fetchone()[0]
        if n >= rows:
            break
        print(f"  waiting for triage... {int(time.time() - t0)}s", end="\r")
        time.sleep(3)
    else:
        raise SystemExit("\nno results after 10 minutes: check the n8n executions page and the API window")

    with db.connect() as conn:
        by = dict(conn.execute("SELECT decided_by, count(*) FROM results WHERE statement_id = 'DEMO' "
                               "GROUP BY 1").fetchall())
        review = conn.execute("SELECT count(*) FROM review_queue q JOIN results r USING (txn_id) "
                              "WHERE r.statement_id = 'DEMO' AND q.status = 'open'").fetchone()[0]
    print(f"\ndone in {int(time.time() - t0)}s: {rows} rows, decided by {by}, {review} waiting in {API}/review")

    if proc:
        try:
            input("\nPress Enter (or Ctrl+C) to stop the API when you have finished recording...")
        except (KeyboardInterrupt, EOFError):
            pass
        proc.terminate()


if __name__ == "__main__":
    main()
