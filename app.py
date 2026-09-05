"""SaneCheck MVP — silent-failure monitor for automations (n8n / Make / Zapier / GPT).

POST your workflow's result to /ingest. SaneCheck runs sanity checks (empty,
refusal, error text, placeholder, malformed JSON, cost/loop) plus a schema-drift
check (did the output SHAPE change vs. this source's learned baseline?), stores
the raw payload for debugging, and alerts you (email / webhook). Dashboard at /.

Run:  pip install -r requirements.txt
      uvicorn app:app --host 0.0.0.0 --port 8000
"""
import os
import json
import sqlite3
import smtplib
import urllib.request
import datetime
import html
from email.mime.text import MIMEText

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

import checks

DB = os.environ.get("SANECHECK_DB", "sanecheck.db")
API_KEY = os.environ.get("SANECHECK_API_KEY", "")  # if set, required on /ingest and /schema/reset
RAW_CAP = int(os.environ.get("SANECHECK_RAW_CAP", "50000"))  # max stored payload chars per run

ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
ALERT_WEBHOOK = os.environ.get("ALERT_WEBHOOK", "")  # generic JSON POST {"text": ...}

CFG = {
    "min_length": int(os.environ.get("CHECK_MIN_LENGTH", "5")),
    "expect_json": os.environ.get("CHECK_EXPECT_JSON", "").lower() in ("1", "true", "yes"),
    "max_tokens": int(os.environ["CHECK_MAX_TOKENS"]) if os.environ.get("CHECK_MAX_TOKENS") else None,
    "max_steps": int(os.environ["CHECK_MAX_STEPS"]) if os.environ.get("CHECK_MAX_STEPS") else None,
}

app = FastAPI(title="SaneCheck MVP")


