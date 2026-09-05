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


# ---- Contracts: declare what a GOOD run must contain (r/n8n feedback: "job drift") ----
# The output can be structurally valid yet quietly stop doing the original task. A contract
# lists the evidence a run must carry; missing evidence = failed run (contract_violation).

def resolve_path(data, path):
    """Resolve 'a.b[0].c' against nested dicts/lists. Returns (found, value)."""
    cur = data
    for token in re.findall(r"[^.\[\]]+|\[\d+\]", str(path)):
        if token.startswith("["):
            idx = int(token[1:-1])
            if not isinstance(cur, list) or idx >= len(cur):
                return False, None
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or token not in cur:
                return False, None
            cur = cur[token]
    return True, cur


def _is_empty(v):
    return v is None or v == [] or v == {} or (isinstance(v, str) and not v.strip())


def check_contract(output, contract):
    """Enforce a declared contract; returns one failure listing every violation, or None."""
    if not isinstance(contract, dict) or not contract:
        return None
    data = as_data(output)
    text = as_text(output).lower()
    problems = []
    for path in contract.get("required") or []:
        found, val = resolve_path(data, path)
        if not found or _is_empty(val):
            problems.append(f"missing required field: {path}")
    for path, pattern in (contract.get("patterns") or {}).items():
        found, val = resolve_path(data, path)
        if found and val is not None and not re.search(str(pattern), str(val)):
            problems.append(f"field {path} does not match pattern {pattern}")
    for path, n in (contract.get("min_items") or {}).items():
        found, val = resolve_path(data, path)
        count = len(val) if isinstance(val, (list, dict, str)) else 0
        if not found or count < int(n):
            problems.append(f"{path} has {count} items, expected at least {n}")
    for s in contract.get("must_contain") or []:
        if str(s).lower() not in text:
            problems.append(f"output does not mention required input: '{s}'")
    for s in contract.get("must_not_contain") or []:
        if str(s).lower() in text:
            problems.append(f"output contains forbidden marker: '{s}'")
    if problems:
        return {"check": "contract_violation", "detail": "; ".join(problems)}
    return None


# ---- Review routing (r/n8n feedback): contract = hard gate; runs that PASS it but may have
# changed meaning go to a human review queue instead of silently passing. ----
import random


def evaluate_review(output, contract, body):
    """Return the reasons (if any) to route a passing run to human review."""
    reasons = []
    if isinstance(body, dict) and body.get("review") is True:
        note = str(body.get("review_note") or "").strip()
        reasons.append("workflow requested review" + (f": {note}" if note else ""))
    rv = contract.get("review") if isinstance(contract, dict) else None
    if isinstance(rv, dict):
        data = as_data(output)
        text = as_text(output).lower()
        for path in rv.get("if_missing") or []:
            found, val = resolve_path(data, path)
            if not found or _is_empty(val):
                reasons.append(f"weak evidence, missing: {path}")
        for s in rv.get("if_contains") or []:
            if str(s).lower() in text:
                reasons.append(f"possible meaning change, contains: '{s}'")
        rate = rv.get("sample_rate")
        try:
            if rate and random.random() < float(rate):
                reasons.append(f"sampled for review (rate {rate})")
        except (TypeError, ValueError):
            pass
    return reasons
