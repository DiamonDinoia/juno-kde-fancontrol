#!/usr/bin/env bash
# Debian package validation: build, inspect, install with apt (real Depends
# resolution), verify the result. Target distro: Debian unstable.
set -u

SRC=/src
DIST=/out/deb
PYTHON=${PYTHON:-python3}
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
         /usr/lib/juno-kde-fancontrol/tray.py \
         /usr/lib/juno-kde-fancontrol/fancurve.py \
         /usr/lib/juno-kde-fancontrol/gputemp.py \
         /usr/lib/juno-kde-fancontrol/backend/fancore.py \
         /usr/lib/juno-kde-fancontrol/backend/sysmon.py \
         /usr/sbin/juno-fancontrol-apply \
         /usr/bin/juno-kde-fancontrol \
         /usr/bin/juno-fan-monitor \
         /usr/bin/juno-fan-curve \
         /usr/bin/juno-gpu-curve \
         /usr/bin/juno-gpu-temp \
         /usr/share/applications/juno-kde-fancontrol.desktop \
         /usr/share/applications/juno-fan-monitor.desktop \
         /usr/share/plasma/systemsettings/externalmodules/juno-fancontrol-settings.desktop \
         /usr/lib/juno-kde-fancontrol/backend/ktheme.py \
         /usr/share/polkit-1/actions/org.juno.kdefancontrol.policy \
         /usr/bin/fan-profile \
         /usr/bin/fan-calibrate \
         /usr/lib/systemd/system/fancontrol.service.d/30-juno-fancontrol.conf \
         /usr/lib/systemd/system-sleep/fancontrol-resume \
         /usr/share/juno-kde-fancontrol/rapl-readable.rules \
         /etc/xdg/autostart/juno-fan-monitor.desktop; do
    grep -q "$f" <<< "$contents" && ok "has:$f" || bad "has:$f" "missing from package"
done
# world-writable files anywhere are a polkit-path-pin bypass waiting to happen
ww=$(awk '$1 ~ /^-/ && substr($1,9,1)=="w"' <<< "$contents")
[[ -z "$ww" ]] && ok no-world-writable || bad no-world-writable "$ww"
dpkg-deb -f "$DEB" Depends | grep -q python3-pyside6.qtwidgets && ok dep-pyside6 || bad dep-pyside6 "$(dpkg-deb -f "$DEB" Depends)"
dpkg-deb -f "$DEB" Depends | grep -q fancontrol    && ok dep-fancontrol || bad dep-fancontrol ""

# --- install + verify (real apt resolution on Debian unstable) -------------------
# --no-install-recommends: the Recommends are fan-calibrate's audio stack,
# which nothing in this gate exercises and which costs minutes to fetch.
if apt-get install -y -qq --no-install-recommends "$DEB" > /tmp/deb-install.log 2>&1; then
    ok deb-install
else
    bad deb-install "$(tail -8 /tmp/deb-install.log)"
fi
# postinst branches: quiet with no legacy /usr/local copy (the container
# case), and a shadowing note when a DIFFERENT one is there.
postinst_out=$(bash /var/lib/dpkg/info/juno-kde-fancontrol.postinst configure 2>&1)
[[ -z "$postinst_out" ]] && ok postinst-quiet-without-legacy \
    || bad postinst-quiet-without-legacy "$postinst_out"
# install.sh already put an identical copy there; stash it and put it back.
mkdir -p /usr/local/bin
LEGACY=$(mktemp -d)
[[ -e /usr/local/bin/fan-profile ]] && cp -a /usr/local/bin/fan-profile "$LEGACY/"
printf '#!/bin/bash\nexit 0\n' > /usr/local/bin/fan-profile
postinst_out=$(bash /var/lib/dpkg/info/juno-kde-fancontrol.postinst configure 2>&1)
grep -q "shadows it on PATH" <<< "$postinst_out" \
    && ok postinst-warns-shadowing || bad postinst-warns-shadowing "$postinst_out"
