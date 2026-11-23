#!/usr/bin/env bash
# Usage: check-bug.sh FLAG. Fails unless FLAG is a known seeded-bug flag.
set -euo pipefail
flag="${1:-}"
if [[ -z "$flag" ]] || ! grep -qx "$flag" "$(dirname "$0")/bugs.txt"; then
  echo "unknown bug flag '${flag}'. Known flags:" >&2
  cat "$(dirname "$0")/bugs.txt" >&2
  exit 1
fi
