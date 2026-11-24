#!/usr/bin/env bash
# Phase 2 smoke: card-service and network-mock. Sourced by smoke.sh (uses its helpers).

CARD="$(service_url card)"
NET="$(service_url network-mock)"

ALICE_CREDIT=ca4d0000-a11c-4000-8000-000000000001
ALICE_DEBIT=ca4d0000-a11c-4000-8000-000000000002
ALICE_UNUSED=ca4d0000-a11c-4000-8000-000000000003
ALICE_PREPAID=ca4d0000-a11c-4000-8000-000000000004
BOB_CREDIT=ca4d0000-b0b0-4000-8000-000000000001
TOK_ALICE_CREDIT=tok_a11ce0c4ed170000000001
TOK_ALICE_DEBIT=tok_a11ce0deb1700000000002

svc_req() { req "$1" "$2" "${3:-}" -H "X-Service-Key: ${SERVICE_KEY_TESTS}" "${@:4}"; }
net_req() { req "$1" "$2" "${3:-}" -H "X-Test-Key: ${TEST_KEY}" "${@:4}"; }
available_of() {
  req GET "$CARD/v1/cards/$1" "" -H "Authorization: Bearer $ALICE"
  json available
}
purchase() {
  net_req POST "$NET/v1/test/purchases" \
    "{\"token_ref\":\"$1\",\"amount\":$2,\"merchant_name\":\"Smoke Coffee\",\"mcc\":\"5814\"}" "${@:3}"
}

req POST "$AUTH/v1/auth/login" "$(login_body alice "$PASSWORD")"
ALICE="$(json access_token)"

section "Cards (alice)"
req GET "$CARD/v1/cards" "" -H "Authorization: Bearer $ALICE"
expect_status 200 "alice lists her cards"
expect_eq "$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])["items"]))' "$BODY")" 4 "alice has 4 cards"
[[ "$BODY" != *pan* ]] || fail "no card number in the response"
pass "no card number in the response"
req GET "$CARD/v1/cards?limit=3" "" -H "Authorization: Bearer $ALICE"
CURSOR="$(json next_cursor)"
[[ -n "$CURSOR" ]] || fail "first page of 3 has a next_cursor"
pass "first page of 3 has a next_cursor"
req GET "$CARD/v1/cards?limit=3&cursor=$CURSOR" "" -H "Authorization: Bearer $ALICE"
expect_eq "$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(len(d["items"]), d["next_cursor"])' "$BODY")" "1 None" "second page has the last card and no cursor"
req GET "$CARD/v1/cards?cursor=not-a-cursor" "" -H "Authorization: Bearer $ALICE"
expect_status 400 "a bad cursor is rejected"
expect_eq "$(json error_code)" "INVALID_CURSOR" "bad cursor code"
req GET "$CARD/v1/cards/$BOB_CREDIT" "" -H "Authorization: Bearer $ALICE"
expect_status 404 "bob's card is 404 for alice (no enumeration)"
req GET "$CARD/v1/cards" "" -H "X-Service-Key: ${SERVICE_KEY_TESTS}"
expect_status 401 "customer endpoint rejects a service key"

section "Service-to-service auth"
AUTH_BODY="{\"trace_id\":\"smoke-direct-1\",\"card_id\":\"$ALICE_CREDIT\",\"token_ref\":\"$TOK_ALICE_DEBIT\",\"token_requestor\":\"WALLET\",\"amount\":100,\"currency\":\"USD\",\"merchant_name\":\"Smoke\",\"mcc\":\"5999\"}"
req POST "$CARD/internal/v1/authorizations" "$AUTH_BODY"
expect_status 401 "internal endpoint without X-Service-Key"
req POST "$CARD/internal/v1/authorizations" "$AUTH_BODY" -H "Authorization: Bearer $ALICE"
expect_status 401 "internal endpoint rejects a customer JWT"
svc_req POST "$CARD/internal/v1/authorizations" "$AUTH_BODY"
expect_status 200 "tests key is accepted"
expect_eq "$(json decline_code)" "TOKEN_MISMATCH" "another card's token is TOKEN_MISMATCH"
svc_req POST "$CARD/internal/v1/authorizations" "${AUTH_BODY/\"amount\":100/\"amount\":0}"
expect_status 422 "amount 0 is a validation error"