# an identical legacy copy is harmless and must NOT warn (positive control for
# the cmp branch: without it the check above passes on any file at all)
cp /usr/bin/fan-profile /usr/local/bin/fan-profile
postinst_out=$(bash /var/lib/dpkg/info/juno-kde-fancontrol.postinst configure 2>&1)
[[ -z "$postinst_out" ]] && ok postinst-quiet-on-identical-legacy \
    || bad postinst-quiet-on-identical-legacy "$postinst_out"
rm -f /usr/local/bin/fan-profile
[[ -e "$LEGACY/fan-profile" ]] && cp -a "$LEGACY/fan-profile" /usr/local/bin/fan-profile
rm -rf "$LEGACY"
[[ -x /usr/sbin/juno-fancontrol-apply ]] && ok helper-exec || bad helper-exec ""

# --- System Settings integration -------------------------------------------------
# systemsettings reads Name, Icon, Comment, Exec and the parent category out of
# every *.desktop under $XDG_DATA_DIRS/plasma/systemsettings/externalmodules
# (systemsettings app/kcmmetadatahelpers.h, findExternalKCMModules). A wrong
# directory or a missing key means the entry silently never appears, with no
# error anywhere, so each one is checked here.
SETTINGS_ENTRY=/usr/share/plasma/systemsettings/externalmodules/juno-fancontrol-settings.desktop
for f in /usr/share/applications/juno-kde-fancontrol.desktop \
         /usr/share/applications/juno-fan-monitor.desktop \
         /etc/xdg/autostart/juno-fan-monitor.desktop \
         "$SETTINGS_ENTRY"; do
    out=$(desktop-file-validate "$f" 2>&1)
    [[ -z "$out" ]] && ok "desktop-valid:$(basename "$f")" \
        || bad "desktop-valid:$(basename "$f")" "$out"
done
for key in Name Icon Exec X-KDE-System-Settings-Parent-Category X-KDE-Weight; do
    grep -q "^$key=." "$SETTINGS_ENTRY" && ok "settings-key:$key" \
        || bad "settings-key:$key" "absent or empty in $SETTINGS_ENTRY"
done
# Exec has to name something the package actually installed: the module page
# launches it, and a stale name fails only when a user clicks it.
sx=$(sed -n 's/^Exec=\([^ ]*\).*/\1/p' "$SETTINGS_ENTRY")
[[ -n "$sx" && -x $(command -v "$sx" 2>/dev/null || echo /nonexistent) ]] \
    && ok settings-exec-installed || bad settings-exec-installed "Exec=$sx not executable on PATH"
# same for the xdg autostart entry: a stale Exec name fails only at the next login
ax=$(sed -n 's/^Exec=\([^ ]*\).*/\1/p' /etc/xdg/autostart/juno-fan-monitor.desktop)
[[ -n "$ax" && -x $(command -v "$ax" 2>/dev/null || echo /nonexistent) ]] \
    && ok autostart-exec-installed || bad autostart-exec-installed "Exec=$ax not executable on PATH"
# The window icon app.py sets and the two entries' Icon= must agree, or the
# task manager, the launcher and the settings page show three different icons.
icons=$(grep -h '^Icon=' "$SETTINGS_ENTRY" /usr/share/applications/juno-kde-fancontrol.desktop \
        | sort -u | wc -l)
appicon=$(sed -n 's/^APP_ICON = "\(.*\)"/\1/p' /usr/lib/juno-kde-fancontrol/app.py)
[[ $icons -eq 1 && "Icon=$appicon" == "$(grep -h '^Icon=' "$SETTINGS_ENTRY")" ]] \
    && ok settings-icon-matches-window \
    || bad settings-icon-matches-window "entry icons=$icons app.py=$appicon"
