# LLM Chatbot — Google Cloud Run + Vertex AI (Gemini)

A production-grade FastAPI chatbot deployed on **Google Cloud Run**, powered by **Gemini on Vertex AI**, with structured Cloud Logging, Secret Manager integration, least-privilege IAM, and a full troubleshooting runbook covering six real failure scenarios.

---

## Architecture

```
                         +------------------------------------------+
  Client (curl/UI) ----> |  Cloud Run service: llm-chatbot          |
   X-API-Key header      |  FastAPI app (container)                 |
                         |  - /health  /ready  /chat                |
                         |  - API-key auth, input validation        |
                         |  - retry + backoff + timeout + semaphore |
                         |  - JSON structured logs                  |
                         +--------+-----------------+---------------+
                                  |                 |
                 runtime service account            | stdout JSON
                 (chatbot-runtime@...)              v
                                  |         Cloud Logging
                  +---------------+----+    (Logs Explorer, log-based
                  |                    |     metrics, alerts)
                  v                    v
          Vertex AI (Gemini)      Secret Manager
          roles/aiplatform.user   roles/secretmanager.secretAccessor
                                  (CHATBOT_API_KEY)
```

**Key design decisions:**

1. **No API keys for Vertex AI.** Cloud Run uses Application Default Credentials from the runtime service account. IAM, not a key, authorizes Gemini calls.
2. **One dedicated service account** with only the roles it needs, never the default compute service account.
3. **Secrets live in Secret Manager**, not in the image, repo, or plain env vars in source.
4. **App timeout is shorter than Cloud Run's request timeout**, so the app fails gracefully with a clear error instead of the platform killing the request.
5. **Retries only for retryable errors** (429, 5xx, timeouts). Never retry 400, 401, 403, or 404.
6. **Upstream auth errors are not leaked to end users.** If Vertex returns 403, the client gets a generic 502.
7. **Logs are structured JSON** with a request ID and trace ID so one request can be followed end to end.

---

## Tech Stack

| Layer | Technology |
|---|---|
| API framework | Python 3.12, FastAPI, Uvicorn |
| LLM | Gemini 2.5 Flash via Vertex AI (`google-genai` SDK) |
| Runtime | Google Cloud Run (serverless containers) |
| Auth | Cloud IAM (service account), Secret Manager (API key) |
| Observability | Cloud Logging (structured JSON), log-based metrics |
| Testing | PyTest, FastAPI TestClient |

---

## Quick Start (Local)

```bash
git clone https://github.com/rijasRasith/LLM-Chatbot-GCP-Cloud-Run-Runbook
cd LLM-Chatbot-GCP-Cloud-Run-Runbook

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Authenticate with your GCP account
gcloud auth application-default login
```

Create `.env` (never commit):

```text
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GEMINI_MODEL=gemini-2.5-flash
CHATBOT_API_KEY=local-dev-key
FAULT_INJECTION_ENABLED=true
```

```bash
uvicorn app.main:app --reload --port 8080

# In another terminal
curl -s localhost:8080/health
curl -s -X POST localhost:8080/chat \
  -H "Content-Type: application/json" -H "X-API-Key: local-dev-key" \
  -d '{"message": "How do I reset my VPN password?"}'
```

---

## Deploy to Cloud Run

```bash
export PROJECT_ID="your-project-id"
export REGION="us-central1"
export SERVICE="llm-chatbot"
export SA_NAME="chatbot-runtime"
export SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
export SECRET_NAME="chatbot-api-key"

# Enable APIs
gcloud services enable \
  run.googleapis.com aiplatform.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com logging.googleapis.com

# Create runtime service account
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="Chatbot Cloud Run runtime"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/aiplatform.user"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/logging.logWriter"

# Create the API key secret
openssl rand -hex 32 | tr -d '\n' | gcloud secrets create "$SECRET_NAME" \
  --replication-policy="automatic" --data-file=-

gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor"

export API_KEY=$(gcloud secrets versions access latest --secret="$SECRET_NAME")

# Deploy
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --allow-unauthenticated \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GEMINI_MODEL=gemini-2.5-flash,GEMINI_TIMEOUT_SECONDS=10,MAX_RETRIES=2" \
  --set-secrets "CHATBOT_API_KEY=${SECRET_NAME}:latest" \
  --timeout 60 \
  --concurrency 20 \
  --max-instances 3 \
  --memory 512Mi \
  --cpu 1

export URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
curl -s "$URL/health"
```

---

## Security

### IAM Roles

| Identity | Role | Scope | Why |
|---|---|---|---|
| `chatbot-runtime` SA | `roles/aiplatform.user` | Project | Call Gemini on Vertex AI |
| `chatbot-runtime` SA | `roles/logging.logWriter` | Project | Write structured logs |
| `chatbot-runtime` SA | `roles/secretmanager.secretAccessor` | Single secret | Read the API key |
| Deployer | `roles/run.admin` + `roles/iam.serviceAccountUser` | Project | Deploy and update the service |

### Secret Handling

- The API key is generated with `openssl rand -hex 32` — never typed in plain text.
- Secret Manager injects it as `CHATBOT_API_KEY` at runtime; the value is never in the image or repo.
- Rotation: add a new secret version, then deploy a new revision.

### API Key Auth

