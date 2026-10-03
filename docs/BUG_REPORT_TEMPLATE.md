## Summary
One or two sentences: what is broken?

## Severity
- [ ] S1: Service down / all requests failing
- [ ] S2: Major feature degraded (e.g., many 429/504)
- [ ] S3: Minor / workaround exists

## Environment
- Environment: local / Cloud Run
- Region:
- Revision: (output of `gcloud run services describe llm-chatbot --format='value(status.latestReadyRevisionName)'`)
- Commit SHA:
- Gemini model / location:
- Fault injection enabled? yes / no

## Steps to Reproduce
1.
2.
3.

## Expected Behavior

## Actual Behavior
(status code, response body)

## Evidence
- request_id (from `x-request-id` header or response body):
- Time (UTC):
- Log query used:
- Relevant log excerpt (remove secrets and personal data):

```json
{ }
```

## Impact
Who is affected and how often?

## Suspected Cause (optional)

## Workaround (optional)

## Resolution (fill in when closed)
- Root cause:
- Fix (PR link):
- How it was verified:
- Follow-up / prevention:
