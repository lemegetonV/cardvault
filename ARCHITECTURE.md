# CardVault architecture

CardVault simulates one bank's side of a bank-backed online checkout wallet. It is an
application under test: every decision below favours production-like behaviour and
testability over features. This file records each decision and its tradeoff. It grows with
each build phase; sections for later phases are marked as such.

Status: **phase 2** (platform, auth-service, card-service, network-mock, CI).

## 1. Services and responsibilities

| Component | Port | Responsibility | Phase |
|---|---|---|---|
| auth-service | 8001 | Login with lockout, RS256 access tokens, rotating refresh tokens, JWKS | 1 |
| card-service | 8002 | Card master, authorization decisions, holds, clearing | 2 |
| enrollment-service | 8003 | Wallet enrolment system of record, eligibility, operator pushes, callbacks, reconciliation | 3 |
| wallet-txn-service | 8004 | Wallet transaction system of record, disputes, CSV export | 4 |
| profile-service | 8005 | Contact details and verification | 3 |
| web | 3000 | Next.js UI and BFF | 5 |
| wallet-operator-mock | 8101 | External wallet operator (controllable) | 3 |
| network-mock | 8102 | Card network and merchant simulator (controllable) | 2 |
| postgres | 5432 | One Postgres 16 instance, one database per service | 1 |
| redpanda | 19092 (host) | Kafka API | 1 |
| console | 8080 | Redpanda Console (local only, not in CI) | 1 |

Port 8006 is reserved for the future agent service.

## 2. Repository and build

**One Python project for all services (`app/pyproject.toml`, one `uv.lock`).** Each
service is a package (`services.auth`, later `services.card`, ...) and imports the
`shared` package directly.
*Tradeoff:* every service carries the same dependency set, and a dependency bump touches all
of them at once. In return there is one lock file, one dependency layer shared by every
image (so CI builds the heavy layer once), and no publishing step for `shared`. For a
handful of small services owned by one team this is the right call; independent release
cadences would argue for per-service projects.

**One Dockerfile for every Python service (`app/Dockerfile`, `ARG SERVICE`).** The
dependency layer is installed before any code is copied, so code changes rebuild in seconds.
The container entrypoint (`shared/entrypoint.py`) runs that service's Alembic migrations,
then starts uvicorn. Images run as a non-root user.
*Tradeoff:* migrations at start-up are simple and keep `docker compose up` a single step,
but they would race with more than one replica. Each service runs one replica here.

**Relays and consumers run in-process** as asyncio background tasks, not as extra containers
(spec 16.10 rule 66).
*Tradeoff:* fewer containers fit the 2 vCPU runner; the cost is that a slow relay shares a
process with the API. `/test/quiet` exposes outbox depth and consumer lag, so tests can wait
on them.

## 3. Data

**One Postgres container, one database per service.** Each database has an owner role
(`<db>_svc`, used by the service) and a read-only role (`<db>_ro`, for tests); default
privileges give the read-only role `SELECT` on every table the owner creates. Created once by
`app/infra/postgres/01-databases.sh`.
*Tradeoff:* a real deployment would use separate instances. Separate databases still give
the property that matters for testing: no service can join across another's tables, so all
cross-service data flows through APIs and events.

**Schema migrations: Alembic per service**, hand-written, in `services/<name>/migrations`.

**Money** is integer cents plus `currency: "USD"` everywhere. **Time** is UTC, ISO-8601.

## 4. Cross-cutting platform (`app/shared`)

| Module | What it does |
|---|---|
| `runtime.py` | `create_app()`: logging, middleware, error handlers, `/health`, `/ready`, `/test/*`, lifecycle of DB, Kafka, relays and consumers |
| `correlation.py`, `middleware.py` | Accept or generate `X-Correlation-ID`, echo it, keep it in a context variable; access log line per request; last-resort 500 in the common shape |
| `logging.py` | JSON log lines with `correlation_id`; masks PAN-like digit runs, JWTs and token references |
| `errors.py` | One error shape: `{error_code, message, correlation_id, details?}` |
| `clock.py` | The shared clock (section 6) |
| `security.py` | JWT verification via JWKS, service keys, test key |
| `hmac_auth.py` | HMAC-SHA256 signing and verification for operator callbacks |
| `events/` | Envelope, topic names, payload models, JSON Schema export |
| `kafka.py` | Topic creation, producer, consumer runner with retries and DLQ |
| `outbox.py` | Transactional outbox table and relay |
| `faults.py` | Runtime fault injection |
| `bugs.py` | Seeded-bug flags (all off by default) |
| `testsupport.py` | `/test/reset`, `/test/seed`, `/test/faults`, `/test/clock`, `/test/quiet` |
| `idempotency.py` | `Idempotency-Key` storage and replay (section 9) |
| `pagination.py` | Keyset cursor pagination (section 9) |
| `http_client.py` | httpx client that always sends the correlation ID (and a service key) |
| `seed_ids.py`, `seed_data.py` | Fixed customer, card, enrolment and token IDs shared by every service and mock |

