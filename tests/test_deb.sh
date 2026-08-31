#!/usr/bin/env bash
# Debian package validation: build, inspect, install with apt (real Depends
# resolution), verify the result. Target distro: Debian unstable.
set -u

SRC=/src
DIST=/out/deb
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "PASS $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL $1: $2"; }

export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq --no-install-recommends dpkg-dev debhelper fakeroot build-essential >/dev/null 2>&1

# --- build (in a scratch copy: a build must not dirty the source mount) ------
mkdir -p "$DIST"
BUILD=$(mktemp -d)
cp -a "$SRC"/. "$BUILD"/src
cd "$BUILD"/src
if dpkg-buildpackage -us -uc -b --root-command=fakeroot > /tmp/deb-build.log 2>&1; then
    ok deb-build
else
    bad deb-build "$(tail -15 /tmp/deb-build.log)"
    echo "deb tests: $PASS passed, $FAIL failed (build failed — skipping rest)"
    exit 1
fi
DEB=$(ls "$BUILD"/juno-kde-fancontrol_*_all.deb 2>/dev/null | head -1)
[[ -n "$DEB" ]] && cp "$DEB" "$DIST/" && ok deb-artifact || bad deb-artifact "no .deb produced"

# --- inspect -------------------------------------------------------------------
contents=$(dpkg-deb -c "$DEB")
for f in /usr/lib/juno-kde-fancontrol/app.py \
         /usr/lib/juno-kde-fancontrol/backend/fancore.py \
         /usr/sbin/juno-fancontrol-apply \
         /usr/bin/juno-kde-fancontrol \
         /usr/share/applications/juno-kde-fancontrol.desktop \
         /usr/share/polkit-1/actions/org.juno.kdefancontrol.policy; do
    grep -q "$f" <<< "$contents" && ok "has:$f" || bad "has:$f" "missing from package"
done
# world-writable files anywhere are a polkit-path-pin bypass waiting to happen
ww=$(awk '$1 ~ /^-/ && substr($1,9,1)=="w"' <<< "$contents")
[[ -z "$ww" ]] && ok no-world-writable || bad no-world-writable "$ww"
dpkg-deb -f "$DEB" Depends | grep -q python3-pyside6.qtwidgets && ok dep-pyside6 || bad dep-pyside6 "$(dpkg-deb -f "$DEB" Depends)"
dpkg-deb -f "$DEB" Depends | grep -q fancontrol    && ok dep-fancontrol || bad dep-fancontrol ""

# --- install + verify (real apt resolution on Debian unstable) -------------------
if apt-get install -y -qq "$DEB" > /tmp/deb-install.log 2>&1; then
    ok deb-install
else
    bad deb-install "$(tail -8 /tmp/deb-install.log)"
fi
# postinst branches: quiet when fan-profile is absent (container), warning
# when it exists WITHOUT the regen fix.
postinst_out=$(bash /var/lib/dpkg/info/juno-kde-fancontrol.postinst configure 2>&1)
[[ -z "$postinst_out" ]] && ok postinst-quiet-without-fanprofile \
    || bad postinst-quiet-without-fanprofile "$postinst_out"
mkdir -p /usr/local/bin
printf '#!/bin/bash\nexit 0\n' > /usr/local/bin/fan-profile && chmod +x /usr/local/bin/fan-profile
postinst_out=$(bash /var/lib/dpkg/info/juno-kde-fancontrol.postinst configure 2>&1)
grep -q "pre-fix version" <<< "$postinst_out" \
    && ok postinst-warns-prefix-fanprofile || bad postinst-warns-prefix-fanprofile "$postinst_out"
rm -f /usr/local/bin/fan-profile
[[ -x /usr/sbin/juno-fancontrol-apply ]] && ok helper-exec || bad helper-exec ""
grep -q '/usr/sbin/juno-fancontrol-apply' /usr/share/polkit-1/actions/org.juno.kdefancontrol.policy \
    && ok policy-path-pin || bad policy-path-pin "$(grep annotate /usr/share/polkit-1/actions/org.juno.kdefancontrol.policy)"
[[ "$(stat -c '%U:%G %a' /usr/sbin/juno-fancontrol-apply)" == "root:root 755" ]] \
    && ok helper-owner || bad helper-owner "$(stat -c '%U:%G %a' /usr/sbin/juno-fancontrol-apply)"
# entry point imports cleanly off the installed tree (catch a broken wrapper)
if QT_QPA_PLATFORM=offscreen timeout 30 /usr/bin/juno-kde-fancontrol --screenshot /tmp/deb-app.png >/tmp/deb-app.log 2>&1; then
    [[ -s /tmp/deb-app.png ]] && ok app-runs-installed || bad app-runs-installed "no png"
else
    bad app-runs-installed "wrapper exited $? : $(tail -3 /tmp/deb-app.log)"
fi
# and the packaged helper still passes its own integration test off the pack
APPLY=/usr/sbin/juno-fancontrol-apply bash "$SRC/tests/test_apply_helper.sh" >/tmp/deb-helper.log 2>&1 \
    && ok deb-helper-suite || bad deb-helper-suite "$(grep FAIL /tmp/deb-helper.log | head -3)"

echo
echo "deb tests: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
