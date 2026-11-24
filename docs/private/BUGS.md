# Seeded bugs (private answer key)

Every flag is `false` by default, and with every flag off CardVault behaves as the spec
says. `make bug-up BUG=<FLAG>` rebuilds and starts the stack with exactly that one flag set
to `true`, then seeds it. `GET /test/bugs` on any service shows which flags it sees.

Each section says what is wrong, then gives a repro. Run the setup block once per shell, from
the repository root, right after `make bug-up` (or after `./scripts/reset.sh &&
./scripts/seed.sh` between repros: every repro expects fresh seed data). The repros need
`curl`, `jq` and `openssl`. "Correct" is what you see with the flag off; "Bug" is what you
see with it on.

## Setup

```bash
set -a; . ./.env; set +a
AUTH=http://localhost:8001 CARD=http://localhost:8002 ENR=http://localhost:8003
WTX=http://localhost:8004 OP=http://localhost:8101 NET=http://localhost:8102 WEB=http://localhost:3000
TK="X-Test-Key: $TEST_KEY"; SK="X-Service-Key: $SERVICE_KEY_TESTS"; JS="Content-Type: application/json"
login() {
  curl -s "$AUTH/v1/auth/login" -H "$JS" \
    -d "{\"username\":\"$1\",\"password\":\"$SEED_USER_PASSWORD\"}" | jq -r .access_token
}
ALICE=$(login alice); BOB=$(login bob)
ALICE_ID=$(curl -s "$AUTH/v1/auth/me" -H "Authorization: Bearer $ALICE" | jq -r .customer_id)
ALICE_CREDIT=ca4d0000-a11c-4000-8000-000000000001   # enrolled, token below
ALICE_DEBIT=ca4d0000-a11c-4000-8000-000000000002    # enrolled
ALICE_UNUSED=ca4d0000-a11c-4000-8000-000000000003   # active, last used over 90 days ago
ALICE_ENR_CREDIT=e4400000-a11c-4000-8000-000000000001
ALICE_ENR_DEBIT=e4400000-a11c-4000-8000-000000000002
TOK_ALICE_CREDIT=tok_a11ce0c4ed170000000001
BOB_TXN=7a000000-0000-4000-8000-000000000056        # one of bob's wallet transactions
CENTS_TXN=7a000000-0000-4000-8000-0000000c0030      # alice's settled 30-cent transaction
available() { curl -s "$CARD/v1/cards/$1" -H "Authorization: Bearer $ALICE" | jq .available; }
purchase() { # TOKEN AMOUNT -> trace_id
  curl -s "$NET/v1/test/purchases" -H "$TK" -H "$JS" \
    -d "{\"token_ref\":\"$1\",\"amount\":$2,\"merchant_name\":\"Repro Shop\",\"mcc\":\"5999\"}" |
    jq -r .trace_id
}
produce() { # TOPIC JSON: publish one event straight to Kafka (uncompressed)
  echo "$2" | docker compose exec -T redpanda rpk topic produce "$1" -k "$ALICE_CREDIT" -z none >/dev/null
}
operator_record() { curl -s "$OP/test/state" -H "$TK" | jq -c ".records[] | select(.enrollment_id==\"$1\")"; }
sign() { printf '%s' "$1.$2" | openssl dgst -sha256 -hmac "$OPERATOR_HMAC_SECRET" | awk '{print $NF}'; }
callback() { # BODY [TIMESTAMP]: a callback signed with the real secret
  local ts="${2:-$(date +%s)}"
  curl -s "$ENR/v1/callbacks/operator" -H "$JS" -H "X-Timestamp: $ts" \
    -H "X-Signature: $(sign "$ts" "$1")" -d "$1"
}
cb_body() { # STATUS OPERATOR_VERSION
  printf '{"event_id":"%s","operator_ref":"x","enrollment_id":"%s","status":"%s","operator_version":%s,"occurred_at":"2026-01-01T00:00:00Z"}' \
    "$(cat /proc/sys/kernel/random/uuid)" "$ALICE_ENR_CREDIT" "$1" "$2"
}
```