**Correlation IDs.** The middleware is plain ASGI rather than Starlette's
`BaseHTTPMiddleware`, so the context variable it sets is the one the handler sees and
streaming responses are not buffered. Incoming IDs are accepted only if they match
`[A-Za-z0-9._:-]{1,128}`; anything else is replaced, so a caller cannot inject text into logs.
The relay restores the ID stored with each outbox row, and the consumer restores the ID
from each event envelope, so one ID follows a request across services and Kafka hops.

**Errors.** FastAPI validation errors are converted to `422 VALIDATION_ERROR` with
`details` listing `loc`, `msg` and `type` only. The raw `input` is dropped because it can
echo secrets (a password) back to the caller. Every code is listed in `docs/errors.md`.

**Log redaction in the formatter.** PAN-like digit runs (13 to 19 digits), JWTs and
`tok_...` references are masked when the line is formatted, not at each call site.
*Tradeoff:* a regex can mask a harmless long number. That is acceptable: a false positive
costs a less useful log line, a false negative leaks card data.

## 5. Authentication

### Customers (auth-service)

- **Access tokens:** RS256 JWTs, 15 minutes. Claims: `sub` (customer_id), `iss`,
  `aud=cardvault`, `exp`, `iat`, `jti`; `kid` in the header.
- **Verification is local in every service** against the JWKS from auth-service, cached by
  `kid` and re-fetched when an unknown `kid` appears (at most every 5 s).
  *Tradeoff:* no network call per request, and auth-service is not a single point of
  failure for reads. The cost is that an access token cannot be revoked before it expires;
  the 15-minute lifetime bounds that window. auth-service verifies its own tokens from memory.
- **`exp` is checked against the shared clock**, not the wall clock: PyJWT's own time checks
  are disabled and replaced, so tests can expire a token by moving the clock (smoke does this).
- **Signing key:** generated on first start and stored in auth's database.
  *Tradeoff:* no private key is ever committed, and tokens survive restarts. A production
  system would keep the key in a KMS/HSM and rotate it; the JWKS-by-`kid` design already
  allows rotation.
- **Refresh tokens:** opaque random strings, 7 days, stored only as SHA-256 hashes. Each
  login starts a *family*; every refresh marks the presented token used and issues a new
  one in the same family. **Presenting a used token revokes the whole family**
  (`REFRESH_TOKEN_REUSED`), because it means two parties hold the same token.
  *Tradeoff:* a client that retries a refresh after a lost response logs itself out. That is
  the accepted cost of theft detection.
- **Logout** revokes the token's family and always returns 204, even for an unknown token,
  so it cannot be used to test tokens.
- **Lockout:** per username, 5 failures lock it for 15 minutes on the shared clock
  (`423 ACCOUNT_LOCKED`). Unknown usernames are counted too, and an unknown username and a
  wrong password both return `401 INVALID_CREDENTIALS`. A dummy hash is verified for unknown
  users so both paths take the same time. The counter row is locked (`SELECT ... FOR
  UPDATE`) so concurrent attempts are counted exactly, and the failure is committed before
  the 401 is raised.
  *Tradeoff:* per-username lockout lets anyone lock a known username (a denial of service on
  that account). Per-IP throttling would be the usual companion; it is out of scope here.
- **Passwords:** stdlib `hashlib.scrypt` (n=2^14, r=8, p=1), run in a worker thread.
  *Tradeoff:* avoids a dependency; argon2id would be the first choice where a library is fine.

### Services and tests

- **Service keys:** one `X-Service-Key` per caller (network-mock, wallet-operator-mock,
  tests), compared in constant time. Internal endpoints reject customer JWTs and customer
  endpoints reject service keys, both with 401. card-service's internal API accepts the
  network-mock and tests keys only.
- **Test controls** are mounted only when `TEST_MODE=true` (otherwise the routes do not exist,
  so they return 404) and need `X-Test-Key`.

### Operator callbacks (used from phase 3)

`X-Timestamp` is Unix seconds; the signed string is `"{timestamp}.{raw_body}"`; `X-Signature`
is lowercase hex HMAC-SHA256. The signature is checked before the timestamp window, so a
forged request learns nothing about the window. A timestamp more than 300 s away from the
shared clock (past or future) is rejected; exactly 300 s is accepted. Replay inside the
window is stopped by `event_id` deduplication, and stale `operator_version`s are ignored.

