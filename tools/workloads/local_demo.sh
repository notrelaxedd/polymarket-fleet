#!/usr/bin/env bash
# local_demo.sh: the whole workload platform on one machine, end to end, then clean up.
#
#   FLEET_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres \
#     tools/workloads/local_demo.sh
#
# It starts a registry:2 container, builds and pushes the hello workload, creates a throwaway
# Postgres database, starts the host app and a fleetagent (both from this checkout), assigns
# hello to the machine, runs a job and checks the secret, the log redaction, the approval
# queue, the cleanup after an unassign and the write-heavy placement refusal. It prints PASS
# or FAIL per step and exits non-zero when any step failed. Everything it creates (containers,
# images, database, temp dirs) is removed on exit.
#
# Needs: docker (the local images registry:2 and python:3.13-slim; nothing is pulled), curl, jq,
# a Python with the host dependencies, python3 for the agent, and a Postgres superuser URL.
# Warning: the agent's cleanup runs `docker image prune` and `docker builder prune -af` on the
# local Docker daemon, exactly as it would on a machine; do not run this where the build cache matters.
#
# Environment (optional): FLEET_DEMO_PYTHON (default python), FLEET_DEMO_AGENT_PYTHON (python3),
# FLEET_DEMO_REGISTRY_PORT (5000), FLEET_DEMO_HOST_ADDR (the docker bridge gateway, so the
# bridge-network container can reach the host app), FLEET_DEMO_TIMEOUT (seconds per wait, 180),
# BASE_IMAGE (passed to the hello build).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY="127.0.0.1,localhost,172.17.0.1" no_proxy="127.0.0.1,localhost,172.17.0.1"

PYTHON="${FLEET_DEMO_PYTHON:-python}"
AGENT_PYTHON="${FLEET_DEMO_AGENT_PYTHON:-python3}"
REG_PORT="${FLEET_DEMO_REGISTRY_PORT:-5000}"
WAIT="${FLEET_DEMO_TIMEOUT:-180}"
OWNER="demo@example.com"
SUFFIX="$$"
REG_NAME="fleet-demo-registry-$SUFFIX"
REGISTRY="127.0.0.1:$REG_PORT"
DB_NAME="fleet_demo_$SUFFIX"
TMP="$(mktemp -d)"
HOST_PID=""
AGENT_PID=""
DB_CREATED=0
PASSED=0
FAILED=0

