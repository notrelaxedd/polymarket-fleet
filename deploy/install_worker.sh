#!/bin/bash
# install_worker.sh: install or upgrade the fleet worker on Debian 12/13. Run as root.
#
#   install_worker.sh HOST_URL [ENROLL_TOKEN] [--name NAME] [--reenroll] [--token-file PATH]
#   install_worker.sh [ENROLL_TOKEN] [--name NAME] ...        (when served from /install.sh)
#
# The enroll token can be given on the command line, in env FLEET_ENROLL_TOKEN or in a
# file (--token-file). Prefer the environment or a file: a token on the command line is
# visible in the sudo log and in /proc/*/cmdline while the installer runs, e.g.
#   curl -fsSL HOST_URL/install.sh | sudo FLEET_ENROLL_TOKEN=... bash -s -- HOST_URL
#
# Needs only root, python3 >= 3.11 and tar. Downloads go through python3 urllib.
# Idempotent: re-running upgrades the code and keeps the worker identity
# (enroll is skipped when worker.conf exists, unless --reenroll is given).
#
# Security note: the sha256 in /dl/version comes from the same host as the tarball, so
# the check guarantees integrity of the download, not authenticity of the host. Serve
# /install.sh and /dl only over the tailnet (tailscale serve) and never over a URL an
# attacker could spoof. The tarball is validated before extraction: only regular files
# and directories under fleet/ are accepted, and it is extracted without preserving the
# archive's owners or permission bits.
set -euo pipefail

DEFAULT_HOST_URL="__FLEET_HOST_URL__"
STATE_DIR="/var/lib/fleet"
APP_DIR="$STATE_DIR/app"
CONF="$STATE_DIR/worker.conf"
UNIT="/etc/systemd/system/fleet-worker.service"
FLEET_USER="fleet"

usage() {
  cat >&2 <<'EOF'
usage: install_worker.sh HOST_URL [ENROLL_TOKEN] [--name NAME] [--reenroll] [--token-file PATH]

The enroll token (needed for a first install or with --reenroll) comes from, in order:
the ENROLL_TOKEN argument, --token-file PATH, or the environment variable
FLEET_ENROLL_TOKEN. Prefer the environment or a file so the token stays out of the
sudo log and the process list:
  curl -fsSL HOST_URL/install.sh | sudo FLEET_ENROLL_TOKEN=... bash -s -- HOST_URL
EOF
  exit 2
}

die() { echo "install_worker: $*" >&2; exit 1; }

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
[ "$(id -u)" = 0 ] || die "run as root (sudo bash install_worker.sh ...)"
command -v python3 >/dev/null 2>&1 || die "python3 not found (apt-get install python3)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "python3 >= 3.11 required, found $(python3 --version 2>&1)"
command -v tar >/dev/null 2>&1 || die "tar not found"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found"
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
# directory under fleet/ (no symlinks, devices, hard links, ../, absolute paths,
# __pycache__ or .pyc). The same rules as fleet/worker/update.py.
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
            if parts[0] != "fleet":
                return f"unexpected top-level entry in tarball: {name}"
            if not (member.isfile() or member.isdir()):
                return f"unsupported member type in tarball: {name}"
            if "__pycache__" in parts or name.endswith(".pyc"):
                return f"compiled file in tarball: {name}"
            if name == "fleet/__init__.py":
                seen_init = True
    if not seen_init:
        return "tarball has no fleet/__init__.py"
    return None

problem = check(sys.argv[1])
if problem:
    print(problem, file=sys.stderr)
    sys.exit(1)
# END tarball-check
PY
}

# --------------------------------------------------------------------- user
if ! id -u "$FLEET_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin --user-group "$FLEET_USER"
  echo "created system user $FLEET_USER"
fi
mkdir -p "$APP_DIR"
chown "$FLEET_USER:$FLEET_USER" "$STATE_DIR" "$APP_DIR"
chmod 750 "$STATE_DIR"

# --------------------------------------------------------------- download
WORK="$(mktemp -d /tmp/fleet-install.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

