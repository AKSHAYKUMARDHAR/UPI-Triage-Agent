# n8n workflow

[triage_workflow.json](triage_workflow.json): a bank statement (CSV, XLS or XLSX, as
downloaded from netbanking, or the pipeline's own CSV) lands in `data/inbox/` → n8n uploads
the file as-is to `POST /triage/file` → if `to_review > 0`, build an alert ("N of M
transactions need review" + review UI link) → Slack. The API does all the parsing
([ingest/](../ingest/)), so the workflow has no format logic of its own.

```
Local File Trigger → Read statement file → POST /triage/file (multipart upload)
   → IF to_review > 0 → Build review alert (Code) → Send Slack alert (disabled)
```

## Run it

1. Start the stack and the API (the API must listen on 0.0.0.0 so the container can reach it):
   ```bash
   docker compose up -d
   uvicorn api.main:app --host 0.0.0.0 --port 8000
   ```
2. Import and publish the workflow (the compose file mounts this folder at `/workflows`):
   ```bash
   docker compose exec n8n n8n import:workflow --input=/workflows/triage_workflow.json
   docker compose exec n8n n8n publish:workflow --id=upiTriageInbox01
   docker compose restart n8n
   ```
   In Git Bash on Windows, prefix with `MSYS_NO_PATHCONV=1` so `/workflows` is not rewritten.
   Or open http://localhost:5678 and use *Import from file*.
3. Trigger it: `python data/drop_statement.py S003`, or copy a statement export into
   `data/inbox/`. Results appear in Postgres and at http://localhost:8000/review. A file the
   API cannot read (a PDF, say) fails the HTTP node with the reason in the execution log.
4. Alerts: enable the *Send Slack alert* node and paste your incoming-webhook URL, or swap
   it for an Email / Telegram node.

## Notes

- n8n 2.x disables the Local File Trigger and restricts file access by default.
  `docker-compose.yml` re-enables only that trigger (Execute Command stays blocked) and only
  for `/data/inbox`.
- The trigger polls, because file events from a Windows/macOS bind mount do not reach the
  container. `drop_statement.py` writes to a temp file and renames it into the inbox, so the
  watcher never reads a half-written file.
- Verified end to end: a made-up statement in HDFC's Excel layout (title rows, summary
  block) dropped into the inbox was uploaded, parsed into its 7 transactions and written to
  Postgres; earlier, a 60-row CSV statement ran every node to `n8n.workflow.success`.
- Everything in `data/inbox/` is gitignored, whatever the extension: a real statement must
  never end up in the repo.
