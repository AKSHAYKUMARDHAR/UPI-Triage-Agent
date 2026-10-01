"""Build the merchant directory index for RAG.

Usage: python -m rag.build_index

Embeds "name + description" from data/merchants.csv with the same MiniLM model the
categorizer uses (384 dims, matches db/init.sql) and writes it to:
  - Postgres `merchants` table (pgvector) with an HNSW cosine index, when Postgres is up
  - rag/index/ (numpy), always, so search works without Docker

Note: 'tail' merchants in merchants.csv are deliberately NOT covered by the rules. They are
what RAG should rescue, which is the measurable difference between eval Versions B and C.
"""
import csv
import json
from pathlib import Path

import numpy as np

import db
from categorizer.semantic import embed

ROOT = Path(__file__).resolve().parent.parent
INDEX_DIR = Path(__file__).resolve().parent / "index"


def doc_text(m: dict) -> str:
    return f"{m['name']}: {m['description']}"


def load_merchants() -> list[dict]:
    with open(ROOT / "data" / "merchants.csv", newline="", encoding="utf-8") as f:
        return [{k: m[k] for k in ("name", "category", "description")} for m in csv.DictReader(f)]


def write_local(merchants: list[dict], vectors: np.ndarray) -> None:
    INDEX_DIR.mkdir(exist_ok=True)
    np.save(INDEX_DIR / "embeddings.npy", vectors.astype(np.float32))
    (INDEX_DIR / "merchants.json").write_text(json.dumps(merchants, indent=1), encoding="utf-8")


def write_postgres(merchants: list[dict], vectors: np.ndarray) -> None:
    from pgvector.psycopg import register_vector

    with db.connect() as conn:
        register_vector(conn)
        conn.execute("TRUNCATE merchants RESTART IDENTITY")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO merchants (name, category, description, embedding) VALUES (%s, %s, %s, %s)",
                [(m["name"], m["category"], m["description"], v) for m, v in zip(merchants, vectors)],
            )
        conn.execute("CREATE INDEX IF NOT EXISTS merchants_embedding_hnsw ON merchants "
                     "USING hnsw (embedding vector_cosine_ops)")


def main():
    merchants = load_merchants()
    vectors = embed([doc_text(m) for m in merchants])
    write_local(merchants, vectors)
    print(f"local index: {len(merchants)} merchants -> {INDEX_DIR}")
    if db.available():
        write_postgres(merchants, vectors)
        print(f"postgres: {len(merchants)} merchants -> merchants table (hnsw cosine index)")
    else:
        print("postgres: not reachable, skipped (start it with `docker compose up -d`)")


if __name__ == "__main__":
    main()
