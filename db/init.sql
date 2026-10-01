CREATE EXTENSION IF NOT EXISTS vector;

-- Every triage decision, one row per transaction
CREATE TABLE IF NOT EXISTS results (
    txn_id         TEXT PRIMARY KEY,
    statement_id   TEXT NOT NULL,
    narration      TEXT NOT NULL,
    category       TEXT,
    confidence     REAL,
    decided_by     TEXT NOT NULL,          -- 'baseline' | 'agent' | 'human'
    reason         TEXT,
    tool_calls     JSONB,                  -- audit trail of the agent's tool calls
    created_at     TIMESTAMPTZ DEFAULT now()
);

-- Human-in-the-loop queue
CREATE TABLE IF NOT EXISTS review_queue (
    txn_id              TEXT PRIMARY KEY,
    narration           TEXT NOT NULL,
    suggested_category  TEXT,
    reason              TEXT,
    status              TEXT DEFAULT 'open',   -- 'open' | 'accepted' | 'corrected'
    final_category      TEXT,
    reviewed_at         TIMESTAMPTZ
);

-- Merchant directory for RAG (384 dims = all-MiniLM-L6-v2)
CREATE TABLE IF NOT EXISTS merchants (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL,
    description  TEXT,
    embedding    vector(384)
);
