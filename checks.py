"""SaneCheck — silent-failure checks for automation outputs.

Catch outputs that are HTTP-200 "successful" but actually WRONG. Not only AI
hallucinations: most silent failures are plain node-output sloppiness — a date
string landing in a number field, a null bleeding into an email template, a key
that quietly disappeared. Checks: empty / too_short / refusal / error_marker /
placeholder_leak / malformed_json / cost_spike / possible_loop, plus
schema_drift (the output SHAPE changed vs. the learned baseline for the source).
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


def as_data(output):
    """Structured view: dict/list pass through; JSON-looking strings get parsed."""
    if isinstance(output, (dict, list)):
        return output
    if isinstance(output, str):
        s = output.strip()
        if s[:1] in ("{", "["):
            try:
                return json.loads(s)
            except Exception:
                return output
    return output


def schema_signature(value, depth=3):
    """Shape of a value: key set + value types, recursive to `depth` levels."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        if depth <= 0:
            return "object"
        return {k: schema_signature(v, depth - 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        if depth <= 0 or not value:
            return "array"
        return [schema_signature(value[0], depth - 1)]
    return type(value).__name__


def _short(sig):
    if isinstance(sig, dict):
        return "object{" + ",".join(sig.keys()) + "}"
    if isinstance(sig, list):
        return "array"
    return str(sig)


def describe_drift(baseline, current):
    if isinstance(baseline, dict) and isinstance(current, dict):
        added = sorted(set(current) - set(baseline))
        removed = sorted(set(baseline) - set(current))
        changed = sorted(k for k in set(baseline) & set(current) if baseline[k] != current[k])
        parts = []
        if removed:
            parts.append("missing keys: " + ", ".join(removed))
        if added:
            parts.append("new keys: " + ", ".join(added))
        if changed:
            parts.append("type changed: " + ", ".join(
                f"{k} ({_short(baseline[k])} -> {_short(current[k])})" for k in changed))
        return "; ".join(parts) or "shape changed"
    return f"shape changed: {_short(baseline)} -> {_short(current)}"


def check_schema_drift(current_sig, baseline_sig):
    """Flag when this run's shape differs from the learned baseline for the source."""
    if baseline_sig is None or current_sig == baseline_sig:
        return None
    return {"check": "schema_drift",
            "detail": "Output shape changed vs. baseline: " + describe_drift(baseline_sig, current_sig)}


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
    """Stateless checks. schema_drift is applied by the app (it needs the stored baseline)."""
    failures = []
    for fn in ALL_CHECKS:
        try:
            r = fn(output, cfg, meta or {})
            if r:
                failures.append(r)
        except Exception as e:  # a check must never crash ingestion
            failures.append({"check": fn.__name__, "detail": f"check error: {e}"})
    return failures