def _now():
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS runs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, status TEXT,
                failed_checks TEXT, output_excerpt TEXT, meta TEXT,
                output_raw TEXT, schema_sig TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS schemas(
                source TEXT PRIMARY KEY, signature TEXT, learned_ts TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS contracts(
                source TEXT PRIMARY KEY, contract TEXT, set_ts TEXT)"""
        )
        cols = {r["name"] for r in c.execute("PRAGMA table_info(runs)")}
        for col in ("output_raw", "schema_sig"):
            if col not in cols:  # migrate databases created before these columns existed
                c.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")


init_db()


def require_key(x_api_key):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def send_alert(source, failures, excerpt, run_id):
    lines = [f"SaneCheck: silent failure in automation '{source}' (run #{run_id})", ""]
    for f in failures:
        lines.append(f"- {f['check']}: {f['detail']}")
    lines += ["", "Output excerpt:", (excerpt or "")[:800]]
    msg = "\n".join(lines)
    if ALERT_EMAIL_TO and SMTP_HOST:
        try:
            m = MIMEText(msg)
            m["Subject"] = f"[SaneCheck] silent failure in {source}"
            m["From"] = SMTP_USER or "sanecheck@localhost"
            m["To"] = ALERT_EMAIL_TO
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                s.starttls()
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(m["From"], [ALERT_EMAIL_TO], m.as_string())
        except Exception as e:
            print("email alert failed:", e)
    if ALERT_WEBHOOK:
        try:
            req = urllib.request.Request(
                ALERT_WEBHOOK, data=json.dumps({"text": msg}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print("webhook alert failed:", e)


@app.post("/ingest")
async def ingest(request: Request, x_api_key: str = Header(default="")):
    require_key(x_api_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="request body must be JSON")
    source = str(body.get("source", "unknown"))
    output = body.get("output")
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}

    failures = checks.run_checks(output, meta, CFG)

    # Schema drift: the first run per source learns the shape; later runs are compared to it.
    sig = checks.schema_signature(checks.as_data(output))
    sig_json = json.dumps(sig, sort_keys=True)
    with db() as c:
        row = c.execute("SELECT signature FROM schemas WHERE source=?", (source,)).fetchone()
        if row is None:
            c.execute("INSERT INTO schemas(source,signature,learned_ts) VALUES(?,?,?)",
                      (source, sig_json, _now()))
        else:
            drift = checks.check_schema_drift(sig, json.loads(row["signature"]))
            if drift:
                failures.append(drift)

    # Contract: declared per run (payload "contract") or stored per source via POST /contract.
    contract = body.get("contract") if isinstance(body.get("contract"), dict) else None
    if contract is None:
        with db() as c:
            crow = c.execute("SELECT contract FROM contracts WHERE source=?", (source,)).fetchone()
        if crow:
            try:
                contract = json.loads(crow["contract"])
            except Exception:
                contract = None
    if contract:
        violation = checks.check_contract(output, contract)
        if violation:
            failures.append(violation)

    status = "fail" if failures else "pass"
    excerpt = checks.as_text(output)[:1000]
    raw = json.dumps(output, ensure_ascii=False)[:RAW_CAP]
    with db() as c:
        cur = c.execute(
            "INSERT INTO runs(ts,source,status,failed_checks,output_excerpt,meta,output_raw,schema_sig)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (_now(), source, status, json.dumps(failures), excerpt, json.dumps(meta), raw, sig_json),
        )
        run_id = cur.lastrowid
    if failures:
        send_alert(source, failures, excerpt, run_id)
    return JSONResponse({"run_id": run_id, "status": status, "failed_checks": failures, "schema": sig})


@app.post("/schema/reset")
def schema_reset(source: str, x_api_key: str = Header(default="")):
    """Forget the learned baseline for a source; it re-learns on the next run."""
    require_key(x_api_key)
    with db() as c:
        c.execute("DELETE FROM schemas WHERE source=?", (source,))
    return {"ok": True, "source": source}


@app.post("/contract")
async def set_contract(request: Request, source: str, x_api_key: str = Header(default="")):
    """Store a contract for a source (JSON object body). Send {} to clear it."""
    require_key(x_api_key)
    try:
        contract = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    if not isinstance(contract, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    with db() as c:
        if contract:
            c.execute("INSERT OR REPLACE INTO contracts(source,contract,set_ts) VALUES(?,?,?)",
                      (source, json.dumps(contract), _now()))
        else:
            c.execute("DELETE FROM contracts WHERE source=?", (source,))
    return {"ok": True, "source": source, "contract": contract}


@app.get("/run/{run_id}")
def get_run(run_id: int):
    with db() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="no such run")
    out = {}
    for k in r.keys():
        v = r[k]
        if k in ("failed_checks", "meta", "schema_sig") and v:
            try:
                v = json.loads(v)
            except Exception:
                pass
        out[k] = v
    return out


@app.get("/health")
def health():
    return {"ok": True}


def _pretty(raw):
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except Exception:
        return raw or ""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    with db() as c:
        rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 50").fetchall()
    fails = sum(1 for r in rows if r["status"] == "fail")
    out = [
        "<meta charset=utf-8><title>SaneCheck</title>",
        "<div style='font-family:sans-serif;max-width:1100px;margin:24px auto'>",
        "<h1>SaneCheck</h1>",
        f"<p>Last {len(rows)} runs. <b style='color:#c0392b'>{fails} silent failures</b> caught.</p>",
        ("<p style='background:#f6f6f6;padding:10px;border-radius:6px'>No runs yet. "
         "Send one from your workflow, or try: <code>curl -X POST https://&lt;your-host&gt;/ingest "
         "-H 'X-API-Key: KEY' -H 'Content-Type: application/json' "
         "-d '{&quot;source&quot;:&quot;demo&quot;,&quot;output&quot;:{&quot;name&quot;:&quot;Ada&quot;,&quot;age&quot;:36}}'</code></p>"
         if not rows else ""),
        "<table border=1 cellpadding=6 style='border-collapse:collapse;width:100%'>",
        "<tr><th>#</th><th>time (UTC)</th><th>source</th><th>status</th>"
        "<th>failed checks</th><th>payload</th></tr>",
    ]
    for r in rows:
        fc = json.loads(r["failed_checks"] or "[]")
        fc_txt = "<br>".join(html.escape(f"{x['check']}: {x['detail']}") for x in fc) or "&mdash;"
        color = "#c0392b" if r["status"] == "fail" else "#27ae60"
        raw = r["output_raw"] if ("output_raw" in r.keys() and r["output_raw"]) else (r["output_excerpt"] or "")
        out.append(
            f"<tr><td>{r['id']}</td><td>{r['ts']}</td><td>{html.escape(r['source'])}</td>"
            f"<td style='color:{color};font-weight:bold'>{r['status']}</td><td>{fc_txt}</td>"
            f"<td><details><summary>show raw JSON</summary>"
            f"<pre style='white-space:pre-wrap;max-width:440px;font-size:12px'>{html.escape(_pretty(raw))}</pre>"
            f"</details></td></tr>"
        )
    out.append(
        "</table><p style='color:#888'>Schema drift: the first run per source sets the baseline shape "
        "(keys + types). To re-learn after an intentional change: POST /schema/reset?source=NAME "
        "with your X-API-Key. Per-run detail: GET /run/{id}. Contracts (job drift): send a 'contract' object with required fields / must_contain, or store one via POST /contract?source=NAME.</p></div>"
    )
    return "\n".join(out)