## Enrolment

### BUG_INELIGIBLE_CARD_ENROLLED

The 90-day usage rule is ignored, so a card unused for months can be added to the wallet.

```bash
curl -s -X POST "$ENR/v1/wallet/cards/$ALICE_UNUSED" -H "Authorization: Bearer $ALICE" -H "$JS" \
  -d '{"terms_version":"2026-01"}' -w '\n%{http_code}\n'
```

### BUG_SUSPEND_NOT_PUSHED

Reporting a card lost does not suspend its token at the operator, so the token stays usable
on the operator's side.

```bash
curl -s -X POST "$CARD/v1/cards/$ALICE_CREDIT/report-lost" -H "Authorization: Bearer $ALICE" -o /dev/null
sleep 5
operator_record "$ALICE_ENR_CREDIT" | jq -r .status
```

### BUG_REISSUE_NOT_PUSHED

After a reissue the operator keeps the old `last4` and expiry.

```bash
curl -s -X POST "$CARD/v1/cards/$ALICE_CREDIT/report-lost" -H "Authorization: Bearer $ALICE" -o /dev/null
sleep 5
NEW=$(curl -s -X POST "$CARD/v1/cards/$ALICE_CREDIT/reissue" -H "Authorization: Bearer $ALICE" | jq -r .last4)
sleep 8
echo "bank: $NEW  operator: $(operator_record "$ALICE_ENR_CREDIT" | jq -r '.status + " " + .last4')"
```

### BUG_OPT_OUT_PARTIAL

Opting out of the wallet leaves one card enrolled.

```bash
curl -s -X POST "$ENR/v1/wallet/opt-out" -H "Authorization: Bearer $ALICE" -o /dev/null
sleep 5
curl -s "$ENR/v1/wallet" -H "Authorization: Bearer $ALICE" |
  jq -c '[.cards[] | select(.enrollment != null) | .enrollment.status]'
```

### BUG_CALLBACK_SIGNATURE_IGNORED

An operator callback with no signature is accepted.

```bash
curl -s "$ENR/v1/callbacks/operator" -H "$JS" -d "$(cb_body REMOVED 999)" -w '\n%{http_code}\n'
```

### BUG_CALLBACK_TIMESTAMP_NOT_CHECKED

A correctly signed callback whose timestamp is older than 5 minutes (a replay) is accepted.

```bash
callback "$(cb_body REMOVED 999)" "$(( $(date +%s) - 600 ))"; echo
```

### BUG_STALE_CALLBACK_OVERWRITES

A callback with an older `operator_version` overwrites a newer status. Here a late
"ENROLLED" (version 1) arrives after the lost card was suspended (version 2) and turns the
token back on.

```bash
curl -s -X POST "$CARD/v1/cards/$ALICE_CREDIT/report-lost" -H "Authorization: Bearer $ALICE" -o /dev/null
sleep 5
callback "$(cb_body ENROLLED 1)"; echo
curl -s "$ENR/v1/wallet/cards/$ALICE_CREDIT" -H "Authorization: Bearer $ALICE" | jq -r .enrollment.status
```

### BUG_DUPLICATE_CALLBACK_APPLIED_TWICE

A redelivered callback (same `event_id`) is applied again: two history rows and two
`enrollment.changed` events for one change.

```bash
curl -s -X POST "$OP/test/behaviour" -H "$TK" -H "$JS" \
  -d '{"controls":[{"type":"duplicate_callback","fail_next":1}]}' -o /dev/null
curl -s -X POST "$CARD/v1/cards/$ALICE_CREDIT/report-lost" -H "Authorization: Bearer $ALICE" -o /dev/null
sleep 6
curl -s "$ENR/test/enrollments/$ALICE_ENR_CREDIT/history" -H "$TK" |
  jq '[.[] | select(.to_status == "SUSPENDED")] | length'
```

## Authorization and clearing (card-service)

### BUG_AUTH_NOT_IDEMPOTENT

A repeated `trace_id` is decided again and takes a second hold, so available drops twice.

