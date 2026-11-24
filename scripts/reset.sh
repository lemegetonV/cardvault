#!/usr/bin/env bash
# Reset every service (tables, faults, idempotency keys, lockouts), clear the shared
# clock offset, then reseed, so the stack is back to exactly the seed state.
source "$(dirname "$0")/lib.sh"

for entry in "${SERVICES[@]}"; do
  name="${entry%%=*}" url="${entry#*=}"
  test_req POST "$url/test/reset"
  [[ "$STATUS" == 200 ]] || { echo "reset failed on $name: $STATUS $BODY" >&2; exit 1; }
  echo "reset $name"
done
test_req POST "$(service_url auth)/test/clock" '{"reset": true}'
[[ "$STATUS" == 200 ]] || { echo "clock reset failed: $STATUS $BODY" >&2; exit 1; }
echo "clock offset cleared"
"$(dirname "$0")/seed.sh"
