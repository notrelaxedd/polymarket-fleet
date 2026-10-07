#!/bin/bash
# install_agent.sh: install or upgrade the fleet machine agent on Debian 12/13. Run as root.
#
#   install_agent.sh HOST_URL [ENROLL_TOKEN] [--name NAME] [--reenroll] [--token-file PATH]
#   install_agent.sh [ENROLL_TOKEN] [--name NAME] ...        (when served from /install-agent.sh)
#
# The agent supervises one workload container per machine: it asks the host what to run,
# pulls the image, runs it and reports heartbeat, logs and resource use.
#
# The enroll token can be given on the command line, in env FLEET_ENROLL_TOKEN or in a
# file (--token-file). Prefer the environment or a file: a token on the command line is
# visible in the sudo log and in /proc/*/cmdline while the installer runs, e.g.
#   curl -fsSL HOST_URL/install-agent.sh | sudo FLEET_ENROLL_TOKEN=... bash -s -- HOST_URL
#
# Needs root, python3 >= 3.11, tar, systemctl and (for Docker) apt-get. Installs the
# Debian package docker.io with apt only when `docker` is missing, and writes
# /etc/docker/daemon.json (local log driver, 10m x 3 files) only when that file is absent.
# Downloads go through python3 urllib.
# Idempotent: re-running upgrades the code and keeps the machine identity
# (enroll is skipped when agent.conf exists, unless --reenroll is given). Restarting the
# agent never stops workload containers (the unit uses KillMode=process).
#
# It never touches the native Polymarket worker: its systemd unit and its state
# directory are left alone, so a machine can keep running the worker natively.
#
# Security note: the sha256 in /dl/agent/version comes from the same host as the tarball,
# so the check guarantees integrity of the download, not authenticity of the host. Serve
# /install-agent.sh and /dl only over the tailnet (tailscale serve). The tarball is
# validated before extraction: only regular files and directories under fleetagent/ are
# accepted, and it is extracted without preserving the archive's owners or permission bits.
# The docker group is root-equivalent: the fleet-agent user can start any container.
set -euo pipefail

DEFAULT_HOST_URL="__FLEET_HOST_URL__"
STATE_DIR="/var/lib/fleet-agent"
DATA_DIR="/var/lib/fleet-workloads"
APP_DIR="$STATE_DIR/app"
CONF="$STATE_DIR/agent.conf"
UNIT="/etc/systemd/system/fleet-agent.service"
AGENT_USER="fleet-agent"
DAEMON_JSON="/etc/docker/daemon.json"

usage() {
  cat >&2 <<'EOF'
usage: install_agent.sh HOST_URL [ENROLL_TOKEN] [--name NAME] [--reenroll] [--token-file PATH]

The enroll token (needed for a first install or with --reenroll) comes from, in order:
the ENROLL_TOKEN argument, --token-file PATH, or the environment variable
FLEET_ENROLL_TOKEN. Prefer the environment or a file so the token stays out of the
sudo log and the process list:
  curl -fsSL HOST_URL/install-agent.sh | sudo FLEET_ENROLL_TOKEN=... bash -s -- HOST_URL
EOF
  exit 2
}

die() { echo "install_agent: $*" >&2; exit 1; }

# ---------------------------------------------------------------- arguments
HOST_URL=""
ENROLL_TOKEN="${FLEET_ENROLL_TOKEN:-}"
TOKEN_FILE=""
NAME=""
REENROLL=0
POSITIONAL=()
while [ $# -gt 0 ]; do
  case "$1" in
    --name) [ $# -ge 2 ] || usage; NAME="$2"; shift 2 ;;
    --name=*) NAME="${1#--name=}"; shift ;;
    --token-file) [ $# -ge 2 ] || usage; TOKEN_FILE="$2"; shift 2 ;;
    --token-file=*) TOKEN_FILE="${1#--token-file=}"; shift ;;
    --reenroll) REENROLL=1; shift ;;
    -h|--help) usage ;;
    --*) die "unknown option: $1" ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
