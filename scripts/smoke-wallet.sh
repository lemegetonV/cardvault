#!/usr/bin/env bash
# Phase 3 smoke: profile-service, enrollment-service and the wallet operator mock.
# Sourced by smoke.sh after a fresh reset (uses its helpers and smoke-cards.sh's).
# Follows spec section 11 steps 1-2 and 4-7; wallet transactions and reconciliation come
# in phase 4. Statuses that arrive by callback are polled, never slept for.

ENR="$(service_url enrollment)"
PROF="$(service_url profile)"
OP="$(service_url wallet-operator-mock)"

ALICE_ENR_CREDIT=e4400000-a11c-4000-8000-000000000001
CHARLIE_CREDIT=ca4d0000-c4a2-4000-8000-000000000001
DANA_CREDIT=ca4d0000-da4a-4000-8000-000000000001
DANA_DEBIT=ca4d0000-da4a-4000-8000-000000000002

as_alice() { req "$1" "$2" "${3:-}" -H "Authorization: Bearer $ALICE" "${@:4}"; }
wallet_status() { # CARD_ID [TOKEN]: enrolment status of one card as the customer sees it
  req GET "$ENR/v1/wallet/cards/$1" "" -H "Authorization: Bearer ${2:-$ALICE}"
  json enrollment.status
}
enrollment_field() { # CARD_ID FIELD: from the test view (includes token_ref)
  test_req GET "$ENR/test/enrollments?card_id=$1"
  json "0.$2"
}
# wait_until DESCRIPTION SECONDS COMMAND...: retry COMMAND every 0.25 s until it succeeds.
wait_until() {
  local what="$1" limit="$2"; shift 2
  local deadline=$(( $(date +%s) + limit ))
  until "$@"; do
    (( $(date +%s) < deadline )) || fail "$what (waited ${limit}s)"
    sleep 0.25
  done
  pass "$what"
}
status_is() { # CARD_ID STATUS [TOKEN]; leaves the wallet card in $BODY
  req GET "$ENR/v1/wallet/cards/$1" "" -H "Authorization: Bearer ${3:-$ALICE}"
  [[ "$(json enrollment.status)" == "$2" ]]
}
decision_is() { purchase "$1" 100; [[ "$(json decision)/$(json decline_code)" == "$2" ]]; }
count_where() { # PY_EXPR over the wallet's cards, e.g. 'c["eligible"]'
  python3 -c 'import json,sys; print(sum(1 for c in json.loads(sys.argv[1])["cards"] if eval(sys.argv[2])))' "$BODY" "$1"
}

req POST "$AUTH/v1/auth/login" "$(login_body alice "$PASSWORD")"
ALICE="$(json access_token)"

section "Wallet (alice): spec 11 step 2"
as_alice GET "$ENR/v1/wallet"
expect_status 200 "alice views her wallet"
expect_eq "$(count_where 'c["enrollment"] and c["enrollment"]["status"] == "ENROLLED"')" 2 "2 cards ENROLLED"
expect_eq "$(count_where 'not c["eligible"]')" 2 "2 cards ineligible"
expect_eq "$(python3 -c 'import json,sys; print(",".join(sorted(c["ineligible_reason"] for c in json.loads(sys.argv[1])["cards"] if not c["eligible"])))' "$BODY")" \
  "NOT_RECENTLY_USED,PRODUCT_NOT_SUPPORTED" "reasons: unused 120 days, prepaid"
[[ "$BODY" != *tok_* ]] || fail "no token_ref in the customer's wallet view"
pass "no token_ref in the customer's wallet view"
as_alice GET "$ENR/v1/wallet/cards/$BOB_CREDIT"
expect_status 404 "bob's card is 404 in alice's wallet"

purchase "$TOK_ALICE_CREDIT" 1500
expect_eq "$(json decision)" "APPROVED" "purchase with the enrolled token (spec 11 step 3)"

