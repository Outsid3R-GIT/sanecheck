"""SaneCheck MVP — silent-failure monitor for AI automations.

Your n8n/Make/Zapier/GPT workflow POSTs its result to /ingest.
SaneCheck runs silent-failure checks and alerts you (email / webhook)
when an output is "200 OK but actually wrong". Minimal dashboard at /.

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
API_KEY = os.environ.get("SANECHECK_API_KEY", "")  # if set, required on /ingest

ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
ALERT_WEBHOOK = os.environ.get("ALERT_WEBHOOK", "")  # generic JSON POST (Slack/Discord/Telegram-compatible)

CFG = {
    "min_length": int(os.environ.get("CHECK_MIN_LENGTH", "5")),
    "expect_json": os.environ.get("CHECK_EXPECT_JSON", "").lower() in ("1", "true", "yes"),
    "max_tokens": int(os.environ["CHECK_MAX_TOKENS"]) if os.environ.get("CHECK_MAX_TOKENS") else None,
    "max_steps": int(os.environ["CHECK_MAX_STEPS"]) if os.environ.get("CHECK_MAX_STEPS") else None,
}

app = FastAPI(title="SaneCheck MVP")


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS runs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, source TEXT, status TEXT,
                failed_checks TEXT, output_excerpt TEXT, meta TEXT)"""
        )


init_db()


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
            data = json.dumps({"text": msg}).encode("utf-8")
            req = urllib.request.Request(
                ALERT_WEBHOOK, data=data, headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print("webhook alert failed:", e)


@app.post("/ingest")
async def ingest(request: Request, x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="request body must be JSON")

    source = str(body.get("source", "unknown"))
    output = body.get("output")
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}

    failures = checks.run_checks(output, meta, CFG)
    status = "fail" if failures else "pass"
    excerpt = checks.as_text(output)[:1000]
    ts = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    with db() as c:
        cur = c.execute(
            "INSERT INTO runs(ts,source,status,failed_checks,output_excerpt,meta) VALUES(?,?,?,?,?,?)",
            (ts, source, status, json.dumps(failures), excerpt, json.dumps(meta)),
        )
        run_id = cur.lastrowid

    if failures:
        send_alert(source, failures, excerpt, run_id)

    return JSONResponse({"run_id": run_id, "status": status, "failed_checks": failures})


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    with db() as c:
        rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 50").fetchall()
    total = len(rows)
    fails = sum(1 for r in rows if r["status"] == "fail")
    out = [
        "<meta charset=utf-8><title>SaneCheck</title>",
        "<div style='font-family:sans-serif;max-width:1000px;margin:24px auto'>",
        "<h1>SaneCheck</h1>",
        f"<p>Last {total} runs — <b style='color:#c0392b'>{fails} silent failures</b> caught.</p>",
        "<table border=1 cellpadding=6 style='border-collapse:collapse;width:100%'>",
        "<tr><th>#</th><th>time (UTC)</th><th>source</th><th>status</th>"
        "<th>failed checks</th><th>output excerpt</th></tr>",
    ]
    for r in rows:
        fc = json.loads(r["failed_checks"] or "[]")
        fc_txt = "<br>".join(html.escape(f"{x['check']}: {x['detail']}") for x in fc) or "—"
        color = "#c0392b" if r["status"] == "fail" else "#27ae60"
        out.append(
            f"<tr><td>{r['id']}</td><td>{r['ts']}</td>"
            f"<td>{html.escape(r['source'])}</td>"
            f"<td style='color:{color};font-weight:bold'>{r['status']}</td>"
            f"<td>{fc_txt}</td>"
            f"<td><code>{html.escape((r['output_excerpt'] or '')[:200])}</code></td></tr>"
        )
    out += ["</table></div>"]
    return "\n".join(out)
