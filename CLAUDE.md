# CLAUDE.md: context for future sessions

## What this repo is
CardVault is an application under test (AUT) for a Senior SDET portfolio: one fictional
bank's side of a bank-backed checkout wallet. `SPEC.md` is the build spec; **section 16
overrides sections 1 to 15**. Claude builds the AUT only (`app/`, compose files, `Makefile`,
`scripts/`, `.github/`, docs). The owner writes `tests-api/` (pytest + httpx) and
`tests-ui/` (Playwright TS). `evals/` is reserved for the future agent work (spec 15).
Never write test frameworks; `scripts/smoke.sh` is the only test code.

## Rules that are easy to forget
- Fictional and generic only: no real bank, wallet, network or operator names or flows.
- Default behaviour is always correct; bugs exist only behind `BUG_*` flags.
- Ask before adding any dependency not in the spec. Approved extras: uvicorn, asyncpg,
  PyJWT, cryptography, httpx.
- Business time comes only from `shared.clock.now()`; retries and delays use real time.
- Every event and every operator call goes through the outbox.
- Money is integer cents. Never log PANs or tokens (the log formatter masks them anyway).
- Never reference `docs/private/BUGS.md` from code, README or ARCHITECTURE.md.
- Record each design decision and its tradeoff in `ARCHITECTURE.md`; errors in `docs/errors.md`.
- After each phase: `make smoke` green locally and `app-ci` green on GitHub, then stop and report.

## Phase status
- [x] 1. Monorepo, compose + CI override, `shared`, auth-service, Makefile, smoke, start-stack action, app-ci
- [ ] 2. card-service + network-mock
- [ ] 3. profile-service + enrollment-service + wallet-operator-mock
- [ ] 4. wallet-txn-service + every Kafka flow + DLQs + reconciliation
- [ ] 5. web app + the 10 Playwright coverage features + operator embed/help pages
- [ ] 6. Seeded bugs + full docs

## Layout
- `app/pyproject.toml`, `app/uv.lock`: one Python project for every service and mock.
- `app/shared/`: platform code (see ARCHITECTURE.md section 4). `create_app(ServiceSpec(...))`
  in `shared/runtime.py` is how a service is assembled.
- `app/services/<name>/`: `main.py` (ServiceSpec + seed/test hooks), `api.py`, `models.py`,
  `migrations/` (Alembic, hand-written, run by the entrypoint at container start).
- `app/Dockerfile`: one image recipe, `ARG SERVICE` selects `services.<name>` or `mocks.<name>`.
- `app/infra/postgres/01-databases.sh`: databases, roles, the shared clock table.
- `scripts/lib.sh`: the `SERVICES` list (add each new service here), `req`/`json` helpers.

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