echo "fetching $HOST_URL/dl/version"
fetch "$HOST_URL/dl/version" "$WORK/version.json" || die "cannot download $HOST_URL/dl/version"
VERSION="$(json_field "$WORK/version.json" code_version)"
SHA="$(json_field "$WORK/version.json" sha256)"
[[ "$VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die "odd code_version from host: $VERSION"

if [ -f "$APP_DIR/$VERSION/fleet/__init__.py" ]; then
  echo "version $VERSION already installed"
else
  echo "downloading worker $VERSION"
  fetch "$HOST_URL/dl/worker.tar.gz" "$WORK/worker.tar.gz" || die "cannot download $HOST_URL/dl/worker.tar.gz"
  ACTUAL="$(sha256_of "$WORK/worker.tar.gz")"
  [ "$ACTUAL" = "$SHA" ] || die "sha256 mismatch: expected $SHA got $ACTUAL"
  check_tarball "$WORK/worker.tar.gz" || die "refusing tarball from $HOST_URL"
  mkdir -p "$WORK/extract"
  tar --no-same-owner --no-same-permissions --no-overwrite-dir -xzf "$WORK/worker.tar.gz" -C "$WORK/extract"
  [ -f "$WORK/extract/fleet/__init__.py" ] || die "tarball does not contain fleet/__init__.py"
  [ "$(ls -A "$WORK/extract" | wc -l)" = 1 ] || die "tarball must contain exactly one top-level directory"
  chmod -R u=rwX,go=rX "$WORK/extract"
  rm -rf "$APP_DIR/$VERSION.staging"
  mv "$WORK/extract" "$APP_DIR/$VERSION.staging"
  rm -rf "$APP_DIR/$VERSION"
  mv "$APP_DIR/$VERSION.staging" "$APP_DIR/$VERSION"
  chown -R "$FLEET_USER:$FLEET_USER" "$APP_DIR/$VERSION"
fi
ln -sfn "$VERSION" "$APP_DIR/current.tmp"
mv -Tf "$APP_DIR/current.tmp" "$APP_DIR/current"
chown -h "$FLEET_USER:$FLEET_USER" "$APP_DIR/current"
rm -f "$APP_DIR/pending.json"
echo "app/current -> $VERSION"

# ----------------------------------------------------------------- enroll
run_as_fleet() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$FLEET_USER" -- env "PYTHONPATH=$APP_DIR/current" "FLEET_STATE_DIR=$STATE_DIR" "$@"
  else
    su -s /bin/sh "$FLEET_USER" -c "PYTHONPATH=$APP_DIR/current FLEET_STATE_DIR=$STATE_DIR $(printf '%q ' "$@")"
  fi
}

if [ -f "$CONF" ] && [ "$REENROLL" != 1 ]; then
  echo "worker.conf exists; keeping identity (use --reenroll to re-register)"
  STORED_URL="$(json_field "$CONF" host_url 2>/dev/null || true)"
  STORED_URL="${STORED_URL%/}"
  if [ -n "$STORED_URL" ] && [ "$STORED_URL" != "$HOST_URL" ]; then
    echo "warning: worker.conf pointed at $STORED_URL; switching it to $HOST_URL (same identity and token)" >&2
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
  ENROLL_ARGS=(python3 -m fleet.worker enroll "--host=$HOST_URL")
  [ -n "$NAME" ] && ENROLL_ARGS+=("--name=$NAME")
  run_as_fleet "${ENROLL_ARGS[@]}" || die "enrollment failed"
  unset FLEET_ENROLL_TOKEN
fi
chown "$FLEET_USER:$FLEET_USER" "$CONF"
chmod 600 "$CONF"

# ---------------------------------------------------------------- systemd
cat > "$UNIT" <<'UNITEOF'
[Unit]
Description=Fleet worker agent (polymarket-fleet)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
User=fleet
Group=fleet
Environment=PYTHONPATH=/var/lib/fleet/app/current
Environment=FLEET_STATE_DIR=/var/lib/fleet
Environment=FLEET_RUN_DIR=/run/fleet
Environment=FLEET_REBOOT_TRIGGER=/run/fleet/reboot
ExecStart=/usr/bin/python3 -m fleet.worker run
Restart=always
RestartSec=3
RestartPreventExitStatus=78
TimeoutStopSec=15
Nice=5
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/fleet
RuntimeDirectory=fleet
RuntimeDirectoryMode=0755
RuntimeDirectoryPreserve=yes
PrivateTmp=yes
MemoryMax=85%

[Install]
WantedBy=multi-user.target
UNITEOF

# Reboot from the dashboard: the agent (user fleet, no privileges) writes the request
# id to /run/fleet/reboot after handing its jobs back; this root path unit reboots.
cat > /etc/systemd/system/fleet-reboot.path <<'UNITEOF'
[Unit]
Description=Reboot when the fleet worker asks for it (polymarket-fleet)

[Path]
PathExists=/run/fleet/reboot
Unit=fleet-reboot.service

[Install]
WantedBy=multi-user.target
UNITEOF
cat > /etc/systemd/system/fleet-reboot.service <<'UNITEOF'
[Unit]
Description=Reboot requested from the fleet dashboard (polymarket-fleet)

[Service]
Type=oneshot
ExecStart=/bin/systemctl reboot
UNITEOF

# Disk wear: SMART needs root, so a root-owned script (never the fleet-owned app code)
# writes /run/fleet-wear/wear.json once an hour. tmpfs only: nothing touches the disk.
if ! command -v smartctl >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends smartmontools >/dev/null 2>&1 \
      || echo "warning: could not install smartmontools; disk wear will show as not reported" >&2
  fi
fi
mkdir -p /usr/local/lib/fleet
cat > /usr/local/lib/fleet/wear.py <<'PY'
# BEGIN fleet-wear
"""Write SMART wear (percent of rated life used) per disk to /run/fleet-wear/wear.json."""
import json, os, subprocess, sys, tempfile, time

SKIP = ("loop", "ram", "zram", "sr", "dm-", "md", "fd", "nbd")
# ATA attributes whose normalised value is the percent of life left.
LIFE_LEFT_IDS = (231, 233, 177, 202, 169)


def wear_from(data):
    nvme = (data.get("nvme_smart_health_information_log") or {}).get("percentage_used")
    if isinstance(nvme, (int, float)):
        return float(nvme)
    for page in (data.get("ata_device_statistics") or {}).get("pages") or []:
        for row in page.get("table") or []:
            if row.get("name") == "Percentage Used Endurance Indicator" and isinstance(row.get("value"), (int, float)):
                return float(row["value"])
    rows = {row.get("id"): row for row in (data.get("ata_smart_attributes") or {}).get("table") or []}
    for attr in LIFE_LEFT_IDS:
        value = (rows.get(attr) or {}).get("value")
        if isinstance(value, (int, float)) and 0 <= value <= 100:
            return float(100 - value)
    return None


def main(out_path="/run/fleet-wear/wear.json"):
    devices = {}
    for dev in sorted(os.listdir("/sys/block")):
        if dev.startswith(SKIP):
            continue
        try:
            proc = subprocess.run(["smartctl", "-j", "-a", "/dev/" + dev], capture_output=True, text=True, timeout=60)
            pct = wear_from(json.loads(proc.stdout))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
        if pct is not None:
            devices[dev] = {"wear_pct": round(pct, 1)}
    os.makedirs(os.path.dirname(out_path), mode=0o755, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(out_path))
    with os.fdopen(fd, "w") as fh:
        json.dump({"at": int(time.time()), "devices": devices}, fh)
    os.chmod(tmp, 0o644)
    os.replace(tmp, out_path)


if __name__ == "__main__":
    main(*sys.argv[1:])
# END fleet-wear
PY
chmod 755 /usr/local/lib/fleet /usr/local/lib/fleet/wear.py
cat > /etc/systemd/system/fleet-wear.service <<'UNITEOF'
[Unit]
Description=Read disk wear for the fleet dashboard (polymarket-fleet)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/fleet/wear.py
UNITEOF
cat > /etc/systemd/system/fleet-wear.timer <<'UNITEOF'
[Unit]
Description=Hourly disk wear reading (polymarket-fleet)

[Timer]
OnBootSec=2min
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
UNITEOF

systemctl daemon-reload
systemctl enable --now fleet-reboot.path >/dev/null 2>&1 || echo "warning: could not enable fleet-reboot.path" >&2
systemctl enable --now fleet-wear.timer >/dev/null 2>&1 || echo "warning: could not enable fleet-wear.timer" >&2
systemctl enable fleet-worker.service >/dev/null 2>&1 || true
systemctl restart fleet-worker.service
sleep 1
systemctl --no-pager --lines=5 status fleet-worker.service || true
echo "fleet worker $VERSION installed; state in $STATE_DIR"