### 404 versus 403 for another customer's resource

A customer asking for another customer's resource gets **404**, the same as for a resource
that does not exist. A 403 would confirm that the ID exists and let an attacker enumerate
IDs. *Tradeoff:* support staff lose the distinction in logs; it is kept in the server log.

## 6. The shared clock

Business rules (eligibility, expiry, lockout, JWT expiry, OTP cooldown, callback timestamps,
reconciliation dates) read time only from `shared.clock.now()`.

- Production mode: real UTC.
- TEST_MODE: real UTC plus an offset stored in **one row** of the `testsupport` database. A
  background task in each process re-reads it every 0.5 s, so a cached offset is at most
  ~1 s old. `now()` stays synchronous with no I/O per call.
- `POST /test/clock` on any service writes the row and updates that service's cache before
  returning; other services follow within 1 s. `GET /test/clock` on each service shows its
  effective time. `make reset` clears the offset.
- Relay backoff, consumer retries, mock delays and UI polling use **real** time, so moving
  the clock never stalls the system.

*Tradeoff:* a DB row instead of a broadcast is simpler and survives restarts; the price is a
bounded 1 s lag between services, which tests absorb by reading `GET /test/clock`.

## 7. Events, outbox and delivery

**Envelope:** `{event_id, event_type, version, occurred_at, correlation_id, payload}`;
`event_type` is the topic name. Pydantic models in `shared/events`; JSON Schemas exported to
`docs/events/`. CI regenerates them and fails if they differ from the committed files.

**Topics** (3 partitions each): `card.lifecycle`, `contact.updated`, `enrollment.changed`,
`auth.decided`, `txn.cleared`, DLQs `enrollment.dlq` and `wallet.txn.dlq`. All keyed by
`card_id` except `contact.updated` (keyed by `customer_id`), so per-card order holds within a
partition. Services create topics at start-up (idempotent), so there is no init container.

**Transactional outbox.** A service writes its business rows and an `outbox` row in one
transaction; an event, or an outbound operator call, exists if and only if the commit
succeeds. The relay:

- picks only the **oldest pending row per key** (`DISTINCT ON (key)`), so a failing event
  blocks later events for the same card instead of being overtaken;
- locks rows with `FOR UPDATE SKIP LOCKED`, so a second relay would not double-send;
- marks a row published after the broker acknowledges it (`acks=all`). A crash between the
  two re-sends it: delivery is **at-least-once** and every consumer dedupes by `event_id`;
- backs off in real time (`base * factor^(n-1)`, capped; from env, with fast defaults in
  TEST_MODE). Event rows retry forever; operator calls (phase 3) stop after 5 attempts and
  run a give-up hook in the same transaction.

*Tradeoff:* polling adds up to 250 ms latency (the API also wakes the relay right after a
commit); change-data-capture would remove the poll but adds infrastructure.

**Consumers** commit offsets only after the handler succeeds. Malformed events (schema
validation fails or unknown envelope `version`) are retried 3 times, then sent to the DLQ
with the source topic, partition, offset and error as headers. Other errors retry with
backoff and block the partition, which preserves per-key order at the cost of
head-of-line blocking. The consumer loop logs and survives any error, because a consumer
task that dies quietly looks exactly like an idle one.

The relay, a forced publish failure (`fail_next: 2`), per-key ordering and DLQ routing were
exercised against the running Redpanda during phase 1.

## 8. Card-service (phase 2)

### Card data

- The full card number (PAN) exists only in card-service, encrypted with **AES-256-GCM**
  (`CARD_PAN_KEY`, 32 bytes). The `card_id` is bound as associated data, so a ciphertext
  copied to another row does not decrypt. APIs return `last4` and `expiry`, never the PAN.
  *Tradeoff:* the key sits in an env var; production would use a KMS with envelope
  encryption and key rotation (the `v1:` prefix leaves room for a new scheme).
- Seed and test PANs use the fictional prefix `990000` and a valid Luhn digit.
- `limit` and `available` are integer cents. `available` may go negative only on CREDIT
  (an over-settle).

### Card lifecycle (spec 16.1 rules 1 to 3)

| Action | From | To | Events |
|---|---|---|---|
| create (seed, `/test/cards`) | | ACTIVE (or as given) | `CARD_ACTIVATED` |
| report lost | ACTIVE | LOST | `CARD_LOST` |
| report lost | LOST | LOST (200, no change) | none |
| reissue | ACTIVE or LOST | ACTIVE, new PAN, last4, expiry; same `card_id` | `CARD_ACTIVATED`, `CARD_REISSUED` |
| close | ACTIVE or LOST | CLOSED | `CARD_CLOSED` |
| close | CLOSED | CLOSED (200, no change) | none |
| anything else | | | 409 `INVALID_STATE_TRANSITION` |

