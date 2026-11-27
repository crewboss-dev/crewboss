#!/usr/bin/env bash
# Installs + enables the auto-rescan timer (system units that run AS the user who owns the checkout). Run it as that user, from
# the dashboard checkout, after the scanner token exists. Uses sudo for /etc/systemd/system + systemctl only.
#
#   scanner/systemd/install.sh              install, enable + start the timer
#   scanner/systemd/install.sh --no-start   install + enable, but don't start it now
#   scanner/systemd/install.sh --uninstall  stop, disable and remove both units
#
# Settings: the token file and state folder come from $CREWBOSS_HOME/scanner/scanner.yaml (scanner/config.py; defaults
# ~/.config/crewboss/tokens/scanner and ~/.local/state/crewboss). The units are filled in from the *.example files here.
# It does NOT create the token: issue it first (scanner/README.md "Auto-rescan"):
#   docker compose exec -T app python -m app.tokens create --kind scanner --name auto-rescan --quiet \
#     > <token file>   (in a umask 077 shell / chmod 600 afterwards)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${CREWBOSS_REPO:-$(cd "$HERE/../.." && pwd)}"
UNIT_DIR="/etc/systemd/system"
NAME="${CREWBOSS_RESCAN_UNIT:-crewboss-rescan}"
SERVICE="$NAME.service"
TIMER="$NAME.timer"
RUN_AS="$(id -un)"

die() { echo "install.sh: $*" >&2; exit 1; }

case "${1:-}" in
  --uninstall)
    sudo systemctl disable --now "$TIMER" 2>/dev/null || true
    sudo systemctl stop "$SERVICE" 2>/dev/null || true
    sudo rm -f "$UNIT_DIR/$SERVICE" "$UNIT_DIR/$TIMER"
    sudo systemctl daemon-reload
    echo "removed $SERVICE + $TIMER (the state folder is left in place)"
    exit 0 ;;
  ""|--no-start) ;;
  *) die "unknown option '$1' (use --no-start or --uninstall)" ;;
esac

[ -x "$REPO/.venv/bin/python" ] || die "no venv at $REPO/.venv (python3 -m venv .venv && .venv/bin/pip install -r scanner/requirements.txt)"
[ -f "$REPO/scanner/auto_rescan.py" ] || die "$REPO has no scanner/auto_rescan.py"
"$REPO/.venv/bin/python" -c "import yaml" 2>/dev/null || die "$REPO/.venv lacks pyyaml (pip install -r scanner/requirements.txt)"
TOKEN_FILE="$("$REPO/.venv/bin/python" -c "import sys; sys.path.insert(0, '$REPO'); from scanner import config; print(config.token_file())")"
STATE_DIR="$("$REPO/.venv/bin/python" -c "import sys; sys.path.insert(0, '$REPO'); from scanner import config; print(config.state_dir())")"
[ -s "$TOKEN_FILE" ] || die "no scanner token at $TOKEN_FILE — issue one first (see the header of this script)"
[ "$(stat -c %a "$TOKEN_FILE")" = "600" ] || die "$TOKEN_FILE must be mode 600 (chmod 600 it)"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
sed -e "s#@REPO@#$REPO#g" -e "s#@RUN_AS@#$RUN_AS#g" "$HERE/crewboss-rescan.service.example" > "$TMP/$SERVICE"
sed -e "s#crewboss-rescan.service#$SERVICE#g" "$HERE/crewboss-rescan.timer.example" > "$TMP/$TIMER"
systemd-analyze verify "$TMP/$SERVICE" "$TMP/$TIMER"

mkdir -p "$STATE_DIR"
sudo install -m 0644 "$TMP/$SERVICE" "$UNIT_DIR/$SERVICE"
sudo install -m 0644 "$TMP/$TIMER" "$UNIT_DIR/$TIMER"
sudo systemctl daemon-reload

if [ "${1:-}" = "--no-start" ]; then
  sudo systemctl enable "$TIMER"
  echo "installed + enabled $TIMER (not started). First run by hand: sudo systemctl start $SERVICE"
else
  sudo systemctl enable --now "$TIMER"
  echo "installed + started $TIMER. First import runs within 5 min; to run it now:"
  echo "  sudo systemctl start $SERVICE && journalctl -u $SERVICE -n 30 --no-pager"
fi
systemctl list-timers "$TIMER" --no-pager || true
