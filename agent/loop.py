"""The agent loop: baseline first, LLM with tool calling only where the baseline is unsure.

triage(rows) -> one decision per row:
    {txn_id, category, confidence, decided_by, routed_to_review, reason, tool_calls,
     violations, tool_errors, cost_usd, latency_s, prompt_version}

- Rows at/above CONFIDENCE_GATE: accept the baseline (decided_by='baseline'). No LLM cost.
- Rows below the gate: an LLM (Claude, or Gemini's free tier via agent/gemini_client.py) with
  tools (lookup_merchant if use_rag, flag_for_review, submit_decision), at most
  guardrails.MAX_TOOL_CALLS calls per row.
- Every tool call is logged (name, args, result summary, latency) for the audit trail;
  tokens and wall time are tracked so the eval can report cost and latency per 100 rows.

Tools come from mcp_server over MCP. TOOLS_TRANSPORT=stdio (default) launches the server as a
subprocess, exactly as Claude Code would; TOOLS_TRANSPORT=inproc connects to the same server
object in-process (faster startup, used by tests). submit_decision is local to the loop: it
is the structured final answer, not a side effect, so it does not belong on the server.

Flags for the eval versions:
  use_agent=False               -> Version A (baseline only)
  use_agent=True, use_rag=False -> Version B
  use_agent=True, use_rag=True  -> Version C
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from agent import guardrails
from agent.prompts import PROMPT_VERSION, system_prompt

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent

CONFIDENCE_GATE = float(os.getenv("CONFIDENCE_GATE", "0.84"))


def _resolve_provider() -> str:
    """LLM_PROVIDER=anthropic|gemini, or auto: whichever key is configured (Claude first)."""
    choice = os.getenv("LLM_PROVIDER", "auto").lower()
    if choice in ("anthropic", "gemini"):
        return choice
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")) and (
            os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        return "gemini"
    return "anthropic"


PROVIDER = _resolve_provider()
MODEL = (os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite") if PROVIDER == "gemini"
         else os.getenv("LLM_MODEL", "claude-opus-5-5"))
EFFORT = os.getenv("LLM_EFFORT", "low")            # per-row classification: low effort is enough
CONCURRENCY = int(os.getenv("AGENT_CONCURRENCY", "6"))
AGENT_MIN_CONFIDENCE = float(os.getenv("AGENT_MIN_CONFIDENCE", "0.7"))
TOOLS_TRANSPORT = os.getenv("TOOLS_TRANSPORT", "stdio")

# USD per 1M tokens: input, output, cache read, cache write (5-minute TTL)
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
}

# Server-side refusal fallback ("default" routing) and effort are only accepted on newer models.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
NO_EFFORT_MODELS = {"claude-haiku-4-5"}

TAXONOMY = json.loads((ROOT / "data" / "taxonomy.json").read_text())["categories"]


def _model_kwargs(model: str) -> dict:
    kw = {}
    if model not in NO_EFFORT_MODELS:
        kw["output_config"] = {"effort": EFFORT}
    if model in FALLBACK_MODELS:
        kw.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    return kw

SUBMIT_TOOL = {
    "name": "submit_decision",
    "description": "Final answer for this transaction when you are confident. Ends the transaction.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": TAXONOMY},
            "confidence": {"type": "number", "description": "Probability 0-1 that the category is right."},
            "reason": {"type": "string", "description": "One short line citing the evidence."},
        },
        "required": ["category", "confidence", "reason"],
        "additionalProperties": False,
    },
}


def has_llm_credentials() -> bool:
    if PROVIDER == "gemini":
        from agent.gemini_client import api_key

        return bool(api_key())
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return True
    try:  # an `ant auth login` profile also works with a zero-arg client (resolved lazily)
        import anthropic

        c = anthropic.Anthropic()
        return any(getattr(c, k, None) for k in ("api_key", "auth_token", "credentials"))
    except Exception:
        return False


def cost_usd(model: str, usage: dict) -> float:
    if PROVIDER == "gemini" or model.startswith(("gemini", "gemma")):
        return 0.0  # Google AI Studio free tier (Gemini and Gemma): rate-limited, not billed
    p_in, p_out, p_read, p_write = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (usage["input"] * p_in + usage["output"] * p_out + usage["cache_read"] * p_read
            + usage["cache_write"] * p_write) / 1_000_000


class Toolbox:
    """MCP client for mcp_server: lists its tools for Claude and executes Claude's calls."""

    def __init__(self, transport: str = TOOLS_TRANSPORT):
        self.transport = transport

    async def __aenter__(self):
        from mcp import Client, StdioServerParameters

        if self.transport == "inproc":
            os.environ["REVIEW_SINK"] = "none"  # the API layer persists review rows itself
            from mcp_server.server import mcp as server

            target = server
        else:
            target = StdioServerParameters(
                command=sys.executable, args=["-m", "mcp_server.server"], cwd=str(ROOT),
                env={"REVIEW_SINK": "none", "PYTHONPATH": str(ROOT), "TOKENIZERS_PARALLELISM": "false",
                     "HF_HUB_VERBOSITY": "error", "TRANSFORMERS_VERBOSITY": "error"},
            )
        self._client = Client(target)
        await self._client.__aenter__()
        listed = await self._client.list_tools()
        self.defs = {t.name: {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                     for t in listed.tools}
        return self

    async def __aexit__(self, *exc):
        await self._client.__aexit__(*exc)

    def tool_defs(self, names: list[str]) -> list[dict]:
        return [self.defs[n] for n in names]

    async def call(self, name: str, args: dict):
        """Returns (ok, payload). payload is the tool's structured result, or the error text."""
        res = await self._client.call_tool(name, args)
        text = " ".join(getattr(c, "text", "") for c in res.content)
        if res.is_error:
            return False, text or "tool error"
        sc = res.structured_content
        if isinstance(sc, dict) and set(sc) == {"result"}:
            sc = sc["result"]
        return True, sc if sc is not None else text


def _baseline_decision(row: dict, base: dict, routed: bool, latency: float) -> dict:
    return {
        "txn_id": row["txn_id"], "category": base["category"], "confidence": base["confidence"],
        "decided_by": "baseline", "routed_to_review": routed, "reason": base["reason"], "stage": base["stage"],
        "tool_calls": [], "violations": [], "tool_errors": 0, "cost_usd": 0.0, "latency_s": latency,
        "prompt_version": None,
    }


def _render_row(row: dict, base: dict) -> str:
    amount, side = (row.get("debit"), "debit") if row.get("debit") else (row.get("credit"), "credit")
    return (f"txn_id: {row['txn_id']}\n"
            f"narration: {row['narration']}\n"
            f"amount: {amount or 'unknown'} ({side})\n"
            f"baseline guess: {base['category']} (confidence {base['confidence']:.2f}) - {base['reason']}")


def _summarize(payload, limit: int = 300) -> str:
    s = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "..."


async def _agent_row(llm, tb: Toolbox, tools: list[dict], system: str, row: dict, base: dict,
                     sem: asyncio.Semaphore, base_latency: float) -> dict:
    async with sem:
        t0 = time.perf_counter()
        messages = [{"role": "user", "content": _render_row(row, base)}]
        calls, violations, tool_errors = [], [], 0
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        final, served_by, nudged = None, MODEL, False
        mcp_names = {t["name"] for t in tools} - {"submit_decision"}

        while final is None and not violations:
            try:
                resp = await llm.beta.messages.create(
                    model=MODEL, max_tokens=8000,
                    system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                    tools=tools, messages=messages, **_model_kwargs(MODEL),
                )
            # Any failure at the provider boundary (API error, no credentials, network) costs this row
            # only: it goes to review with the error in its audit trail; the batch carries on.
            except Exception as e:
                violations.append("llm_error")
                calls.append({"name": "llm", "ok": False, "result": f"{type(e).__name__}: {e}"[:300]})
                break
            served_by = getattr(resp, "model", MODEL) or MODEL
            u = resp.usage
            usage["input"] += u.input_tokens or 0
            usage["output"] += u.output_tokens or 0
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

            if resp.stop_reason == "refusal":
                violations.append("refusal")
                break
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": resp.content})
            if not tool_uses:
                if nudged:
                    violations.append("no_decision")
                    break
                nudged = True
                messages.append({"role": "user", "content": "Finish by calling submit_decision or flag_for_review."})
                continue

            results = []
            for tu in tool_uses:
                if len(calls) >= guardrails.MAX_TOOL_CALLS:
                    violations.append("max_tool_calls")
                    break
                t_call = time.perf_counter()
                args = dict(tu.input) if isinstance(tu.input, dict) else {}
                if tu.name == "submit_decision":
                    ok, payload = True, "decision recorded"
                    final = {"category": args.get("category"), "confidence": args.get("confidence"),
                             "reason": args.get("reason", ""), "flagged": False}
                elif tu.name in mcp_names:
                    if tu.name == "flag_for_review":
                        args["txn_id"] = row["txn_id"]  # never trust the model to copy ids
                        args.setdefault("narration", row["narration"])
                    try:
                        ok, payload = await tb.call(tu.name, args)
                    except Exception as e:  # transport failure: report it to the model as a tool error
                        ok, payload = False, f"{type(e).__name__}: {e}"
                    if ok and tu.name == "flag_for_review":
                        final = {"category": args.get("suggested_category"), "confidence": base["confidence"],
                                 "reason": args.get("reason", ""), "flagged": True}
                else:
                    ok, payload = False, f"unknown tool '{tu.name}'"
                tool_errors += not ok
                calls.append({"name": tu.name, "args": args, "ok": ok, "result": _summarize(payload),
                              "latency_ms": round((time.perf_counter() - t_call) * 1000, 1)})
                results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": not ok,
                                "content": payload if isinstance(payload, str) else json.dumps(payload)})
                if final:
                    break
            if final is None and not violations:
                messages.append({"role": "user", "content": results})

        decision = {
            "txn_id": row["txn_id"], "decided_by": "agent", "stage": "agent", "tool_calls": calls,
            "prompt_version": PROMPT_VERSION, "cost_usd": round(cost_usd(served_by, usage), 6),
            "latency_s": round(base_latency + time.perf_counter() - t0, 3), "tokens": usage, "model": served_by,
        }
        if final is None:
            decision.update(category=base["category"], confidence=base["confidence"], routed_to_review=True,
                            reason=f"agent could not decide ({', '.join(violations)}); baseline: {base['reason']}")
        else:
            decision.update(category=final["category"], confidence=final["confidence"], reason=final["reason"])
            violations += guardrails.validate({**final, "tool_calls": calls}, TAXONOMY)
            low = isinstance(final["confidence"], (int, float)) and final["confidence"] < AGENT_MIN_CONFIDENCE
            decision["routed_to_review"] = final["flagged"] or bool(violations) or low
            if violations and decision["category"] not in TAXONOMY:
                decision["category"] = base["category"]
        decision["violations"] = violations
        decision["tool_errors"] = tool_errors + len(violations)
        return decision