section "Report lost, then reissue: spec 11 steps 4-6"
as_alice POST "$CARD/v1/cards/$ALICE_CREDIT/report-lost"
expect_status 200 "alice reports her credit card lost"
wait_until "enrolment becomes SUSPENDED by callback" 20 status_is "$ALICE_CREDIT" SUSPENDED
expect_eq "$(enrollment_field "$ALICE_CREDIT" token_ref)" "$TOK_ALICE_CREDIT" "the token is kept while suspended"
wait_until "purchase is DECLINED / CARD_LOST" 10 decision_is "$TOK_ALICE_CREDIT" DECLINED/CARD_LOST
as_alice POST "$CARD/v1/cards/$ALICE_CREDIT/reissue"
expect_status 200 "alice reissues the card"
NEW_LAST4="$(json last4)"
enrolled_with_new_last4() {
  status_is "$ALICE_CREDIT" ENROLLED && [[ "$(json enrollment.last4)" == "$NEW_LAST4" && "$(json enrollment.version)" -ge 4 ]]
}
wait_until "ENROLLED again with the new last4 $NEW_LAST4" 20 enrolled_with_new_last4
operator_has_new_last4() {
  test_req GET "$OP/test/state"
  python3 -c 'import json,sys; r=[x for x in json.loads(sys.argv[1])["records"] if x["enrollment_id"]==sys.argv[2]][0]; sys.exit(0 if (r["status"],r["last4"])==("ENROLLED",sys.argv[3]) else 1)' "$BODY" "$ALICE_ENR_CREDIT" "$NEW_LAST4"
}
wait_until "the operator has the new last4 too (resume, then update)" 20 operator_has_new_last4
wait_until "the wallet token is APPROVED again" 10 decision_is "$TOK_ALICE_CREDIT" APPROVED/
test_req GET "$ENR/test/enrollments/$ALICE_ENR_CREDIT/history"
expect_eq "$(python3 -c 'import json,sys; print(">".join(h["to_status"] for h in json.loads(sys.argv[1])))' "$BODY")" \
  "ENROLLED>SUSPENDED>ENROLLED" "history: ENROLLED > SUSPENDED > ENROLLED"

section "Opt out, opt in: spec 11 step 7"
as_alice POST "$ENR/v1/wallet/opt-out" "" -H "Idempotency-Key: smoke-optout-1"
expect_status 200 "alice opts out"
expect_eq "$(count_where 'c["enrollment"] and c["enrollment"]["status"] == "REMOVED"')" 2 "both enrolments REMOVED at once"
FIRST="$BODY"
as_alice POST "$ENR/v1/wallet/opt-out" "" -H "Idempotency-Key: smoke-optout-1"
expect_eq "$BODY" "$FIRST" "same Idempotency-Key replays the opt-out"
as_alice POST "$ENR/v1/wallet/opt-out"
expect_status 200 "opting out again is a no-op"
wait_until "the removed token is DECLINED / TOKEN_INACTIVE" 10 decision_is "$TOK_ALICE_CREDIT" DECLINED/TOKEN_INACTIVE
as_alice POST "$ENR/v1/wallet/cards/$ALICE_CREDIT" '{"terms_version":"2026-01"}'
expect_status 409 "adding a card while opted out"
expect_eq "$(json error_code)" "WALLET_OPTED_OUT" "opted-out code"
as_alice POST "$ENR/v1/wallet/opt-in" '{}'
expect_status 422 "opt-in needs terms_version"
as_alice POST "$ENR/v1/wallet/opt-in" '{"terms_version":"2026-01"}'
expect_status 200 "alice opts in"
expect_eq "$(count_where 'c["enrollment"] and c["enrollment"]["status"] == "PENDING"')" 2 "both eligible cards are PENDING"
wait_until "credit card re-ENROLLED" 20 status_is "$ALICE_CREDIT" ENROLLED
wait_until "debit card re-ENROLLED" 20 status_is "$ALICE_DEBIT" ENROLLED
NEW_TOKEN="$(enrollment_field "$ALICE_CREDIT" token_ref)"
[[ -n "$NEW_TOKEN" && "$NEW_TOKEN" != "$TOK_ALICE_CREDIT" ]] || fail "re-enrolment issues a new token_ref"
pass "re-enrolment issues a new token_ref"
wait_until "the new token is APPROVED" 10 decision_is "$NEW_TOKEN" APPROVED/
decision_is "$TOK_ALICE_CREDIT" DECLINED/TOKEN_INACTIVE || fail "the old token stays inactive"
pass "the old token stays inactive"