ok()  { PASSED=$((PASSED + 1)); echo "PASS: $1"; }
bad() { FAILED=$((FAILED + 1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "      $2"; }
wait_for() { # wait_for DESCRIPTION COMMAND...  (retries for $WAIT seconds)
  local desc="$1"; shift
  local end=$((SECONDS + WAIT))
  while [ "$SECONDS" -lt "$end" ]; do
    if "$@" >/dev/null 2>&1; then ok "$desc"; return 0; fi
    sleep 1
  done
  bad "$desc" "timed out after ${WAIT}s"
  return 1
}
stop_here() { # a step that later steps depend on failed: report and leave (the trap cleans up)
  echo "stopping: $1"
  summary
}
summary() {
  echo
  echo "$PASSED passed, $FAILED failed"
  [ "$FAILED" -eq 0 ] && exit 0
  exit 1
}

cleanup() {
  local rc=$?
  set +e
  [ -n "$AGENT_PID" ] && kill "$AGENT_PID" 2>/dev/null
  [ -n "$HOST_PID" ] && kill "$HOST_PID" 2>/dev/null
  wait 2>/dev/null
  for w in hello heavy-test; do
    docker ps -aq --filter "label=fleet.workload=$w" | xargs -r docker rm -f >/dev/null 2>&1
  done
  docker rm -f -v "$REG_NAME" >/dev/null 2>&1
  docker images -q "$REGISTRY/fleet/hello" | sort -u | xargs -r docker rmi -f >/dev/null 2>&1
  if [ "$DB_CREATED" = 1 ]; then
    "$PYTHON" - "$FLEET_TEST_DATABASE_URL" "$DB_NAME" >/dev/null 2>&1 <<'PY'
import sys, psycopg
with psycopg.connect(sys.argv[1], autocommit=True) as c:
    c.execute(f'DROP DATABASE IF EXISTS "{sys.argv[2]}" WITH (FORCE)')
PY
  fi
  rm -rf "$TMP"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ---------------------------------------------------------------- preflight
for tool in docker curl jq "$PYTHON" "$AGENT_PYTHON"; do
  command -v "$tool" >/dev/null 2>&1 || { echo "error: $tool not found" >&2; exit 2; }
done
if [ -z "${FLEET_TEST_DATABASE_URL:-}" ]; then
  echo "error: set FLEET_TEST_DATABASE_URL to a Postgres superuser URL" >&2
  exit 2
fi

free_port() { "$PYTHON" -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1])"; }
HOST_ADDR="${FLEET_DEMO_HOST_ADDR:-$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null)}"
if [ -z "$HOST_ADDR" ]; then
  echo "warning: no docker bridge gateway found; using 127.0.0.1 (a bridge container will not reach the host app)"
  HOST_ADDR="127.0.0.1"
fi
PORT="$(free_port)"
BASE="http://$HOST_ADDR:$PORT"
SECRET="Ahoy-$("$PYTHON" -c 'import secrets;print(secrets.token_hex(6))')"
SECRETS_KEY="$("$PYTHON" -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())')"
DB_URL="$("$PYTHON" - "$FLEET_TEST_DATABASE_URL" "$DB_NAME" <<'PY'
import sys, urllib.parse as u
p = u.urlsplit(sys.argv[1])
print(u.urlunsplit(p._replace(path="/" + sys.argv[2])))
PY
)"
echo "demo: host $BASE, registry $REGISTRY, database $DB_NAME, temp dir $TMP"

# The workloads the host syncs: hello, plus a write-heavy one for the placement refusal.
mkdir -p "$TMP/workloads"
cp -r "$ROOT/workloads/hello" "$TMP/workloads/hello"
find "$TMP/workloads" -name __pycache__ -prune -exec rm -rf {} +
mkdir -p "$TMP/workloads/heavy-test"
cat >"$TMP/workloads/heavy-test/workload.toml" <<'EOF'
schema = 1
name = "heavy-test"
description = "Write-heavy test workload (placement refusal on flash)"
image = "fleet/hello"
protocol = "workload-v1"

[resources]
min_ram_mb = 128
min_disk_mb = 300
write_heavy = true

[runtime]
mode = "jobs"
job_kinds = ["hello"]
EOF

HOST_ENV=(env DATABASE_URL="$DB_URL" FLEET_DEV=1 FLEET_PUBLIC_URL="$BASE" FLEET_ALLOWED_ORIGINS="$BASE"
  FLEET_BIND="$HOST_ADDR:$PORT" FLEET_REGISTRY="$REGISTRY" FLEET_SECRETS_KEY="$SECRETS_KEY"
  FLEET_WORKLOADS_DIR="$TMP/workloads" FLEET_LOOP_SECONDS=1 FLEET_TRUST_PROXY=0
  FLEET_OWNER_ALLOW_WORKER_IPS=1 PYTHONPATH="$ROOT" PYTHONDONTWRITEBYTECODE=1)
api() { # api METHOD PATH [JSON]: sets HTTP_CODE and BODY (the owner API; FLEET_DEV skips the login check)
  local args=(-sS --noproxy '*' -o "$TMP/resp.json" -w '%{http_code}' -X "$1" -H "Tailscale-User-Login: $OWNER" -H "Origin: $BASE" -H 'Accept: application/json')
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' --data "$3")
  HTTP_CODE="$(curl "${args[@]}" "$BASE$2" 2>/dev/null)" || HTTP_CODE=000
  BODY="$(cat "$TMP/resp.json" 2>/dev/null)"
}
status_of() { # status_of JSON_LIST ID: the status of the object with that id anywhere in the document
  echo "$1" | jq -r --arg id "$2" '[.. | objects | select((.id? // "") == $id)][0].status // empty'
}
machine_field() { # machine_field NAME JQ_EXPRESSION (relative to the machine object)
  api GET /api/machines
  echo "$BODY" | jq -r --arg n "$1" "[(.machines? // .)[]? | select(.name == \$n)][0] | $2 // empty"
}

# ---------------------------------------------------------------- registry, database, host
docker run -d --pull never --name "$REG_NAME" -p "127.0.0.1:$REG_PORT:5000" -e REGISTRY_STORAGE_DELETE_ENABLED=true registry:2 >/dev/null 2>"$TMP/registry.err" \
  || { cat "$TMP/registry.err"; bad "registry container starts (is port $REG_PORT free?)"; stop_here "no registry"; }
wait_for "registry answers on $REGISTRY" curl -fsS --noproxy '*' "http://$REGISTRY/v2/" || stop_here "no registry"

"$PYTHON" - "$FLEET_TEST_DATABASE_URL" "$DB_NAME" <<'PY' || { bad "fresh database created"; stop_here "no database"; }
import sys, psycopg
with psycopg.connect(sys.argv[1], autocommit=True) as c:
    c.execute(f'CREATE DATABASE "{sys.argv[2]}"')
PY
DB_CREATED=1
ok "fresh database $DB_NAME created"

"${HOST_ENV[@]}" "$PYTHON" -m host.main >"$TMP/host.log" 2>&1 &
HOST_PID=$!
wait_for "host app healthy on $BASE" curl -fsS --noproxy '*' "$BASE/healthz" || { tail -n 30 "$TMP/host.log"; stop_here "host did not start"; }

# ---------------------------------------------------------------- sync, publish
"${HOST_ENV[@]}" "$PYTHON" -m host.cli workloads-sync >"$TMP/sync.log" 2>&1 || { cat "$TMP/sync.log"; bad "workloads-sync"; stop_here "sync failed"; }
api GET /api/workloads
if echo "$BODY" | jq -e '[.. | objects | .name? // empty] | (index("hello") != null and index("heavy-test") != null)' >/dev/null 2>&1; then
  ok "workloads-sync registered hello and heavy-test"
else bad "workloads-sync registered hello and heavy-test" "$BODY"; stop_here "sync"; fi

if FLEET_LOCAL_REGISTRY="$REGISTRY" FLEET_HOST_CLI="env DATABASE_URL=$DB_URL FLEET_REGISTRY=$REGISTRY FLEET_SECRETS_KEY=$SECRETS_KEY FLEET_WORKLOADS_DIR=$TMP/workloads PYTHONPATH=$ROOT $PYTHON -m host.cli" \
  tools/workloads/publish.sh hello >"$TMP/publish.log" 2>&1; then ok "publish.sh built, pushed and recorded hello"
else tail -n 20 "$TMP/publish.log"; bad "publish.sh hello"; stop_here "publish failed"; fi
DIGEST="$(sed -n 's/^digest \(sha256:[0-9a-f]\{64\}\),.*/\1/p' "$TMP/publish.log" | tail -n 1)"
SIZE_MB="$(sed -n 's/^digest sha256:[0-9a-f]*, \([0-9]*\) MB/\1/p' "$TMP/publish.log" | tail -n 1)"
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$REGISTRY/fleet/hello:latest" 2>/dev/null)"
# The heavy-test workload shares the hello image so that write_heavy_on_flash is its only refusal.
"${HOST_ENV[@]}" "$PYTHON" -m host.cli workload-image heavy-test "$DIGEST" --size-mb "${SIZE_MB:-1}" >"$TMP/image2.log" 2>&1 || { cat "$TMP/image2.log"; bad "workload-image heavy-test"; }
# Drop the local tag so that the machine really pulls from the registry, and its cleanup can remove the image.
docker rmi "$REGISTRY/fleet/hello:latest" >/dev/null 2>&1
api GET /api/workloads/hello
if echo "$BODY" | grep -q "$DIGEST"; then ok "host recorded the image digest"; else bad "host recorded the image digest" "$BODY"; fi

# ---------------------------------------------------------------- machine
api POST /api/machine-enroll-token
TOKEN="$(echo "$BODY" | jq -r '.token // empty')"
[ -n "$TOKEN" ] && ok "enroll token minted" || { bad "enroll token minted" "HTTP $HTTP_CODE $BODY"; stop_here "no token"; }

printf '#!/bin/sh\necho inactive\nexit 3\n' >"$TMP/systemctl"
chmod +x "$TMP/systemctl"
AGENT_ENV=(env FLEET_AGENT_STATE_DIR="$TMP/agent/state" FLEET_AGENT_DATA_DIR="$TMP/agent/data" FLEET_AGENT_RUN_DIR="$TMP/agent/run"
  FLEET_AGENT_SYSTEMCTL="$TMP/systemctl" PYTHONPATH="$ROOT" PYTHONDONTWRITEBYTECODE=1)
mkdir -p "$TMP/agent/state" "$TMP/agent/data" "$TMP/agent/run"
if "${AGENT_ENV[@]}" FLEET_ENROLL_TOKEN="$TOKEN" "$AGENT_PYTHON" -m fleetagent enroll --host "$BASE" --name demo-box >"$TMP/enroll.log" 2>&1 \
  || "${AGENT_ENV[@]}" "$AGENT_PYTHON" -m fleetagent enroll --host "$BASE" --token "$TOKEN" --name demo-box >>"$TMP/enroll.log" 2>&1; then
  ok "fleetagent enrolled"
else cat "$TMP/enroll.log"; bad "fleetagent enroll"; stop_here "enroll failed"; fi
"${AGENT_ENV[@]}" "$AGENT_PYTHON" -m fleetagent run >"$TMP/agent.log" 2>&1 &
AGENT_PID=$!
machine_online() { [ "$(machine_field demo-box '.online')" = "true" ]; }
wait_for "machine demo-box is online" machine_online || { tail -n 20 "$TMP/agent.log"; stop_here "machine never came online"; }
MID="$(machine_field demo-box '.id')"
echo "machine id: $MID"

# ---------------------------------------------------------------- secret, assign, job
api PUT /api/workloads/hello/secrets/HELLO_GREETING "{\"value\": \"$SECRET\"}"
[ "$HTTP_CODE" = 200 ] && ok "secret HELLO_GREETING stored" || { bad "secret HELLO_GREETING stored" "HTTP $HTTP_CODE $BODY"; stop_here "secret"; }
api GET /api/workloads/hello/secrets
if echo "$BODY" | grep -q "$SECRET"; then bad "owner API never returns secret values"; else ok "owner API never returns secret values"; fi

api POST "/api/machines/$MID/assign" '{"workload": "hello"}'
[ "$HTTP_CODE" = 200 ] && ok "hello assigned to the machine" || { bad "hello assigned to the machine" "HTTP $HTTP_CODE $BODY"; stop_here "assign"; }
hello_container_running() { [ -n "$(docker ps -q --filter label=fleet.workload=hello)" ]; }
wait_for "hello container is running" hello_container_running || { tail -n 30 "$TMP/agent.log"; stop_here "container never started"; }

api POST /api/workload-jobs '{"workload": "hello", "kind": "hello", "params": {"name": "demo", "steps": 3, "notify": true}}'
JOB_ID="$(echo "$BODY" | jq -r '(.job // .) | .id // empty')"
[ "$HTTP_CODE" = 201 ] && [ -n "$JOB_ID" ] && ok "hello job queued ($JOB_ID)" || { bad "hello job queued" "HTTP $HTTP_CODE $BODY"; stop_here "job"; }
job_status() { api GET "/api/workload-jobs/$JOB_ID"; echo "$BODY" | jq -r '(.job // .) | .status // empty'; }
job_done() { case "$(job_status)" in succeeded|failed|cancelled) return 0 ;; *) return 1 ;; esac; }
wait_for "hello job finished" job_done
api GET "/api/workload-jobs/$JOB_ID"
if [ "$(echo "$BODY" | jq -r '(.job // .) | .status')" = succeeded ]; then ok "hello job succeeded"; else bad "hello job succeeded" "$BODY"; fi
GREETING="$(echo "$BODY" | jq -r '(.job // .) | .result.greeting // empty')"
if [ "$GREETING" = "$SECRET, demo!" ]; then ok "result greeting uses the secret"; else bad "result greeting uses the secret" "got: $GREETING"; fi

