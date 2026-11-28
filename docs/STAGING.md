# CardVault stage environment

The stage environment is one Oracle Cloud VM that runs the whole CardVault stack, the public API docs, a password-protected tools page and the live-lab server used by the learning site (`lemegetonV/cardvault-learn`).

This file has no secrets in it. Passwords, keys and `.env` values are kept on the VM and in the owner's password store, never in git.

Facts were checked on the VM on 2026-11-28.

## 1. The machine

| | |
|---|---|
| Provider | Oracle Cloud (OCI), Ampere A1 |
| OS | Ubuntu 24.04.5 LTS, `aarch64` |
| Size | 2 vCPU, 11 GiB RAM, 4 GiB swap, 96 GB root disk |
| Public IP | `140.245.206.220` |
| SSH | `ssh -i ~/.ssh/id_ed25519 ubuntu@140.245.206.220` (key only) |
| Software | Docker 29.10, Docker Compose v5.8, Caddy 2.11.9, Python 3.12.3, Node 22.23, uv 0.13 (in `~/.local/bin`, not on the non-interactive SSH `PATH`) |

Hostnames use [sslip.io](https://sslip.io), so `<name>.140-245-206-220.sslip.io` resolves to the VM with no DNS setup. Caddy gets the TLS certificates automatically.

## 2. Pages and links

| URL (`*.140-245-206-220.sslip.io`) | What it is | Access |
|---|---|---|
| `https://cardvault.` | The CardVault web app (sign in as `alice`, password `CardVault-Demo-1`) | Public |
| `https://operator.` | Wallet-operator mock pages only: `/embed/*` (terms embed) and `/help` | Public |
| `https://docs.` | Landing page that links to every service's API docs | Public |
| `https://auth-docs.`, `card-docs.`, `enrollment-docs.`, `wallet-txn-docs.`, `profile-docs.`, `wallet-operator-mock-docs.`, `network-mock-docs.` | Swagger UI (`/docs`), ReDoc (`/redoc`), `/openapi.json`. GET and HEAD only | Public |
| `https://tools.` | Stage tools page, Redpanda console (`/console/`) and every service's `/test/*` routes (`/<service>/test/...`) | Basic auth |
| `https://run.` | Live-lab server that the learning site's Worker calls (`/vm/*` on the site) | Basic auth |

Everything else on these hosts returns 404.

### The tools page

The page is served from `/var/www/cardvault-tools/index.html`.

Links:
- Kafka console (`/console/`)
- API docs
- App
- Operator help

Buttons. Each one calls `/<service>/test/...` on the same host. Caddy adds the `X-Test-Key` header itself, so the key never reaches the browser.

| Button | Calls |
|---|---|
| Reset & reseed everything | `POST reset` on every service, then `POST /auth/test/clock {reset:true}`, then `POST seed` on every service. Service order: auth, card, profile, enrollment, wallet-txn, network-mock, wallet-operator-mock |
| Reseed everything | `POST seed` on every service |
| Clock back to real time | `POST /auth/test/clock {"reset": true}` |
| Clear all faults | `POST faults {"faults": []}` on every service |
| Check status | `GET clock` and `GET faults` on every service |

Example from a terminal: `curl -u sdetuser https://tools.140-245-206-220.sslip.io/auth/test/clock`

## 3. Network and firewall

- iptables allows inbound **22, 80 and 443 only**. The OCI security list was not checked from the VM. iptables `INPUT` accepts established traffic, ICMP, loopback and new TCP on 22/80/443, then rejects the rest.
- Every Docker port is published on `127.0.0.1` only. Caddy is the only way in.

| Port (127.0.0.1) | Owner |
|---|---|
| 3000 | web (Next.js + BFF) |
| 8001 to 8005 | auth, card, enrollment, wallet-txn, profile |
| 8101, 8102 | wallet-operator-mock, network-mock |
| 8080 | Redpanda console |
| 19092, 29092 | Kafka external listener, Kafka SSH-tunnel listener |
| 5432 | Postgres |
| 8790 | live-lab `server.py` |
| 2019 | Caddy admin API |

### Reaching internals from a laptop

Use an SSH tunnel:

```bash
ssh -i ~/.ssh/id_ed25519 -N -L 29092:127.0.0.1:29092 -L 15432:127.0.0.1:5432 ubuntu@140.245.206.220
```

Kafka is then on `localhost:29092`. Your local stack keeps `19092`. Postgres is on `localhost:15432`.

## 4. Caddy

- Config file: `/etc/caddy/Caddyfile`. The service is the stock `caddy.service` (enabled), which runs `caddy run --environ --config /etc/caddy/Caddyfile`.
- Drop-in `/etc/systemd/system/caddy.service.d/test-key.conf` sets `EnvironmentFile=/etc/caddy/cardvault-test-key.env`. That file (root, mode 600) holds `CV_TEST_KEY`, which must match `TEST_KEY` in `/opt/cardvault/.env`.

Snippets:
- `(no_traversal)`: returns 404 for `.`/`..` path segments, backslashes, and `%2e`, `%2f`, `%5c` in the path. Every site imports it.
- `(docs_only <port>)`: GET/HEAD on the docs paths goes to `127.0.0.1:<port>`. Other methods on those paths get 405, and everything else gets 404.

Site rules:

| Site | Rule |
|---|---|
| `cardvault.` | gzip. Blocks `(?i)^/(api/)?(.*/)?(test\|internal\|callbacks)(/\|$)` with 404, so test, service-to-service and callback routes are never public. Everything else goes to `127.0.0.1:3000` |
| `operator.` | `/embed/*`, `/help` and `/help/*` go to `127.0.0.1:8101`. Everything else gets 404 |
| `docs.` | Serves `/` from `/var/www/cardvault-docs` |
| `*-docs.` | `import docs_only <port>` |
| `tools.` | `basic_auth` (user `sdetuser`, bcrypt hash in the Caddyfile). Serves `/` from `/var/www/cardvault-tools`. `/console/*` goes to `127.0.0.1:8080` with `Authorization` removed and `X-Forwarded-Prefix /console` added. `/<svc>/test/*` has the `/<svc>` prefix stripped and goes to the service port with `Authorization` removed and `X-Test-Key {env.CV_TEST_KEY}` added |
| `run.` | `basic_auth` (same user). Everything goes to `127.0.0.1:8790` with `flush_interval -1`, so streamed test output is not buffered |

To change the basic-auth password, run `caddy hash-password`, put the hash in both `basic_auth` blocks, run `sudo systemctl reload caddy`, then update the learning site's Worker secret.

## 5. Checkouts in /opt

| Path | Repo | Used for |
|---|---|---|
| `/opt/cardvault` | `lemegetonV/cardvault`, `main` | The running stack (`docker compose`) |
| `/opt/cardvault-suite` | `lemegetonV/cardvault`, `main` (second checkout) | Test suites the live lab runs: `tests/api` (has its own `.venv`), `tests/ui` (`.auth/` holds saved logins) |
| `/opt/cardvault-learn` | `lemegetonV/cardvault-learn`, `main` | Only `lab/` is used: `server.py` and the systemd unit |

Files in `/opt/cardvault` that git does not track:
- `.env`: gitignored. Copied from `.env.example`, with stage secrets and `TEST_MODE=true`, `COOKIE_SECURE=true`.
- `docker-compose.override.yml`: listed in `.git/info/exclude`. It changes three things:
  - Adds `restart: unless-stopped` to postgres, redpanda and console.
  - Gives Redpanda a third listener, `tunnel://0.0.0.0:29092`, advertised as `127.0.0.1:29092`, and publishes `127.0.0.1:29092:29092`.
  - Sets Redpanda to dev-container mode with `--smp=1 --memory=1G`.
- `stage-bugs.sh`: listed in `.git/info/exclude`. It switches the seeded `BUG_*` flags in `.env`, then rebuilds and reseeds. Modes: `set-a` (default stage set, 22 bugs), `archived` (only the 4 masking bugs), `all` (26), `none`, `status`. Run `./stage-bugs.sh status` to see the current set. On 2026-11-28 it was `none`.

`.env` variable names, values not shown:

- General:
  - `TEST_MODE`, `TEST_KEY`, `LOG_LEVEL`, `SEED_USER_PASSWORD`
- Database passwords:
  - `POSTGRES_PASSWORD`, `AUTH_DB_PASSWORD`, `CARD_DB_PASSWORD`, `ENROLLMENT_DB_PASSWORD`
  - `WALLET_TXN_DB_PASSWORD`, `PROFILE_DB_PASSWORD`, `TESTSUPPORT_DB_PASSWORD`, `READONLY_DB_PASSWORD`
- Service and operator keys:
  - `SERVICE_KEY_NETWORK_MOCK`, `SERVICE_KEY_OPERATOR_MOCK`, `SERVICE_KEY_TESTS`
  - `OPERATOR_HMAC_SECRET`, `OPERATOR_CALLBACK_DELAY_MS`, `OPERATOR_PUBLIC_URL`
- Web, JWT and Kafka:
  - `WEB_ALLOWED_ORIGINS`, `COOKIE_SECURE`, `CARD_PAN_KEY`
  - `JWT_ISSUER`, `JWT_AUDIENCE`, `KAFKA_BOOTSTRAP_HOST`
- Seeded bugs:
  - the `BUG_*` flags (26)

## 6. systemd

`cardvault-lab.service` (enabled) is installed from `/opt/cardvault-learn/lab/cardvault-lab.service`:

```ini
[Service]
User=ubuntu
WorkingDirectory=/opt/cardvault-learn/lab
Environment=CARDVAULT_DIR=/opt/cardvault
Environment=SUITE_DIR=/opt/cardvault-suite/tests/api
Environment=UI_DIR=/opt/cardvault-suite/tests/ui
Environment=UI_BASE_URL=https://cardvault.140-245-206-220.sslip.io
Environment=PATH=/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 server.py
Restart=on-failure
```

`caddy.service` is enabled, with the `test-key.conf` drop-in. `caddy-api.service` is disabled.

The Docker containers are not a systemd unit. They come back after a reboot through Docker's restart policies.

There are no cron jobs and no CardVault timers.

## 7. Everyday tasks

These commands are standard; only the `stage-bugs.sh` modes were checked against the script.

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@140.245.206.220
cd /opt/cardvault && docker compose ps                         # health of the stack
cd /opt/cardvault && git pull && docker compose up -d --build  # deploy a new main
cd /opt/cardvault && ./stage-bugs.sh set-a                     # switch the seeded bug set (rebuilds and reseeds)
cd /opt/cardvault-suite && git pull                            # update the suites the live lab runs
cd /opt/cardvault-learn && git pull && sudo systemctl restart cardvault-lab
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
journalctl -u cardvault-lab -f                                 # live-lab logs
```

To reset the data, use the tools page's **Reset & reseed everything** button.

## 8. Rebuilding the VM from scratch

1. Create an Ubuntu 24.04 aarch64 instance. Open 22, 80 and 443 in the OCI security list, and add matching iptables `ACCEPT` rules before the final `REJECT`.
2. Install Docker Engine with the compose plugin, Caddy (official apt repo), Node 22 and uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`). Add `ubuntu` to the `docker` group.
3. Clone the three checkouts into `/opt` as in section 5. Create `/opt/cardvault/.env` from `.env.example` with fresh secrets, `TEST_MODE=true` and `COOKIE_SECURE=true`. Recreate `docker-compose.override.yml` and `stage-bugs.sh` from section 5. Add both names to `.git/info/exclude`.
4. Start the stack: `make up && make seed`, then `./stage-bugs.sh set-a` if you want the bug set on.
5. Write `/etc/caddy/cardvault-test-key.env` (`CV_TEST_KEY=<same as TEST_KEY>`, mode 600), add the `test-key.conf` drop-in, then install the Caddyfile with a new `basic_auth` hash. Run `sudo systemctl daemon-reload && sudo systemctl restart caddy`.
6. Put the tools page and docs landing page in `/var/www/cardvault-tools` and `/var/www/cardvault-docs`.
7. Set up the test suites:
   - In `/opt/cardvault-suite/tests/api`, create the venv with uv.
   - In `/opt/cardvault-suite/tests/ui`, run `npm ci` and install Playwright's Chromium.
8. Install and enable `cardvault-lab.service` (see section 6).
9. Update the learning site's Worker so it points at `run.` with the basic-auth credentials.

## 9. Backups and leftovers on the VM

| File | What it is |
|---|---|
| `/etc/caddy/Caddyfile.pre-open-lab` | Identical to the current Caddyfile |
| `/etc/caddy/Caddyfile.pre-runner` | The Caddyfile before the `run.` site was added |
| `/var/www/cardvault-tools/index.html.bak`, `index.html.pre-testkey` | Older versions of the tools page |
| `~/.env.before-stage-bugs-20261126015226` | Full `.env` backup (secrets, mode 600) |
| `~/stray-backup/` | One small file, mode 600 |

These are candidates for deletion. Nothing backs up Postgres, because stage data can be rebuilt from the seed.

## 10. Where the secrets live

- **On the VM:**
  - `/opt/cardvault/.env`
  - `/etc/caddy/cardvault-test-key.env`
  - the bcrypt hash in the Caddyfile
- **Owner's Mac:** the tools and run basic-auth password, in a local file outside every git checkout. It is never committed.
- **Cloudflare:** the learning site's Worker secrets.
