# CardVault

CardVault simulates **one fictional bank's side of a bank-backed online checkout wallet**:
the bank enrols its customers' eligible cards into a wallet run by an external operator,
keeps the system of record for enrolments and wallet purchases, and decides authorizations
when a wallet purchase arrives from the card network. Every bank, operator and network here
is fictional.

It is an **application under test**: it exists so that API and UI test frameworks can be
built against realistic behaviour (auth, idempotency, events, signed callbacks, failures,
reconciliation) with test-only controls, deterministic seed data and observable state.

> Build status: phase 1 of 6 (platform, auth-service, CI). See `SPEC.md` section 13.

## Quick start

Requirements: Docker with Compose v2, GNU Make, curl, python3.

```bash
make up      # build and start the stack, wait until healthy (copies .env.example to .env)
make seed    # load the deterministic seed data
make smoke   # end-to-end smoke test
```

| URL | What |
|---|---|
| http://localhost:8001/docs | auth-service OpenAPI |
| http://localhost:8080 | Redpanda Console |
| `127.0.0.1:19092` | Kafka (Redpanda) from the host |
| `127.0.0.1:5432` | Postgres (read-only roles in `.env.example`) |

Seed logins: `alice`, `bob`, `charlie`, `dana`, password in `.env.example`
(`SEED_USER_PASSWORD`).

```bash
curl -s localhost:8001/v1/auth/login -H 'content-type: application/json' \
  -d '{"username":"alice","password":"CardVault-Demo-1"}'
```

## Make targets

| Target | What it does |
|---|---|
| `up` / `down` / `clean` | Start (build, wait healthy) / stop / stop and delete volumes |
| `seed` / `reset` | Load seed data / reset every service, clear the clock offset, reseed |
| `smoke` | Run `scripts/smoke.sh` |
| `logs` | Follow container logs |
| `ci-up` | Start with `docker-compose.ci.yml` (CI limits), wait, seed |
| `bug-up BUG=<FLAG>` | Start with exactly one seeded-bug flag on |
| `lint` / `format` / `events` | ruff checks / ruff fixes / regenerate `docs/events` |

## Test controls (TEST_MODE only, header `X-Test-Key`)

Every service: `POST /test/reset`, `POST /test/seed`, `GET|POST /test/faults`,
`GET|POST /test/clock`, `GET /test/quiet`, `GET /test/bugs`. See `ARCHITECTURE.md`.

## Continuous integration

`.github/workflows/app-ci.yml` lints and then starts the whole stack inside the GitHub runner
with `.github/actions/start-stack`, runs the smoke test, and uploads the container logs.
Test workflows reuse the same composite action:

```yaml
- uses: ./.github/actions/start-stack
  with:
    bug_flag: ""        # optional: one seeded-bug flag
    test_mode: "true"
```

## Docs

- `SPEC.md`: the build spec (section 16 overrides earlier sections)
- `ARCHITECTURE.md`: design decisions and tradeoffs
- `docs/errors.md`: every error code
- `docs/events/`: JSON Schemas for every event
