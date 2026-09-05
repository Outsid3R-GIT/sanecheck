# SaneCheck (MVP working name)

**Silent-failure monitor for automations.** Your n8n / Make / Zapier / GPT workflow
sends its result here; SaneCheck flags outputs that are *"200 OK but actually wrong"*
and alerts you — the blind spot normal uptime monitoring misses.

Not just for AI steps. Most silent failures are plain node-output sloppiness:
a date string landing in a number field, a `null` bleeding into an email template,
a key that quietly disappeared. SaneCheck flags those on purpose.

## Checks
| check | catches |
|---|---|
| `empty_output`, `too_short` | nothing / almost nothing came back |
| `refusal_detected` | the AI step refused instead of doing the task |
| `error_marker` | error text, `undefined`, `null`, `NaN` inside the output |
| `placeholder_leak` | unfilled `{{template}}`, `[name]`, lorem ipsum |
| `malformed_json` | expected JSON, got something else (`CHECK_EXPECT_JSON=true`) |
| `cost_spike`, `possible_loop` | tokens / steps above your limits (from `meta`) |
| **`schema_drift`** | the output **shape** changed vs. this source's baseline: missing or new keys, type changes — even when every field looks valid |

Schema drift: the **first run per `source` learns the shape** (keys + types, 3 levels deep).
Changed the workflow on purpose? `POST /schema/reset?source=NAME` (with `X-API-Key`) and it re-learns.

## Run locally
```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```
- Dashboard: http://localhost:8000/ — each run has a collapsible **raw JSON payload** for debugging
- Per-run detail: `GET /run/{id}` · Health: `GET /health`

## Send a run (what your automation does at the end)
`POST /ingest` with header `X-API-Key: <your key>` and JSON body:
```json
{ "source": "my-lead-enricher", "output": { "name": "Ada", "age": 36 }, "meta": { "tokens": 1200 } }
```
`output` can be a string OR a JSON object/array. `meta` is optional.

## Wire into n8n (2 minutes)
Add an **HTTP Request** node at the end of your workflow: `POST https://<your-host>/ingest`,
header `X-API-Key`, JSON body with `source` = workflow name and `output` = the previous node's result
(e.g. `{{ $json }}`). Silent failures show red on the dashboard and trigger your email / Slack alert.

## Deploy
Dockerfile included — works on Render, Railway, Fly. Set `SANECHECK_API_KEY` and either
`ALERT_WEBHOOK` (Slack/Discord) or `ALERT_EMAIL_TO` + `SMTP_*`.

## Roadmap (after signal)
- LLM-based semantic check ("does this output actually complete the task?")
- Hosted multi-tenant + per-user keys + billing (free / Pro / Team)
- n8n community node + Make/Zapier templates for 1-click wiring
- Trends, per-source thresholds, pinned (explicit) schema baselines
