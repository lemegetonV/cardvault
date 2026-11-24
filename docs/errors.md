# Error catalog

Every error response has this shape:

```json
{"error_code": "INVALID_CREDENTIALS", "message": "Username or password is incorrect",
 "correlation_id": "6f1c...", "details": {}}
```

`details` is present only when it adds something (validation errors, lockout end time).
The list grows with each phase.

## Common (every service)

| error_code | HTTP | When |
|---|---|---|
| `VALIDATION_ERROR` | 422 | Request body, query or path failed validation. `details` lists `loc`, `msg`, `type` |
| `UNAUTHORIZED` | 401 | A customer endpoint was called without a bearer token |
| `TOKEN_INVALID` | 401 | Bearer token malformed, wrong signature, issuer, audience or unknown `kid` |
| `TOKEN_EXPIRED` | 401 | Access token `exp` is in the past on the shared clock |
| `SERVICE_KEY_INVALID` | 401 | Internal endpoint called without a valid `X-Service-Key` |
| `TEST_KEY_INVALID` | 401 | `/test/*` called without a valid `X-Test-Key` |
| `NOT_FOUND` | 404 | No such route or resource |
| `INVALID_CURSOR` | 400 | Pagination cursor is not one this service issued |
| `IDEMPOTENCY_KEY_REUSED` | 422 | The `Idempotency-Key` was already used for a different request (method, path or body) |
| `IDEMPOTENCY_IN_PROGRESS` | 409 | The first request with this `Idempotency-Key` is still running |
| `METHOD_NOT_ALLOWED` | 405 | Wrong HTTP method for the route |
| `INTERNAL_ERROR` | 500 | Unexpected server error |
| `FAULT_INJECTED` | 5xx | An injected `error` fault fired (status as configured, default 503) |
| `FAULT_TIMEOUT` | 504 | An injected `timeout` fault fired |

## Callback signatures

| error_code | HTTP | When |
|---|---|---|
| `SIGNATURE_MISSING` | 401 | `X-Signature` or `X-Timestamp` missing |
| `SIGNATURE_INVALID` | 401 | Signature does not match, or `X-Timestamp` is not Unix seconds |
| `SIGNATURE_EXPIRED` | 401 | `X-Timestamp` more than 300 s from the shared clock |

The same three codes are returned by the wallet operator mock when a request from the bank
(including the GET of the daily enrolment file) is not signed correctly.

## auth-service

| error_code | HTTP | When |
|---|---|---|
| `INVALID_CREDENTIALS` | 401 | Unknown username or wrong password (deliberately the same) |
| `ACCOUNT_LOCKED` | 423 | 5 failed logins for this username in a row; locked 15 minutes. `details.locked_until` |
| `REFRESH_TOKEN_INVALID` | 401 | Unknown or revoked refresh token |
| `REFRESH_TOKEN_REUSED` | 401 | An already-used refresh token was presented; the whole family is now revoked |
| `REFRESH_TOKEN_EXPIRED` | 401 | Refresh token older than 7 days on the shared clock |
| `USER_EXISTS` | 409 | `POST /test/users` with a username or customer_id that exists |
| `SEED_CONFIG_MISSING` | 500 | `POST /test/seed` without `SEED_USER_PASSWORD` configured |

## card-service

| error_code | HTTP | When |
|---|---|---|
| `CARD_NOT_FOUND` | 404 | No such card, or the card belongs to another customer (deliberately the same) |
| `INVALID_STATE_TRANSITION` | 409 | Lifecycle action not allowed from the card's status (e.g. reissue a CLOSED card). `details.status`, `details.action` |
| `TRACE_NOT_FOUND` | 404 | Clearing for a `trace_id` with no authorization |
| `AUTH_DECLINED` | 409 | Clearing for a declined authorization |
| `ALREADY_SETTLED` | 409 | A second SETTLE, or a REVERSE after SETTLE (refund instead) |
| `HOLD_RELEASED` | 409 | SETTLE or REVERSE after the hold was fully reversed |
| `NOT_SETTLED` | 409 | REFUND before SETTLE |
| `OVER_SETTLE_NOT_ALLOWED` | 422 | SETTLE above the held amount on a DEBIT or PREPAID card |
| `AMOUNT_EXCEEDS_HOLD` | 422 | REVERSE larger than what is still held |
| `REFUND_EXCEEDS_CLEARED` | 422 | REFUND larger than cleared minus already refunded. `details.refundable` |
| `CLEARING_ID_REUSED` | 422 | Same `clearing_id` with a different body |

Authorization declines are not errors: they are `200` with `decision: DECLINED` and one of
`CARD_LOST`, `CARD_CLOSED`, `CARD_EXPIRED`, `TOKEN_MISMATCH`, `TOKEN_INACTIVE`,
`INSUFFICIENT_FUNDS` in `decline_code`.

## network-mock

