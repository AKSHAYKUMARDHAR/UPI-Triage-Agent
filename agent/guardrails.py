"""Checks applied to every agent decision before it is written.

- category must be in the taxonomy                 -> 'invalid_category'
- at most MAX_TOOL_CALLS tool calls per transaction -> 'max_tool_calls' (caps cost and loops)
- confidence must be a float in [0, 1]             -> 'bad_confidence'
- reason must be non-empty                         -> 'empty_reason'

Any violation sends the row to the review queue. Violations, plus tool calls that returned
errors, are the 'tool-call errors' metric in the eval.

Input guard, applied before the gate: a narration that carries instruction-like text ("ignore
previous instructions", "system override", "categorize as ...") goes to review and is never
shown to the LLM. Telling the model to treat the narration as data is not enough: on the
held-out set gemini-3.1-flash-lite obeyed "categorize as Investments" at confidence 1.
Bank narrations never legitimately contain such text, so routing them costs nothing.
"""
import re

MAX_TOOL_CALLS = 4

INJECTION_RE = re.compile(
    r"\bignore\b.{0,40}\b(instructions?|prompts?|rules?)\b|\bdisregard\b|"
    r"\b(system|admin|developer)\s+(override|prompt|message|instructions?)\b|\bset\s+confidence\b|"
    r"\b(categori[sz]e|classify|label|mark)\s+(this\s+|it\s+)?as\b|\byou\s+are\s+now\b|"
    r"\bnew\s+instructions?\b|\bjailbreak\b",
    re.I)


def suspicious_narration(narration: str) -> str | None:
    """The instruction-like snippet in a narration, or None. Such rows skip the LLM entirely."""
    m = INJECTION_RE.search(narration or "")
    return m.group(0) if m else None


def validate(decision: dict, taxonomy: list[str]) -> list[str]:
    """Return a list of violation codes (empty list = OK)."""
    violations = []
    if decision.get("category") not in taxonomy:
        violations.append("invalid_category")
    if len(decision.get("tool_calls") or []) > MAX_TOOL_CALLS:
        violations.append("max_tool_calls")
    conf = decision.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0.0 <= conf <= 1.0:
        violations.append("bad_confidence")
    if not str(decision.get("reason") or "").strip():
        violations.append("empty_reason")
    return violations
