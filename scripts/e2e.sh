#!/usr/bin/env bash
# docker compose で起動し、設計書・Markdown・差分を送って結果が返るまでを確かめる（CI でも使う）.
#   scripts/e2e.sh local|aws|azure
set -euo pipefail
profile="${1:-local}"
cd "$(dirname "$0")/.."
token="rp_e2e-$(python -c 'import secrets;print(secrets.token_hex(16))')"
hash="$(python -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$token")"
printf 'RP_API_TOKENS_SHA256=%s\nRP_LLM=fake\n' "$hash" > .env
cleanup() { docker compose --profile "$profile" logs --no-color > "e2e-$profile.log" 2>&1 || true; docker compose --profile "$profile" down -v || true; rm -f .env; }
trap cleanup EXIT
docker compose --profile "$profile" up -d --build
for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS http://127.0.0.1:8080/healthz
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/v1/jobs)
[ "$code" = 401 ] || { echo "認証なしで 401 になりません（$code）"; exit 1; }
for f in samples/member-api-design.xlsx samples/member-api-design.md samples/change.diff; do
  echo "== $f"
  review-platform submit "$f" --url http://127.0.0.1:8080 --token "$token" --title "e2e $profile" --wait --timeout 300 > "e2e-out.md" || { cat e2e-out.md; exit 1; }
  head -5 e2e-out.md
  grep -q "^# レビュー結果" e2e-out.md
done
# MCP: initialize と tools/list
init=$(curl -fsS -X POST http://127.0.0.1:8080/mcp -H "authorization: Bearer $token" -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')
echo "$init" | grep -q submit_document || { echo "MCP の tools/list に submit_document がありません: $init"; exit 1; }
echo "e2e $profile: OK"
