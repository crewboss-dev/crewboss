#!/usr/bin/env bash
# The end-to-end browser smoke suite (tests/e2e/README.md). Run from the repo root:  scripts/e2e.sh [pytest args]
#
# It boots the app against a THROWAWAY Postgres container (tests/conftest.py), never the live stack, and drives headless Chromium through Playwright.
# Overridable: CREWBOSS_TEST_CONTAINER / CREWBOSS_TEST_PORT (so two checkouts can run at once), CREWBOSS_E2E_SHOTS (screenshot folder),
# PYTHON (an interpreter that has the app's requirements plus playwright; default python3).
set -euo pipefail
cd "$(dirname "$0")/.."
for f in app/static/css/app.css app/static/js/htmx.min.js app/static/js/alpine.min.js; do
  [ -f "$f" ] || { echo "missing $f (a build output): see tests/e2e/README.md, 'Before the first run'" >&2; exit 2; }
done
export CREWBOSS_E2E_SHOTS="${CREWBOSS_E2E_SHOTS:-/tmp/crewboss-e2e-shots}"
exec "${PYTHON:-python3}" -m pytest -m e2e -p no:cacheprovider "$@" tests/e2e
