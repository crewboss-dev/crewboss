#!/usr/bin/env bash
# Runs the workflow+AI discovery scan and writes out/catalog-<sha>.json + out/latest.json.
#
#   ./run.sh                 scan the repos in $CREWBOSS_HOME/scanner/scanner.yaml at origin/main
#   ./run.sh --check          scan, then FAIL (exit 1) on any schema/privacy problem instead of writing
#   ./run.sh test             run the pytest suite instead of scanning
#
# Any other arguments are passed straight through to `python -m scanner.scan` (see its --help).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."   # repo root, so `scanner.*` imports resolve either way

PY="${SCANNER_PYTHON:-python3}"

if [ "${1:-}" = "test" ]; then
  shift
  exec "$PY" -m pytest scanner/tests -q "$@"
fi

exec "$PY" -m scanner.scan "$@"
