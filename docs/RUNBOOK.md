# Runbook: LLM Chatbot on Cloud Run

## Quick Facts

- **Service:** `llm-chatbot`
- **Region:** `us-central1`
- **Runtime SA:** `chatbot-runtime@PROJECT_ID.iam.gserviceaccount.com`
- **Rollback:** `gcloud run services update-traffic llm-chatbot --region us-central1 --to-revisions=REVISION_NAME=100`
- **Logs Explorer base filter:**
  ```
  resource.type="cloud_run_revision"
  resource.labels.service_name="llm-chatbot"
  ```

---

## Triage Cheat Sheet

| Symptom from client | Likely layer | First log query | See |
|---|---|---|---|
| `401` from `/chat` | App API key | `jsonPayload.event="auth_failed"` | 11.1 |
| `403` HTML/JSON with no app logs | Cloud Run IAM invoker | Request log only (no app lines) | 11.1 |
| `502 upstream_auth` | Service account IAM / API disabled | `jsonPayload.kind="upstream_auth"` | 11.2 |
| `502 model_not_found` | Wrong model name or region | `jsonPayload.kind="model_not_found"` | 11.3 |
| `429 rate_limited` | Vertex quota / burst | `jsonPayload.kind="rate_limited"` | 11.4 |
| `504 timeout` | App timeout or Cloud Run timeout | `jsonPayload.kind="timeout"` plus request log status 504 | 11.5 |
| Deploy fails / revision never ready | Container start or secret access | Revision logs in console | 11.6 |

---

## Incident: Client Auth Failure (401 and Cloud Run 403)

**Symptom:** `401 Invalid or missing API key`, or a `403 Forbidden` from Cloud Run when deployed with `--no-allow-unauthenticated`.

**Impact:** All `/chat` calls fail for the affected client. Other clients with valid keys are unaffected.

**Detect:**
```
jsonPayload.event="auth_failed"
```
For Cloud Run 403: only the platform request log exists — no app log lines appear.

**Reproduce:**
```bash
# Missing key
curl -i -X POST "$URL/chat" -H "Content-Type: application/json" -d '{"message":"hi"}'

# Wrong key
curl -i -X POST "$URL/chat" -H "Content-Type: application/json" -H "X-API-Key: wrong" -d '{"message":"hi"}'
```

**Diagnose:**
1. Check `jsonPayload.reason` — either `missing_key` or `invalid_key`.
2. For Cloud Run 403: no `auth_failed` log exists. The request log alone carries the 403. This means the Cloud Run IAM invoker policy rejected the request before the container received it.
3. Confirm the current API key: `gcloud secrets versions access latest --secret="chatbot-api-key"`.

**Root Causes (ranked):**
1. Client is sending no `X-API-Key` header.
2. Client has a stale or incorrect API key value.
3. Service deployed with `--no-allow-unauthenticated` but the caller lacks `roles/run.invoker`.

**Fix:**
```bash
# For wrong/missing key: give the client the current value
gcloud secrets versions access latest --secret="chatbot-api-key"

# For Cloud Run IAM 403: grant invoker to the calling identity
gcloud run services add-iam-policy-binding llm-chatbot \
  --region us-central1 \
  --member="user:CALLER_EMAIL" \
  --role="roles/run.invoker"
```

**Verify:** `./scripts/smoke_test.sh` returns `200` for the happy path case.

**Prevent:** Add the smoke test to CI. Alert on sustained `auth_failed` count exceeding threshold.

---

## Incident: Service Account Lacks Vertex AI Permission (502 upstream_auth)

**Symptom:** Client receives `502` with `"error": "upstream_auth"`. Usually appears after IAM changes, new projects, or new service accounts.

**Impact:** All `/chat` calls return 502. `/health` and `/ready` remain unaffected.

**Detect:**
```
jsonPayload.kind="upstream_auth"
```

**Reproduce:**
```bash
gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/aiplatform.user"
# Wait a few minutes for propagation, then:
curl -s -X POST "$URL/chat" \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" -d '{"message":"hi"}'
```

**Diagnose:**
1. `jsonPayload.kind="upstream_auth"` — the `error` field names the denied permission (e.g., `aiplatform.endpoints.predict`).
2. Confirm the runtime service account:
   ```bash
   gcloud run services describe llm-chatbot --region us-central1 \
     --format='value(spec.template.spec.serviceAccountName)'
   ```
3. List its roles:
   ```bash
   gcloud projects get-iam-policy "$PROJECT_ID" \
     --flatten="bindings[].members" \
     --filter="bindings.members:${SA_EMAIL}" \
     --format="table(bindings.role)"
   ```

**Root Causes (ranked):**
1. `roles/aiplatform.user` removed from the runtime SA.
2. Vertex AI API disabled (`SERVICE_DISABLED` in error).
3. Service deployed under wrong service account (default compute SA lacks the role).
4. Wrong `GOOGLE_CLOUD_PROJECT` env var pointing to a different project.

**Fix:**
```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/aiplatform.user"

# If API is disabled:
gcloud services enable aiplatform.googleapis.com
```

