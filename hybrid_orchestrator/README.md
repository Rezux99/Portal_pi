# Hybrid Async Orchestrator

A real, provider-neutral FastAPI handler for coordinating OpenAI-compatible LLM APIs. It is designed to sit next to Portal Pi v2 and call your existing providers through base URLs, while keeping keys in environment variables.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env. Never commit it.
uvicorn app:app --host 127.0.0.1 --port 8787
```

## API

```bash
curl http://127.0.0.1:8787/health
curl -H "X-Orchestrator-Secret: $ORCHESTRATOR_API_SECRET" http://127.0.0.1:8787/v1/providers
curl -X POST http://127.0.0.1:8787/v1/run \
  -H "Content-Type: application/json" \
  -H "X-Orchestrator-Secret: $ORCHESTRATOR_API_SECRET" \
  -d '{
    "objective":"Analiza este lote y devuelve decisiones accionables",
    "mode":"balanced",
    "evidence":[
      {"id":"art-1","source":"local","text":"Texto de la noticia..."}
    ]
  }'
```

## What it does

- Async HTTP calls with bounded per-provider concurrency.
- Provider routing by mode: `fast`, `balanced`, `quality`.
- OpenAI-compatible `base_url`, including Modal's configured URL.
- Secret-only configuration through environment variables.
- Prompt firewall: evidence is explicitly untrusted data and suspicious content is redacted.
- Independent factual/adversarial passes, critique, judge and bounded revision.
- Evidence-linked claims, contradiction tracking and `needs_review` fallback.
- Retries with backoff, timeouts and provider failure handling.
- `dry_run` to inspect routing without spending calls.

## Portal Pi integration

Import the orchestrator from your existing `core.py` or call the HTTP endpoint from `dashboard.py`. Keep `core.py` as the single business-logic owner. The dashboard should only submit a validated objective and evidence packet; it should never receive or store provider keys.

Recommended first integration point:

```python
# in core.py, or a small adapter module
import httpx

async def run_collective(objective, articles, secret):
    payload = {
        "objective": objective,
        "mode": "balanced",
        "evidence": [
            {"id": a.filename, "source": getattr(a, "url", ""), "text": a.content}
            for a in articles
        ],
    }
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(
            "http://127.0.0.1:8787/v1/run",
            headers={"X-Orchestrator-Secret": secret},
            json=payload,
        )
        r.raise_for_status()
        return r.json()
```

## Security limits

This is a defensive application layer, not a complete network security product. Put it behind localhost, a reverse proxy or private network; rotate the orchestrator secret; use least-privilege provider keys; add TLS when leaving localhost; and require human approval before any destructive action or deployment.
