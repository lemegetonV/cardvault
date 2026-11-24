#!/usr/bin/env bash
# Runs once, when the Postgres volume is first created.
# One database per service, owned by that service's login role, plus a read-only role per
# database for tests (spec section 3). The shared "testsupport" database holds the single
# shared-clock row (spec section 5).
set -euo pipefail

create_db() {
  local db="$1" password="$2"
  local owner="${db}_svc" ro="${db}_ro"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE ${owner} LOGIN PASSWORD '${password}';
CREATE DATABASE ${db} OWNER ${owner};
REVOKE ALL ON DATABASE ${db} FROM PUBLIC;
CREATE ROLE ${ro} LOGIN PASSWORD '${READONLY_DB_PASSWORD}';
GRANT CONNECT ON DATABASE ${db} TO ${ro};
SQL
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<SQL
ALTER SCHEMA public OWNER TO ${owner};
GRANT USAGE ON SCHEMA public TO ${ro};
ALTER DEFAULT PRIVILEGES FOR ROLE ${owner} IN SCHEMA public GRANT SELECT ON TABLES TO ${ro};
ALTER DEFAULT PRIVILEGES FOR ROLE ${owner} IN SCHEMA public GRANT SELECT ON SEQUENCES TO ${ro};
SQL
}

create_db auth "$AUTH_DB_PASSWORD"
create_db card "$CARD_DB_PASSWORD"
create_db enrollment "$ENROLLMENT_DB_PASSWORD"
create_db wallet_txn "$WALLET_TXN_DB_PASSWORD"
create_db profile "$PROFILE_DB_PASSWORD"
create_db testsupport "$TESTSUPPORT_DB_PASSWORD"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname testsupport <<SQL
SET ROLE testsupport_svc;
CREATE TABLE clock_offset (
  id smallint PRIMARY KEY CHECK (id = 1),
  offset_seconds double precision NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO clock_offset (id, offset_seconds) VALUES (1, 0);
SQL
