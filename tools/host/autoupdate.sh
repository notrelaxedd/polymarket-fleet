#!/usr/bin/env bash
# autoupdate.sh: pull the branch from GitHub and redeploy the host's compose stack.
#
#   tools/host/autoupdate.sh                    one pass (what fleet-autoupdate.timer runs)
#   tools/host/autoupdate.sh --force-exchange   also restart the exchange while games are live
#
# Run it on the host as root, from anywhere. Install the timer with
# tools/host/install_autoupdate.sh. Workers need nothing: they update themselves from the host.
#
# One pass:
#   1. git fetch; fast-forward to origin/BRANCH (never a merge, never over local commits).
#   2. Rebuild and restart everything that changed (`docker compose up -d --build`). The
#      exchange is left out while an assigned game is live or kicks off within the hour; it
#      follows on the first pass after that.
#   3. Check /healthz. If the host does not come back, go back to the last good commit,
#      rebuild, and skip the bad commit until a newer one lands on GitHub.
#
# Pause updates with: touch /var/lib/fleet-autoupdate/hold  (remove the file to resume).
#
# Environment (all optional, mostly for tests):
#   FLEET_AUTOUPDATE_BRANCH  branch to follow (default main)
#   FLEET_AUTOUPDATE_STATE   state folder (default /var/lib/fleet-autoupdate)
#   FLEET_HEALTH_URL         health URL (default http://127.0.0.1:8080/healthz)
#   FLEET_HEALTH_TRIES       health attempts, FLEET_HEALTH_DELAY seconds apart (default 45 x 2)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BRANCH="${FLEET_AUTOUPDATE_BRANCH:-main}"
STATE="${FLEET_AUTOUPDATE_STATE:-/var/lib/fleet-autoupdate}"
HEALTH_URL="${FLEET_HEALTH_URL:-http://127.0.0.1:8080/healthz}"
HEALTH_TRIES="${FLEET_HEALTH_TRIES:-45}"
HEALTH_DELAY="${FLEET_HEALTH_DELAY:-2}"

# Games that keep the exchange running: an active or halted assignment (paper or live) whose
# game is not final and kicks off within the hour or started in the last 6 hours. A game
# without a kickoff time counts on the day before, the day of and the day after its gameday.
BUSY_SQL="SELECT count(*) FROM assignments a JOIN games g ON g.game_id = a.game_id
 WHERE a.status IN ('active', 'halted') AND g.status <> 'final'
   AND ((g.kickoff_at BETWEEN now() - interval '6 hours' AND now() + interval '1 hour')
     OR (g.kickoff_at IS NULL AND g.gameday BETWEEN current_date - 1 AND current_date + 1))"

log() { echo "autoupdate: $*"; }
die() { echo "autoupdate: $*" >&2; exit 1; }
get() { cat "$STATE/$1" 2>/dev/null || true; }
put() { echo "$2" > "$STATE/$1"; }

# Prints why the exchange must not restart now and succeeds, or fails when it may restart.
exchange_busy() {
  local n
  if ! n=$(docker compose exec -T db psql -U fleet -d fleet -tAc "$BUSY_SQL" 2>/dev/null); then
    echo "the database did not answer"
    return 0
  fi
  n=$(echo "$n" | tr -d '[:space:]')
  [ "$n" = "0" ] && return 1
  echo "${n:-?} assignment(s) on a game that is live or starts within the hour"
}

healthy() {
  local i
  for ((i = 0; i < HEALTH_TRIES; i++)); do
    curl -fsS -m 5 "$HEALTH_URL" >/dev/null 2>&1 && return 0
    sleep "$HEALTH_DELAY"
  done
  return 1
}

deploy() {  # deploy SERVICE...  (no arguments: every service)
  docker compose up -d --build "$@"
}

main() {
  local force=0 head remote services="" reason
  [ "${1:-}" = "--force-exchange" ] && force=1
  cd "$ROOT"
  mkdir -p "$STATE"
  exec 9>"$STATE/lock"
  flock -n 9 || { log "another pass is running"; return 0; }
  if [ -e "$STATE/hold" ]; then
    log "paused ($STATE/hold exists)"
    return 0
  fi

  [ "$(git symbolic-ref -q --short HEAD || true)" = "$BRANCH" ] \
    || die "$ROOT is not on branch $BRANCH (git checkout $BRANCH)"
  git fetch -q origin "$BRANCH" || die "git fetch failed (no internet?)"
  head=$(git rev-parse HEAD)
  remote=$(git rev-parse "origin/$BRANCH")
  if [ "$remote" != "$head" ] && [ "$remote" = "$(get bad)" ]; then
    log "origin/$BRANCH ($remote) failed its health check before; waiting for a newer commit"
  elif [ "$remote" != "$head" ]; then
    git merge -q --ff-only "origin/$BRANCH" \
      || die "cannot fast-forward to origin/$BRANCH: undo local edits in $ROOT (git status)"
    head="$remote"
    log "pulled $(git log -1 --format='%h %s')"
  fi

  local need_host=0 need_exchange=0
  [ "$(get host)" != "$head" ] && need_host=1
  [ "$(get exchange)" != "$head" ] && need_exchange=1
  if [ "$need_host" = 0 ] && [ "$need_exchange" = 0 ]; then
    return 0
  fi

  if [ "$need_exchange" = 1 ]; then
    if [ "$force" = 0 ] && reason=$(exchange_busy); then
      log "exchange restart postponed: $reason"
      need_exchange=0
      services=$(docker compose config --services | grep -vx exchange | tr '\n' ' ')
    fi
  fi
  if [ "$need_host" = 0 ] && [ "$need_exchange" = 0 ]; then
    return 0
  fi

  local prev
  prev=$(get host)
  # shellcheck disable=SC2086  # services is a word list on purpose
  deploy $services
  if ! healthy; then
    if [ -n "$prev" ] && [ "$prev" != "$head" ] && git cat-file -e "$prev^{commit}" 2>/dev/null; then
      put bad "$head"
      git reset -q --hard "$prev"
      # shellcheck disable=SC2086
      deploy $services || true
      die "host unhealthy on $head; rolled back to $prev (it stays there until a newer commit)"
    fi
    die "host unhealthy on $head (check: docker compose logs host)"
  fi
  put host "$head"
  [ "$need_exchange" = 1 ] && put exchange "$head"
  docker image prune -f >/dev/null 2>&1 || true
  log "deployed $(git log -1 --format='%h %s')$([ "$need_exchange" = 1 ] || echo ' (exchange later)')"
}

# Everything runs inside main, so bash has read the whole file before git can rewrite it.
main "$@"
exit
