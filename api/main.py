"""HTTP entry point that n8n calls, plus the human-in-the-loop review UI.

Run: uvicorn api.main:app --port 8000

POST /triage   {"statement_id": str, "rows": [{txn_id, narration, debit, credit}]}
               -> runs agent.loop.triage_async, writes results + review_queue to Postgres,
                  returns {"processed", "auto", "to_review", "decided_by", "cost_usd", ...}
GET  /health   -> {"ok": true, "postgres": bool, "agent": bool, "rag_backend": str}
GET  /review   -> review UI (review_ui/index.html)
GET  /api/review              open review rows with the agent's reason and tool calls
POST /api/review/{txn_id}     {"final_category": str} -> accept / correct
GET  /api/reviewed            resolved rows: new labelled examples for the golden set
GET  /api/taxonomy

The agent runs only when Claude credentials are configured (AGENT_ENABLED=auto|true|false);
without them /triage still works as Version A and routes uncertain rows to review.
"""
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import db
from agent import loop

ROOT = Path(__file__).resolve().parent.parent
TAXONOMY = json.loads((ROOT / "data" / "taxonomy.json").read_text())["categories"]

app = FastAPI(title="UPI Triage Agent")


class Row(BaseModel):
    txn_id: str
    narration: str
    debit: str | float | None = None
    credit: str | float | None = None


class TriageRequest(BaseModel):
    statement_id: str
    rows: list[Row] = Field(min_length=1, max_length=2000)
    use_agent: bool | None = None   # default: AGENT_ENABLED
    use_rag: bool = True


class Resolution(BaseModel):
    final_category: str


def _agent_enabled() -> bool:
    setting = os.getenv("AGENT_ENABLED", "auto").lower()
    return loop.has_llm_credentials() if setting == "auto" else setting == "true"


@app.get("/health")
def health():
    from rag.search import backend

    return {"ok": True, "postgres": db.available(), "agent": _agent_enabled(), "rag_backend": backend()}


@app.post("/triage")
async def triage_endpoint(payload: TriageRequest):
    use_agent = _agent_enabled() if payload.use_agent is None else payload.use_agent
    if use_agent and not loop.has_llm_credentials():
        raise HTTPException(400, "use_agent=true but no Claude credentials are configured (ANTHROPIC_API_KEY)")
    rows = [{"txn_id": r.txn_id, "narration": r.narration, "debit": str(r.debit or ""), "credit": str(r.credit or "")}
            for r in payload.rows]
    decisions = await loop.triage_async(rows, use_agent=use_agent, use_rag=payload.use_rag)

    persisted = db.available()
    if persisted:
        narrations = {r["txn_id"]: r["narration"] for r in rows}
        db.write_results(payload.statement_id, decisions, narrations)
        for d in decisions:
            if d["routed_to_review"]:
                suggestion = d["category"] if d["category"] in TAXONOMY else None
                db.upsert_review(d["txn_id"], narrations[d["txn_id"]], suggestion, d["reason"])

    to_review = sum(d["routed_to_review"] for d in decisions)
    return {
        "statement_id": payload.statement_id, "processed": len(decisions), "auto": len(decisions) - to_review,
        "to_review": to_review, "agent_used": use_agent, "persisted": persisted,
        "decided_by": {k: sum(d["decided_by"] == k for d in decisions) for k in ("baseline", "agent")},
        "tool_errors": sum(d["tool_errors"] for d in decisions),
        "cost_usd": round(sum(d["cost_usd"] for d in decisions), 4),
        "review_url": "http://localhost:8000/review",
    }


def _require_db():
    if not db.available():
        raise HTTPException(503, "Postgres is not reachable: run `docker compose up -d`")


@app.get("/review")
def review_page():
    return FileResponse(ROOT / "review_ui" / "index.html")


@app.get("/api/taxonomy")
def taxonomy():
    return TAXONOMY


@app.get("/api/review")
def review_queue(limit: int = 200):
    _require_db()
    return db.open_reviews(limit)


@app.post("/api/review/{txn_id}")
def resolve(txn_id: str, body: Resolution):
    _require_db()
    if body.final_category not in TAXONOMY:
        raise HTTPException(422, f"final_category must be one of {TAXONOMY}")
    if not db.resolve_review(txn_id, body.final_category):
        raise HTTPException(404, f"{txn_id} is not in the review queue")
    return {"txn_id": txn_id, "final_category": body.final_category}


@app.get("/api/reviewed")
def reviewed():
    _require_db()
    return db.reviewed_examples()
