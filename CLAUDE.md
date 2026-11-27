# CLAUDE.md: context for future sessions

## What this repo is
CardVault is an application under test (AUT) for a Senior SDET portfolio: one fictional
bank's side of a bank-backed checkout wallet. `SPEC.md` is the build spec; **section 16
overrides sections 1 to 15**. Claude built the AUT (`app/`, compose files, `Makefile`,
`scripts/`, `.github/`, docs). The test suites came later, at the owner's request
(2026-11-25): the pytest + requests API suite lives in `tests/api/` (rewritten 2026-11-25 as a
small beginner-readable suite for interview prep; see `tests/api/README.md`), and the Playwright suite lives in
`tests/ui/` (started 2026-11-27; see `tests/ui/README.md`). `evals/` is reserved for the future agent
work (spec 15). Inside `app/`, `scripts/smoke.sh` stays the only test code.

## Rules that are easy to forget
- Fictional and generic only: no real bank, wallet, network or operator names or flows.
- Default behaviour is always correct; bugs exist only behind `BUG_*` flags.
- Web dependencies are the spec's stack only (next, react, react-dom, typescript, eslint +
  eslint-config-next, @types/*, @eslint/eslintrc for the flat config); no UI/CSS libraries.
- Ask before adding any dependency not in the spec. Approved extras: uvicorn, asyncpg,
  PyJWT, cryptography, httpx, python-multipart (dispute uploads). (Not approved:
  python-snappy, so Kafka messages must be uncompressed; `rpk topic produce` needs `-z none`.)
- Business time comes only from `shared.clock.now()`; retries and delays use real time.
- Every event and every operator call goes through the outbox.
- Money is integer cents. Never log PANs or tokens (the log formatter masks them anyway).
- Never reference `docs/private/BUGS.md` from code, README or ARCHITECTURE.md.
- Record each design decision and its tradeoff in `ARCHITECTURE.md`; errors in `docs/errors.md`.
- After each phase: `make smoke` green locally and `app-ci` green on GitHub, then stop and report.

## Phase status
- [x] 1. Monorepo, compose + CI override, `shared`, auth-service, Makefile, smoke, start-stack action, app-ci
- [x] 2. card-service + network-mock
- [x] 3. profile-service + enrollment-service + wallet-operator-mock
- [x] 4. wallet-txn-service + every Kafka flow + DLQs + reconciliation
- [x] 5. web app + the 10 Playwright coverage features + operator embed/help pages
- [x] 6. Seeded bugs + full docs (the AUT is complete; next is the owner's test work)

## Layout
- `app/pyproject.toml`, `app/uv.lock`: one Python project for every service and mock.
- `app/shared/`: platform code (see ARCHITECTURE.md section 4). `create_app(ServiceSpec(...))`
  in `shared/runtime.py` is how a service is assembled.
- `app/services/<name>/`: `main.py` (ServiceSpec + seed/test hooks), `api.py`, `models.py`,
  `migrations/` (Alembic, hand-written, run by the entrypoint at container start).
- `app/Dockerfile`: one image recipe, `ARG SERVICE` selects `services.<name>` or `mocks.<name>`.
- `app/infra/postgres/01-databases.sh`: databases, roles, the shared clock table.
- `scripts/lib.sh`: the `SERVICES` list (add each new service here; order is the reset and
  seed order, and the operator mock must stay after enrollment), `req`/`json` helpers.
- `scripts/smoke.sh` resets and reseeds first, then runs; per-phase parts are sourced
  (`smoke-cards.sh`, reseed, `smoke-wallet.sh`, reseed, `smoke-txns.sh`, `smoke-web.sh`;
  the web part is HTTP only, browser behaviour is the owner's Playwright suite). Callback- and
  event-driven statuses are polled with `wait_until`, never slept for.
- `app/shared/seed_data.py`: seed cards, enrolments and tokens shared by all services/mocks.
  `app/shared/seed_txns.py`: the 58 seed wallet transactions; card-service derives each
  card's `available` from them (`seed_txns.available(card)`), so never hardcode balances.
- `app/mocks/<name>/main.py`: mocks use `create_app` too (`uses_db=False`, state in memory).
- enrollment-service: `domain.py` holds the state machine (pushes, `intent()`, callbacks,
  reactions); `eligibility.py` is the pure eligibility function; `operator_client.py`
  delivers `kind=operator` outbox rows. Seed cards have fixed `last4`s in `seed_data.LAST4`.
- wallet-txn-service: `domain.py` folds `auth.decided`/`txn.cleared` into one row per
  trace_id (upsert, status recomputed by `derive_status`); `disputes.py` is the multipart
  upload (passes `fingerprint_body` to `idempotency.run`). Reconciliation is
  `services/enrollment/recon.py` (pure `compare()`).
- `app/web`: Next.js App Router, no UI framework. `src/app/api/[...path]/route.ts` is the BFF
  proxy (path prefix -> service), `src/app/api/auth/*` the session routes, `src/middleware.ts`
  CSP + CSRF cookie + login redirect, `src/components/` shared UI (consent iframe, dialog,
  shell with session timer, toasts), `src/lib/client/` fetch helper, formatting, types.
  Pages are client components; keep `data-testid` on everything interactive or asserted.
  Never use `dangerouslySetInnerHTML` (ESLint fails); the one exception is the seeded bug
  in `components/merchant.tsx`. `npm run typecheck`, `npm run lint`.
  Build the image with the same `--secret id=build_ca` flags (context `app/web`, no SERVICE).
- Seeded bugs: `if bugs.enabled("BUG_X"):` right where the correct code runs (Python), or
  `useBug("BUG_UI_X")` from `components/providers.tsx` (web; the root layout passes the
  `BUG_UI_*` env flags in the config). A new flag goes in `shared/bugs.py`, `scripts/bugs.txt`
  and the `x-bug-flags` anchor in `docker-compose.yml`, plus a curl repro in
  `docs/private/BUGS.md`. When a flow changes, re-run the affected repros there.
- `OutboxRelay(kinds=[...])` limits a relay to some row kinds; a deliver function raises
  `PermanentDeliveryError` to give up at once.

## Adding a service (checklist)
1. `app/services/<name>/` with `main.py`, `models.py`, `migrations/` (copy auth's env.py).
2. Compose service (copy `auth`), CI override entry with `mem_limit`, add to `SERVICES` in
   `scripts/lib.sh`, extend `scripts/smoke.sh`, add error codes to `docs/errors.md`.
3. If it uses Kafka: `uses_kafka=True`, register relays/consumers in an `on_startup` hook,
   list tables in `reset_tables`.

## Commands
- `make up`, `make seed`, `make smoke`, `make reset`, `make logs`, `make down`, `make clean`
- `make ci-up` (CI override), `make bug-up BUG=<FLAG>`
- `make lint`, `make format`, `make events` (regenerate `docs/events`)

## Working in the cloud dev container
Docker Hub may rate-limit; pull from `mirror.gcr.io/library/...` and
`mirror.gcr.io/redpandadata/...` and retag to the canonical names. Builds behind the TLS
proxy need the CA as a build secret:
`docker build --network host --secret id=build_ca,src=/root/.ccr/ca-bundle.crt --build-arg HTTPS_PROXY=$HTTPS_PROXY --build-arg SERVICE=auth -t cardvault/auth:local app`
then `docker compose up -d --no-build --wait`.

## API test suite (`tests/api/`)
- Written for a learner new to Python (the owner's interview prep, 2026-11-25): plain functions,
  simple classes, dictionaries and `assert`. No generics or pydantic. Database and Kafka
  access only in `database.py`/`events.py` and the `database`/`events` tests (owner's ask,
  2026-11-26): read-only DB users, watch a topic before the action. If a line needs a
  paragraph to explain, rewrite it. The earlier 397-case suite is in git history (991fbdc).
- Layout: `config.py` (URLs from env vars with local defaults), `conftest.py` (fixtures
  `alice_token`, `alice_session` (yield, logs out), `customer`), `helpers.py` (`create_logged_in_customer`, wait helpers),
  `database.py` (`query_one`), `events.py` (`EventWatcher`, `list_topics`),
  `api/` (one class per service, methods return the `requests` response; all send through
  the logging `requests.Session` in `api/client.py`), `data/`, `tests/`.
- State-changing tests use a fresh customer (`POST /test/customers`); alice is read-only.
- Never use `/test/bugs` or name seeded bugs in tests or docs. Green locally and in
  `api-tests.yml` with every bug flag off.
- Run: `cd tests/api && pip install -r requirements.txt && pytest` (93 passed; `pytest -n 4` works too).
  `TEST_ENV=stage` loads `envs/stage.env` (python-dotenv; secrets stay out of the file).
  `--alluredir` feeds the Allure report; each call in `api/client.py` is an Allure step.
- Lint: `tests/ruff.toml` (run `ruff check .` and `ruff format --check .` from `tests/api` or `tests/perf`).
- Load test: `tests/perf/locustfile.py` (Locust, read-only demo users, p95/failure budgets
  set the exit code); `api-tests.yml` runs it for 30 s after the API tests.

## UI test suite (`tests/ui/`)
- Playwright Test + TypeScript (pinned in `package.json`), same beginner rules as the API suite.
  It backs the Playwright track on the learning site (plan approved by the owner 2026-11-27).
- `learn/`: one plain spec per Part 1 lesson (no page objects or helpers, each file signs in
  itself); read-only tests use alice. The framework part (page objects, fixtures, journeys,
  a fresh customer per state-changing test) comes next. Never use `/test/bugs`.
- Always wait for `/wallet` after signing in before `page.goto` elsewhere (the login cookie is
  set by the response). Next.js adds a hidden `role="alert"` route announcer, so do not use a
  bare `getByRole('alert')`.
- Run: `cd tests/ui && npm install && npx playwright install chromium && npx playwright test`
  (22 passed). `npm run typecheck`. CI: `.github/workflows/ui-tests.yml`.