```bash
BEFORE=$(available "$ALICE_CREDIT")
BODY="{\"trace_id\":\"repro-dup-1\",\"card_id\":\"$ALICE_CREDIT\",\"amount\":1000,\"currency\":\"USD\",\"merchant_name\":\"Repro Shop\",\"mcc\":\"5999\",\"token_requestor\":\"CARD\"}"
for i in 1 2; do curl -s "$CARD/internal/v1/authorizations" -H "$SK" -H "$JS" -d "$BODY" | jq -c '{decision, hold_id}'; done
echo "available dropped by $(( BEFORE - $(available "$ALICE_CREDIT") ))"
```

### BUG_TOKEN_STATUS_IGNORED

The token status check is skipped, so a removed wallet token still buys.

```bash
curl -s -X DELETE "$ENR/v1/wallet/cards/$ALICE_CREDIT" -H "Authorization: Bearer $ALICE" -o /dev/null
sleep 5
curl -s "$NET/v1/test/purchases" -H "$TK" -H "$JS" \
  -d "{\"token_ref\":\"$TOK_ALICE_CREDIT\",\"amount\":500,\"merchant_name\":\"Repro Shop\",\"mcc\":\"5999\"}" |
  jq -c '{decision, decline_code}'
```

### BUG_HOLD_NOT_RELEASED_ON_REVERSAL

A full reversal does not release the hold, so the money never comes back to available.

```bash
BEFORE=$(available "$ALICE_CREDIT")
TR=$(purchase "$TOK_ALICE_CREDIT" 2500)
curl -s -X POST "$NET/v1/test/purchases/$TR/reverse" -H "$TK" -H "$JS" -d '{}' -o /dev/null
echo "available is short by $(( BEFORE - $(available "$ALICE_CREDIT") ))"
```

### BUG_SETTLE_USES_AUTH_AMOUNT

Settlement charges the authorized amount and ignores the amount that actually cleared.

```bash
BEFORE=$(available "$ALICE_CREDIT")
TR=$(purchase "$TOK_ALICE_CREDIT" 2000)
curl -s -X POST "$NET/v1/test/purchases/$TR/settle" -H "$TK" -H "$JS" -d '{"amount":1500}' -o /dev/null
echo "charged $(( BEFORE - $(available "$ALICE_CREDIT") )) for a 1500 clearing"
```

### BUG_FLOAT_MONEY

Hold maths is done in float dollars and truncated to cents. The seed has the shape that
shows it (the 30-cent transaction, settled as 10 + 20 cents); this repro replays it live:
authorize 30, reverse 20, settle 10. 0.30 - 0.20 is 0.09999999999999998 in floating point,
so 9 cents stay held instead of 10, and the card is charged 11 cents for a 10-cent clearing.

```bash
BEFORE=$(available "$ALICE_CREDIT")
TR=$(purchase "$TOK_ALICE_CREDIT" 30)
curl -s -X POST "$NET/v1/test/purchases/$TR/reverse" -H "$TK" -H "$JS" -d '{"amount":20}' | jq -c '{hold_amount}'
curl -s -X POST "$NET/v1/test/purchases/$TR/settle" -H "$TK" -H "$JS" -d '{"amount":10}' -o /dev/null
echo "charged $(( BEFORE - $(available "$ALICE_CREDIT") )) cents for 10 cents settled"
```

### BUG_EVENT_PUBLISHED_WITHOUT_COMMIT

card-service publishes `card.lifecycle` straight to Kafka inside the request instead of
writing an outbox row. One failed publish loses the event for good, so the wallet never
hears that the card was lost.

```bash
curl -s -X POST "$CARD/test/faults" -H "$TK" -H "$JS" \
  -d '{"faults":[{"type":"kafka_publish_failure","fail_next":1,"scope":{"topic":"card.lifecycle"}}]}' -o /dev/null
curl -s -X POST "$CARD/v1/cards/$ALICE_CREDIT/report-lost" -H "Authorization: Bearer $ALICE" -o /dev/null
sleep 8
curl -s "$ENR/v1/wallet/cards/$ALICE_CREDIT" -H "Authorization: Bearer $ALICE" | jq -r .enrollment.status
```

