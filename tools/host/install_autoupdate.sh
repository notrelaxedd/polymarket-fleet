#!/usr/bin/env bash
# install_autoupdate.sh: install the fleet-autoupdate timer on the host. Run as root.
#
#   tools/host/install_autoupdate.sh            install (or refresh), then run one pass
#   tools/host/install_autoupdate.sh --remove   stop and remove the timer
#
# Every 5 minutes the timer runs tools/host/autoupdate.sh from this checkout: it pulls main
# from GitHub and rebuilds what changed (see that script for the exchange rule and rollback).
# Logs: journalctl -u fleet-autoupdate
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE=/etc/systemd/system/fleet-autoupdate.service
TIMER=/etc/systemd/system/fleet-autoupdate.timer

die() { echo "install_autoupdate: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"
command -v systemctl >/dev/null 2>&1 || die "systemd is required"

if [ "${1:-}" = "--remove" ]; then
  systemctl disable --now fleet-autoupdate.timer 2>/dev/null || true
  rm -f "$SERVICE" "$TIMER"
  systemctl daemon-reload
  echo "fleet-autoupdate removed (state in /var/lib/fleet-autoupdate is kept)"
  exit 0
fi
[ $# -eq 0 ] || die "usage: $0 [--remove]"

for tool in git docker curl flock; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not found"
done
docker compose version >/dev/null 2>&1 || die "docker compose (v2) not found"
[ -f "$ROOT/docker-compose.yml" ] || die "$ROOT is not the polymarket-fleet checkout"
[ -f "$ROOT/.env" ] || die "$ROOT/.env is missing (see README, Host setup)"

cat > "$SERVICE" <<EOF
[Unit]
Description=Pull polymarket-fleet from GitHub and redeploy the host stack
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$ROOT
ExecStart=/bin/bash $ROOT/tools/host/autoupdate.sh
TimeoutStartSec=30min
EOF

cat > "$TIMER" <<'EOF'
[Unit]
Description=Check GitHub for polymarket-fleet updates every 5 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
RandomizedDelaySec=30s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now fleet-autoupdate.timer
echo "fleet-autoupdate installed for $ROOT; running one pass now..."
systemctl start fleet-autoupdate.service || true
journalctl -u fleet-autoupdate -n 20 --no-pager || true
