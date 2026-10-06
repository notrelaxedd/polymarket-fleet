#!/usr/bin/env bash
# publish.sh NAME: build the image of workloads/NAME on this machine, push it to the host's
# registry and record its digest and size on the host (docs/workloads-design.md section 9).
#
#   tools/workloads/publish.sh hello
#
# Run it on the host (where `docker compose` can reach the host service). Images are built on
# the host only, never on machines: machines pull by digest.
#
# Environment (all optional):
#   FLEET_LOCAL_REGISTRY  registry address used for the push (default localhost:5000)
#   BASE_IMAGE            passed to the Dockerfile as --build-arg BASE_IMAGE
#   FLEET_HOST_CLI        command that replaces the final call, for local runs, for example
#                         "python -m host.cli"; it is run as: $FLEET_HOST_CLI workload-image NAME DIGEST --size-mb N
#                         (default: docker compose exec -T host python -m host.cli)
set -euo pipefail

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "usage: $0 NAME   (a folder under workloads/)" >&2
  exit 2
fi
NAME="$1"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIR="$ROOT/workloads/$NAME"
REGISTRY="${FLEET_LOCAL_REGISTRY:-localhost:5000}"

if [ ! -f "$DIR/workload.toml" ] || [ ! -f "$DIR/Dockerfile" ]; then
  echo "error: $DIR needs workload.toml and Dockerfile" >&2
  exit 1
fi
# The repository name comes from the manifest's `image = "..."` line.
IMAGE="$(sed -n 's/^image[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$DIR/workload.toml" | head -n 1)"
if [ -z "$IMAGE" ]; then
  echo "error: no image = \"...\" line in $DIR/workload.toml" >&2
  exit 1
fi
REF="$REGISTRY/$IMAGE"

BUILD_ARGS=()
if [ -n "${BASE_IMAGE:-}" ]; then
  BUILD_ARGS=(--build-arg "BASE_IMAGE=$BASE_IMAGE")
fi

echo "building $REF:latest from workloads/$NAME"
docker build ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} -t "$REF:latest" "$DIR"
echo "pushing $REF:latest"
PUSH_LOG="$(mktemp)"
trap 'rm -f "$PUSH_LOG"' EXIT
docker push "$REF:latest" | tee "$PUSH_LOG"

# The digest: the repo digest of this repository, else the line `digest: sha256:... size: ...` of the push.
DIGEST="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$REF:latest" \
  | sed -n "s|^$REF@\(sha256:[0-9a-f]\{64\}\)\$|\1|p" | head -n 1)"
if [ -z "$DIGEST" ]; then
  DIGEST="$(sed -n 's/.*digest: \(sha256:[0-9a-f]\{64\}\).*/\1/p' "$PUSH_LOG" | tail -n 1)"
fi
if [ -z "$DIGEST" ]; then
  echo "error: could not read the digest of $REF" >&2
  exit 1
fi
BYTES="$(docker image inspect --format '{{.Size}}' "$REF:latest")"
SIZE_MB=$(( (BYTES + 1048575) / 1048576 ))
echo "digest $DIGEST, $SIZE_MB MB"

if [ -n "${FLEET_HOST_CLI:-}" ]; then
  read -r -a CLI <<<"$FLEET_HOST_CLI"
else
  CLI=(docker compose exec -T host python -m host.cli)
fi
cd "$ROOT"
"${CLI[@]}" workload-image "$NAME" "$DIGEST" --size-mb "$SIZE_MB"
echo "published $NAME: $REF@$DIGEST"