## Wallet transactions (wallet-txn-service)

### BUG_TXN_DUPLICATE_ON_REDELIVERY

The consumer is not idempotent: a redelivered `txn.cleared` is applied again, so a refund
counts twice.

```bash
TR=$(purchase "$TOK_ALICE_CREDIT" 4000)
curl -s -X POST "$NET/v1/test/purchases/$TR/settle" -H "$TK" -H "$JS" -d '{}' -o /dev/null
sleep 2
AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EV="{\"event_id\":\"$(cat /proc/sys/kernel/random/uuid)\",\"event_type\":\"txn.cleared\",\"version\":1,\"occurred_at\":\"$AT\",\"correlation_id\":\"repro\",\"payload\":{\"clearing_id\":\"repro-dup\",\"trace_id\":\"$TR\",\"type\":\"REFUND\",\"amount\":100,\"card_id\":\"$ALICE_CREDIT\",\"customer_id\":\"$ALICE_ID\",\"token_ref\":\"$TOK_ALICE_CREDIT\",\"token_requestor\":\"WALLET\",\"currency\":\"USD\",\"cleared_at\":\"$AT\"}}"
produce txn.cleared "$EV"; produce txn.cleared "$EV"   # the same event twice
sleep 3
curl -s "$WTX/test/transactions/$TR" -H "$TK" | jq .refunded_amount
```

### BUG_CLEARED_BEFORE_AUTH_LOST

A clearing that arrives before its `auth.decided` is dropped, so the transaction never
settles in the wallet. card-service's outbox keeps the two events in order per card, so the
repro publishes them by hand, clearing first.

```bash
AT=$(date -u +%Y-%m-%dT%H:%M:%SZ); TR="repro-early-$RANDOM"
produce txn.cleared "{\"event_id\":\"$(cat /proc/sys/kernel/random/uuid)\",\"event_type\":\"txn.cleared\",\"version\":1,\"occurred_at\":\"$AT\",\"correlation_id\":\"repro\",\"payload\":{\"clearing_id\":\"$TR-c\",\"trace_id\":\"$TR\",\"type\":\"SETTLE\",\"amount\":777,\"card_id\":\"$ALICE_CREDIT\",\"customer_id\":\"$ALICE_ID\",\"token_ref\":\"$TOK_ALICE_CREDIT\",\"token_requestor\":\"WALLET\",\"currency\":\"USD\",\"cleared_at\":\"$AT\"}}"
sleep 1
produce auth.decided "{\"event_id\":\"$(cat /proc/sys/kernel/random/uuid)\",\"event_type\":\"auth.decided\",\"version\":1,\"occurred_at\":\"$AT\",\"correlation_id\":\"repro\",\"payload\":{\"trace_id\":\"$TR\",\"card_id\":\"$ALICE_CREDIT\",\"customer_id\":\"$ALICE_ID\",\"token_ref\":\"$TOK_ALICE_CREDIT\",\"token_requestor\":\"WALLET\",\"decision\":\"APPROVED\",\"decline_code\":null,\"amount\":777,\"currency\":\"USD\",\"merchant_name\":\"Repro Shop\",\"mcc\":\"5999\",\"decided_at\":\"$AT\"}}"
sleep 2
curl -s "$WTX/test/transactions/$TR" -H "$TK" | jq -c '{status, auth_amount, cleared_amount}'
```

### BUG_CSV_FORMULA_INJECTION

The export does not neutralise cells that a spreadsheet would run as a formula.

```bash
curl -s "$WTX/v1/wallet/transactions/export" -H "Authorization: Bearer $ALICE" | grep -o "[,']*=HYPERLINK[^,]*" | head -1
```

## Reconciliation

### BUG_RECON_MISSES_STATUS_MISMATCH

Reconciliation does not report a token whose status differs between the bank and the
operator.