Repeating a destructive action returns 200 with the current state, so retries (including a
future agent's) are safe even without an `Idempotency-Key`.

### Authorization

`POST /internal/v1/authorizations` (network-mock or tests key). Checked in this order, the
first failing check wins:

| # | Check | Result |
|---|---|---|
| 1 | Card LOST | `DECLINED / CARD_LOST` |
| 2 | Card CLOSED | `DECLINED / CARD_CLOSED` |
| 3 | Expired on the shared clock (00:00 UTC on the 1st of the month after expiry) | `DECLINED / CARD_EXPIRED` |
| 4 | WALLET only: `token_ref` not mapped to this `card_id` | `DECLINED / TOKEN_MISMATCH` |
| 5 | WALLET only: token not ENROLLED in the local copy | `DECLINED / TOKEN_INACTIVE` |
| 6 | Amount above available | `DECLINED / INSUFFICIENT_FUNDS` |
| | Otherwise | `APPROVED`: hold created, available reduced, `last_used_at` set |

Malformed requests (amount <= 0, currency not USD, missing fields, WALLET without a token)
return 422; an unknown card returns 404. Every decision publishes `auth.decided`; an
approval also publishes `card.lifecycle CARD_USED`. CARD purchases (`token_requestor=CARD`,
no token) skip checks 4 and 5.

**Idempotent by `trace_id`.** The decision is stored with the trace; a duplicate returns the
stored decision and creates no second hold. The card row is locked (`SELECT ... FOR
UPDATE`) before deciding, and the stored decision is checked again after the lock, so two
concurrent requests with one `trace_id` cannot both take a hold. The same `trace_id` with a
different body returns the original decision (the network owns trace IDs).

**Eventually consistent token status.** card-service does not call enrollment-service while
authorizing. It keeps a local `wallet_tokens` table fed by `enrollment.changed`. Each row
carries the enrolment `version` and only moves forward, so redelivered or out-of-order events
are harmless; when an enrolment gets a new token, its other tokens become REMOVED.
*Tradeoff:* authorization stays fast and works when enrollment-service is down, but for a
short window after a suspend or remove a token can still be approved. A real issuer accepts
this window for availability; tests can measure it with `/test/quiet`.

### Hold maths (spec 16.4 rule 25)

| Record | Preconditions | Effect on hold | Effect on available |
|---|---|---|---|
| SETTLE `x` | approved, not settled, hold not fully reversed; `x` above the hold only for CREDIT (else 422) | status SETTLED | `+ hold_remaining - x` |
| REVERSE `x` | not settled (else 409, refund instead); `x` <= hold remaining (else 422) | amount `- x`; RELEASED at 0, otherwise stays ACTIVE | `+ x` |
| REFUND `x` | settled (else 409); `x` <= cleared - refunded (else 422) | none | `+ x`, capped at `limit` for DEBIT and PREPAID |

Other rules: one SETTLE per trace (409 `ALREADY_SETTLED`); SETTLE after a partial reverse
settles against what is still held; clearing for an unknown trace is 404 and for a declined
one 409. Clearing is idempotent by `clearing_id`; the same ID with a different body is 422
`CLEARING_ID_REUSED`. Every record publishes `txn.cleared`. Holds never expire (known
limitation).

Locks are always taken in the order card row, then authorization row, then hold, in both
authorization and clearing, so concurrent traffic on one card serialises without deadlocks.

## 9. Idempotency keys and pagination

**`Idempotency-Key`** (`shared/idempotency.py`, spec 16.5 rule 32) is optional on every
state-changing POST. Its scope is the caller (customer ID or service name) plus the route
template. The stored fingerprint is the method, the concrete path and the body, so reusing a
key on another card is 422 `IDEMPOTENCY_KEY_REUSED`. A key whose first request is running is
409 `IDEMPOTENCY_IN_PROGRESS`. A finished key replays the exact stored bytes and status with
`Idempotent-Replayed: true`; 4xx answers are stored too, 5xx are not (so the client can
retry). Keys last 24 hours on the shared clock.
*Tradeoff:* the key row and the business change commit in separate transactions. A crash in
between leaves the key IN_PROGRESS; after 60 s it is treated as abandoned and the next
request runs again. That is safe here because the guarded actions are themselves idempotent
by state (repeating report-lost is a no-op).

**Pagination** (`shared/pagination.py`): newest first, ties broken by id, default 20, maximum
100, opaque base64 cursor of the last item's `(created_at, id)`, keyset query (no OFFSET, so
pages stay stable while rows are added). A bad cursor is 400 `INVALID_CURSOR`.

