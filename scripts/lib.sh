#!/usr/bin/env bash
# Shared helpers for the CardVault scripts. Source it; do not run it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Load .env (falls back to .env.example) without overriding variables already set.
_env_file="$ROOT/.env"
[[ -f "$_env_file" ]] || _env_file="$ROOT/.env.example"
while IFS='=' read -r _k _v; do
  [[ -z "$_k" || "$_k" == \#* ]] && continue
  [[ -z "${!_k:-}" ]] && export "$_k=$_v"
done < "$_env_file"

# Services that exist so far, as name=base URL. Each phase adds its services here.
SERVICES=(
  "auth=http://localhost:8001"
)

service_url() {
  local want="$1" entry
  for entry in "${SERVICES[@]}"; do
    [[ "${entry%%=*}" == "$want" ]] && { echo "${entry#*=}"; return; }
  done
  echo "unknown service $want" >&2
  return 1
}

# req METHOD URL [JSON_BODY] [extra curl args...]
# Sets STATUS, BODY and HEADERS (a file path) for the caller.
HEADERS="$(mktemp)"
trap 'rm -f "$HEADERS"' EXIT
req() {
  local method="$1" url="$2" body="${3:-}"
  shift $(( $# >= 3 ? 3 : $# ))
  local args=(-sS -X "$method" -D "$HEADERS" -o /dev/stdout -w '\n%{http_code}' --max-time 30)
  if [[ -n "$body" ]]; then
    args+=(-H 'Content-Type: application/json' --data "$body")
  fi
  local out
  out="$(curl "${args[@]}" "$@" "$url")"
  STATUS="${out##*$'\n'}"
  BODY="${out%$'\n'*}"
}

# test_req METHOD URL [JSON_BODY]: req with the X-Test-Key header.
test_req() { req "$1" "$2" "${3:-}" -H "X-Test-Key: ${TEST_KEY}"; }

# json FIELD_PATH: read a field from $BODY, e.g. json access_token, json checks.database
json() {
  python3 -c '
import json, sys
value = json.loads(sys.argv[1])
for part in sys.argv[2].split("."):
    value = value[int(part)] if isinstance(value, list) else value.get(part)
print("" if value is None else (json.dumps(value) if isinstance(value, (dict, list)) else value))
' "$BODY" "$1"
}

header() { grep -i "^$1:" "$HEADERS" | head -1 | cut -d' ' -f2- | tr -d '\r'; }
