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
    "[insert", "lorem ipsum", "todo:", "your text here",
    "<placeholder", "xxxxx", "[name]", "[company]",
]
# An unfilled template expression: {{ $json.name }}, {{name}}, ${VAR}. Bare "}}" is NOT a marker:
# any nested JSON object ends with "}}" (fixed after a false positive on nested outputs).
TEMPLATE_RE = re.compile(r"\{\{\s*[^{}\s][^{}]{0,200}?\s*\}\}|\$\{[A-Za-z_][\w.\-]*\}")


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


def _drift_parts(baseline, current, prefix, removed, added, changed):
    """Walk both signatures; collect dotted paths so nested changes read as meta.id (number -> string)."""
    # An array that is merely empty on one side is not a shape change (volume_drop covers "went empty").
    if (baseline == "array" and isinstance(current, list)) or (current == "array" and isinstance(baseline, list)):
        return
    if isinstance(baseline, dict) and isinstance(current, dict):
        for k in sorted(set(baseline) - set(current)):
            removed.append(prefix + k)
        for k in sorted(set(current) - set(baseline)):
            added.append(prefix + k)
        for k in sorted(set(baseline) & set(current)):
            if baseline[k] != current[k]:
                _drift_parts(baseline[k], current[k], prefix + k + ".", removed, added, changed)
    elif isinstance(baseline, list) and isinstance(current, list) and baseline and current:
        _drift_parts(baseline[0], current[0], prefix[:-1] + "[]." if prefix else "[].", removed, added, changed)
    else:
        changed.append(f"{prefix[:-1] or 'output'} ({_short(baseline)} -> {_short(current)})")


def describe_drift(baseline, current):
    removed, added, changed = [], [], []
    _drift_parts(baseline, current, "", removed, added, changed)
    parts = []
    if removed:
        parts.append("missing keys: " + ", ".join(removed))
    if added:
        parts.append("new keys: " + ", ".join(added))
    if changed:
        parts.append("type changed: " + ", ".join(changed))
    return "; ".join(parts)


def check_schema_drift(current_sig, baseline_sig):
    """Compare a run's shape with the learned baseline; empty-vs-filled arrays are not drift."""
    if baseline_sig is None or current_sig == baseline_sig:
        return None
    detail = describe_drift(baseline_sig, current_sig)
    if not detail:
        return None
    return {"check": "schema_drift", "detail": "Output shape changed vs. baseline: " + detail}


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
    m = TEMPLATE_RE.search(as_text(output))
    if m:
        return {"check": "placeholder_leak", "detail": f"Unfilled template expression in output: '{m.group(0)[:60]}'."}
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


def _call_signature(call):
    """tool name + canonical args, so identical calls collapse to one string."""
    if isinstance(call, dict):
        name = call.get("tool") or call.get("name") or call.get("function") or "?"
        args = call.get("args", call.get("input", call.get("arguments", call.get("parameters"))))
        return f"{name}({json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)})"
    return str(call)


def _most_repeated(items):
    counts = {}
    for it in items:
        counts[it] = counts.get(it, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1]) if counts else (None, 0)


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STR_CALL_RE = re.compile(r"^\s*([\w.\-]+)\s*\((.*)\)\s*$", re.S)


def _flatten(v, prefix=""):
    if isinstance(v, dict):
        out = []
        for k in sorted(v, key=str):
            out += _flatten(v[k], f"{prefix}{k}.")
        return out
    if isinstance(v, list):
        out = []
        for i, x in enumerate(v):
            out += _flatten(x, f"{prefix}{i}.")
        return out
    val = str(v).strip().lower()
    return [f"{prefix[:-1]}={val}"] if prefix else [val]


def _canon(call):
    """Two-pass recipe from r/n8n (ParrotIntegrated): flatten args, sort keys, lowercase, strip
    whitespace -> (tool name, token set). {"query":"Fix bug","limit":5} == {"limit":5,"query":"fix bug "}."""
    if isinstance(call, dict):
        name = str(call.get("tool") or call.get("name") or call.get("function") or "?")
        args = call.get("args", call.get("input", call.get("arguments", call.get("parameters"))))
    else:
        m = _STR_CALL_RE.match(str(call))
        name, args = (m.group(1), m.group(2)) if m else ("?", str(call))
    text = " ".join(_flatten(args)) if args is not None else ""
    return name.strip().lower(), set(_TOKEN_RE.findall(text))


