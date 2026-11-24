#!/usr/bin/env bash
# Load the deterministic seed data into every service (each one resets itself first).
source "$(dirname "$0")/lib.sh"

for entry in "${SERVICES[@]}"; do
  name="${entry%%=*}" url="${entry#*=}"
  test_req POST "$url/test/seed"
  [[ "$STATUS" == 200 ]] || { echo "seed failed on $name: $STATUS $BODY" >&2; exit 1; }
  echo "seeded $name"
done