def make_llm():
    if PROVIDER == "gemini":
        from agent.gemini_client import GeminiClient

        return GeminiClient(MODEL)
    import anthropic

    return anthropic.AsyncAnthropic()


async def triage_async(rows: list[dict], use_agent: bool = True, use_rag: bool = True,
                       llm=None, transport: str = TOOLS_TRANSPORT) -> list[dict]:
    if not rows:
        return []
    async with Toolbox(transport) as tb:
        t0 = time.perf_counter()
        ok, base = await tb.call("categorize_transactions", {"narrations": [r["narration"] for r in rows]})
        if not ok:
            raise RuntimeError(f"baseline categorizer failed: {base}")
        base_latency = round((time.perf_counter() - t0) / len(rows), 4)

        decisions: list = [None] * len(rows)
        pending = []
        for i, (row, b) in enumerate(zip(rows, base)):
            if b["confidence"] >= CONFIDENCE_GATE:
                decisions[i] = _baseline_decision(row, b, routed=False, latency=base_latency)
            elif not use_agent:
                decisions[i] = _baseline_decision(row, b, routed=True, latency=base_latency)
            else:
                pending.append(i)

        if pending:
            if llm is None:
                llm = make_llm()
            tools = tb.tool_defs((["lookup_merchant"] if use_rag else []) + ["flag_for_review"]) + [SUBMIT_TOOL]
            system, sem = system_prompt(TAXONOMY), asyncio.Semaphore(CONCURRENCY)
            done = await asyncio.gather(*[_agent_row(llm, tb, tools, system, rows[i], base[i], sem, base_latency)
                                          for i in pending])
            for i, d in zip(pending, done):
                decisions[i] = d
        return decisions


def triage(rows: list[dict], use_agent: bool = True, use_rag: bool = True) -> list[dict]:
    return asyncio.run(triage_async(rows, use_agent=use_agent, use_rag=use_rag))