section "Remove and add one card"
as_alice DELETE "$ENR/v1/wallet/cards/$ALICE_DEBIT"
expect_status 200 "alice removes her debit card"
expect_eq "$(json enrollment.status)/$(json ineligible_reason)" "REMOVED/CARD_REMOVED_BY_CUSTOMER" "REMOVED, and the sweep will not re-add it"
req POST "$ENR/internal/v1/sweep" "" -H "X-Service-Key: ${SERVICE_KEY_TESTS}"
expect_status 200 "sweep runs"
expect_eq "$(wallet_status "$ALICE_DEBIT")" "REMOVED" "the sweep leaves a customer-removed card alone"
as_alice POST "$ENR/v1/wallet/cards/$ALICE_PREPAID" '{"terms_version":"2026-01"}'
expect_status 422 "a prepaid card cannot be added"
expect_eq "$(json error_code)/$(json details.reason)" "CARD_INELIGIBLE/PRODUCT_NOT_SUPPORTED" "ineligible code and reason"
as_alice POST "$ENR/v1/wallet/cards/$ALICE_DEBIT" '{"terms_version":"2026-01"}'
expect_status 202 "alice adds the debit card back"
expect_eq "$(json enrollment.status)" "PENDING" "it is PENDING"
wait_until "and then ENROLLED" 20 status_is "$ALICE_DEBIT" ENROLLED
as_alice POST "$ENR/v1/wallet/cards/$ALICE_DEBIT" '{"terms_version":"2026-01"}'
expect_status 200 "adding an ENROLLED card returns 200"

section "Operator failures"
test_req POST "$OP/test/behaviour" '{"controls":[{"type":"error","status":503,"fail_next":2}]}'
expect_status 200 "the operator will fail the next 2 requests with 503"
as_alice DELETE "$ENR/v1/wallet/cards/$ALICE_DEBIT"
as_alice POST "$ENR/v1/wallet/cards/$ALICE_DEBIT" '{"terms_version":"2026-01"}'
wait_until "the push is retried until the operator accepts it: ENROLLED" 25 status_is "$ALICE_DEBIT" ENROLLED
test_req POST "$OP/test/behaviour" '{"controls":[{"type":"decline_enrollment","fail_next":1,"reason":"RISK_DECLINED"}]}'
expect_status 200 "the operator will decline the next enrolment"
as_alice DELETE "$ENR/v1/wallet/cards/$ALICE_DEBIT"
as_alice POST "$ENR/v1/wallet/cards/$ALICE_DEBIT" '{"terms_version":"2026-01"}'
wait_until "a declined enrolment is FAILED" 20 status_is "$ALICE_DEBIT" FAILED
expect_eq "$(json enrollment.failure_reason)" "RISK_DECLINED" "with the operator's reason"
as_alice POST "$ENR/v1/wallet/cards/$ALICE_DEBIT" '{"terms_version":"2026-01"}'
expect_status 202 "adding a FAILED card starts a new enrolment"
wait_until "which succeeds" 20 status_is "$ALICE_DEBIT" ENROLLED

