"""MCP server exposing the triage tools (MCP Python SDK 2.x, `MCPServer`).

Run:      python -m mcp_server.server
Connect:  claude mcp add upi-triage -- "<repo>/.venv/Scripts/python.exe" -m mcp_server.server
          (run from the repo root), then ask Claude Code to categorize data/statements.csv.

The agent (agent/loop.py) is also an MCP client of this server: it launches it over stdio
and calls the same tools, so Claude Code and the agent share one tool implementation.

Review sink (env REVIEW_SINK): auto (Postgres if up, else data/review_queue.jsonl) |
postgres | jsonl | none. Eval runs use `none` so they never pollute the real queue.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

ROOT = Path(__file__).resolve().parent.parent
TAXONOMY = json.loads((ROOT / "data" / "taxonomy.json").read_text())["categories"]
REVIEW_LOG = ROOT / "data" / "review_queue.jsonl"

mcp = MCPServer(
    "upi-triage",
    instructions="Tools for categorizing Indian UPI bank-statement transactions. Start with "
                 "categorize_transactions; use lookup_merchant for unknown payees; send anything "
                 "uncertain to flag_for_review rather than guessing.",
    log_level="WARNING",  # stdout is the protocol channel; keep stderr quiet too
)


@mcp.tool()
def get_taxonomy() -> list[str]:
    """Return the only categories a transaction may be assigned to."""
    return TAXONOMY


@mcp.tool()
def categorize_transactions(narrations: list[str]) -> list[dict]:
    """Run the baseline categorizer (rules, extraction, SBERT) on bank narrations.

    Returns one {category, confidence, reason, stage} per narration, in order.
    confidence >= 0.84 is usually safe to accept; lower values need a second look.
    """
    from categorizer import categorize

    return [vars(categorize(n)) for n in narrations]


@mcp.tool()
def lookup_merchant(text: str, k: int = 3) -> list[dict]:
    """Search the merchant directory by meaning (RAG). Use for unknown or misspelled payees.

    Pass the payee name and/or VPA, e.g. "SAI RAM MEDICALS sairammedicals@okicici".
    Returns up to k {name, category, description, score} matches, best first. score is cosine
    similarity: above ~0.6 is usually the same merchant, below ~0.45 is probably unrelated.
    """
    from rag.search import search

    return search(text, k)


@mcp.tool()
def flag_for_review(txn_id: str, narration: str, suggested_category: str, reason: str) -> str:
    """Send a transaction to the human review queue instead of auto-categorizing it.

    Use when confidence stays low after lookup, or when the evidence conflicts.
    suggested_category must be one of get_taxonomy(); reason is one line for the reviewer.
    """
    if suggested_category not in TAXONOMY:
        raise ToolError(f"suggested_category '{suggested_category}' is not in the taxonomy: {TAXONOMY}")
    if not reason.strip():
        raise ToolError("reason must be a non-empty one-line explanation for the reviewer")

    sink = os.getenv("REVIEW_SINK", "auto")
    if sink == "none":
        return f"flagged {txn_id} for review (not persisted: REVIEW_SINK=none)"

    import db

    if sink == "postgres" or (sink == "auto" and db.available()):
        db.upsert_review(txn_id, narration, suggested_category, reason)
        return f"flagged {txn_id} for review in Postgres review_queue (suggested: {suggested_category})"

    with open(REVIEW_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"txn_id": txn_id, "narration": narration, "suggested_category": suggested_category,
                            "reason": reason, "flagged_at": datetime.now(timezone.utc).isoformat()}) + "\n")
    return f"flagged {txn_id} for review in {REVIEW_LOG.name} (Postgres not reachable)"


if __name__ == "__main__":
    mcp.run()
