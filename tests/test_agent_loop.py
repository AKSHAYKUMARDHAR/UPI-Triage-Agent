"""Agent loop tests with a scripted fake Claude client.

The MCP server (in-process transport), the categorizer and the RAG index are all real; only
the LLM is faked, so these run free and offline once the SBERT model is cached.
Requires the RAG index: python -m rag.build_index
"""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from agent import loop

SWIGGY = {"txn_id": "T1", "narration": "UPI-SWIGGY-swiggy.order@icici-SBI1266-145215326988-Payment from Ph",
          "debit": "300", "credit": ""}
SAIRAM = {"txn_id": "T2", "narration": "UPI/DR/1/SAI RAM MEDICALS/HDFC/sairammedicals@okicici/Payment from Ph",
          "debit": "450", "credit": ""}


def _usage():
    return NS(input_tokens=500, output_tokens=60, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def _tool(name, args, i=1):
    return NS(content=[NS(type="tool_use", id=f"tu_{i}", name=name, input=args)], stop_reason="tool_use",
              usage=_usage(), model="claude-opus-5-5")


class FakeClaude:
    """Calls lookup_merchant first, then submits the directory's top category (or a scripted answer)."""

    def __init__(self, final=None, silent=False):
        self.final, self.silent, self.requests = final, silent, []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kw):
        self.requests.append(kw)
        msgs = kw["messages"]
        if self.silent:
            return NS(content=[NS(type="text", text="hmm")], stop_reason="end_turn", usage=_usage(),
                      model="claude-opus-5-5")
        if len(msgs) == 1:
            return _tool("lookup_merchant", {"text": "SAI RAM MEDICALS sairammedicals@okicici", "k": 3})
        if self.final:
            return _tool(*self.final, i=2)
        hits = json.loads(msgs[-1]["content"][0]["content"])
        return _tool("submit_decision", {"category": hits[0]["category"], "confidence": 0.93,
                                         "reason": f"directory match {hits[0]['name']} ({hits[0]['score']})"}, i=2)


def run(rows, **kw):
    return asyncio.run(loop.triage_async(rows, transport="inproc", **kw))


def test_version_a_routes_low_confidence_without_llm():
    swiggy, sairam = run([SWIGGY, SAIRAM], use_agent=False)
    assert swiggy["decided_by"] == "baseline" and not swiggy["routed_to_review"]
    assert sairam["decided_by"] == "baseline" and sairam["routed_to_review"]


def test_agent_uses_rag_and_logs_tool_calls():
    fake = FakeClaude()
    swiggy, sairam = run([SWIGGY, SAIRAM], llm=fake)
    assert swiggy["decided_by"] == "baseline"          # above the gate: no LLM call
    assert len(fake.requests) == 2                      # only the uncertain row reached Claude
    assert sairam["decided_by"] == "agent" and sairam["category"] == "Health & Pharmacy"
    assert not sairam["routed_to_review"] and sairam["violations"] == []
    assert [c["name"] for c in sairam["tool_calls"]] == ["lookup_merchant", "submit_decision"]
    assert sairam["cost_usd"] > 0 and sairam["prompt_version"] == loop.PROMPT_VERSION


def test_version_b_has_no_lookup_tool():
    fake = FakeClaude(final=("submit_decision", {"category": "Health & Pharmacy", "confidence": 0.9,
                                                 "reason": "chemist"}))
    run([SAIRAM], llm=fake, use_rag=False)
    assert "lookup_merchant" not in [t["name"] for t in fake.requests[0]["tools"]]


def test_guardrail_violation_goes_to_review():
    fake = FakeClaude(final=("submit_decision", {"category": "Health & Pharmacy", "confidence": 1.7, "reason": "x"}))
    (d,) = run([SAIRAM], llm=fake)
    assert d["routed_to_review"] and "bad_confidence" in d["violations"] and d["tool_errors"] >= 1


def test_invalid_flag_category_is_a_tool_error_not_a_crash():
    fake = FakeClaude(final=("flag_for_review", {"txn_id": "T2", "narration": "n", "suggested_category": "Medicine",
                                                 "reason": "unsure"}))
    (d,) = run([SAIRAM], llm=fake)  # keeps re-flagging with the bad category until the call cap
    assert d["routed_to_review"] and d["tool_errors"] >= 1
    assert any(not c["ok"] and c["name"] == "flag_for_review" for c in d["tool_calls"])


def test_agent_that_never_decides_is_routed():
    (d,) = run([SAIRAM], llm=FakeClaude(silent=True))
    assert d["routed_to_review"] and d["violations"] == ["no_decision"]


@pytest.mark.parametrize("model, has_fallback", [("claude-opus-5-5", True), ("claude-haiku-4-5", False)])
def test_model_kwargs(model, has_fallback):
    kw = loop._model_kwargs(model)
    assert ("fallbacks" in kw) == has_fallback
