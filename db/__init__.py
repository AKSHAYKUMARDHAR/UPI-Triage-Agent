"""Thin Postgres helpers shared by the API, the MCP server and the RAG index.

Everything degrades gracefully: `available()` is False when Docker is not running, and
callers fall back (local RAG index, JSONL review log) instead of crashing.
"""
import json
import os

from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://triage:triage@localhost:5433/triage")


def connect():
    import psycopg

    return psycopg.connect(DATABASE_URL, connect_timeout=3, autocommit=True)


_up = False


def available() -> bool:
    """True once Postgres answers. Only success is cached, so a late `docker compose up` is picked up."""
    global _up
    if not _up:
        try:
            with connect() as conn:
                conn.execute("SELECT 1")
            _up = True
        except Exception:
            pass
    return _up


def write_results(statement_id: str, decisions: list[dict], narrations: dict[str, str]) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO results (txn_id, statement_id, narration, category, confidence, decided_by, reason, tool_calls)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (txn_id) DO UPDATE SET statement_id = EXCLUDED.statement_id,
                 narration = EXCLUDED.narration, category = EXCLUDED.category, confidence = EXCLUDED.confidence,
                 decided_by = EXCLUDED.decided_by, reason = EXCLUDED.reason, tool_calls = EXCLUDED.tool_calls,
                 created_at = now()""",
            [(d["txn_id"], statement_id, narrations.get(d["txn_id"], ""), d["category"], d["confidence"],
              d["decided_by"], d["reason"], json.dumps(d.get("tool_calls", []))) for d in decisions],
        )


def upsert_review(txn_id: str, narration: str, suggested_category: str, reason: str) -> None:
    """Open (or re-open) a review item. A row a human already resolved stays resolved."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO review_queue (txn_id, narration, suggested_category, reason)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (txn_id) DO UPDATE SET narration = EXCLUDED.narration,
                 suggested_category = EXCLUDED.suggested_category, reason = EXCLUDED.reason
               WHERE review_queue.status = 'open'""",
            (txn_id, narration, suggested_category, reason),
        )


def open_reviews(limit: int = 200) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT q.txn_id, q.narration, q.suggested_category, q.reason, r.confidence, r.decided_by, r.tool_calls
               FROM review_queue q LEFT JOIN results r USING (txn_id)
               WHERE q.status = 'open' ORDER BY q.txn_id LIMIT %s""",
            (limit,),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def resolve_review(txn_id: str, final_category: str) -> bool:
    """Record the human decision and copy it into results (decided_by='human')."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE review_queue SET final_category = %s, reviewed_at = now(),
                 status = CASE WHEN suggested_category = %s THEN 'accepted' ELSE 'corrected' END
               WHERE txn_id = %s RETURNING txn_id""",
            (final_category, final_category, txn_id),
        )
        if cur.fetchone() is None:
            return False
        cur.execute("UPDATE results SET category = %s, decided_by = 'human', confidence = 1.0 WHERE txn_id = %s",
                    (final_category, txn_id))
        return True


def reviewed_examples() -> list[dict]:
    """Resolved review rows: new labelled examples to grow the golden set."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT txn_id, narration, suggested_category, final_category, status, reviewed_at
                       FROM review_queue WHERE status <> 'open' ORDER BY reviewed_at""")
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
