#!/usr/bin/env bash
# Hardware test: time a role change on a real worker.
# Usage: roletest.sh <worker-id> [compose-dir]
set -u

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
    echo "usage: roletest.sh <worker-id> [compose-dir]" >&2
    exit 2
fi

worker="$1"
default_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
compose_dir="${2:-$default_dir}"

cd "$compose_dir" || { echo "cannot enter $compose_dir" >&2; exit 2; }

echo "The number printed below is the seconds from the role change request to the worker's acknowledgement; under 10 passes."
docker compose exec host python -m host.cli roletest "$worker"
exit $?
