#!/usr/bin/env bash
# install.sh: install or upgrade the BTC Odds Terminal backend as the btc-odds-terminal
# systemd service on Debian 12/13. Run as root from a checkout of this repo:
#
#   cd btc-odds-terminal/backend/deploy && sudo ./install.sh
#
# Idempotent: re-run it to upgrade. It re-syncs the code to /opt/btc-odds-terminal, reruns
# npm ci only when package-lock.json or the Node version changes, keeps backend/.env and the
# SQLite data, seeds history until the first successful seed (seed.ts exits non-zero when too
# few bars come back, so a failed seed is retried), and restarts the service.
#
# It opens no firewall ports. HOST and PORT come from backend/.env. Remote access goes over
# the tailnet (see README, "Frontend: Vercel pointed at the worker").
set -euo pipefail
# Fixed regardless of the caller's umask, so files created here (node_modules, stamps) stay
# readable by the service user.
umask 022

APP_USER=btcodds
APP_ROOT=/opt/btc-odds-terminal
BACKEND_DIR="$APP_ROOT/backend"
DATA_DIR="$BACKEND_DIR/data"
ENV_FILE="$BACKEND_DIR/.env"
SEED_STAMP="$DATA_DIR/.seeded"
UNIT=btc-odds-terminal
NODE_MAJOR=22

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Run a command as the service user, with HOME in the data dir (writable, private to the app).
run_as_app() {
  setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups \
    env HOME="$DATA_DIR" npm_config_update_notifier=false "$@"
}

node_major() {
  if command -v node >/dev/null 2>&1; then
    node -p 'process.versions.node.split(".")[0]'
  else
    echo 0
  fi
}

[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0"
[ -f "$REPO_ROOT/backend/package-lock.json" ] && [ -d "$REPO_ROOT/shared" ] \
  || die "$REPO_ROOT is not a btc-odds-terminal checkout (expected backend/ and shared/)"
[ -f "$HERE/$UNIT.service" ] || die "missing $HERE/$UNIT.service"

if [ -r /etc/os-release ]; then . /etc/os-release; fi
[ "${ID:-}" = debian ] || warn "this script is written for Debian; detected ${ID:-unknown}"

export DEBIAN_FRONTEND=noninteractive

log "installing OS packages"
apt-get update -qq
apt-get install -y -qq ca-certificates curl rsync build-essential python3 >/dev/null

if [ "$(node_major)" -lt "$NODE_MAJOR" ]; then
  log "installing Node $NODE_MAJOR from NodeSource"
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y -qq nodejs >/dev/null
fi
[ "$(node_major)" -ge "$NODE_MAJOR" ] || die "Node $NODE_MAJOR or newer is required"
log "node $(node -v), npm $(npm -v)"

log "service user and directories"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_ROOT" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -m 0755 -o root -g root "$APP_ROOT" "$BACKEND_DIR"
install -d -m 0750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"

log "syncing code to $APP_ROOT (code is root-owned; the service user cannot modify it)"
# --chmod normalizes modes: a checkout made under umask 077 would otherwise leave the tree
# unreadable by the service user.
rsync -a --delete --chown=root:root --chmod=D755,F644 \
  --exclude=/node_modules --exclude=/data --exclude=/.env \
  "$REPO_ROOT/backend/" "$BACKEND_DIR/"
rsync -a --delete --chown=root:root --chmod=D755,F644 "$REPO_ROOT/shared/" "$APP_ROOT/shared/"

# Root installs the dependencies (npm ci needs a lockfile match) and re-runs only when the
# lockfile or Node version changes.
STAMP_NEW="$(sha256sum "$BACKEND_DIR/package-lock.json" | cut -d' ' -f1)-node$(node -v)"
STAMP_FILE="$BACKEND_DIR/node_modules/.install-stamp"
if [ ! -x "$BACKEND_DIR/node_modules/.bin/tsx" ] || [ "$(cat "$STAMP_FILE" 2>/dev/null || true)" != "$STAMP_NEW" ]; then
  log "npm ci (backend)"
  ( cd "$BACKEND_DIR" && npm ci --no-audit --no-fund )
  echo "$STAMP_NEW" > "$STAMP_FILE"
else
  log "dependencies unchanged, skipping npm ci"
fi

log "configuration"
if [ ! -f "$ENV_FILE" ]; then
  install -m 0600 -o "$APP_USER" -g "$APP_USER" "$BACKEND_DIR/.env.example" "$ENV_FILE"
  log "created $ENV_FILE from .env.example. Review HOST, CORS_ORIGIN and TRADING_MODE."
else
  chown "$APP_USER:$APP_USER" "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
fi
CORS_VAL="$(sed -n 's/^CORS_ORIGIN=//p' "$ENV_FILE" | tail -n1 | tr -d '[:space:]')"
if [ "$CORS_VAL" = "*" ]; then
  warn "CORS_ORIGIN=* in $ENV_FILE lets any web page in any browser that can reach this host change settings. Set it to your app origin(s)."
fi

if [ ! -f "$SEED_STAMP" ]; then
  log "seeding BTC history (first install)"
  if run_as_app bash -c 'cd "$2" && set -a && . "$1" && set +a && npm run seed' _ "$ENV_FILE" "$BACKEND_DIR"; then
    touch "$SEED_STAMP"
    chown "$APP_USER:$APP_USER" "$SEED_STAMP"
  else
    warn "seed failed or returned too few bars (no network to api.exchange.coinbase.com?). The service still starts. Re-run install.sh later to retry the seed."
  fi
fi

log "installing and (re)starting the systemd unit"
install -m 0644 -o root -g root "$HERE/$UNIT.service" "/etc/systemd/system/$UNIT.service"
systemctl daemon-reload
systemctl enable "$UNIT.service" >/dev/null
systemctl restart "$UNIT.service"

PORT_VAL="$(sed -n 's/^PORT=//p' "$ENV_FILE" | tail -n1 | tr -d '[:space:]')"
HOST_VAL="$(sed -n 's/^HOST=//p' "$ENV_FILE" | tail -n1 | tr -d '[:space:]')"
PORT_VAL="${PORT_VAL:-8787}"
CHECK_HOST="${HOST_VAL:-127.0.0.1}"
case "$CHECK_HOST" in
  0.0.0.0)       CHECK_HOST=127.0.0.1 ;;
  ::|'[::]')     CHECK_HOST='[::1]' ;;
  \[*\])         ;;                          # already bracketed
  *:*)           CHECK_HOST="[$CHECK_HOST]" ;; # other IPv6 literal
esac
HEALTH="http://$CHECK_HOST:$PORT_VAL/api/health"

log "waiting for $HEALTH"
healthy=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "$HEALTH" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 1
done

systemctl --no-pager --lines=3 status "$UNIT.service" || true
if [ "$healthy" -ne 1 ]; then
  journalctl -u "$UNIT" -n 40 --no-pager || true
  die "service is not healthy at $HEALTH (log above; full log: journalctl -u $UNIT)"
fi

cat <<EOF

BTC Odds Terminal is running.
  Health:  $HEALTH
  Status:  systemctl status $UNIT
  Logs:    journalctl -u $UNIT -f
  Config:  $ENV_FILE  (edit, then: systemctl restart $UNIT)
  Remote:  sudo tailscale serve --bg --https=443 http://127.0.0.1:$PORT_VAL   (see README)
EOF
