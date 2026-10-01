"""Query the merchant directory by meaning.

search(text, k=3) -> [{name, category, description, score}], best first.

Backend: Postgres/pgvector when it is up and indexed, otherwise the local numpy index in
rag/index/ (same vectors, same scores). Force one with RAG_BACKEND=pg|local.
Scores are cosine similarities, returned so the agent can judge how close a match is.
"""
import json
import os
from functools import lru_cache
from pathlib import Path

import numpy as np

import db
from categorizer.semantic import embed

INDEX_DIR = Path(__file__).resolve().parent / "index"


@lru_cache(maxsize=1)
def _backend() -> str:
    forced = os.getenv("RAG_BACKEND", "auto")
    if forced in ("pg", "local"):
        return forced
    if db.available():
        try:
            with db.connect() as conn:
                if conn.execute("SELECT count(*) FROM merchants WHERE embedding IS NOT NULL").fetchone()[0]:
                    return "pg"
        except Exception:
            pass
    return "local"


@lru_cache(maxsize=1)
def _local_index():
    if not (INDEX_DIR / "embeddings.npy").exists():
        raise FileNotFoundError("RAG index missing: run `python -m rag.build_index` first")
    merchants = json.loads((INDEX_DIR / "merchants.json").read_text(encoding="utf-8"))
    return merchants, np.load(INDEX_DIR / "embeddings.npy")


def _search_local(vec: np.ndarray, k: int) -> list[dict]:
    merchants, matrix = _local_index()
    scores = matrix @ vec
    return [{**merchants[i], "score": round(float(scores[i]), 4)} for i in np.argsort(-scores)[:k]]


def _search_pg(vec: np.ndarray, k: int) -> list[dict]:
    from pgvector.psycopg import register_vector

    with db.connect() as conn:
        register_vector(conn)
        rows = conn.execute(
            """SELECT name, category, description, 1 - (embedding <=> %s) AS score
               FROM merchants ORDER BY embedding <=> %s LIMIT %s""",
            (vec, vec, k),
        ).fetchall()
    return [{"name": n, "category": c, "description": d, "score": round(float(s), 4)} for n, c, d, s in rows]


@lru_cache(maxsize=2048)
def _cached(text: str, k: int) -> tuple:
    vec = embed(text)
    hits = _search_pg(vec, k) if _backend() == "pg" else _search_local(vec, k)
    return tuple(hits)


def search(text: str, k: int = 3) -> list[dict]:
    text = (text or "").strip()
    if not text:
        return []
    return [dict(h) for h in _cached(text.lower(), max(1, min(int(k), 10)))]


def backend() -> str:
    return _backend()
