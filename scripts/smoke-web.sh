#!/usr/bin/env bash
# Phase 5 smoke: the web app's BFF and security headers, and the operator's embed pages.
# HTTP level only (no browser): the pages themselves are covered by the Playwright suite
# the owner writes in tests-ui/. Sourced by smoke.sh (uses its helpers).

WEB="${WEB_URL:-http://localhost:3000}"
JAR="$(mktemp)"
web() { req "$1" "$WEB$2" "${3:-}" -b "$JAR" -c "$JAR" "${@:4}"; }
csrf() { awk '$6 == "cv_csrf" { print $7 }' "$JAR"; }

section "Web app (BFF, cookies, CSRF, headers)"
web GET /api/health; expect_status 200 "web /api/health"
web GET /wallet
expect_eq "$STATUS $(header location)" "307 /login?next=%2Fwallet" "a signed-out visit goes to /login"
web GET /login; expect_status 200 "the login page renders"
[[ "$(header content-security-policy)" == *"frame-src ${OPERATOR_PUBLIC_URL:-http://localhost:8101}"* ]] \
  || fail "CSP allows framing only the operator"
pass "CSP allows framing only the operator"
expect_eq "$(header x-content-type-options)/$(header referrer-policy)" "nosniff/strict-origin-when-cross-origin" "nosniff and Referrer-Policy"
[[ -n "$(csrf)" ]] || fail "a CSRF cookie is set"
pass "a CSRF cookie is set"

web POST /api/auth/login "$(login_body alice "$PASSWORD")"
expect_eq "$STATUS/$(json error_code)" "403/CSRF_FAILED" "login without the CSRF header is refused"
web POST /api/auth/login "$(login_body alice "$PASSWORD")" -H "X-CSRF-Token: $(csrf)"
expect_status 200 "login through the BFF"
expect_eq "$(json session_expires_in)" 900 "a 15-minute session"
[[ "$BODY" != *access_token* ]] || fail "no token in the response body"
pass "no token in the response body"
grep -i '^set-cookie: cv_access=' "$HEADERS" | grep -i 'httponly' | grep -qi 'samesite=lax' \
  || fail "the access token is an HttpOnly, SameSite=Lax cookie"
pass "the access token is an HttpOnly, SameSite=Lax cookie"

web GET /api/wallet; expect_status 200 "the wallet through the BFF"
expect_eq "$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])["cards"]))' "$BODY")" 4 "alice's 4 cards"
web GET "/api/wallet/transactions?limit=5"; expect_status 200 "transactions through the BFF (routed to wallet-txn)"
web GET "/api/wallet/transactions/export?from=2026-10-31T18:30:00.000Z&to=2026-11-30T18:29:59.999Z&from_date=2026-11-01&to_date=2026-11-30"
expect_eq "$STATUS $(header content-disposition)" \
  '200 attachment; filename="wallet-transactions-2026-11-01-2026-11-30.csv"' "the export filename uses the local dates"
web POST /api/wallet/opt-out
expect_eq "$STATUS/$(json error_code)" "403/CSRF_FAILED" "a state change without the CSRF header is refused"
web GET /api/nothing; expect_status 404 "unknown BFF routes are 404"
web POST /api/auth/logout "" -H "X-CSRF-Token: $(csrf)"; expect_status 204 "logout"
web GET /api/wallet; expect_status 401 "signed out: the BFF refuses"
rm -f "$JAR"

section "Operator embed and help pages"
req GET "$OP/embed/terms"; expect_status 200 "the terms page"
[[ "$(header content-security-policy)" == *"frame-ancestors http://localhost:3000 http://127.0.0.1:3000"* ]] \
  || fail "only the web app may frame the terms"
pass "only the web app may frame the terms"
[[ "$BODY" == *terms-accepted* && "$BODY" == *data-testid=\"terms-agree\"* ]] || fail "the terms page posts terms-accepted"
pass "the terms page posts terms-accepted"
req GET "$OP/help"; expect_status 200 "the help page"
