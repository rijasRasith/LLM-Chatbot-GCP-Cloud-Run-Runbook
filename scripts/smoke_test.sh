#!/usr/bin/env bash
set -euo pipefail
: "${URL:?set URL}"; : "${API_KEY:?set API_KEY}"

echo "1) health";        curl -sf "$URL/health" && echo
echo "2) no key -> 401"; curl -s -o /dev/null -w "%{http_code}\n" -X POST "$URL/chat" \
  -H "Content-Type: application/json" -d '{"message":"hi"}'
echo "3) bad key -> 401"; curl -s -o /dev/null -w "%{http_code}\n" -X POST "$URL/chat" \
  -H "Content-Type: application/json" -H "X-API-Key: wrong" -d '{"message":"hi"}'
echo "4) empty msg -> 422"; curl -s -o /dev/null -w "%{http_code}\n" -X POST "$URL/chat" \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" -d '{"message":""}'
echo "5) happy path -> 200"; curl -s -o /dev/null -w "%{http_code}\n" -X POST "$URL/chat" \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" -d '{"message":"Say hello."}'
