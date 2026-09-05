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

## Contracts: declare what a good run must contain
Schema drift catches shape changes. **Job drift** is subtler: the output is valid, but it quietly
stops doing the original task. Declare a contract and SaneCheck fails the run when the evidence is missing:
```json
{ "source": "weekly-research", "output": { "...": "..." },
  "contract": {
    "required": ["summary", "sources[0].url", "confidence"],
    "min_items": { "sources": 2 },
    "patterns": { "confidence": "^(high|medium|low)$" },
    "must_contain": ["Acme Corp", "Q3 2026"],
    "must_not_contain": ["could not find", "as an AI"]
  } }
```
Send it with each run (recommended: it lives next to your workflow), or store it once with
`POST /contract?source=weekly-research` and the contract as the JSON body (`{}` clears it).
Violations come back as `contract_violation`, listing every missing field, failed pattern or absent input.

## Review queue: the human gate for meaning drift
Contracts are the hard gate. Some runs pass every rule yet quietly change meaning; those belong
in a **review queue**, not in "pass". Route them with a `review` block inside the contract, or from the
workflow itself with `"review": true` in the payload:
```json
"contract": { "required": ["summary"], "review": {
    "if_missing": ["sources[1].url"],
    "if_contains": ["preliminary", "estimate"],
    "sample_rate": 0.05 } }
```
Such runs get `status: "review"`, show up in the dashboard's **Needs review** list, and trigger a
`needs_review` alert. Decide with `POST /review/{id}?decision=approve|reject` (X-API-Key). Rejecting
with a rule hardens the source's contract, so the next occurrence fails automatically:
```json
{ "note": "wandered into last year's numbers", "add_rule": { "must_not_contain": ["FY2025"] } }
```

## Roadmap (after signal)
- LLM-based semantic check ("does this output actually complete the task?")
- Hosted multi-tenant + per-user keys + billing (free / Pro / Team)
- n8n community node + Make/Zapier templates for 1-click wiring
- Trends, per-source thresholds, pinned (explicit) schema baselines