section "Purchase, duplicate trace_id, clearing (alice credit)"
BASE="$(available_of $ALICE_CREDIT)"  # limit minus the seeded transactions
expect_eq "$BASE" 137974 "seeded transactions are reflected in available (1379.74)"
purchase "$TOK_ALICE_CREDIT" 2500 -H "X-Correlation-ID: smoke-purchase-1"
expect_status 200 "wallet purchase of 25.00"
expect_eq "$(json decision)" "APPROVED" "approved"
TRACE1="$(json trace_id)"
expect_eq "$(available_of $ALICE_CREDIT)" "$((BASE - 2500))" "available drops by 25.00"

net_req POST "$NET/test/behaviour" '{"controls":[{"type":"duplicate_authorization","fail_next":1}]}'
expect_status 200 "network will resend the next authorization"
purchase "$TOK_ALICE_CREDIT" 1000
expect_status 200 "purchase of 10.00 sent twice with one trace_id"
TRACE2="$(json trace_id)"
expect_eq "$(json duplicate_response.hold_id)" "$(json hold_id)" "the duplicate returns the same decision and hold"
expect_eq "$(available_of $ALICE_CREDIT)" "$((BASE - 3500))" "only one hold was taken (35.00 held in total)"

net_req POST "$NET/v1/test/purchases/$TRACE1/settle" '{"amount":2000}'
expect_status 200 "settle the 25.00 purchase at 20.00"
expect_eq "$(json hold_status)" "SETTLED" "hold is SETTLED"
expect_eq "$(available_of $ALICE_CREDIT)" "$((BASE - 3000))" "available corrected to the cleared amount"
net_req POST "$NET/v1/test/purchases/$TRACE1/settle" '{"amount":2000}'
expect_status 409 "a second SETTLE is rejected"
net_req POST "$NET/v1/test/purchases/$TRACE1/refund" '{"amount":500}'
expect_status 200 "refund 5.00"
expect_eq "$(available_of $ALICE_CREDIT)" "$((BASE - 2500))" "refund credits available"
net_req POST "$NET/v1/test/purchases/$TRACE1/refund" '{"amount":2000}'
expect_status 422 "refund above what is left is rejected"
expect_eq "$(json error_code)" "REFUND_EXCEEDS_CLEARED" "refund cap code"
net_req POST "$NET/v1/test/purchases/$TRACE2/reverse" '{"amount":400}'
expect_status 200 "partial reverse of 4.00"
expect_eq "$(json hold_status)/$(json hold_amount)" "ACTIVE/600" "hold stays ACTIVE with 6.00"
expect_eq "$(available_of $ALICE_CREDIT)" "$((BASE - 2100))" "partial reverse credits 4.00"

CLR="{\"clearing_id\":\"smoke-clr-1\",\"trace_id\":\"$TRACE2\",\"type\":\"REVERSE\",\"amount\":100,\"currency\":\"USD\"}"
svc_req POST "$CARD/internal/v1/clearing" "$CLR"; expect_status 200 "clearing record smoke-clr-1"
FIRST="$BODY"
svc_req POST "$CARD/internal/v1/clearing" "$CLR"; expect_status 200 "the same clearing_id again"
expect_eq "$BODY" "$FIRST" "replay returns the identical response"
svc_req POST "$CARD/internal/v1/clearing" "${CLR/\"amount\":100/\"amount\":200}"
expect_status 422 "same clearing_id, different body"
expect_eq "$(json error_code)" "CLEARING_ID_REUSED" "clearing reuse code"

purchase "$TOK_ALICE_CREDIT" 99999999
expect_eq "$(json decline_code)" "INSUFFICIENT_FUNDS" "a huge purchase is INSUFFICIENT_FUNDS"

section "Plain card purchase makes an unused card recently used"
net_req POST "$NET/v1/test/card-purchases" "{\"card_id\":\"$ALICE_UNUSED\",\"amount\":700}"
expect_eq "$(json decision)" "APPROVED" "card purchase (no token) approved"
req GET "$CARD/v1/cards/$ALICE_UNUSED" "" -H "Authorization: Bearer $ALICE"
python3 -c 'import json,sys,datetime as d; t=d.datetime.fromisoformat(json.loads(sys.argv[1])["last_used_at"]); sys.exit(0 if (d.datetime.now(d.UTC)-t).total_seconds() < 120 else 1)' "$BODY" \
  || fail "last_used_at moved to now"
