#!/bin/bash
# Build the release tarball the way CI does: the daemon is compiled in an Ubuntu 22.04 container (glibc 2.35),
# so the prebuilt binary also runs on older distributions; then `make check` and the tarball (dist/).
# Needs docker (or podman: DOCKER=podman). Output files belong to you, not root.
set -euo pipefail
cd "$(dirname "$0")/.."
DOCKER=${DOCKER:-docker}
IMAGE=${IMAGE:-ubuntu:22.04}
$DOCKER run --rm -v "$PWD:/src" -w /src -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" "$IMAGE" bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq > /dev/null
  apt-get install -y -qq --no-install-recommends gcc make libc6-dev python3 > /dev/null
  make clean > /dev/null
  make
  tests/unit/run.sh
  packaging/make-tarball.sh
  chown -R "$HOST_UID:$HOST_GID" dist vram/usm-vramd third_party/liburing
'