**Verify:** `/chat` returns `200` and logs show `event="llm_call_ok"`. Note: IAM propagation can take 1–3 minutes.

**Prevent:** Include IAM role assertions in an infrastructure test or Terraform plan. Alert on `upstream_auth` kind in production.

---

## Incident: Wrong Model Name or Region (502 model_not_found)

**Symptom:** Client receives `502` with `"error": "model_not_found"`.

**Impact:** All `/chat` calls fail. `/ready` still returns 200 (it does not validate the model name against Vertex).

**Detect:**
```
jsonPayload.kind="model_not_found"
```

**Reproduce:**
```bash
gcloud run services update llm-chatbot --region us-central1 \
  --update-env-vars GEMINI_MODEL=not-a-real-model
```

**Diagnose:**
1. `jsonPayload.model` shows the invalid value.
2. Check the Vertex AI console for available models in the configured region.
3. Confirm `GOOGLE_CLOUD_LOCATION` matches a region where the model is available.

**Root Causes (ranked):**
1. `GEMINI_MODEL` set to a name that does not exist or has been deprecated.
2. Model exists but is not available in `GOOGLE_CLOUD_LOCATION`.
3. Typo introduced during a manual `gcloud run services update`.

**Fix:**
```bash
# Set a valid model
gcloud run services update llm-chatbot --region us-central1 \
  --update-env-vars GEMINI_MODEL=gemini-2.5-flash

# Or roll back to the last known-good revision
gcloud run revisions list --service llm-chatbot --region us-central1
gcloud run services update-traffic llm-chatbot --region us-central1 \
  --to-revisions=REVISION_NAME=100
```

**Verify:** `/ready` returns the expected model name. `/chat` returns `200`.

**Prevent:** Validate the model name in a pre-deploy smoke test. Pin the model name to a tested value in source control.

---

## Incident: Rate Limiting (429 rate_limited)

**Symptom:** Client receives `429` with `Retry-After` header. Logs show `llm_retry` warnings followed by `llm_call_failed` if retries are exhausted.

**Impact:** Affected requests fail with a structured 429. Requests that succeed during a retry window are transparent to the client.

**Detect:**
```
jsonPayload.kind="rate_limited"
jsonPayload.event="llm_call_failed"
```

**Reproduce (fault injection):**
```bash
# Enable fault injection first
gcloud run services update llm-chatbot --region us-central1 \
  --update-env-vars FAULT_INJECTION_ENABLED=true

# All attempts fail
curl -i -X POST "$URL/chat" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Debug-Fault: 429" \
  -d '{"message":"hi"}'

# Only first attempt fails, retry succeeds
curl -i -X POST "$URL/chat" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Debug-Fault: 429_once" \
  -d '{"message":"hi"}'

# Disable after drill
gcloud run services update llm-chatbot --region us-central1 \
  --update-env-vars FAULT_INJECTION_ENABLED=false
```

**Reproduce (real burst):**
```bash
URL=$URL API_KEY=$API_KEY python scripts/burst.py 30
```

**Diagnose:**
1. Count `llm_retry` events per `request_id` — shows how many retries each request needed.
2. Distinguish burst (short spike, recovers) from sustained quota exhaustion (persistent 429s).
3. Check Vertex AI quotas: GCP console > IAM & Admin > Quotas, filter "Vertex AI".
4. Review `--max-instances` × `--concurrency` × `MAX_CONCURRENT_LLM_CALLS` to estimate peak model calls.

**Root Causes (ranked):**
1. Traffic burst exceeds per-minute token or request quota.
2. `MAX_CONCURRENT_LLM_CALLS` too high relative to quota.
3. `--max-instances` too high, allowing too many parallel instances.

**Fix (in preference order):**
1. Exponential backoff with jitter is already implemented and handles transient bursts.
2. Lower `MAX_CONCURRENT_LLM_CALLS` to smooth concurrency within an instance.
3. Lower `--max-instances` to reduce aggregate throughput.
4. Return `429` + `Retry-After` to clients and instruct clients to back off.
5. Request a quota increase via the GCP console.
6. Reduce `MAX_OUTPUT_TOKENS` to decrease per-request token consumption.

**Verify:** Re-run `burst.py`; status distribution is predominantly `200` with any remaining `429`s carrying `Retry-After`.

**Prevent:** Create a log-based metric on `kind="rate_limited" AND event="llm_call_failed"`. Alert when count exceeds 5 in 5 minutes.

---

## Incident: Timeout (504)

**Symptom:** Client receives `504`. Two distinct failure modes exist with different causes.

**Impact:** Affected requests do not return a reply. Retried requests may succeed if the model recovers.

**Detect:**
```
jsonPayload.kind="timeout"
```
For platform-level kills: request log shows `504` but no matching `request_completed` app log for that `request_id`.