| error_code | HTTP | When |
|---|---|---|
| `TOKEN_UNKNOWN` | 404 | Purchase with a `token_ref` the network never issued |
| `TRACE_UNKNOWN` | 404 | Settle, reverse or refund for a `trace_id` the network never sent |

Other network-mock responses pass card-service's status and body through unchanged.

## profile-service

| error_code | HTTP | When |
|---|---|---|
| `PROFILE_NOT_FOUND` | 404 | The customer has no profile |
| `INVALID_IF_MATCH` | 400 | `If-Match` is not a version number |
| `VERSION_MISMATCH` | 412 | `If-Match` differs from the current version. `details.current_version` |
| `CONTACT_MISSING` | 422 | Verifying a channel with no email or phone set |
| `ALREADY_VERIFIED` | 409 | Asking for a code for a channel that is already verified |
| `OTP_COOLDOWN` | 429 | A code was sent less than 30 s ago. `details.retry_after_seconds` |
| `OTP_INVALID` | 422 | Wrong code. `details.attempts_remaining` |
| `OTP_LOCKED` | 423 | Fifth wrong code, or a code while locked (15 minutes). `details.locked_until` |
| `PROFILE_EXISTS` | 409 | `POST /test/profiles` for a customer that has a profile |

`PATCH /v1/profile` validation errors (email shape, phone not E.164) are `VALIDATION_ERROR`.

## enrollment-service

| error_code | HTTP | When |
|---|---|---|
| `CARD_NOT_FOUND` | 404 | No such card in the wallet, or another customer's card (deliberately the same) |
| `WALLET_OPTED_OUT` | 409 | Adding a card while opted out (opt in first) |
| `CARD_INELIGIBLE` | 422 | Adding an ineligible card. `details.reason` is the eligibility reason code |
| `ENROLLMENT_NOT_FOUND` | 404 | Operator callback for an unknown `enrollment_id` |
| `ORCHESTRATION_FAILED` | 502 | `POST /test/customers`: a call to another service failed. `details.upstream` |

| `RUN_NOT_FOUND` | 404 | `GET /internal/v1/reconciliation/runs/{id}` for an unknown run |
| `OPERATOR_FILE_UNAVAILABLE` | 502 | Reconciliation could not fetch the operator's file; the run is stored as FAILED. `details.run_id` |

A missing `terms_version` on opt-in or add-card is `VALIDATION_ERROR` (422).

Eligibility reason codes (in priority order; the first failing one is shown):
`CARD_NOT_ACTIVE`, `CARD_EXPIRED`, `PRODUCT_NOT_SUPPORTED`, `NOT_RECENTLY_USED`,
`CUSTOMER_OPTED_OUT`, `CARD_REMOVED_BY_CUSTOMER`, `EMAIL_NOT_VERIFIED`, `PHONE_NOT_VERIFIED`.

Operator callbacks that are acknowledged but change nothing are not errors: they return 200
with `result` set to `DUPLICATE`, `STALE`, `SUPERSEDED`, `IGNORED_REMOVED` or
`FAILURE_RECORDED` (`APPLIED` when the callback took effect).

## wallet-txn-service

| error_code | HTTP | When |
|---|---|---|
| `TRANSACTION_NOT_FOUND` | 404 | No such transaction, or another customer's (deliberately the same) |
| `DISPUTE_NOT_FOUND` | 404 | No such dispute, or another customer's |
| `TRANSACTION_NOT_DISPUTABLE` | 422 | Disputing a DECLINED, REVERSED or REFUNDED transaction. `details.status` |
| `DISPUTE_ALREADY_OPEN` | 409 | The transaction already has an OPEN dispute. `details.dispute_id` when known |
| `RECEIPT_TYPE_NOT_ALLOWED` | 422 | The receipt's content is not PDF, PNG or JPEG (its name does not matter) |
| `RECEIPT_TOO_LARGE` | 422 | The receipt is over 5,242,880 bytes |
| `INVALID_CURSOR` | 400 | An unreadable `cursor` |

A `from`/`to` that is not a date or a datetime with an offset, `from` after `to`, an unknown
`status`, an unknown dispute `reason` or a note over 500 characters is `VALIDATION_ERROR` (422).
`Idempotency-Key` errors are the common ones (`IDEMPOTENCY_KEY_REUSED`, `IDEMPOTENCY_IN_PROGRESS`).

## wallet-operator-mock

| error_code | HTTP | When |
|---|---|---|
| `IDEMPOTENCY_KEY_REQUIRED` | 422 | A request from the bank without `Idempotency-Key` |
| `IDEMPOTENCY_KEY_REUSED` | 409 | The same key with a different request (the bank treats 409 as already done) |
| `ENROLLMENT_UNKNOWN` | 404 | No record with this `operator_ref` |
| `ENROLLMENT_REMOVED` | 422 | Update, suspend or resume on a removed record |
| `OPERATOR_UNAVAILABLE` | 5xx | The `error` control fired (nothing was applied) |
| `OPERATOR_TIMEOUT` | 504 | The `timeout` control fired, after hanging (nothing was applied) |
