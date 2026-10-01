# Review UI (human-in-the-loop)

A single static page ([index.html](index.html)) served by the API at
http://localhost:8000/review. Built by hand rather than with Lovable or Replit, so it
runs with no external account and the code is reviewable in the repo.

- Lists open rows from `review_queue`: narration, suggested category, confidence, the
  agent's reason, and (expandable) every tool call the agent made for that row.
- Accept or correct the category from a taxonomy dropdown. Saving writes `status`
  (`accepted` / `corrected`), `final_category` and `reviewed_at`, and updates `results`
  to `decided_by = 'human'`.
- *Export reviewed as CSV* downloads resolved rows: new labelled examples to grow the
  golden set.

Backed by `GET /api/review`, `POST /api/review/{txn_id}`, `GET /api/reviewed` and
`GET /api/taxonomy` in [api/main.py](../api/main.py). Needs Postgres (`docker compose up -d`).