**Reproduce 1 — App-level timeout (expected behavior):**
```bash
gcloud run services update llm-chatbot --region us-central1 \
  --update-env-vars FAULT_INJECTION_ENABLED=true

curl -i -X POST "$URL/chat" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Debug-Fault: timeout" \
  -d '{"message":"hi"}'

gcloud run services update llm-chatbot --region us-central1 \
  --update-env-vars FAULT_INJECTION_ENABLED=false
```
Expected: `llm_retry` warnings, then a clean `504` JSON response after ~35–40 s, within Cloud Run's 60 s limit.

**Reproduce 2 — Platform timeout (misconfiguration):**
```bash
gcloud run services update llm-chatbot --region us-central1 --timeout 5

curl -i -X POST "$URL/chat" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -H "X-Debug-Fault: timeout" \
  -d '{"message":"hi"}'
```
Expected: platform 504 with no `request_completed` log line.

**Diagnose:**
1. Compare request log latency with `jsonPayload.latency_ms`.
2. Check for `chat_request` log entries with no corresponding `request_completed` — platform-level kill signature.
3. Compute worst-case app duration: `(max_retries + 1) × (GEMINI_TIMEOUT_SECONDS + margin) + total backoff`. This must be less than `--timeout`.
   - Defaults: `3 × 12 s + ~3 s backoff ≈ 39 s < 60 s` ✓

**Root Causes (ranked):**
1. Cloud Run `--timeout` set lower than the worst-case app duration.
2. Gemini model is consistently slow (high token count, region latency).
3. `MAX_RETRIES` too high relative to `--timeout`.

**Fix:**
```bash
# Restore Cloud Run timeout after drill
gcloud run services update llm-chatbot --region us-central1 --timeout 60

# Reduce output tokens for faster model responses
gcloud run services update llm-chatbot --region us-central1 \
  --update-env-vars MAX_OUTPUT_TOKENS=256
```

**Verify:** Timeout fault injection now produces a clean `504` JSON, not a platform kill. Logs show `request_completed` with `status=504`.

**Prevent:** Enforce the timeout relationship in a pre-deploy check: `Cloud Run timeout > (max_retries + 1) × (gemini_timeout + margin) + max_backoff`.

---

## Incident: Revision Fails to Start

**Symptom:** `gcloud run deploy` fails, or the new revision never becomes ready. Traffic stays on the old revision.

**Impact:** New revisions cannot serve traffic. Old revision continues running if it was healthy.

**Detect:** Cloud Run console > Revisions > failing revision > Logs.
```bash
gcloud run revisions describe REVISION_NAME --region us-central1
```
Look at `status.conditions` for the failure reason.

**Reproduce A — Port mismatch:**
Temporarily change Dockerfile `CMD` to `--port 8000` (hard-coded) and deploy. The container will fail to listen on `PORT` and Cloud Run will report a health check failure.

**Reproduce B — Secret access denied:**
```bash
gcloud secrets remove-iam-policy-binding chatbot-api-key \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor"
gcloud run services update llm-chatbot --region us-central1 \
  --update-env-vars DRILL=1
```
The new revision fails at startup with a permission-denied error on the secret. The old revision keeps serving.

**Diagnose:**
1. Check revision condition messages via `gcloud run revisions describe`.
2. For secret errors: `PERMISSION_DENIED` accessing `projects/.../secrets/chatbot-api-key`.
3. For port errors: `Container failed to start and listen on the port defined by the PORT env var`.

**Root Causes (ranked):**
1. Runtime SA lacks `roles/secretmanager.secretAccessor` on the secret.
2. Hard-coded port in CMD instead of `${PORT:-8080}`.
3. Application crash at startup (syntax error, missing import, bad config).
4. Secret version deleted or secret itself deleted.

**Fix:**
```bash
# Restore secret access
gcloud secrets add-iam-policy-binding chatbot-api-key \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor"

# Restore correct port in Dockerfile CMD
# CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --no-access-log

# Redeploy
gcloud run deploy llm-chatbot --source . --region us-central1 ...
```

**Verify:** New revision becomes ready. `/health` returns `{"status":"ok"}`.

**Prevent:** Run `docker run` with `--env PORT=8080` locally before deploying. Include secret binding assertions in infrastructure tests.

---

## Post-Incident Examples

### Drill: IAM Role Removed → 502 upstream_auth

**What happened:** The `roles/aiplatform.user` binding was removed from `chatbot-runtime` as part of a drill simulating an accidental IAM change.

**Timeline (UTC):**
- T+0: IAM binding removed via `gcloud projects remove-iam-policy-binding`.
- T+2m: First `502 upstream_auth` responses observed (propagation delay).
- T+3m: Log query `jsonPayload.kind="upstream_auth"` confirmed the cause.
- T+3m30s: IAM binding restored.
- T+5m: Service recovered to 200 responses.

**Root cause:** Vertex AI API authorization denied for the runtime service account.

**Fix:** Re-added `roles/aiplatform.user` to the runtime SA.

**Verified:** `/chat` returned `200` and logs showed `event="llm_call_ok"`.

**Prevention:** Log-based alert on `kind="upstream_auth"` with a 1-minute window.
