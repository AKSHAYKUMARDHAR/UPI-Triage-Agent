"""Gemini adapter: lets the agent loop run on Google's free tier instead of Claude.

The loop (agent/loop.py) only uses one call, `llm.beta.messages.create(...)`, and reads
`.content` blocks (text / tool_use), `.stop_reason`, `.usage` and `.model` from the result.
GeminiClient exposes exactly that surface on top of the google-genai SDK, so the loop,
guardrails, audit trail and eval are identical for both providers.

Translation:
  system + tools (JSON schema)   -> system_instruction + FunctionDeclaration(parameters_json_schema)
  assistant turns                -> the model's original Content, replayed unchanged (keeps the
                                    thought signatures Gemini requires for multi-turn tool use)
  tool_result blocks             -> FunctionResponse parts, matched to the call by id/name

Free tier: requests are throttled to GEMINI_RPM per minute, and 429 / 5xx responses are
retried with backoff. List the models your key can use with:
    python -m agent.gemini_client
"""
import asyncio
import json
import os
import time
import uuid
from types import SimpleNamespace as NS

GEMINI_RPM = float(os.getenv("GEMINI_RPM", "10"))
MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "6"))
RETRY_BASE_DELAY = float(os.getenv("GEMINI_RETRY_DELAY", "5"))
REFUSAL_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY"}


class LLMCallError(Exception):
    """The provider call failed after retries; the loop routes the row to review."""


def _daily_quota(e) -> bool:
    """A per-day quota 429 will not clear by retrying; fail fast so the run can resume tomorrow."""
    details = (getattr(e, "details", None) or {}).get("error", {}).get("details", [])
    return any("PerDay" in v.get("quotaId", "") for d in details for v in d.get("violations", []))


def api_key() -> str | None:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


class _RateLimiter:
    def __init__(self, rpm: float):
        self.interval, self.next_at, self.lock = 60.0 / max(rpm, 0.1), 0.0, asyncio.Lock()

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            await asyncio.sleep(delay)


class _AssistantContent(list):
    """Blocks the loop reads, plus the raw Gemini Content to replay on the next turn."""

    raw = None


class GeminiClient:
    def __init__(self, model: str, client=None):
        from google import genai

        self.model = model
        self._genai = client or genai.Client(api_key=api_key())
        self._names: dict[str, str] = {}   # tool_use id -> function name, for FunctionResponse
        self._limiter = _RateLimiter(GEMINI_RPM)
        self.beta = NS(messages=NS(create=self._create))

    # ---- request translation -------------------------------------------------------------
    def _config(self, system, tools):
        from google.genai import types

        text = system if isinstance(system, str) else "\n\n".join(b["text"] for b in system or [])
        decls = [types.FunctionDeclaration(name=t["name"], description=t.get("description", ""),
                                           parameters_json_schema=t["input_schema"]) for t in tools or []]
        return types.GenerateContentConfig(
            system_instruction=text,
            tools=[types.Tool(function_declarations=decls)] if decls else None,
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="AUTO")),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def _contents(self, messages):
        from google.genai import types

        contents = []
        for m in messages:
            content = m["content"]
            if m["role"] == "assistant":
                if getattr(content, "raw", None) is not None:
                    contents.append(content.raw)
                continue
            if isinstance(content, str):
                contents.append(types.Content(role="user", parts=[types.Part(text=content)]))
                continue
            parts = []
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                raw = block.get("content", "")
                try:
                    value = json.loads(raw) if isinstance(raw, str) else raw
                except json.JSONDecodeError:
                    value = raw
                response = {"error": value} if block.get("is_error") else {"result": value}
                call_id = block["tool_use_id"]
                parts.append(types.Part(function_response=types.FunctionResponse(
                    id=None if call_id.startswith("local_") else call_id,
                    name=self._names.get(call_id, "unknown"), response=response)))
            contents.append(types.Content(role="user", parts=parts))
        return contents

    # ---- response translation ------------------------------------------------------------
    def _translate(self, resp):
        blocks, stop = _AssistantContent(), "end_turn"
        cand = (resp.candidates or [None])[0]
        if cand is None or cand.content is None:
            reason = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
            stop = "refusal" if reason else "end_turn"
        else:
            blocks.raw = cand.content
            for part in cand.content.parts or []:
                if part.function_call:
                    call_id = part.function_call.id or f"local_{uuid.uuid4().hex[:12]}"
                    self._names[call_id] = part.function_call.name
                    blocks.append(NS(type="tool_use", id=call_id, name=part.function_call.name,
                                     input=dict(part.function_call.args or {})))
                elif part.text and not part.thought:
                    blocks.append(NS(type="text", text=part.text))
            finish = str(getattr(cand.finish_reason, "name", cand.finish_reason) or "")
            if any(b.type == "tool_use" for b in blocks):
                stop = "tool_use"
            elif finish in REFUSAL_REASONS:
                stop = "refusal"
            elif finish == "MAX_TOKENS":
                stop = "max_tokens"
        um = resp.usage_metadata
        usage = NS(
            input_tokens=(getattr(um, "prompt_token_count", 0) or 0) - (getattr(um, "cached_content_token_count", 0) or 0),
            output_tokens=(getattr(um, "candidates_token_count", 0) or 0) + (getattr(um, "thoughts_token_count", 0) or 0),
            cache_read_input_tokens=getattr(um, "cached_content_token_count", 0) or 0,
            cache_creation_input_tokens=0,
        )
        return NS(content=blocks, stop_reason=stop, usage=usage, model=self.model)

    async def _create(self, *, system=None, tools=None, messages, max_tokens=None, **_ignored):
        """Same call shape as anthropic's messages.create; Claude-only options are ignored."""
        from google.genai import errors

        config, contents = self._config(system, tools), self._contents(messages)
        if max_tokens:
            config.max_output_tokens = max_tokens
        delay = RETRY_BASE_DELAY
        for attempt in range(MAX_RETRIES + 1):
            await self._limiter.wait()
            try:
                resp = await self._genai.aio.models.generate_content(model=self.model, contents=contents, config=config)
                return self._translate(resp)
            except errors.APIError as e:
                retryable = getattr(e, "code", None) in (429, 500, 502, 503, 504) and not _daily_quota(e)
                if not retryable or attempt == MAX_RETRIES:
                    raise LLMCallError(f"Gemini {getattr(e, 'code', '?')}: {str(e)[:300]}") from e
            except Exception as e:  # dropped connection, timeout, DNS: transient on a home network
                if attempt == MAX_RETRIES:
                    raise LLMCallError(f"Gemini transport error: {type(e).__name__}: {str(e)[:200]}") from e
            await asyncio.sleep(delay)
            delay = min(delay * 2, 90.0)


def list_models():
    from google import genai

    client = genai.Client(api_key=api_key())
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or []
        if not actions or "generateContent" in actions:
            print(m.name.removeprefix("models/"))


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    if not api_key():
        raise SystemExit("Set GEMINI_API_KEY in .env first (free key: https://aistudio.google.com/apikey)")
    list_models()