# ---------------------------------------------------------------- logs are redacted
machine_logs() { api GET "/api/machines/$MID/logs?limit=200"; echo "$BODY"; }
logs_shipped() { machine_logs | grep -q "hello workload started"; }
wait_for "machine logs were shipped to the host" logs_shipped
if machine_logs | grep -qF "$SECRET"; then bad "shipped machine logs do not contain the secret"; else ok "shipped machine logs do not contain the secret"; fi
if grep -qF "$SECRET" "$TMP/agent.log"; then bad "agent output does not contain the secret"; else ok "agent output does not contain the secret"; fi

# ---------------------------------------------------------------- outbound approval
api GET "/api/outbound?status=pending"
ACTION_ID="$(echo "$BODY" | jq -r '[.. | objects | select(.workload? == "hello" and .kind? == "log")][0].id // empty')"
[ -n "$ACTION_ID" ] && ok "log action is pending approval ($ACTION_ID)" || { bad "log action is pending approval" "$BODY"; stop_here "no outbound action"; }
sleep 4  # a few host loop passes: nothing may be sent without approval
api GET "/api/outbound?status=pending"
if [ "$(status_of "$BODY" "$ACTION_ID")" = pending ]; then ok "action stays pending until approved"; else bad "action stays pending until approved" "$BODY"; fi
api POST "/api/outbound/$ACTION_ID/approve"
[ "$HTTP_CODE" = 200 ] && ok "action approved through the owner API" || bad "action approved through the owner API" "HTTP $HTTP_CODE $BODY"
action_sent() { api GET "/api/outbound?status=sent"; [ "$(status_of "$BODY" "$ACTION_ID")" = sent ]; }
wait_for "approved action becomes sent" action_sent

