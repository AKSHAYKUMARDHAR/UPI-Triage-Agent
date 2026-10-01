"""Checks applied to every agent decision before it is written.

- category must be in the taxonomy                 -> 'invalid_category'
- at most MAX_TOOL_CALLS tool calls per transaction -> 'max_tool_calls' (caps cost and loops)
- confidence must be a float in [0, 1]             -> 'bad_confidence'
- reason must be non-empty                         -> 'empty_reason'

Any violation sends the row to the review queue. Violations, plus tool calls that returned
errors, are the 'tool-call errors' metric in the eval.
"""
MAX_TOOL_CALLS = 4


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
