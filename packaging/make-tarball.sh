#!/bin/bash
# Pack the release: dist/usm-VERSION-linux-ARCH.tar.gz (+ .sha256) with the daemon as built here (make first).
# The tarball is the whole source plus the prebuilt vram/usm-vramd: unpack, sudo ./install.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=$(cat VERSION)
ARCH=$(uname -m)
[ -x vram/usm-vramd ] || { echo "build first: make" >&2; exit 1; }
[ "$(vram/usm-vramd --version)" = "usm-vramd $VERSION" ] || { echo "vram/usm-vramd is not $VERSION: make clean && make" >&2; exit 1; }
NAME=usm-$VERSION-linux-$ARCH
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$NAME" dist
# Everything a user may need, nothing of a build or an editor.
tar -c --exclude='__pycache__' --exclude='*.pyc' --exclude='*.o' --exclude='*.ol' --exclude='*.os' --exclude='*.d' \
  --exclude='*.a' --exclude='config-host.*' --exclude='config.log' --exclude='compat.h' --exclude='io_uring_version.h' \
  README.md LICENSE VERSION CHANGELOG.md Makefile install.sh uninstall.sh vram guard tools system desktop docs \
  third_party tests packaging | tar -x -C "$STAGE/$NAME"
chmod 755 "$STAGE/$NAME/vram/usm-vramd"
tar -C "$STAGE" --owner=0 --group=0 --numeric-owner -czf "dist/$NAME.tar.gz" "$NAME"
(cd dist && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
echo "dist/$NAME.tar.gz ($(du -h "dist/$NAME.tar.gz" | cut -f1))"
cat "dist/$NAME.tar.gz.sha256"