# ---------------------------------------------------------------- unassign cleans up
api POST "/api/machines/$MID/assign" '{"workload": null}'
[ "$HTTP_CODE" = 200 ] && ok "hello unassigned" || { bad "hello unassigned" "HTTP $HTTP_CODE $BODY"; }
no_hello_container() { [ -z "$(docker ps -aq --filter label=fleet.workload=hello)" ]; }
wait_for "hello container is gone" no_hello_container
image_gone() { ! docker image inspect "$IMAGE_ID" >/dev/null 2>&1; }
wait_for "hello image is removed from the machine" image_gone
scratch_empty() { [ -z "$(ls -A "$TMP/agent/data/hello/scratch" 2>/dev/null)" ]; }
wait_for "hello scratch is empty" scratch_empty
secret_files_gone() { [ -z "$(find "$TMP/agent/run" -name HELLO_GREETING 2>/dev/null)" ]; }
wait_for "hello secret files are removed from the machine" secret_files_gone

# ---------------------------------------------------------------- write-heavy placement
api POST "/api/machines/$MID/disk-type" '{"disk_type": "flash"}'
[ "$HTTP_CODE" = 200 ] && ok "disk type override set to flash" || bad "disk type override set to flash" "HTTP $HTTP_CODE $BODY"
api POST "/api/machines/$MID/assign" '{"workload": "heavy-test"}'
if [ "$HTTP_CODE" = 422 ] && echo "$BODY" | grep -q write_heavy_on_flash; then ok "write-heavy workload refused on flash with 422"
else bad "write-heavy workload refused on flash with 422" "HTTP $HTTP_CODE $BODY"; fi

summary