def _jaccard(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _call_status(call):
    """'retryable' when the call itself failed in transport (408/425/429, 5xx, timeout, ok:false,
    error), else 'success'. Repeating a retryable call is correct behaviour, not thrash."""
    if not isinstance(call, dict):
        return "success"
    st = call.get("status", call.get("http_status", call.get("status_code")))
    try:
        st = int(st) if st is not None else None
    except (TypeError, ValueError):
        st = None
    if st is not None and (st in (408, 425, 429) or st >= 500):
        return "retryable"
    if call.get("ok") is False or call.get("error") or call.get("timeout") is True:
        return "retryable"
    return "success"


def _empty_result(call):
    """A successful call that returned nothing useful: the classic trigger for stubborn re-querying."""
    if not isinstance(call, dict) or "result" not in call:
        return False
    r = call.get("result")
    if r is None or r == [] or r == {} or r == "":
        return True
    if isinstance(r, str):
        low = r.strip().lower()
        return not low or "not found" in low or "no results" in low
    return False


def _mark_retries(calls, threshold, window=3):
    """A call that repeats (>= threshold) the closest similar previous call of the same tool is a valid
    retry when that previous call failed in transport (429, 5xx, timeout). Those are exempt."""
    canon = [_canon(c) for c in calls]
    retry = [False] * len(calls)
    for i, (name, toks) in enumerate(canon):
        for j in range(i - 1, max(-1, i - window - 1), -1):
            n, t = canon[j]
            if n == name and _jaccard(toks, t) >= threshold:
                retry[i] = _call_status(calls[j]) == "retryable"
                break
    return retry


def _thrash(calls, threshold, window=3, max_repeat=3):
    """Same tool, near-identical arguments against a rolling window of the last `window` calls.
    Feed it non-retry calls only. Returns (tool, min similarity, count, after_empty_result)."""
    canon = [_canon(c) for c in calls]
    for i, (name, toks) in enumerate(canon):
        prev = list(zip(canon[max(0, i - window):i], calls[max(0, i - window):i]))
        hits = []
        for (n, t), c in prev:
            if n == name:
                x = _jaccard(toks, t)
                if x >= threshold:
                    hits.append((x, c))
        if len(hits) >= max_repeat - 1:
            return name, min(x for x, _ in hits), len(hits) + 1, any(_empty_result(c) for _, c in hits)
    return None


def check_loop(output, cfg, meta):
    steps = meta.get("steps") or meta.get("iterations")
    limit = cfg.get("max_steps")
    if steps and limit and steps > limit:
        return {"check": "possible_loop", "detail": f"Steps {steps} > limit {limit} (possible runaway loop)."}
    max_repeat = int(cfg.get("max_repeat") or 3)
    # Repeated tool signatures (r/n8n feedback): the same tool called with identical args again and again.
    calls = meta.get("tool_calls") or meta.get("calls")
    if isinstance(calls, list) and calls:
        th = float(cfg.get("thrash_similarity") or 0.85)
        # Valid retries are exempt (r/n8n feedback): repeating a call whose previous attempt failed in
        # transport (429, 5xx, timeout) is what an agent should do. Only repeats after a success count.
        retry = _mark_retries(calls, th)
        kept = [c for c, r in zip(calls, retry) if not r]
        sig, n = _most_repeated(_call_signature(c) for c in kept)
        if n >= max_repeat:
            why = " after an empty result" if any(_empty_result(c) for c in kept if _call_signature(c) == sig) else ""
            return {"check": "possible_loop",
                    "detail": f"Tool call repeated {n}x with identical arguments{why}: {sig[:120]} (possible loop)."}
        # Semantic thrash: the agent permutes words on the same failing query. Canonicalize + Jaccard
        # against the last 3 calls of the same tool; above the threshold it is a loop.
        hit = _thrash(kept, th, max_repeat=max_repeat)
        if hit:
            name, sim, n, after_empty = hit
            why = " after an empty result" if after_empty else ""
            return {"check": "possible_loop",
                    "detail": f"Semantic thrash: {name} called {n}x with near-identical arguments{why} "
                              f"(similarity {sim:.2f} >= {th}) (possible loop)."}
    # Repeated content: the same non-trivial sentence/line or list item over and over is what a text loop looks like.
    data = as_data(output)
    segments = [json.dumps(x, sort_keys=True, ensure_ascii=False) for x in data] if isinstance(data, list) else []
    segments += [seg.strip() for seg in re.split(r"(?<=[.!?])\s+|\n+", as_text(output))]
    seg, n = _most_repeated(x for x in segments if len(x) >= 20)
    if n >= max_repeat:
        return {"check": "possible_loop", "detail": f"Same content repeated {n}x in output: '{seg[:80]}' (possible loop)."}


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


# ---- Volume baseline (n8n forum feedback): "the parser returns 0 items and the run is still a success". ----
def item_count(output, meta):
    """How many items a run produced: meta.count / items / items_out, else the length of the output list,
    else the length of the first list field (items, results, data, rows, records, entries)."""
    for k in ("count", "items", "items_out", "item_count"):
        v = (meta or {}).get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    data = as_data(output)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for k in ("items", "results", "data", "rows", "records", "entries"):
            if isinstance(data.get(k), list):
                return len(data[k])
    return None


def check_volume_drop(count, history, ratio=0.2, min_history=3):
    """Learned per-source baseline: flag a run whose item count collapses against the median of recent runs."""
    if count is None or len(history) < min_history:
        return None
    hist = sorted(history)
    median = hist[len(hist) // 2]
    if median <= 0:
        return None
    if count == 0 or count < median * ratio:
        return {"check": "volume_drop",
                "detail": f"Only {count} item(s) this run; typical is {median} (median of the last {len(history)} runs)."}
    return None


def day_bucket(meta, when=None, mode="weekday_weekend"):
    """Which volume baseline a run belongs to. The workflow may say so itself (meta.daytype: weekday,
    weekend or holiday; holiday counts as weekend); otherwise the UTC calendar decides."""
    if mode == "flat":
        return "all"
    dt = str((meta or {}).get("daytype", "")).strip().lower()
    if dt in ("weekend", "holiday"):
        return "weekend"
    if dt in ("weekday", "workday", "business"):
        return "weekday"
    import datetime as _dt
    when = when or _dt.datetime.now(_dt.timezone.utc)
    return "weekend" if when.weekday() >= 5 else "weekday"