## 10. network-mock (phase 2)

A controllable card network. `POST /v1/tokens` (wallet-operator-mock key) maps a new
`token_ref` to a `card_id`. Tests drive `POST /v1/test/purchases` (wallet token),
`/v1/test/card-purchases` (plain card) and `/v1/test/purchases/{trace_id}/settle|reverse|refund`,
all with `X-Test-Key`. Each purchase gets a new `trace_id`; each clearing record a new
`clearing_id`. Responses from card-service are passed through unchanged.

Controls (`/test/behaviour`, same `fail_next`/`rate`+`seed` semantics as faults):
`duplicate_authorization` (resend the same trace), `timeout_retry` (give up after 1 ms, then
retry the same trace) and `clearing_before_auth_event` (settle in the same call).

State lives in memory; `/test/seed` restores the seed tokens.
*Tradeoff:* no database to run for a mock, at the cost of losing ad-hoc tokens on restart.

## 11. Fault injection and seeded bugs

`POST /test/faults` replaces the active set: `latency`, `error` (5xx), `timeout`,
`kafka_publish_failure`, `consumer_pause`, `relay_pause`. Each fault can be scoped (method
and path, or topic) and made deterministic with `fail_next: N` or `rate` plus `seed`
(seeded RNG). Probes and `/test/*` are never faulted, so tests can always recover.

Seeded bugs are environment flags, all `false` by default. `make bug-up BUG=<FLAG>` (or the
start-stack action's `bug_flag` input) turns exactly one on. The flag list lives in
`shared/bugs.py` and `scripts/bugs.txt`; CI checks they agree. `GET /test/bugs` shows which
flags a service sees. Bug behaviour is implemented in phase 6.

## 12. Continuous integration and resource budget

Nothing is hosted. `app-ci.yml` runs on every push and pull request:

- `lint`: ruff check and format check on `app/`, event schemas up to date, bug lists agree,
  shell scripts parse; `tsc` and ESLint on `app/web` once it exists (phase 5).
- `stack-smoke`: the start-stack composite action, then `make smoke`, then always: resource
  usage, `docker compose logs` uploaded as an artifact (7 days), `docker compose down -v`.

The composite action builds every image with Buildx and the GitHub Actions layer cache, one
cache scope per image, starts the stack with `docker-compose.ci.yml` and `--wait`, seeds it,
and prints each `/ready`. It takes `bug_flag` and `test_mode` inputs.

`docker-compose.ci.yml` sets memory limits on every container, Redpanda in dev mode with
`--smp 1 --memory 1G --overprovisioned`, no Redpanda Console, and INFO logs. Ports are bound
to `127.0.0.1` in the base file already, so the override does not need to change them.

**Measurements (startup excludes image builds, rule 66):**

| Phase | Where | Startup to all healthy | Memory after smoke |
|---|---|---|---|
| 1 | Cloud dev container pinned to 2 CPUs (CI override) | 11 s | ~380 MiB (redpanda 237, auth 103, postgres 42) |
| 1 | GitHub-hosted runner, 4 vCPU (app-ci run 45179541033) | 19 s | ~237 MiB (redpanda 116, auth 87, postgres 35) |
| 2 | Cloud dev container pinned to 2 CPUs (CI override) | 23 s | ~532 MiB (redpanda 241, auth 87, network-mock 76, card 74, postgres 55) |

Targets: under 3 minutes on 2 vCPU and under 5 GB with tests running.

## 13. Agent-ready rules (spec 15.1)

- Customer actions an agent will need are customer APIs that accept the customer's JWT.
- Destructive card actions (report lost, close) are idempotent by state and accept
  `Idempotency-Key`; wallet actions follow in phase 3.
- Every OpenAPI operation has a `summary` and `description`, and parameters have descriptions.
- Correlation IDs flow through every hop.
- Port 8006 and `evals/` are reserved.

## 14. Known limitations

- An access token stays valid until it expires (up to 15 minutes) after logout.
- Per-username lockout can be abused to lock a known account.
- Migrations run at container start; safe only with one replica per service.
- One Postgres instance hosts every database.
- No PAN ever leaves the bank in this simulation; PANs are encrypted at rest in card-service.
- Holds never expire.
- Consumers cannot read compressed Kafka batches (snappy, lz4, zstd need extra libraries).
  Our producers do not compress; a tool such as `rpk topic produce` needs `-z none`.
- network-mock keeps its state in memory.