if [ "${#POSITIONAL[@]}" -ge 1 ] && [[ "${POSITIONAL[0]}" == http://* || "${POSITIONAL[0]}" == https://* ]]; then
  HOST_URL="${POSITIONAL[0]}"
  [ -n "${POSITIONAL[1]:-}" ] && ENROLL_TOKEN="${POSITIONAL[1]}"
else
  HOST_URL="$DEFAULT_HOST_URL"
  [ -n "${POSITIONAL[0]:-}" ] && ENROLL_TOKEN="${POSITIONAL[0]}"
fi
# The host substitutes the placeholder when it serves this file, so never compare against
# the literal placeholder: a real host URL is anything that starts with http(s)://.
[[ "$HOST_URL" == http://* || "$HOST_URL" == https://* ]] || usage
HOST_URL="${HOST_URL%/}"
if [ -n "$TOKEN_FILE" ]; then
  [ -r "$TOKEN_FILE" ] || die "cannot read token file $TOKEN_FILE"
  ENROLL_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
fi
if [ -z "$ENROLL_TOKEN" ] && { [ ! -f "$CONF" ] || [ "$REENROLL" = 1 ]; }; then
  die "an enroll token is required for a first install (or with --reenroll); pass it as an argument, in FLEET_ENROLL_TOKEN or with --token-file"
fi

# ------------------------------------------------------------- prerequisites
# Everything that can fail is checked here, before the enroll token is used.
[ "$(id -u)" = 0 ] || die "run as root (sudo bash install_agent.sh ...)"
command -v python3 >/dev/null 2>&1 || die "python3 not found (apt-get install python3)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "python3 >= 3.11 required, found $(python3 --version 2>&1)"
command -v tar >/dev/null 2>&1 || die "tar not found"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found"
if ! command -v docker >/dev/null 2>&1; then
  command -v apt-get >/dev/null 2>&1 || die "docker is not installed and apt-get is not available; install Docker (Debian package docker.io) first"
fi
case "$HOST_URL" in
  https://*)
    [ -f /etc/ssl/certs/ca-certificates.crt ] \
      || echo "warning: /etc/ssl/certs/ca-certificates.crt missing (apt-get install ca-certificates)" >&2 ;;
esac

# fetch URL DEST: python3 urllib (no proxy), falling back to curl or wget.
fetch() {
  local url="$1" dest="$2"
  if python3 - "$url" "$dest" <<'PY'
import shutil, sys, urllib.request
url, dest = sys.argv[1], sys.argv[2]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(url, timeout=120) as resp, open(dest, "wb") as out:
    shutil.copyfileobj(resp, out)
PY
  then return 0; fi
  if command -v curl >/dev/null 2>&1; then curl -fsSL --noproxy '*' -o "$dest" "$url" && return 0; fi
  if command -v wget >/dev/null 2>&1; then wget -q --no-proxy -O "$dest" "$url" && return 0; fi
  return 1
}

sha256_of() {
  python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$1"
}

json_field() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

# check_tarball FILE: exit non-zero unless every member is a regular file or a
# directory under fleetagent/ (no symlinks, devices, hard links, ../, absolute paths,
# __pycache__ or .pyc). The same rules as fleetagent/update.py.
check_tarball() {
  python3 - "$1" <<'PY'
# BEGIN tarball-check
import sys, tarfile

def check(path: str) -> str | None:
    try:
        tar = tarfile.open(path, mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        return f"bad tarball: {exc}"
    with tar:
        seen_init = False
        for member in tar:
            name = member.name
            parts = name.split("/")
            if name.startswith(("/", "\\")) or any(p in ("", ".", "..") for p in parts):
                return f"unsafe path in tarball: {name}"
            if parts[0] != "fleetagent":
                return f"unexpected top-level entry in tarball: {name}"
            if not (member.isfile() or member.isdir()):
                return f"unsupported member type in tarball: {name}"
            if "__pycache__" in parts or name.endswith(".pyc"):
                return f"compiled file in tarball: {name}"
            if name == "fleetagent/__init__.py":
                seen_init = True
    if not seen_init:
        return "tarball has no fleetagent/__init__.py"
    return None

problem = check(sys.argv[1])
if problem:
    print(problem, file=sys.stderr)
    sys.exit(1)
# END tarball-check
PY
}

# ------------------------------------------------------------------- docker
# daemon.json goes in before the package so the first daemon start already uses it; an
# existing file is never touched (Docker keeps running untouched on a machine that has it).
if [ ! -e "$DAEMON_JSON" ]; then
  mkdir -p /etc/docker
  cat > "$DAEMON_JSON" <<'JSONEOF'
{
  "log-driver": "local",
  "log-opts": {"max-size": "10m", "max-file": "3"}
}
JSONEOF
  chmod 644 "$DAEMON_JSON"
  echo "wrote $DAEMON_JSON (takes effect when the Docker daemon starts or restarts)"
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "installing docker.io"
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends docker.io
fi
getent group docker >/dev/null 2>&1 || groupadd --system docker
systemctl enable --now docker.service >/dev/null 2>&1 || echo "warning: could not enable docker.service" >&2

# --------------------------------------------------------------------- user
if ! id -u "$AGENT_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin --user-group "$AGENT_USER"
  echo "created system user $AGENT_USER"
fi
usermod -aG docker "$AGENT_USER"
mkdir -p "$APP_DIR" "$DATA_DIR"
chown "$AGENT_USER:$AGENT_USER" "$STATE_DIR" "$APP_DIR" "$DATA_DIR"
chmod 750 "$STATE_DIR"
chmod 755 "$DATA_DIR"

# --------------------------------------------------------------- download
WORK="$(mktemp -d /tmp/fleet-agent-install.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

echo "fetching $HOST_URL/dl/agent/version"
fetch "$HOST_URL/dl/agent/version" "$WORK/version.json" || die "cannot download $HOST_URL/dl/agent/version"
VERSION="$(json_field "$WORK/version.json" agent_version)"
SHA="$(json_field "$WORK/version.json" sha256)"
[[ "$VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die "odd agent_version from host: $VERSION"

if [ -f "$APP_DIR/$VERSION/fleetagent/__init__.py" ]; then
  echo "version $VERSION already installed"
else
  echo "downloading agent $VERSION"
  fetch "$HOST_URL/dl/agent.tar.gz" "$WORK/agent.tar.gz" || die "cannot download $HOST_URL/dl/agent.tar.gz"
  ACTUAL="$(sha256_of "$WORK/agent.tar.gz")"
  [ "$ACTUAL" = "$SHA" ] || die "sha256 mismatch: expected $SHA got $ACTUAL"
  check_tarball "$WORK/agent.tar.gz" || die "refusing tarball from $HOST_URL"
  mkdir -p "$WORK/extract"
  tar --no-same-owner --no-same-permissions --no-overwrite-dir -xzf "$WORK/agent.tar.gz" -C "$WORK/extract"
  [ -f "$WORK/extract/fleetagent/__init__.py" ] || die "tarball does not contain fleetagent/__init__.py"
  [ "$(ls -A "$WORK/extract" | wc -l)" = 1 ] || die "tarball must contain exactly one top-level directory"
  chmod -R u=rwX,go=rX "$WORK/extract"
  rm -rf "$APP_DIR/$VERSION.staging"
  mv "$WORK/extract" "$APP_DIR/$VERSION.staging"
  rm -rf "$APP_DIR/$VERSION"
  mv "$APP_DIR/$VERSION.staging" "$APP_DIR/$VERSION"
  chown -R "$AGENT_USER:$AGENT_USER" "$APP_DIR/$VERSION"
fi
ln -sfn "$VERSION" "$APP_DIR/current.tmp"
mv -Tf "$APP_DIR/current.tmp" "$APP_DIR/current"
chown -h "$AGENT_USER:$AGENT_USER" "$APP_DIR/current"
rm -f "$APP_DIR/pending.json"
echo "app/current -> $VERSION"

# ----------------------------------------------------------------- enroll
run_as_agent() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$AGENT_USER" -- env "PYTHONPATH=$APP_DIR/current" "FLEET_AGENT_STATE_DIR=$STATE_DIR" "$@"
  else
    su -s /bin/sh "$AGENT_USER" -c "PYTHONPATH=$APP_DIR/current FLEET_AGENT_STATE_DIR=$STATE_DIR $(printf '%q ' "$@")"
  fi
}

if [ -f "$CONF" ] && [ "$REENROLL" != 1 ]; then
  echo "agent.conf exists; keeping identity (use --reenroll to re-register)"
  STORED_URL="$(json_field "$CONF" host_url 2>/dev/null || true)"
  STORED_URL="${STORED_URL%/}"
  if [ -n "$STORED_URL" ] && [ "$STORED_URL" != "$HOST_URL" ]; then
    echo "warning: agent.conf pointed at $STORED_URL; switching it to $HOST_URL (same identity and token)" >&2
    python3 - "$CONF" "$HOST_URL" <<'PY'
import json, os, sys
path, url = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    conf = json.load(fh)
conf["host_url"] = url
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(conf, fh, indent=2, sort_keys=True)
    fh.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
  fi
else
  # The token travels in the environment, not on the enroll command line.
  export FLEET_ENROLL_TOKEN="$ENROLL_TOKEN"
  ENROLL_ARGS=(python3 -m fleetagent enroll "--host=$HOST_URL")
  [ -n "$NAME" ] && ENROLL_ARGS+=("--name=$NAME")
  run_as_agent "${ENROLL_ARGS[@]}" || die "enrollment failed"
  unset FLEET_ENROLL_TOKEN
fi
chown "$AGENT_USER:$AGENT_USER" "$CONF"
chmod 600 "$CONF"

# ---------------------------------------------------------------- systemd
cat > "$UNIT" <<'UNITEOF'
[Unit]
Description=Fleet machine agent (polymarket-fleet)
After=network-online.target docker.service
Wants=network-online.target docker.service
StartLimitIntervalSec=0

[Service]
User=fleet-agent
Group=fleet-agent
# The docker group is root-equivalent: this unit can start any container on this machine.
SupplementaryGroups=docker
Environment=PYTHONPATH=/var/lib/fleet-agent/app/current
Environment=FLEET_AGENT_STATE_DIR=/var/lib/fleet-agent
Environment=FLEET_AGENT_DATA_DIR=/var/lib/fleet-workloads
Environment=FLEET_AGENT_RUN_DIR=/run/fleet-agent
Environment=HOME=/var/lib/fleet-agent
StateDirectory=fleet-agent fleet-workloads
RuntimeDirectory=fleet-agent
RuntimeDirectoryPreserve=yes
ExecStart=/usr/bin/python3 -m fleetagent run
Restart=always
RestartSec=3
RestartPreventExitStatus=78
# Stopping or restarting the agent must never stop the workload containers.
KillMode=process
TimeoutStopSec=15
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable fleet-agent.service >/dev/null 2>&1 || true
systemctl restart fleet-agent.service
sleep 1
systemctl --no-pager --lines=5 status fleet-agent.service || true
echo "fleet agent $VERSION installed; state in $STATE_DIR, workload data in $DATA_DIR"
