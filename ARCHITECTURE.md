# CardVault architecture

CardVault simulates one bank's side of a bank-backed online checkout wallet. It is an
application under test: every decision below favours production-like behaviour and
testability over features. This file records each decision and its tradeoff. It grows with
each build phase; sections for later phases are marked as such.

Status: **phase 1** (platform, shared package, auth-service, CI).

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
  endpoints reject service keys, both with 401 (phase 2 onwards).
- **Test controls** are mounted only when `TEST_MODE=true` (otherwise the routes do not exist,
  so they return 404) and need `X-Test-Key`.

### Operator callbacks (used from phase 3)

`X-Timestamp` is Unix seconds; the signed string is `"{timestamp}.{raw_body}"`; `X-Signature`
is lowercase hex HMAC-SHA256. The signature is checked before the timestamp window, so a
forged request learns nothing about the window. A timestamp more than 300 s away from the
shared clock (past or future) is rejected; exactly 300 s is accepted. Replay inside the
window is stopped by `event_id` deduplication, and stale `operator_version`s are ignored.

### 404 versus 403 for another customer's resource (phase 2 onwards)

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
head-of-line blocking.

The relay, a forced publish failure (`fail_next: 2`), per-key ordering and DLQ routing were
exercised against the running Redpanda during phase 1.

## 8. Fault injection and seeded bugs

`POST /test/faults` replaces the active set: `latency`, `error` (5xx), `timeout`,
`kafka_publish_failure`, `consumer_pause`, `relay_pause`. Each fault can be scoped (method
and path, or topic) and made deterministic with `fail_next: N` or `rate` plus `seed`
(seeded RNG). Probes and `/test/*` are never faulted, so tests can always recover.

Seeded bugs are environment flags, all `false` by default. `make bug-up BUG=<FLAG>` (or the
start-stack action's `bug_flag` input) turns exactly one on. The flag list lives in
`shared/bugs.py` and `scripts/bugs.txt`; CI checks they agree. `GET /test/bugs` shows which
flags a service sees. Bug behaviour is implemented in phase 6.

## 9. Continuous integration and resource budget

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
| 1 | GitHub-hosted runner | see the `stack-smoke` job summary | see the job summary |

Targets: under 3 minutes on 2 vCPU and under 5 GB with tests running.

## 10. Agent-ready rules (spec 15.1)

- Customer actions an agent will need are customer APIs that accept the customer's JWT.
- Destructive actions will be idempotent with clear error codes (phase 2 and 3).
- Every OpenAPI operation has a `summary` and `description`, and parameters have descriptions.
- Correlation IDs flow through every hop.
- Port 8006 and `evals/` are reserved.

## 11. Known limitations

- An access token stays valid until it expires (up to 15 minutes) after logout.
- Per-username lockout can be abused to lock a known account.
- Migrations run at container start; safe only with one replica per service.
- One Postgres instance hosts every database.
- No PAN ever leaves the bank in this simulation (phase 2 adds encryption at rest).