pass "last_used_at moved to now"

section "Lifecycle and idempotency (alice credit)"
req POST "$CARD/v1/cards/$ALICE_CREDIT/report-lost" "" -H "Authorization: Bearer $ALICE" -H "Idempotency-Key: smoke-lost-1"
expect_status 200 "report lost"
expect_eq "$(json status)" "LOST" "card is LOST"
FIRST="$BODY"
req POST "$CARD/v1/cards/$ALICE_CREDIT/report-lost" "" -H "Authorization: Bearer $ALICE" -H "Idempotency-Key: smoke-lost-1"
expect_eq "$BODY" "$FIRST" "same Idempotency-Key replays the same body"
expect_eq "$(header Idempotent-Replayed)" "true" "replay is marked"
req POST "$CARD/v1/cards/$ALICE_DEBIT/report-lost" "" -H "Authorization: Bearer $ALICE" -H "Idempotency-Key: smoke-lost-1"
expect_status 422 "the same key on another card"
expect_eq "$(json error_code)" "IDEMPOTENCY_KEY_REUSED" "key reuse code"
req POST "$CARD/v1/cards/$ALICE_CREDIT/report-lost" "" -H "Authorization: Bearer $ALICE"
expect_status 200 "report lost again is a no-op"
purchase "$TOK_ALICE_CREDIT" 100
expect_eq "$(json decision)/$(json decline_code)" "DECLINED/CARD_LOST" "purchase on a LOST card"
req GET "$CARD/v1/cards/$ALICE_CREDIT" "" -H "Authorization: Bearer $ALICE"
OLD_EXPIRY="$(json expiry)"
req POST "$CARD/v1/cards/$ALICE_CREDIT/reissue" "" -H "Authorization: Bearer $ALICE"
expect_status 200 "reissue"
expect_eq "$(json status)" "ACTIVE" "reissued card is ACTIVE"
[[ "$(json expiry)" != "$OLD_EXPIRY" ]] || fail "reissue sets a new expiry"
pass "reissue sets a new expiry"
expect_eq "$(json card_id)" "$ALICE_CREDIT" "card_id is unchanged"
# The wallet token after report-lost and reissue depends on the enrolment flow (suspend,
# resume, update callbacks), so smoke-wallet.sh checks it with waits.

req POST "$CARD/v1/cards/$ALICE_DEBIT/close" "" -H "Authorization: Bearer $ALICE"
expect_eq "$(json status)" "CLOSED" "close the debit card"
req POST "$CARD/v1/cards/$ALICE_DEBIT/close" "" -H "Authorization: Bearer $ALICE"
expect_status 200 "close again is a no-op"
req POST "$CARD/v1/cards/$ALICE_DEBIT/reissue" "" -H "Authorization: Bearer $ALICE"
expect_status 409 "a CLOSED card cannot be reissued"
expect_eq "$(json error_code)" "INVALID_STATE_TRANSITION" "transition code"
purchase "$TOK_ALICE_DEBIT" 100
expect_eq "$(json decline_code)" "CARD_CLOSED" "purchase on a CLOSED card"

section "Shared clock drives card expiry"
test_req POST "$AUTH/test/clock" '{"now":"2027-10-01T00:00:00Z"}'
expect_status 200 "clock set to 2027-10-01 (alice's prepaid expires 2027-09)"
sleep 1.2   # other services follow the shared clock within 1 s
net_req POST "$NET/v1/test/card-purchases" "{\"card_id\":\"$ALICE_PREPAID\",\"amount\":100}"
expect_eq "$(json decline_code)" "CARD_EXPIRED" "prepaid card is expired on the shared clock"
test_req POST "$AUTH/test/clock" '{"reset": true}'
sleep 1.2
net_req POST "$NET/v1/test/card-purchases" "{\"card_id\":\"$ALICE_PREPAID\",\"amount\":100}"
expect_eq "$(json decision)" "APPROVED" "and valid again at real time"

section "Events drained"
for _ in $(seq 1 50); do
  test_req GET "$CARD/test/quiet"
  [[ "$(json outbox_depth)" == 0 ]] && break
  sleep 0.2
done
expect_eq "$(json outbox_depth)" 0 "card-service outbox is empty (every event published)"