```bash
curl -s -X POST "$OP/test/state" -H "$TK" -H "$JS" \
  -d "{\"upsert\":[{\"enrollment_id\":\"$ALICE_ENR_DEBIT\",\"status\":\"SUSPENDED\"}]}" -o /dev/null
curl -s -X POST "$ENR/internal/v1/reconciliation/runs" -H "$SK" -H "$JS" -d '{}' |
  jq -c '{finding_count, types: [.findings[].type]}'
```

## Security

### BUG_CROSS_CUSTOMER_WALLET_READ

`GET /v1/wallet/transactions/{id}` is not scoped to the caller: alice can read bob's
transaction.

```bash
curl -s "$WTX/v1/wallet/transactions/$BOB_TXN" -H "Authorization: Bearer $ALICE" -w '\n%{http_code}\n'
```

### BUG_PAN_IN_LOGS

card-service logs the full card number when a card is reissued.

```bash
curl -s -X POST "$CARD/v1/cards/$ALICE_CREDIT/report-lost" -H "Authorization: Bearer $ALICE" -o /dev/null
curl -s -X POST "$CARD/v1/cards/$ALICE_CREDIT/reissue" -H "Authorization: Bearer $ALICE" -o /dev/null
docker compose logs card | grep -E '[0-9]{16}' | tail -1   # an unmasked card number
```

### BUG_IDEMPOTENCY_KEY_NOT_SCOPED

Idempotency keys are not scoped to the caller: bob reusing a key alice used gets alice's
stored response, and his own request never runs.

```bash
curl -s -X POST "$ENR/v1/wallet/opt-out" -H "Authorization: Bearer $ALICE" -H "Idempotency-Key: repro-shared-1" -o /dev/null
curl -s -X POST "$ENR/v1/wallet/opt-out" -H "Authorization: Bearer $BOB" -H "Idempotency-Key: repro-shared-1" -D - -o /tmp/bob-optout.json | grep -i idempotent-replayed
jq -r .customer_id /tmp/bob-optout.json
```

## Disputes and the web app

### BUG_DISPUTE_FILE_TYPE_BY_NAME

The receipt type is taken from the file name, so a text file named `.pdf` is accepted.

```bash
printf 'just text' > /tmp/note.pdf
curl -s "$WTX/v1/wallet/transactions/$CENTS_TXN/disputes" -H "Authorization: Bearer $ALICE" \
  -F reason=WRONG_AMOUNT -F "receipt=@/tmp/note.pdf" -w '\n%{http_code}\n'
```

The four UI bugs show only in a browser. For each, the curl confirms the web app sees the
flag, then the steps show the misbehaviour (sign in at http://localhost:3000 as alice with
`SEED_USER_PASSWORD`).

```bash
curl -s "$WEB/login" | grep -o 'BUG_UI_[A-Z_]*' | sort -u
```

### BUG_UI_UNESCAPED_MERCHANT

Merchant names are rendered as HTML (XSS). Open Wallet, then Transactions: the seeded
merchant `<img src=x onerror=alert('cv-xss')>... Bistro` runs its `onerror` handler (an alert
says `cv-xss`) and shows as a broken image instead of literal text. Correct: the name shows
as plain text and nothing runs.

### BUG_UI_PENDING_NEVER_REFRESHES

The wallet page never polls a PENDING card. Open Wallet, opt out, then opt in again
(accept the terms in the dialog): the badges stay PENDING until you reload, although the
server already says ENROLLED. Correct: they turn ENROLLED by themselves a few seconds later.

### BUG_UI_DOUBLE_SUBMIT

The dispute submit button stays enabled while the request is in flight. Open a settled
transaction with no dispute, pick a reason and double-click "Open dispute": the network log
shows two `POST .../disputes` requests and the second gets 409 `DISPUTE_ALREADY_OPEN` (an
error toast appears next to the success toast). Correct: the button disables after the first
click and one request is sent.

### BUG_UI_TIMEZONE_UTC

Times are shown in UTC regardless of the browser's timezone. With the browser in a timezone
other than UTC (for example `America/New_York`), the transaction list shows times 4 or 5
hours off from the local clock. Correct: times are in the browser's timezone.
