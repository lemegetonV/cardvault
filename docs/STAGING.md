# The stage server

CardVault has one public copy, called **stage**, on an Oracle Cloud VM. It runs the same
`docker-compose.yml` stack as a laptop, behind Caddy, with HTTPS from Let's Encrypt. This page
records how that server is set up and how to look after it. It holds no secrets: passwords and keys
live only in the files named under [Secrets](#secrets).

Facts checked on the server on 2026-11-28.

## The machine

| | |
| --- | --- |
| Provider | Oracle Cloud Infrastructure, home region Hyderabad, Pay As You Go account |
| Shape | VM.Standard.A1.Flex (Arm), 2 OCPU, 12 GB memory (11 GiB usable + 4 GiB swap) |
| Disk | 100 GB boot volume at 10 VPU (about 13 GB used) |
| OS | Ubuntu 24.04 LTS, aarch64 |
| Public IP | `140.245.206.220` (ephemeral: it changes if the instance is stopped and started from the console) |
| SSH | `ssh -i ~/.ssh/id_ed25519 ubuntu@140.245.206.220` (the owner's Mac key only) |
| Software | Docker 29 with Compose v5, Caddy 2.11, Python 3.12, uv (in `~/.local/bin`), Node.js 22 |

Node.js was installed from the official `linux-arm64` tarball from nodejs.org into `/usr/local`,
with its SHA256 checked against `SHASUMS256.txt`. Ubuntu's own package is too old for Playwright,
and apt does not update this copy: to upgrade, download a newer tarball the same way.

## Addresses

Every name is `<name>.140-245-206-220.sslip.io`. sslip.io resolves a name that contains an IP
address to that address, so no DNS set-up is needed. If the IP changes, every name changes with it
(see [If the IP address changes](#if-the-ip-address-changes)).

| Name | What it serves | Access |
| --- | --- | --- |
| `cardvault.` | The web app (`127.0.0.1:3000`). Any path with a `test`, `internal` or `callbacks` segment answers 404. | Public. Sign in as `alice` / `CardVault-Demo-1` |
| `operator.` | The wallet company simulator: only `/embed/*` (the consent frame) and `/help` | Public |
| `docs.` | A landing page (`/var/www/cardvault-docs/index.html`) linking to every service's API docs | Public |
| `auth-docs.`, `card-docs.`, `enrollment-docs.`, `wallet-txn-docs.`, `profile-docs.`, `wallet-operator-mock-docs.`, `network-mock-docs.` | Each service's Swagger UI and ReDoc: only `GET` on `/docs`, `/redoc`, `/openapi.json` | Public, read only |
| `tools.` | The stage tools page (`/var/www/cardvault-tools/index.html`), the Redpanda (Kafka) console at `/console/`, and each service's test endpoints at `/<service>/test/...` | Basic auth, user `sdetuser` |
| `run.` | The live lab that powers the learning site's Run buttons (`127.0.0.1:8790`) | Basic auth, user `sdetuser` |

The tools page has buttons for **Reset & reseed everything**, **Reseed everything**, **Clock back
to real time**, **Clear all faults** and **Check status**. It holds no key: Caddy adds the
`X-Test-Key` header itself on `/<service>/test/*`, so the basic-auth password is all a person needs.

## Network and firewall

- Open to the internet: 22 (SSH), 80 and 443 (Caddy), in both the OCI security list and the host's
  iptables `INPUT` chain, which rejects everything else.
- Everything Docker publishes is bound to `127.0.0.1`: the web app 3000, the services 8001 to 8005,
  the simulators 8101 and 8102, Postgres 5432, the Redpanda console 8080, and Kafka 19092 and 29092.
- So Postgres, Kafka and the services are reached from a laptop only through an SSH tunnel. The
  API suite's `envs/stage.env` expects this tunnel:

  ```
  ssh -i ~/.ssh/id_ed25519 -N \
    -L 18001:127.0.0.1:8001 -L 18002:127.0.0.1:8002 -L 18003:127.0.0.1:8003 \
    -L 18004:127.0.0.1:8004 -L 18005:127.0.0.1:8005 \
    -L 18101:127.0.0.1:8101 -L 18102:127.0.0.1:8102 \
    -L 15432:127.0.0.1:5432 -L 29092:127.0.0.1:29092 \
    ubuntu@140.245.206.220
  ```

  The local ports differ from the server's (18001 instead of 8001, 15432 instead of 5432), so a
  laptop can run its own stack and reach stage at the same time.
- Kafka has an extra listener for this: `tunnel://127.0.0.1:29092` (see the override file below).
  Through the tunnel a laptop reaches stage Kafka on `localhost:29092`, while its own stack keeps
  19092.

## What runs where

| Path or unit | What it is |
| --- | --- |
| `/opt/cardvault` | A checkout of this repository on `main`. It runs the stack. |
| `/opt/cardvault/.env` | The stage settings and secrets (git-ignored). `TEST_MODE=true`, `COOKIE_SECURE=true`. |
| `/opt/cardvault/docker-compose.override.yml` | Server-only changes, listed in `.git/info/exclude` |
| `/opt/cardvault/stage-bugs.sh` | Switches the seeded bugs on stage, listed in `.git/info/exclude` |
| `/opt/cardvault-suite` | A second checkout of this repository, used only for its test suites. `tests/api/.venv` holds the API suite's packages, and `tests/ui/node_modules` plus `~/.cache/ms-playwright` hold Playwright and Chromium. |
| `/opt/cardvault-learn` | A checkout of `lemegetonV/cardvault-learn`, for its `lab/` folder |
| `cardvault-lab.service` | The live lab: `/opt/cardvault-learn/lab/server.py` as user `ubuntu` on `127.0.0.1:8790`. The unit file's source is `lab/cardvault-lab.service` in cardvault-learn. |
| `caddy.service` | The stock Caddy unit, plus the drop-in `/etc/systemd/system/caddy.service.d/test-key.conf`, which loads `/etc/caddy/cardvault-test-key.env` (one variable, `CV_TEST_KEY`) |
| `/etc/caddy/Caddyfile` | All the sites above. Two snippets: `no_traversal` (404 for dot segments and encoded slashes, used by every site) and `docs_only <port>` (the read-only docs routes). |
| `/var/www/cardvault-docs`, `/var/www/cardvault-tools` | The two static pages |

There are no cron jobs or timers of CardVault's own.

### The override file

`docker-compose.override.yml` is read automatically by `docker compose` in `/opt/cardvault`.
It changes three things:

- `restart: unless-stopped` on Postgres, Redpanda and the console, so they come back after a reboot.
- Redpanda runs in dev-container mode with 1 CPU and 1 GB of memory.
- The Kafka listeners are `internal` 9092, `external` 19092 and `tunnel` 29092, advertised as
  `redpanda:9092`, `127.0.0.1:19092` and `127.0.0.1:29092`. Port `127.0.0.1:29092` is published.

### Seeded bugs on stage

`/opt/cardvault/stage-bugs.sh <mode>` rewrites only the `BUG_*` lines of `.env`, rebuilds and
reseeds. It keeps a backup of `.env` first. Modes: `set-a` (22 bugs), `archived` (4), `all` (26),
`none`, and `status`. Since 2026-11-26 stage runs `none`, at the owner's choice, so stage matches
the lessons and the live lab.

## The live lab

The learning site at https://cardvault-learn.x-bookmarks.workers.dev sends `/vm/*` to its
Cloudflare Worker. The Worker signs in to the `run.` site with basic auth and forwards the call
to `server.py`, so the page itself never asks for a password.

```
browser ──/vm/*──▶ Cloudflare Worker ──▶ Caddy (run., basic auth) ──▶ server.py ──▶ the services
```

`server.py` sends customer API requests, resets the stack with `scripts/reset.sh`, and runs API
tests from `/opt/cardvault-suite/tests/api` and Playwright tests from `/opt/cardvault-suite/tests/ui`.
The Playwright tests open the public `cardvault.` address. Its own README is `lab/README.md` in
cardvault-learn.

## Secrets

| Secret | Where it lives |
| --- | --- |
| Stage settings: database passwords, `TEST_KEY`, service and operator keys, JWT and card keys | `/opt/cardvault/.env` on the server (mode 600) |
| The test key Caddy adds on the tools site | `/etc/caddy/cardvault-test-key.env` (mode 600, root) |
| The `sdetuser` password for `tools.` and `run.` | Hashed in the Caddyfile. The plain password is on the owner's Mac only. |
| The same password, for the learning site's Worker | Cloudflare Worker secret `LAB_AUTH` (dashboard: Workers, cardvault-learn, Settings, Variables and Secrets) |
| SSH access | The owner's Mac key `~/.ssh/id_ed25519` |

This repository is public, so none of these may ever be committed here. To change the `sdetuser`
password, run `caddy hash-password`, put the hash in both `basic_auth` blocks, reload Caddy, and
update `LAB_AUTH` in Cloudflare.

## Looking after it

**Update CardVault after a merge to `main`:**

```
cd /opt/cardvault && git pull --ff-only && make up && make seed
```

`make seed` is needed after every restart, because the wallet company simulator keeps its data in
memory. `make smoke` has one check that fails on stage (it expects the `localhost:3000` frame
ancestor), which is expected.

**Update the test suites the lab runs:**

```
git -C /opt/cardvault-suite pull --ff-only
cd /opt/cardvault-suite/tests/api && uv pip install --python .venv/bin/python -r requirements.txt   # if requirements changed
cd /opt/cardvault-suite/tests/ui && npm ci                                                      # if package.json changed
sudo systemctl restart cardvault-lab
```

After a Playwright version change, also run `npx playwright install chromium` in `tests/ui`.

**Update the live lab:**
`git -C /opt/cardvault-learn pull --ff-only && sudo systemctl restart cardvault-lab`.
If the unit file changed, copy it to `/etc/systemd/system/` and run `sudo systemctl daemon-reload` first.

**Check health:** `docker compose ps` in `/opt/cardvault` (every service healthy, the console has
no healthcheck), `systemctl status caddy cardvault-lab`, and the tools page's **Check status** button.

**Logs:** `make logs` in `/opt/cardvault`, `journalctl -u cardvault-lab -f`, `journalctl -u caddy -f`.

### If the IP address changes

All the names change with it. Update:

1. The site names in `/etc/caddy/Caddyfile`, then `sudo systemctl reload caddy`.
2. `OPERATOR_PUBLIC_URL` and `WEB_ALLOWED_ORIGINS` in `/opt/cardvault/.env`, then `make up && make seed`.
3. `UI_BASE_URL` in `cardvault-lab.service` (in cardvault-learn).
4. `LAB_UPSTREAM` in cardvault-learn's `wrangler.jsonc`.
5. The links on the tools and docs pages, and the stage addresses in `tests/api/envs/stage.env`
   and in this file.

Reserving a public IP in the OCI console avoids all of this.

### Rebuilding the server from scratch

1. Create an A1.Flex instance on Ubuntu 24.04 (aarch64), open 80 and 443 in the security list
   and the host iptables, and add the owner's SSH key.
2. Install Docker with the Compose plugin, Caddy (from Caddy's apt repository), uv and Node.js 22.
3. Clone this repository to `/opt/cardvault`, copy `.env.example` to `.env` and replace every
   secret in it with a fresh value, set `TEST_MODE=true` and `COOKIE_SECURE=true`, add the override
   file described above, then `make up && make seed`.
4. Write the Caddyfile with the sites in [Addresses](#addresses), the test-key env file and its
   systemd drop-in, and the two static pages.
5. Set up the live lab: steps in `lab/README.md` in cardvault-learn.

## Old files on the server

Kept from earlier changes. They can be deleted when no longer wanted:

- `/etc/caddy/Caddyfile.pre-open-lab` (identical to the current file) and `/etc/caddy/Caddyfile.pre-runner`
- `/var/www/cardvault-tools/index.html.bak` and `index.html.pre-testkey`
- `~/.env.before-stage-bugs-20261126015226`: a full `.env` backup, so it holds secrets
- `~/stray-backup/`: one small file that may be a secret
