#!/bin/bash
# Does KDE System Settings actually find our entry?
#
# The container gate cannot answer this: installing `systemsettings` in
# debian:unstable pulls 288 packages, most of Plasma, into a test image. So this
# runs on a machine that already has it, against a staged XDG_DATA_DIRS -- it
# installs nothing and touches no user config.
#
# It is a gate, not a probe: a missing systemsettings is a hard failure, so it
# can never pass by doing nothing. The negative control makes it able to fail --
# with the stage removed the entry must disappear from the same command.
set -u
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "PASS $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL $1: $2"; }

command -v systemsettings >/dev/null \
    || { echo "systemsettings not installed -- this gate needs it"; exit 1; }
command -v desktop-file-validate >/dev/null \
    || { echo "desktop-file-validate not installed (desktop-file-utils)"; exit 1; }

ENTRY=juno-fancontrol-settings.desktop
# The exact directory systemsettings scans: app/kcmmetadatahelpers.h,
# findExternalKCMModules(), over $XDG_DATA_DIRS/plasma/systemsettings/externalmodules.
SUBDIR=plasma/systemsettings/externalmodules
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$SUBDIR"
install -m644 "$ENTRY" "$STAGE/$SUBDIR/$ENTRY"

desktop-file-validate "$STAGE/$SUBDIR/$ENTRY" \
    && ok validate || bad validate "desktop-file-validate rejected $ENTRY"

# `systemsettings --list` runs the same findKCMsMetaData() + findExternalKCMModules()
# lookup that builds the sidebar, without opening a window.
found=$(XDG_DATA_DIRS="$STAGE:/usr/share" timeout 120 systemsettings --list 2>/dev/null \
        | grep -c '^juno-fancontrol-settings ')
[[ "$found" -eq 1 ]] && ok listed \
    || bad listed "systemsettings --list found $found entries, expected 1"

# Negative control: without the stage the same command must not find it. Without
# this the check above could be passing on something already installed.
absent=$(XDG_DATA_DIRS=/usr/share timeout 120 systemsettings --list 2>/dev/null \
         | grep -c '^juno-fancontrol-settings ')
[[ "$absent" -eq 0 ]] && ok negative-control \
    || bad negative-control "entry found with the stage removed ($absent) -- the lookup proves nothing"

# The launch target has to exist, or the page is a dead link.
exec_cmd=$(sed -n 's/^Exec=//p' "$ENTRY" | awk '{print $1}')
[[ -n "$exec_cmd" ]] && ok has-exec || bad has-exec "no Exec= in $ENTRY"
[[ -x "scripts/$exec_cmd" ]] && ok exec-shipped \
    || bad exec-shipped "Exec=$exec_cmd has no scripts/$exec_cmd in this tree"

echo "settings entry tests: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