section "Callback security"
CB_BODY="{\"event_id\":\"$(python3 -c 'import uuid; print(uuid.uuid4())')\",\"operator_ref\":\"x\",\"enrollment_id\":\"$ALICE_ENR_CREDIT\",\"status\":\"REMOVED\",\"operator_version\":999,\"occurred_at\":\"2026-01-01T00:00:00Z\"}"
sign() { python3 -c 'import hmac,hashlib,sys; print(hmac.new(sys.argv[1].encode(), (sys.argv[2]+"."+sys.argv[3]).encode(), hashlib.sha256).hexdigest())' "$1" "$2" "$3"; }
callback() { # BODY TIMESTAMP SECRET
  req POST "$ENR/v1/callbacks/operator" "$1" -H "X-Timestamp: $2" -H "X-Signature: $(sign "$3" "$2" "$1")"
}
NOW_TS="$(date +%s)"
req POST "$ENR/v1/callbacks/operator" "$CB_BODY"
expect_eq "$STATUS/$(json error_code)" "401/SIGNATURE_MISSING" "unsigned callback is rejected"
callback "$CB_BODY" "$NOW_TS" wrong-secret
expect_eq "$STATUS/$(json error_code)" "401/SIGNATURE_INVALID" "wrong signature is rejected"
callback "$CB_BODY" "$(( NOW_TS - 301 ))" "$OPERATOR_HMAC_SECRET"
expect_eq "$STATUS/$(json error_code)" "401/SIGNATURE_EXPIRED" "a 301 s old timestamp is rejected"
callback "$CB_BODY" "$NOW_TS" "$OPERATOR_HMAC_SECRET"
expect_eq "$STATUS/$(json result)/$(json status)" "200/SUPERSEDED/ENROLLED" "a signed REMOVED nobody asked for changes nothing"
callback "$CB_BODY" "$NOW_TS" "$OPERATOR_HMAC_SECRET"
expect_eq "$(json result)" "DUPLICATE" "the same event_id again is a duplicate"
STALE="$(python3 -c 'import json,sys,uuid; d=json.loads(sys.argv[1]); d.update(event_id=str(uuid.uuid4()), status="SUSPENDED", operator_version=1); print(json.dumps(d,separators=(",",":")))' "$CB_BODY")"
callback "$STALE" "$NOW_TS" "$OPERATOR_HMAC_SECRET"
expect_eq "$(json result)/$(json status)" "STALE/ENROLLED" "an older operator_version is ignored as stale"

section "Profile and eligibility (dana: phone not verified)"
req POST "$AUTH/v1/auth/login" "$(login_body dana "$PASSWORD")"
DANA="$(json access_token)"
req GET "$PROF/v1/profile" "" -H "Authorization: Bearer $DANA"
expect_status 200 "dana reads her profile"
expect_eq "$(json email_verified)/$(json phone_verified)" "true/false" "email verified, phone not"
DANA_VERSION="$(json version)"
req GET "$ENR/v1/wallet" "" -H "Authorization: Bearer $DANA"
expect_eq "$(count_where 'c["ineligible_reason"] == "PHONE_NOT_VERIFIED"')" 2 "both cards ineligible: PHONE_NOT_VERIFIED"
req POST "$PROF/v1/profile/verify/send" '{"channel":"PHONE"}' -H "Authorization: Bearer $DANA"
expect_status 202 "a code is sent to the phone"
req POST "$PROF/v1/profile/verify/send" '{"channel":"PHONE"}' -H "Authorization: Bearer $DANA"
expect_eq "$STATUS/$(json error_code)" "429/OTP_COOLDOWN" "a second code within 30 s is refused"
req POST "$PROF/v1/profile/verify" '{"channel":"PHONE","code":"123456"}' -H "Authorization: Bearer $DANA"
expect_eq "$STATUS/$(json error_code)/$(json details.attempts_remaining)" "422/OTP_INVALID/4" "a wrong code"
req PATCH "$PROF/v1/profile" '{"phone":"+15550100199"}' -H "Authorization: Bearer $DANA" -H "If-Match: 999"
expect_eq "$STATUS/$(json error_code)" "412/VERSION_MISMATCH" "a stale If-Match is refused"
req POST "$PROF/v1/profile/verify" '{"channel":"PHONE","code":"000000"}' -H "Authorization: Bearer $DANA"
expect_status 200 "the test code verifies the phone"
expect_eq "$(json phone_verified)/$(json version)" "true/$(( DANA_VERSION + 1 ))" "phone verified, version +1"
dana_eligible() {
  req GET "$ENR/v1/wallet" "" -H "Authorization: Bearer $DANA"
  [[ "$(count_where 'c["eligible"]')" == 2 ]]
}
wait_until "contact.updated makes both cards eligible" 15 dana_eligible
req POST "$ENR/internal/v1/sweep" "" -H "X-Service-Key: ${SERVICE_KEY_TESTS}"
expect_eq "$(python3 -c 'import json,sys; print(sorted(a["card_id"] for a in json.loads(sys.argv[1])["actions"] if a["action"]=="ENROL"))' "$BODY")" \
  "['$DANA_CREDIT', '$DANA_DEBIT']" "the sweep enrols dana's two cards (and nothing else)"
