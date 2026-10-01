# 2-minute demo: shot list

The demo statement ([data/demo_statement.csv](../data/demo_statement.csv), 16 rows) is built so
each row shows one path: rules (salary, ATM, Swiggy, the typo "ZETO"), person payments (rent
note, dinner, an unreadable "school fees jan"), local merchants the lookup rescues, cryptic
payees the agent should flag, and one prompt-injection attempt the guard stops.

## Before you record (once, about 10 minutes)

1. **Fresh Gemini quota.** The demo sends 9 rows to the agent, about 15 requests. Record on a
   day you have not run the evals: the free tier allows only so many requests per day.
2. **Stack up:** `docker compose up -d`. Open http://localhost:5678; the first visit asks you
   to create a local n8n owner account (it stays on your machine). Open the *UPI statement
   triage* workflow, then its *Executions* tab.
3. **Demo driver:** `python scripts/demo.py --reset`. It starts the API, warms up the model,
   clears old demo rows, opens the n8n, review and README tabs, then waits for Enter.
4. **Recorder:** [OBS Studio](https://obsproject.com/) (free): *Display Capture*, 1920x1080,
   30 fps. Windows Game Bar (Win+Alt+R) also works but records only one window. Set the
   browser zoom to 125% and turn on Focus Assist so notifications stay quiet.
5. **Do one dry run** without recording, then `python scripts/demo.py --reset` again.

## Shot list

| Time | On screen | Say |
|---|---|---|
| 0:00 | README top: title and architecture diagram | "Indian bank statements are messy: truncated merchant names, UPI handles, payments to people that are really rent. This agent categorizes them, and knows when to ask a human." |
| 0:12 | Terminal: press Enter. Switch to the n8n Executions tab; the run appears | "A statement lands in the inbox. n8n picks it up and posts it to the triage API." |
| 0:25 | n8n run in progress (cut the wait in editing if it runs long) | "Rules and a small embedding model settle the easy rows for free: salary, ATM, Swiggy, even a typo like 'ZETO'. The nine rows they are unsure about go to an LLM agent that calls tools over MCP: a merchant lookup on pgvector, and a tool to flag a row for review." |
| 0:55 | Review page (localhost:8000/review), Refresh. Expand the tool calls on *M S TRADING CO* | "These are the rows it chose not to decide. Here it looked the payee up, found nothing close, and flagged it instead of guessing." |
| 1:10 | The *SYSTEM OVERRIDE* row | "This narration tries to prompt-inject the agent. A guard sent it straight to review; the model never saw it." |
| 1:18 | Pick a category on one row and Save; the status shows *corrected* | "I correct one, and it is recorded as a human decision." |
| 1:25 | README, Results: the held-out table | "Every number comes from logged eval runs. On a hand-written held-out set, the agent with the lookup auto-posts 86% of rows at 98% precision and flags every payee it cannot identify. Without the lookup, the same model guessed. The held-out set also caught two real bugs, including that injection, and I fixed both." |
| 1:50 | The GitHub repo page | "The code, the evals and the MCP server are on GitHub. Link below." |

## After recording

Upload to YouTube (*Unlisted*) or Loom and send the link: it goes at the top of the README.
To stop the API, press Enter in the demo terminal.