# the wrappers run these through python3, so an executable bit here is a
# lintian error and a sign install.sh and debian/install have drifted apart
pymodes=$(stat -c '%n %a' /usr/lib/juno-kde-fancontrol/{app,tray,fancurve,gputemp}.py /usr/lib/juno-kde-fancontrol/backend/*.py)
if grep -qv ' 644$' <<< "$pymodes"; then
    bad py-modules-not-executable "$pymodes"
else
    ok py-modules-not-executable
fi
[[ -x /usr/bin/fan-profile && -x /usr/bin/fan-calibrate ]] \
    && ok fan-cli-exec || bad fan-cli-exec "$(stat -c '%n %a' /usr/bin/fan-profile /usr/bin/fan-calibrate)"
[[ -x /usr/lib/systemd/system-sleep/fancontrol-resume ]] \
    && ok resume-hook-exec || bad resume-hook-exec "$(stat -c %a /usr/lib/systemd/system-sleep/fancontrol-resume)"
DROPIN=/usr/lib/systemd/system/fancontrol.service.d/30-juno-fancontrol.conf
# the ExecStartPre must name a path the package really ships, or the boot-time
# hwmon resync silently never runs
regen=$(sed -n 's/^ExecStartPre=\(.*\) regen$/\1/p' "$DROPIN")
[[ -n "$regen" && -x "$regen" ]] \
    && ok dropin-regen-path || bad dropin-regen-path "ExecStartPre regen -> '$regen'"
grep -q '^ExecStartPre=$' "$DROPIN" \
    && ok dropin-resets-execstartpre || bad dropin-resets-execstartpre "$(cat "$DROPIN")"
# the packaged fan-profile must be the fixed one, or custom curves die at boot
grep -q regen_custom /usr/bin/fan-profile \
    && ok fan-profile-has-regen-custom || bad fan-profile-has-regen-custom "pre-fix copy packaged"
# knob curves survive a reboot only if the packaged fan-profile carries the
# knob-mode regen path; deb-helper-suite below proves it behaves, this catches
# an outright stale copy
for sym in KNOB_HELPER cap_knobs; do
    grep -q "$sym" /usr/bin/fan-profile \
        && ok "fan-profile-has:$sym" || bad "fan-profile-has:$sym" "pre-knob copy packaged"
done
# The FCTEMPS source the GUI writes into /etc/fancontrol must be the packaged
# path, executable, and able to evaluate a curve off the installed tree.
[[ -x /usr/bin/juno-fan-curve ]] \
    && ok fan-curve-exec || bad fan-curve-exec "$(stat -c '%n %a' /usr/bin/juno-fan-curve)"
KNOBWORK=$(mktemp -d)
"$PYTHON" - "$KNOBWORK" "$SRC/tests" <<'PYEOF'
import sys
sys.path.insert(0, "/usr/lib/juno-kde-fancontrol")
sys.path.insert(0, sys.argv[2])       # tests/mktree.py, for the fixture tree
from pathlib import Path
from mktree import make_platform
from backend.fancore import Curve, discover, render_config
work = Path(sys.argv[1])
platform = make_platform(work / "sys", temp_millic=67000)
(work / "fancontrol").write_text(render_config(
    Curve(label="custom", minstart=70, knobs=((45, 0), (60, 55), (75, 110), (95, 255))),
    discover(str(platform)), "2026-08-31 07:35",
    fan_curve="/usr/bin/juno-fan-curve"))
PYEOF
got=$(/usr/bin/juno-fan-curve --config "$KNOBWORK/fancontrol" --sysfs "$KNOBWORK/sys" 2>&1)
# 67 C on the (60,55)..(75,110) segment: (67-60)*55//15 + 55 = 80 -> 80000 mC
[[ "$got" == "80000" ]] \
    && ok fan-curve-evaluates-installed || bad fan-curve-evaluates-installed "got '$got'"
# the GPU fan's pair: a dual config, an awake dGPU fixture, a fake nvidia-smi
"$PYTHON" - "$KNOBWORK" "$SRC/tests" <<'PYEOF'
import sys
sys.path.insert(0, "/usr/lib/juno-kde-fancontrol")
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from mktree import make_dgpu, write_fake_nvidia_smi
from backend.fancore import Curve, discover, parse_knobs, render_config
work = Path(sys.argv[1])
ct = work / "fancontrol"
cpu = parse_knobs(ct.read_text())
make_dgpu(work / "pci", awake=True)
write_fake_nvidia_smi(work / "smi", work / "smi.log", temp_c=67)
(work / "gpu.fancontrol").write_text(render_config(
    Curve(label="custom", minstart=70, knobs=cpu), discover(str(work / "sys")),
    "2026-08-31 07:35", fan_curve="/usr/bin/juno-fan-curve", dgpu=True,
    gpu_curve=Curve(label="custom", minstart=70, knobs=((40, 0), (60, 80), (85, 255))),
    gpu_helper="/usr/bin/juno-gpu-curve"))
PYEOF
got=$(/usr/bin/juno-gpu-curve --config "$KNOBWORK/gpu.fancontrol" --sysfs "$KNOBWORK/sys" \
        --pci "$KNOBWORK/pci" --smi "$KNOBWORK/smi" 2>&1)
# 67 C on the (60,80)..(85,255) segment: (67-60)*175//25 + 80 = 129 -> 129000 mC
[[ "$got" == "129000" ]] \
    && ok gpu-curve-evaluates-installed || bad gpu-curve-evaluates-installed "got '$got'"
got=$(/usr/bin/juno-gpu-temp --pci "$KNOBWORK/pci" --smi "$KNOBWORK/smi" --sysfs "$KNOBWORK/sys" 2>&1)
[[ "$got" == "67000" ]] \
    && ok gpu-temp-evaluates-installed || bad gpu-temp-evaluates-installed "got '$got'"
# a suspended card answers cold and never wakes. The awake checks above are
# legitimate smi calls, so the never-wake assertion is measured from here.
: > "$KNOBWORK/smi.log"
echo suspended > "$KNOBWORK/pci/power/runtime_status"
echo D3cold > "$KNOBWORK/pci/power_state"
got=$(/usr/bin/juno-gpu-temp --pci "$KNOBWORK/pci" --smi "$KNOBWORK/smi" --sysfs "$KNOBWORK/sys" 2>&1)
[[ "$got" == "25000" && ! -s "$KNOBWORK/smi.log" ]] \
    && ok gpu-temp-suspended-installed || bad gpu-temp-suspended-installed "got '$got'"
# fancontrol itself must accept the config that names it as a `!` source
if command -v fancontrol >/dev/null || [[ -x /usr/sbin/fancontrol ]]; then
    /usr/sbin/fancontrol --check "$KNOBWORK/fancontrol" >/dev/null 2>&1 \
        && ok fan-curve-config-checks || bad fan-curve-config-checks "fancontrol --check rejected a knob config"
fi
rm -rf "$KNOBWORK"
# the installed fan-profile is what the GUI scrapes its presets from
"$PYTHON" - <<'PYEOF' && ok presets-scrape-installed || bad presets-scrape-installed "scrape failed"
import sys
sys.path.insert(0, "/usr/lib/juno-kde-fancontrol")
from backend.fancore import parse_presets
p = parse_presets(open("/usr/bin/fan-profile").read())
sys.exit(0 if set(p) == {"quiet", "balanced", "cool", "turbo"} else 1)
PYEOF
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
# the tray monitor must survive a machine with none of its sensors: the
# container has no clevofan, no drm card, no battery and no dGPU.
if QT_QPA_PLATFORM=offscreen timeout 30 /usr/bin/juno-fan-monitor \
        --interval 200 --screenshot-samples 3 --screenshot /tmp/deb-tray.png \
        >/tmp/deb-tray.log 2>&1; then
    [[ -s /tmp/deb-tray.png ]] && ok tray-runs-installed || bad tray-runs-installed "no png"
else
    bad tray-runs-installed "wrapper exited $? : $(tail -3 /tmp/deb-tray.log)"
fi
# The packaged helper AND the packaged fan-profile replay the whole integration
# suite. FANPROFILE points at /usr/bin so T9-T12 exercise the regen path of the
# file the deb actually shipped, not the one in the source tree. The
# regen_custom grep above only reads a name; this is what proves the behaviour.
APPLY=/usr/sbin/juno-fancontrol-apply FANPROFILE=/usr/bin/fan-profile \
    bash "$SRC/tests/test_apply_helper.sh" >/tmp/deb-helper.log 2>&1 \
    && ok deb-helper-suite || bad deb-helper-suite "$(grep FAIL /tmp/deb-helper.log | head -3)"

echo
echo "deb tests: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
