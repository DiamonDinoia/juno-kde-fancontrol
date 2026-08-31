#!/usr/bin/env bash
# Validate juno-kde-fancontrol in a clean Debian unstable container:
# unit tests, privileged-helper integration test, offscreen GUI renders.
# Screenshots land in tests/out/ for tools/vision_check.py.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$HERE/.." && pwd)   # juno-kde-fancontrol
OUT="$HERE/out"
mkdir -p "$OUT"

IMAGE="${JFC_IMAGE:-docker.io/library/debian:unstable}"
# fan-profile now lives in the repo, so the helper test replays the real regen
# path (the drop-in's ExecStartPre contract) straight off /src.
podman run --rm \
    -v "$REPO_ROOT:/src:ro" \
    -v "$OUT:/out" \
    -e FANPROFILE=/src/fan-profile \
    "$IMAGE" bash /src/tests/container-entry.sh
echo "screenshots in $OUT; deb in $OUT/deb/"
