"""SaneCheck — silent-failure checks for AI-automation outputs.

Each check inspects a run's output/meta and returns a failure dict or None.
The point: catch outputs that are HTTP-200 "successful" but actually WRONG
(empty, refusal, error text, unfilled template, malformed, runaway cost/loop).
"""
import json
import re

REFUSAL_PATTERNS = [
    r"\bI can(?:not|'?t)\b[^.]{0,40}\b(help|assist|do that|comply|provide|generate|create)\b",
    r"\bI'?m (?:sorry|unable|not able)\b",
    r"\bas an? (?:AI|language model|assistant)\b",
    r"\bI (?:do not|don'?t) have (?:access|the ability|enough)\b",
    r"\bI'?m just an AI\b",
]
ERROR_MARKERS = [
    "traceback (most recent call last)", "stack trace", "econnrefused",
    "error:", "exception:", "referenceerror", "typeerror:", "timeout",
    "undefined", "null", "nan",
]
PLACEHOLDER_MARKERS = [
    "{{", "}}", "[insert", "lorem ipsum", "todo:", "your text here",
    "<placeholder", "xxxxx", "[name]", "[company]",
]


def as_text(output):
    if output is None:
        return ""
    if isinstance(output, (dict, list)):
        try:
            return json.dumps(output, ensure_ascii=False)
        except Exception:
            return str(output)
    return str(output)


def check_empty(output, cfg, meta):
    if not as_text(output).strip():
        return {"check": "empty_output", "detail": "Output is empty / whitespace only."}


def check_too_short(output, cfg, meta):
    t = as_text(output).strip()
    minlen = cfg.get("min_length", 5)
    if 0 < len(t) < minlen:
        return {"check": "too_short", "detail": f"Output length {len(t)} < min {minlen}."}


def check_refusal(output, cfg, meta):
    t = as_text(output)
    for p in REFUSAL_PATTERNS:
        if re.search(p, t, re.IGNORECASE):
            return {"check": "refusal_detected",
                    "detail": "Output reads like an AI refusal, not a real result."}


def check_error_markers(output, cfg, meta):
    t = as_text(output).lower()
    for m in ERROR_MARKERS:
        if m in t:
            return {"check": "error_marker", "detail": f"Output contains error marker: '{m}'."}


def check_placeholder_leak(output, cfg, meta):
    t = as_text(output).lower()
    for m in PLACEHOLDER_MARKERS:
        if m in t:
            return {"check": "placeholder_leak", "detail": f"Unfilled placeholder in output: '{m}'."}


def check_malformed_json(output, cfg, meta):
    if not cfg.get("expect_json"):
        return None
    t = as_text(output).strip()
    try:
        json.loads(t)
    except Exception:
        return {"check": "malformed_json", "detail": "Expected JSON, but output is not valid JSON."}


def check_cost_spike(output, cfg, meta):
    tokens = meta.get("tokens") or meta.get("token_count")
    limit = cfg.get("max_tokens")
    if tokens and limit and tokens > limit:
        return {"check": "cost_spike", "detail": f"Tokens {tokens} > limit {limit}."}


def check_loop(output, cfg, meta):
    steps = meta.get("steps") or meta.get("iterations")
    limit = cfg.get("max_steps")
    if steps and limit and steps > limit:
        return {"check": "possible_loop", "detail": f"Steps {steps} > limit {limit} (possible runaway loop)."}


ALL_CHECKS = [
    check_empty, check_too_short, check_refusal, check_error_markers,
    check_placeholder_leak, check_malformed_json, check_cost_spike, check_loop,
]


def run_checks(output, meta, cfg):
    """Return a list of failure dicts (empty list = output looks sane)."""
    failures = []
    for fn in ALL_CHECKS:
        try:
            r = fn(output, cfg, meta or {})
            if r:
                failures.append(r)
        except Exception as e:  # a check must never crash ingestion
            failures.append({"check": fn.__name__, "detail": f"check error: {e}"})
    return failures
