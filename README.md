# UPI Transaction Triage Agent

An AI agent that categorizes Indian bank-statement (UPI) transactions. A cheap baseline
(rules + extraction + SBERT) handles what it is sure about. Everything below a confidence
gate goes to an **LLM agent** (Claude, or Gemini's free tier) that calls tools over **MCP**, looks up unknown merchants
with **RAG** (pgvector), and sends anything it is still unsure about to a **human review
queue**. An **n8n** workflow triggers the pipeline when a statement lands, and an
**evaluation harness** decides whether each version is good enough to ship.

All data is synthetic. No real customer, client or employer data is used.

## Architecture

```
 Statement CSV dropped in data/inbox/
          │
        n8n  (Local File Trigger → parse CSV → POST /triage → IF to_review > 0 → alert)
          │
          ▼
   FastAPI /triage  (api/main.py) ─────────────► Postgres: results, review_queue
          │                                         ▲
          ▼                                         │  accept / correct
   agent/loop.py   ── MCP client (stdio) ──►  mcp_server/server.py
          │                                    ├─ categorize_transactions  rules → extract → SBERT
          │  confidence ≥ gate: accept          ├─ lookup_merchant          RAG over merchants (pgvector)
          │  confidence < gate: Claude ─tools──►├─ flag_for_review          review queue
          │                                    └─ get_taxonomy
          ▼
   decision + audit trail (every tool call, tokens, cost, latency)
                                                    Review UI  (/review)
```

Claude Code can use the same MCP server directly, so the agent and an interactive session
share one tool implementation.

## Results

Golden set: 250 synthetic rows, 121 marked hard (typos, truncated names, small local
merchants, P2P payments that are really rent). Gate 0.84 for all versions.
Every number below comes from an eval run logged in [eval/results/runs.jsonl](eval/results/runs.jsonl).

| Version | Accuracy | Automation rate | Precision (automated) | Review rate | Hard-row accuracy | Tool-call errors | Cost / 100 rows | Latency / 100 rows |
|---|---|---|---|---|---|---|---|---|
| A: baseline | 93.2% | 70.8% | 100.0% | 29.2% | 86.0% | 0 | $0 | 0.14 s warm (7.1 s incl. model load via MCP) |
| B: A + agent, no RAG | _pending_ | | | | | | | |
| C: A + agent + RAG | _pending_ | | | | | | | |

**Version A, read honestly.** Every automated answer was correct, and all 17 errors were in
rows the gate sent to review, so nothing wrong was auto-posted. That precision comes partly
from the data: the rules target the same brand list the generator draws from, and
narrations follow three templates. Real statements will be messier; the number to watch is
how much of the 29.2% review load B and C can absorb without losing precision.

By stage (Version A): rules decided 115 rows (100% correct), person/rent routing 87 rows
(100%), SBERT 48 rows (64.6%, 45 of them routed to review). The SBERT misses are the case
for RAG: names like "Indane Gas booking" (read as Travel) or "Shell Indiranagar" (Dining).

**Choosing the gate.** The sweep (`python -m eval.run_eval eval/predictions_baseline.csv --sweep`)
shows gate 0.80 would automate 79.2% at 100% precision. The difference is P2P payments with
no note (confidence 0.80). They were all correct here, but the generator never produces rent
without a note, so the golden set cannot measure the risk that gate guards against. The
default stays at 0.84; it is a policy decision, not a tuning one.

### Held-out set: what the system has never seen

`data/holdout_set.csv`: 57 hand-written rows, built independently of the rules and the RAG
directory (no merchant in it appears in `merchants.csv` or the rules brand map). Families:
25 unseen national brands, 7 unseen local merchants, 6 cryptic payees (BharatPe/Paytm QR,
"SKR ENTERPRISES"), 9 person payments with hand-written notes ("rnt", "pg fees", "tuition"),
8 bank formats the generator never produces (POS, BIL/ONL, ACH, NACH, NEFT rent, interest,
card AMC, a refund credit) and 2 prompt-injection narrations. 9 rows are marked
`expect_review`: the narration does not hold enough evidence, so the right answer is review.

| Version | Accuracy | Automation | Precision (automated) | Unknowns sent to review | Unknowns auto-posted wrong |
|---|---|---|---|---|---|
| A: baseline | 42.1% | 24.6% | **64.3%** | 88.9% | 0.0% |
| B: A + agent, no RAG | _pending_ | | | | |
| C: A + agent + RAG | _pending_ | | | | |

**Version A, held out.** Precision on automated rows falls from 100% to 64.3%: 5 of 14
auto-posted rows are wrong, and all 5 come from one rule. A person payment with a note the
rule does not recognise ("rnt", "flat maint", "tuition", "doctor consultation", "milk sept")
is posted as P2P Transfer at confidence 0.90, above the gate, so neither the agent nor a human
ever sees it. The note is evidence the rule cannot read; that confidence should sit below the
gate. Unseen brands are never auto-posted (0% automation), which is safe but leaves all 25
for the agent: B shows what the model knows on its own, C whether a directory that does not
contain them pulls it toward a wrong near match ("TATA PLAY DTH" vs "Tata Power DDL").
Run: `python -m eval.predict_agent --version C --gold data/holdout_set.csv`.

## Setup

Python **3.12** (PyTorch / sentence-transformers wheels lag the newest Python).

```bash
python3.12 -m venv .venv
.venv\Scripts\activate                       # Windows; source .venv/bin/activate elsewhere
pip install torch --index-url https://download.pytorch.org/whl/cpu   # optional: small CPU build
pip install -r requirements.txt
copy .env.example .env                        # add a free GEMINI_API_KEY (or ANTHROPIC_API_KEY) for B/C
docker compose up -d                          # Postgres+pgvector on :5433, n8n on :5678
python data/generator.py                      # statements.csv + golden_set.csv
python -m rag.build_index                     # merchant embeddings -> pgvector + rag/index/
pytest                                        # 32 tests, no API key needed
```

Postgres is published on host port **5433** so it never collides with a locally installed
Postgres on 5432.

## Run it

| What | Command |
|---|---|
| Version A eval | `python -m eval.predict_baseline` then `python -m eval.run_eval eval/predictions_baseline.csv --sweep` |
| Held-out eval | add `--gold data/holdout_set.csv` to any predict / run_eval command |
| Version B / C eval | `python -m eval.predict_agent --version C --limit 20` (smoke), then without `--limit`; score with `python -m eval.run_eval eval/predictions_C.csv`. On Gemini's free tier a full run takes ~15-25 min because of the rate limit |
| API | `uvicorn api.main:app --host 0.0.0.0 --port 8000` |
| Review UI | http://localhost:8000/review |
| n8n workflow | see [n8n/README.md](n8n/README.md); then `python data/drop_statement.py S003` |
| MCP in Claude Code | `claude mcp add upi-triage -- <repo>\.venv\Scripts\python.exe -m mcp_server.server` (run from the repo root) |
| Rule coverage | `python -m categorizer.rules data/statements.csv` |

## How it works

**Baseline** ([categorizer/](categorizer/)). Only the narration is used.
1. *Rules*: bank-rail patterns (salary NEFT, ATM, charges) and a brand map for 35 national
   brands, matched on the VPA handle first, then on the payee name with truncation and
   one-letter-typo tolerance ("ZEROD", "HDFC H", "ZETO").
2. *Extraction*: parses the three UPI formats plus IMPS/NEFT into payee, VPA and note, and
   detects person-to-person payments from the VPA shape. For a person, the note decides:
   a rent/maintenance note means Rent, a refund note goes to review, no note gets 0.80.
3. *SBERT*: MiniLM similarity between the payee text and hand-written category examples.
   Confidence blends similarity with the margin over the runner-up category. The examples
   deliberately do not come from `merchants.csv`, which is the RAG directory: using it here
   would leak Version C's knowledge into Version A.

**Agent** ([agent/loop.py](agent/loop.py)). Rows below the gate go to an LLM with the tools
listed by the MCP server plus a local `submit_decision` tool (category enum = taxonomy).
Two providers, picked by whichever key is in `.env`:
- **Claude** (`claude-sonnet-5-5` / `claude-opus-5-5`, effort `low`) through the Anthropic SDK.
- **Gemini free tier** (`gemini-3.1-flash-lite` by default: on a new key `gemini-2.5-flash` is closed and `gemini-3.5-flash` allows only 20 free requests/day) through
  [agent/gemini_client.py](agent/gemini_client.py), an adapter that exposes the same call
  shape the loop uses. It translates tool schemas and tool results, replays the model's own
  turns unchanged (Gemini needs its thought signatures back for multi-turn tool use), throttles
  to the free tier's requests-per-minute and retries 429s. `python -m agent.gemini_client`
  lists the models a key can use.

The loop:
- caps tool calls per row (4), nudges once if the model answers in text, and routes to
  review on refusal, API error, guardrail violation or agent confidence below 0.7;
- never trusts the model to copy ids (`txn_id` is overwritten in `flag_for_review`);
- treats the narration as untrusted data (the prompt says so; it is never an instruction);
- logs every tool call with args, result summary and latency, plus tokens and cost;
- on Claude, uses server-side refusal fallback (`fallbacks: "default"`) so a declined
  request is re-run on a fallback model instead of failing the row;
- versions the prompt ([agent/prompts.py](agent/prompts.py)); each eval run records it.

**Guardrails** ([agent/guardrails.py](agent/guardrails.py)): category in taxonomy, ≤ 4 tool
calls, confidence a float in [0, 1], non-empty reason. Violations plus failed tool calls are
the *tool-call errors* metric.

**RAG** ([rag/](rag/)). "name: description" for each merchant, embedded with the same
MiniLM model, stored in pgvector with an HNSW cosine index. A local numpy index with
identical vectors is the fallback when Docker is not running.

**MCP server** ([mcp_server/server.py](mcp_server/server.py)), MCP Python SDK 2.x
`MCPServer`. Expected failures (a category outside the taxonomy) raise `ToolError`, so the
model sees the message and can correct itself instead of getting a generic crash.

**Human in the loop**. `/triage` writes every decision to `results` and every routed row
to `review_queue`. The review UI shows the suggestion, the agent's reason and its tool
calls; accepting or correcting writes `final_category` and flips `results.decided_by` to
`human`. Resolved rows export as CSV: new labelled examples for the golden set.

## Golden set review notes

The generator's labels are a draft. Labels worth a deliberate decision:

| Rows | Draft label | Question |
|---|---|---|
| 9 IMPS "REFUND" credits (e.g. G00037) | P2P Transfer | Could be a merchant refund; the baseline sends them to review (0.70) |
| Kotak credit card bill (G00099, G00144) | EMI & Loans | A card bill is a transfer of debt, not a loan EMI |
| LIC premium (G00248) | Investments | No Insurance category in the taxonomy |
| Cult Fit (3 rows) | Entertainment & Subscriptions | Arguably Health |
| Urban Company (2 rows) | Shopping | Home services, not goods |

Also: several "tail" merchants in `merchants.csv` (Rapido, Decathlon, Lenskart, Reliance
Digital, Tata Power, Indane, LIC) are national brands. B vs C therefore measures "brands
the rules don't cover", not only small local shops.

## Repo map

| Path | Purpose |
|---|---|
| `data/generator.py` | Synthetic UPI statements + draft golden set |
| `data/drop_statement.py` | Drop one statement into `data/inbox/` to trigger n8n |
| `categorizer/` | Baseline: rules, extraction, SBERT |
| `rag/` | Build and query the merchant embedding index |
| `mcp_server/server.py` | MCP tools (stdio) |
| `agent/` | Agent loop, Gemini adapter, versioned prompt, guardrails |
| `api/main.py` | `/triage`, `/health`, review API |
| `review_ui/index.html` | Review queue UI (served at `/review`) |
| `n8n/triage_workflow.json` | Importable n8n workflow |
| `db/` | Postgres schema (`init.sql`) and helpers |
| `eval/` | Prediction runners, metrics, run log (`results/runs.jsonl`) |
| `tests/` | Extraction, rules, guardrails, agent loop with fake Claude and Gemini backends |
