# SaneCheck (MVP working name)

**Silent-failure monitor for AI automations.** Your n8n / Make / Zapier / GPT workflow
sends its result here; SaneCheck flags outputs that are *"200 OK but actually wrong"*
(empty, AI-refusal, error text, unfilled template, malformed JSON, runaway cost/loop)
and alerts you — the blind spot normal uptime monitoring misses.

## Run locally
```bash
pip install -r requirements.txt
# optional: copy .env.example -> set env vars (API key, email/webhook alerts)
uvicorn app:app --host 0.0.0.0 --port 8000
```
- Dashboard: http://localhost:8000/
- Health: http://localhost:8000/health

## Send a run (what your automation does at the end)
`POST /ingest` with header `X-API-Key: <your key>` and JSON body:
```json
{
  "source": "my-lead-enricher",
  "output": "As an AI language model, I cannot help with that.",
  "meta": { "tokens": 1200, "steps": 3 }
}
```
- `output` can be a string OR a JSON object/array (whatever your last node produced).
- `meta` is optional (used for cost/loop checks).

Test with curl:
```bash
curl -X POST http://localhost:8000/ingest \
  -H "X-API-Key: changeme-123" -H "Content-Type: application/json" \
  -d '{"source":"test","output":"","meta":{}}'
# -> {"run_id":1,"status":"fail","failed_checks":[{"check":"empty_output",...}]}
```

## Wire into n8n (2 minutes)
Add an **HTTP Request** node at the end of your workflow:
- Method: `POST`, URL: `https://<your-host>/ingest`
- Header: `X-API-Key` = your key
- Body (JSON): `source` = your workflow name, `output` = the previous node's result
  (e.g. `{{$json}}` or the specific field), `meta` optional.

When a run silently fails, you get an email/Slack alert + it shows red on the dashboard.

## What this validates
The whole point of the MVP: deploy it, wire one real automation, share it in an
n8n/Make community post, and watch whether builders install + use it. Real usage =
the signal that decides if we build out (more checks, LLM semantic check, hosted
multi-user tiers, community node). No usage = adjust the wedge.

## Roadmap (after signal)
- LLM-based semantic check ("does this output actually complete the task?")
- Hosted multi-tenant + per-user API keys + billing (free / $19-29 Pro / Team)
- n8n community node + Make/Zapier templates for 1-click wiring
- Trends, quality-drift detection, per-source thresholds