wait_until "dana's credit card ENROLLED" 20 status_is "$DANA_CREDIT" ENROLLED "$DANA"
wait_until "dana's debit card ENROLLED" 20 status_is "$DANA_DEBIT" ENROLLED "$DANA"
req PATCH "$PROF/v1/profile" '{"email":"dana.new@example.com"}' -H "Authorization: Bearer $DANA"
expect_eq "$STATUS/$(json email_verified)" "200/false" "a new email is unverified"
wait_until "an unverified email removes dana's cards (by callback)" 20 status_is "$DANA_CREDIT" REMOVED "$DANA"
expect_eq "$(json enrollment.status_reason)" "EMAIL_NOT_VERIFIED" "removal reason EMAIL_NOT_VERIFIED"

section "Charlie (opted out) and the test customer orchestrator"
req POST "$AUTH/v1/auth/login" "$(login_body charlie "$PASSWORD")"
req GET "$ENR/v1/wallet/cards/$CHARLIE_CREDIT" "" -H "Authorization: Bearer $(json access_token)"
expect_eq "$(json enrollment.status)/$(json ineligible_reason)" "REMOVED/CUSTOMER_OPTED_OUT" "charlie's card is REMOVED, opted out"
test_req POST "$ENR/test/customers" '{"cards":[{"product":"CREDIT","used_days_ago":5,"enrolled":true},{"product":"DEBIT","used_days_ago":200}]}'
expect_status 201 "POST /test/customers creates a customer"
NEW_USER="$(json username)" NEW_PASS="$(json password)" NEW_TOKEN="$(json cards.0.token_ref)"
expect_eq "$(json cards.0.enrollment_status)" "ENROLLED" "with an ENROLLED card"
purchase "$NEW_TOKEN" 100
expect_eq "$(json decision)" "APPROVED" "its token works at once"
req POST "$AUTH/v1/auth/login" "$(login_body "$NEW_USER" "$NEW_PASS")"
expect_status 200 "the new customer can log in"
req GET "$ENR/v1/wallet" "" -H "Authorization: Bearer $(json access_token)"
expect_eq "$(count_where 'c["eligible"]')/$(count_where 'c["ineligible_reason"] == "NOT_RECENTLY_USED"')" "1/1" "its wallet: 1 eligible, 1 not recently used"

section "Nothing left in flight"
quiet() {
  local u
  for u in "$CARD" "$ENR" "$PROF"; do
    test_req GET "$u/test/quiet"
    [[ "$(json quiet)" == true ]] || return 1
  done
  test_req GET "$OP/test/state"
  [[ "$(json pending_callbacks)" == 0 ]]
}
wait_until "outboxes empty, consumers caught up, no callback pending" 30 quiet
