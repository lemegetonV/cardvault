#!/usr/bin/env bash
# CardVault smoke test: the happy path end to end, for whatever exists so far.
# Phase 1 covers the platform (probes, error shape, correlation IDs, shared clock) and
# auth-service (login, JWKS, /me, refresh rotation and reuse, logout, lockout).
# Phase 2 adds card-service and network-mock: cards, lifecycle, authorization, holds,
# clearing, idempotency. Later phases extend it towards spec section 11.
# It starts by resetting and reseeding the stack, so it can be run again and again.
source "$(dirname "$0")/lib.sh"

"$(dirname "$0")/reset.sh" >/dev/null

PASSED=0
pass() { PASSED=$((PASSED + 1)); printf '  ok   %s\n' "$1"; }
fail() {
  printf '  FAIL %s\n       status=%s body=%s\n' "$1" "${STATUS:-}" "${BODY:-}" >&2
  exit 1
}
expect_status() { [[ "$STATUS" == "$1" ]] || fail "$2 (expected HTTP $1)"; pass "$2"; }
expect_eq() { [[ "$1" == "$2" ]] || fail "$3 (expected '$2', got '$1')"; pass "$3"; }
section() { printf '\n%s\n' "$1"; }

AUTH="$(service_url auth)"
PASSWORD="${SEED_USER_PASSWORD}"
login_body() { printf '{"username":"%s","password":"%s"}' "$1" "$2"; }

section "Platform"
for entry in "${SERVICES[@]}"; do
  name="${entry%%=*}" url="${entry#*=}"
  req GET "$url/health"; expect_status 200 "$name /health"
  req GET "$url/ready"; expect_status 200 "$name /ready"
  req GET "$url/openapi.json"; expect_status 200 "$name /openapi.json"
done

# Clear any clock offset left by an earlier interrupted run.
test_req POST "$AUTH/test/clock" '{"reset": true}'; expect_status 200 "shared clock at real time"

section "Login and tokens (alice)"
req POST "$AUTH/v1/auth/login" "$(login_body alice "$PASSWORD")" -H "X-Correlation-ID: smoke-login-1"
expect_status 200 "alice logs in"
expect_eq "$(header X-Correlation-ID)" "smoke-login-1" "correlation ID is echoed"
ACCESS="$(json access_token)"
REFRESH="$(json refresh_token)"
ALICE_ID="$(json customer_id)"
expect_eq "$ALICE_ID" "a11ce000-0000-4000-8000-000000000001" "alice has her fixed seed customer_id"

KID="$(python3 -c 'import base64,json,sys; h=sys.argv[1].split(".")[0]; print(json.loads(base64.urlsafe_b64decode(h+"="*(-len(h)%4)))["kid"])' "$ACCESS")"
req GET "$AUTH/.well-known/jwks.json"; expect_status 200 "JWKS is served"
expect_eq "$(json keys.0.kid)" "$KID" "token kid is in the JWKS"

req GET "$AUTH/v1/auth/me" "" -H "Authorization: Bearer $ACCESS"
expect_status 200 "/me with the access token"
expect_eq "$(json username)" "alice" "/me returns alice"

req GET "$AUTH/v1/auth/me"
expect_status 401 "/me without a token is rejected"
expect_eq "$(json error_code)" "UNAUTHORIZED" "common error shape: error_code"
[[ -n "$(json correlation_id)" ]] || fail "common error shape: correlation_id"
pass "common error shape: correlation_id"

section "Refresh rotation and reuse detection"
req POST "$AUTH/v1/auth/refresh" "{\"refresh_token\":\"$REFRESH\"}"
expect_status 200 "refresh returns a new pair"
REFRESH2="$(json refresh_token)"
[[ "$REFRESH2" != "$REFRESH" ]] || fail "refresh token rotates"
pass "refresh token rotates"
req POST "$AUTH/v1/auth/refresh" "{\"refresh_token\":\"$REFRESH\"}"
expect_status 401 "reusing the old refresh token is rejected"
expect_eq "$(json error_code)" "REFRESH_TOKEN_REUSED" "reuse is detected"
req POST "$AUTH/v1/auth/refresh" "{\"refresh_token\":\"$REFRESH2\"}"
expect_status 401 "reuse revoked the whole family"

section "Logout"
req POST "$AUTH/v1/auth/login" "$(login_body alice "$PASSWORD")"; expect_status 200 "alice logs in again"
REFRESH3="$(json refresh_token)"
req POST "$AUTH/v1/auth/logout" "{\"refresh_token\":\"$REFRESH3\"}"; expect_status 204 "logout"
req POST "$AUTH/v1/auth/refresh" "{\"refresh_token\":\"$REFRESH3\"}"
expect_status 401 "refresh after logout is rejected"

section "Credentials, validation and lockout"
req POST "$AUTH/v1/auth/login" "$(login_body alice wrong-password)"
expect_status 401 "wrong password"
expect_eq "$(json error_code)" "INVALID_CREDENTIALS" "wrong password code"
req POST "$AUTH/v1/auth/login" '{"username":"alice"}'
expect_status 422 "missing password is a validation error"
expect_eq "$(json error_code)" "VALIDATION_ERROR" "validation error code"

LOCK_USER="smoke-lock-$RANDOM$RANDOM"
for i in 1 2 3 4 5; do
  req POST "$AUTH/v1/auth/login" "$(login_body "$LOCK_USER" nope)"
  [[ "$STATUS" == 401 ]] || fail "failed login $i for an unknown username is 401"
done
pass "five failed logins for an unknown username return 401"
req POST "$AUTH/v1/auth/login" "$(login_body "$LOCK_USER" nope)"
expect_status 423 "the sixth attempt is locked"
expect_eq "$(json error_code)" "ACCOUNT_LOCKED" "lockout code"

section "Shared clock drives token expiry"
req POST "$AUTH/v1/auth/login" "$(login_body bob "$PASSWORD")"; expect_status 200 "bob logs in"
BOB_ACCESS="$(json access_token)"
test_req POST "$AUTH/test/clock" '{"advance_seconds": 960}'
expect_status 200 "clock moves 16 minutes ahead"
OFFSET="$(json offset_seconds)"
python3 -c "import sys; sys.exit(0 if abs(float(sys.argv[1]) - 960) < 1 else 1)" "$OFFSET" \
  || fail "offset is 960 s (got $OFFSET)"
pass "offset is 960 s"
req GET "$AUTH/v1/auth/me" "" -H "Authorization: Bearer $BOB_ACCESS"
expect_status 401 "bob's access token has expired on the shared clock"
expect_eq "$(json error_code)" "TOKEN_EXPIRED" "expiry code"
test_req POST "$AUTH/test/clock" '{"reset": true}'; expect_status 200 "clock back to real time"
req GET "$AUTH/v1/auth/me" "" -H "Authorization: Bearer $BOB_ACCESS"
expect_status 200 "bob's token is valid again at real time"

section "Test controls"
req POST "$AUTH/test/reset"
expect_status 401 "test endpoints need X-Test-Key"
test_req POST "$AUTH/test/faults" '{"faults":[{"type":"error","status":503,"fail_next":1,"scope":{"path":"/v1/auth/login"}}]}'
expect_status 200 "inject one 503 on login"
req POST "$AUTH/v1/auth/login" "$(login_body alice "$PASSWORD")"
expect_status 503 "the next login gets the injected 503"
req POST "$AUTH/v1/auth/login" "$(login_body alice "$PASSWORD")"
expect_status 200 "the fault expired after one use"

source "$(dirname "$0")/smoke-cards.sh"

printf '\nSmoke passed: %d checks.\n' "$PASSED"
