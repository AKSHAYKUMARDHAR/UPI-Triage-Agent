"""The full agent loop on the Gemini adapter, against a fake google-genai backend.

Responses are real google.genai `types` objects, so the request/response translation,
function-response matching, thought-signature replay and 429 retry are exercised offline.
"""
import asyncio
import json
from types import SimpleNamespace as NS

from google.genai import errors, types

from agent import gemini_client, loop
from agent.gemini_client import GeminiClient, _RateLimiter

SAIRAM = {"txn_id": "T2", "narration": "UPI/DR/1/SAI RAM MEDICALS/HDFC/sairammedicals@okicici/Payment from Ph",
          "debit": "450", "credit": ""}


def _response(name, args, signature=b"sig"):
    part = types.Part(function_call=types.FunctionCall(name=name, args=args), thought_signature=signature)
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[part]), finish_reason="STOP")],
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=400, candidates_token_count=30,
                                                                   thoughts_token_count=50),
    )


class FakeGenai:
    def __init__(self, fail_first_with_429=False):
        self.calls, self.fail = [], fail_first_with_429
        self.aio = NS(models=NS(generate_content=self._generate))

    async def _generate(self, *, model, contents, config):
        if self.fail:
            self.fail = False
            raise errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
        self.calls.append({"contents": contents, "config": config})
        if len(contents) == 1:
            return _response("lookup_merchant", {"text": "SAI RAM MEDICALS sairammedicals@okicici", "k": 3})
        fr = contents[-1].parts[0].function_response
        hits = fr.response["result"]
        return _response("submit_decision", {"category": hits[0]["category"], "confidence": 0.9,
                                             "reason": f"directory: {hits[0]['name']}"})


def _client(fake):
    c = GeminiClient("gemini-2.5-flash", client=fake)
    c._limiter = _RateLimiter(60000)
    return c


def test_agent_loop_runs_on_gemini(monkeypatch):
    monkeypatch.setattr(gemini_client, "RETRY_BASE_DELAY", 0)
    fake = FakeGenai(fail_first_with_429=True)
    (d,) = asyncio.run(loop.triage_async([SAIRAM], llm=_client(fake), transport="inproc"))

    assert d["decided_by"] == "agent" and d["category"] == "Health & Pharmacy" and not d["routed_to_review"]
    assert [c["name"] for c in d["tool_calls"]] == ["lookup_merchant", "submit_decision"]

    first, second = fake.calls
    names = [f.name for f in first["config"].tools[0].function_declarations]
    assert names == ["lookup_merchant", "flag_for_review", "submit_decision"]
    assert "Categories: use ONLY these" in first["config"].system_instruction
    # turn 2 replays the model's own content (with its thought signature), then the tool result
    replayed = second["contents"][1]
    assert replayed.role == "model" and replayed.parts[0].thought_signature == b"sig"
    fr = second["contents"][2].parts[0].function_response
    assert fr.name == "lookup_merchant" and fr.response["result"][0]["name"] == "Sai Ram Medicals"


def test_tool_error_reaches_gemini_as_error_response(monkeypatch):
    client = _client(FakeGenai())
    client._names["local_x"] = "flag_for_review"
    contents = client._contents([{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "local_x", "is_error": True, "content": "bad category"}]}])
    fr = contents[0].parts[0].function_response
    assert fr.name == "flag_for_review" and fr.response == {"error": "bad category"} and fr.id is None


def test_google_free_tier_is_free():
    usage = {"input": 10**6, "output": 10**6, "cache_read": 0, "cache_write": 0}
    assert loop.cost_usd("gemini-3.5-flash", usage) == 0 and loop.cost_usd("gemma-4-31b-it", usage) == 0


class FlakyNetwork(FakeGenai):
    """Drops the connection once, the way the first full Version C run died."""

    def __init__(self):
        super().__init__()
        self.dropped = False

    async def _generate(self, **kw):
        if not self.dropped:
            self.dropped = True
            import httpx

            raise httpx.ConnectError("connection reset")
        return await super()._generate(**kw)


def test_dropped_connection_is_retried(monkeypatch):
    monkeypatch.setattr(gemini_client, "RETRY_BASE_DELAY", 0)
    fake = FlakyNetwork()
    fake.aio = NS(models=NS(generate_content=fake._generate))
    (d,) = asyncio.run(loop.triage_async([SAIRAM], llm=_client(fake), transport="inproc"))
    assert fake.dropped and d["decided_by"] == "agent" and not d["violations"]


def test_one_failing_row_does_not_sink_the_batch():
    class Broken:
        beta = NS(messages=NS(create=None))

    async def boom(**kw):
        raise RuntimeError("network down")

    Broken.beta.messages.create = boom
    swiggy = {"txn_id": "T1", "narration": "UPI-SWIGGY-swiggy.order@icici-SBI1-1-Payment from Ph", "debit": "1",
              "credit": ""}
    ok, failed = asyncio.run(loop.triage_async([swiggy, SAIRAM], llm=Broken(), transport="inproc"))
    assert ok["decided_by"] == "baseline" and not ok["routed_to_review"]
    assert failed["routed_to_review"] and failed["violations"] == ["llm_error"]
    assert "network down" in failed["tool_calls"][0]["result"]
