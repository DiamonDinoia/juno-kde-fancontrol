#!/usr/bin/env bash
# Validate juno-kde-fancontrol in a clean Ubuntu 24.04 container:
# unit tests, privileged-helper integration test, offscreen GUI renders.
# Screenshots land in tests/out/ for tools/vision_check.py.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$HERE/.." && pwd)   # juno-kde-fancontrol
OUT="$HERE/out"
mkdir -p "$OUT"

IMAGE="${JFC_IMAGE:-docker.io/library/debian:unstable}"
# fan-profile is mounted too: the helper test replays the real regen path
# (the 20-resync.conf ExecStartPre contract) against it.
podman run --rm \
    -v "$REPO_ROOT:/src:ro" \
    -v "$HOME/system-fixes/fan-profile:/opt/fan-profile:ro" \
    -v "$OUT:/out" \
    -e FANPROFILE=/opt/fan-profile \
    "$IMAGE" bash /src/tests/container-entry.sh
echo "screenshots in $OUT; deb in $OUT/deb/"