- All `/chat` requests require `X-API-Key`.
- Key comparison uses `secrets.compare_digest` to prevent timing attacks.
- The key value is never logged.

### What Is (and Is Not) Logged

- **Logged:** event names, latency, token counts, status codes, error kinds, request IDs.
- **Never logged:** raw prompt text, reply text, API key, any PII.

---

## Reliability Design

### Retry Logic

- Retryable errors: `429`, `500`, `502`, `503`, `504`, `asyncio.TimeoutError`.
- Non-retryable: `400`, `401`, `403`, `404` — retrying these never helps.
- Exponential backoff with jitter: `delay = min(8s, base × 2^(attempt-1)) + random(0, base/2)`.

### Timeout Hierarchy

```
Cloud Run --timeout 60s
  └── asyncio.wait_for: GEMINI_TIMEOUT_SECONDS + margin = 12s per attempt
        └── SDK HTTP timeout: GEMINI_TIMEOUT_SECONDS = 10s

Worst-case app duration = (MAX_RETRIES + 1) × 12s + ~3s backoff ≈ 39s < 60s ✓
```

### Concurrency Cap

- `MAX_CONCURRENT_LLM_CALLS=8` semaphore per instance prevents quota exhaustion.
- Cloud Run `--concurrency 20` and `--max-instances 3` bound overall scale.

### Health Probes

- `/health` — liveness: process is alive. No external calls. Used by Cloud Run startup and liveness probes.
- `/ready` — readiness: GeminiService is initialized.

---

## Observability

### Structured Log Fields

Every log line is a JSON object with:

```json
{
  "severity": "INFO",
  "message": "llm_call_ok",
  "logger": "chatbot.llm",
  "request_id": "9f2c...",
  "logging.googleapis.com/trace": "projects/PROJECT/traces/TRACE_ID",
  "event": "llm_call_ok",
  "attempt": 1,
  "model": "gemini-2.5-flash",
  "latency_ms": 812,
  "prompt_tokens": 34,
  "output_tokens": 96
}
```

### Saved Log Queries

Base filter (prepend to all queries):
```
resource.type="cloud_run_revision"
resource.labels.service_name="llm-chatbot"
```

| Purpose | Add this line |
|---|---|
| All errors | `severity>=ERROR` |
| Failed model calls | `jsonPayload.event="llm_call_failed"` |
| Retries happening | `jsonPayload.event="llm_retry"` |
| Rate limits | `jsonPayload.kind="rate_limited"` |
| Upstream auth failures | `jsonPayload.kind="upstream_auth"` |
| Timeouts | `jsonPayload.kind="timeout"` |
| Client auth failures | `jsonPayload.event="auth_failed"` |
| One request end to end | `jsonPayload.request_id="REQUEST_ID"` |
| Slow requests | `jsonPayload.event="request_completed" AND jsonPayload.latency_ms>5000` |

### Log-Based Metric

```bash
gcloud logging metrics create llm_rate_limited_count \
  --description="Gemini 429 responses after retries" \
  --log-filter='resource.type="cloud_run_revision" AND resource.labels.service_name="llm-chatbot" AND jsonPayload.kind="rate_limited" AND jsonPayload.event="llm_call_failed"'
```

---

## Failure Drills

All six incidents are documented in [`docs/RUNBOOK.md`](docs/RUNBOOK.md) with reproduce/diagnose/fix/verify steps.

| # | Incident | HTTP Status | Log Signal |
|---|---|---|---|
| 11.1 | Client auth failure (missing/wrong API key) | 401 | `event="auth_failed"` |
| 11.1 | Cloud Run IAM invoker blocked | 403 | No app logs (platform reject) |
| 11.2 | Service account lacks Vertex AI permission | 502 | `kind="upstream_auth"` |
| 11.3 | Wrong model name or region | 502 | `kind="model_not_found"` |
| 11.4 | Rate limiting (429) | 429 | `kind="rate_limited"` |
| 11.5 | Timeout (app-level vs. platform-level) | 504 | `kind="timeout"` |
| 11.6 | Revision fails to start (port/secret) | — | Revision condition logs |

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest -v
```

**Test coverage:**

| File | What is tested |
|---|---|
| `test_api.py` | Health check, missing key (401), wrong key (401), empty message (422), happy path (200), rate-limit error mapping (429 + Retry-After) |
| `test_gemini_client.py` | Retry succeeds on 3rd attempt, exhausted retries → LLMError, 403 not retried and hidden from client, hang → 504 timeout |
| `test_logging.py` | CloudJsonFormatter produces valid JSON with `severity` and custom fields |

---

## Limitations and Next Steps

- **Auth:** API key only. Next step: per-user rate limiting, OAuth/OIDC for multi-tenant use.
- **History:** Stateless — the client sends full history in each request. No server-side session storage.
- **Region:** Single-region deployment. Multi-region with Global Load Balancer for production resilience.
- **Infrastructure:** Manual `gcloud` commands. Next step: Terraform module for reproducible provisioning.
- **CI/CD:** No pipeline. Next step: Cloud Build or GitHub Actions trigger on push.
- **Streaming:** Responses are buffered. Next step: SSE streaming for long replies.
- **Observability:** No dashboard. Next step: Cloud Monitoring dashboard for latency p50/p95/p99 and error rate.
