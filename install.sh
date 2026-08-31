#!/usr/bin/env bash
# Manual install (no .deb): app under /usr/local/lib, privileged helper under
# /usr/local/sbin, polkit policy, .desktop launcher.
#   sudo bash install.sh
# Prefer the Debian package (dpkg -i dist/juno-kde-fancontrol_*_all.deb) — it
# uses /usr paths and apt-tracked files instead.
set -euo pipefail

SELF=$(cd "$(dirname "$0")" && pwd)
LIB=/usr/local/lib/juno-kde-fancontrol
HELPER=/usr/local/sbin/juno-fancontrol-apply

if [[ $EUID -ne 0 ]]; then
    echo "install.sh needs root — re-run with sudo" >&2
    exit 1
fi

install -d "$LIB/backend"
install -m755 "$SELF/app.py" "$LIB/app.py"
install -m644 "$SELF/backend/__init__.py" "$LIB/backend/__init__.py"
install -m644 "$SELF/backend/fancore.py" "$LIB/backend/fancore.py"
install -m755 "$SELF/juno-fancontrol-apply" "$HELPER"
sed "s|@HELPER@|$HELPER|" "$SELF/org.juno.kdefancontrol.policy.in" \
    | install -D -m644 /dev/stdin /usr/share/polkit-1/actions/org.juno.kdefancontrol.policy
install -D -m644 "$SELF/juno-kde-fancontrol.desktop" /usr/share/applications/juno-kde-fancontrol.desktop
# entry point: script's python3 + /usr/lib path hardcodes; rewrite for /usr/local install
sed "s|/usr/lib/juno-kde-fancontrol|$LIB|" "$SELF/scripts/juno-kde-fancontrol" \
    | install -m755 /dev/stdin /usr/local/bin/juno-kde-fancontrol

echo "installed:"
echo "  app      /usr/local/bin/juno-kde-fancontrol"
echo "  helper   $HELPER"
echo "  policy   /usr/share/polkit-1/actions/org.juno.kdefancontrol.policy"
echo "  launcher /usr/share/applications/juno-kde-fancontrol.desktop"
echo
echo "NOTE: custom (non-preset) curves also need the regen fix in fan-profile:"
echo "  sudo install -m755 ~/system-fixes/fan-profile /usr/local/bin/fan-profile"
