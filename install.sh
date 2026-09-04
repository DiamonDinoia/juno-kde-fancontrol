#!/usr/bin/env bash
# Manual install (no .deb) under /usr/local. Prefer the Debian package
# (sudo apt install ./tests/out/deb/juno-kde-fancontrol_*_all.deb): it uses
# /usr paths, tracks every file in apt, and removes cleanly.
#   sudo bash install.sh
set -euo pipefail

SELF=$(cd "$(dirname "$0")" && pwd)
LIB=/usr/local/lib/juno-kde-fancontrol
HELPER=/usr/local/sbin/juno-fancontrol-apply
DROPIN=/etc/systemd/system/fancontrol.service.d

if [[ $EUID -ne 0 ]]; then
    echo "install.sh needs root — re-run with sudo" >&2
    exit 1
fi

# --- fan stack -------------------------------------------------------------
install -m755 "$SELF/fan-profile"   /usr/local/bin/fan-profile
install -m755 "$SELF/fan-calibrate" /usr/local/bin/fan-calibrate
# The drop-in hardcodes /usr/bin because that is where the package puts
# fan-profile; a /usr/local install has to point it at its own copy.
sed 's|/usr/bin/fan-profile|/usr/local/bin/fan-profile|' \
    "$SELF/systemd/30-juno-fancontrol.conf" \
    | install -D -m644 /dev/stdin "$DROPIN/30-juno-fancontrol.conf"
install -D -m755 "$SELF/systemd/fancontrol-resume" \
    /usr/lib/systemd/system-sleep/fancontrol-resume
install -D -m644 "$SELF/rapl-readable.rules" \
    /usr/local/share/juno-kde-fancontrol/rapl-readable.rules

# --- GUI, tray, root helper ------------------------------------------------
install -d "$LIB/backend"
# Both wrappers run these through python3, so they stay non-executable
# (lintian: a 755 file without an interpreter on PATH is a script error).
install -m644 "$SELF/app.py"  "$LIB/app.py"
install -m644 "$SELF/tray.py" "$LIB/tray.py"
install -m644 "$SELF/fancurve.py" "$LIB/fancurve.py"
install -m644 "$SELF/gputemp.py" "$LIB/gputemp.py"
install -m644 "$SELF/backend/__init__.py" "$LIB/backend/__init__.py"
install -m644 "$SELF/backend/fancore.py"  "$LIB/backend/fancore.py"
install -m644 "$SELF/backend/sysmon.py"   "$LIB/backend/sysmon.py"
install -m644 "$SELF/backend/ktheme.py"   "$LIB/backend/ktheme.py"
install -m755 "$SELF/juno-fancontrol-apply" "$HELPER"
sed "s|@HELPER@|$HELPER|" "$SELF/org.juno.kdefancontrol.policy.in" \
    | install -D -m644 /dev/stdin /usr/share/polkit-1/actions/org.juno.kdefancontrol.policy
install -D -m644 "$SELF/juno-kde-fancontrol.desktop" /usr/share/applications/juno-kde-fancontrol.desktop
install -D -m644 "$SELF/juno-fan-monitor.desktop"    /usr/share/applications/juno-fan-monitor.desktop
# Start the tray monitor at login in every xdg session.
install -D -m644 "$SELF/juno-fan-monitor.desktop" /etc/xdg/autostart/juno-fan-monitor.desktop
# System Settings scans this directory for external modules, so the entry
# appears under System beside Power Management.
install -D -m644 "$SELF/juno-fancontrol-settings.desktop" \
    /usr/share/plasma/systemsettings/externalmodules/juno-fancontrol-settings.desktop
# entry points: the scripts hardcode /usr/lib; rewrite them for a /usr/local install
for w in juno-kde-fancontrol juno-fan-monitor juno-fan-curve juno-gpu-curve juno-gpu-temp; do
    sed "s|/usr/lib/juno-kde-fancontrol|$LIB|" "$SELF/scripts/$w" \
        | install -m755 /dev/stdin "/usr/local/bin/$w"
done

# `[[ ]] && cmd` as the last statement of a set -e script exits 1 when the
# test is false, which is exactly the container case. Use an if.
if [[ -d /run/systemd/system ]]; then systemctl daemon-reload; fi

echo "installed:"
echo "  fan CLI  /usr/local/bin/fan-profile, /usr/local/bin/fan-calibrate"
echo "  app      /usr/local/bin/juno-kde-fancontrol"
echo "  monitor  /usr/local/bin/juno-fan-monitor"
echo "  knobs    /usr/local/bin/juno-fan-curve (FCTEMPS source for knob curves)"
echo "  helper   $HELPER"
echo "  drop-in  $DROPIN/30-juno-fancontrol.conf"
echo "  resume   /usr/lib/systemd/system-sleep/fancontrol-resume"
echo "  policy   /usr/share/polkit-1/actions/org.juno.kdefancontrol.policy"
echo "  launcher /usr/share/applications/juno-{kde-fancontrol,fan-monitor}.desktop"
echo "  autostart /etc/xdg/autostart/juno-fan-monitor.desktop"
echo "  settings /usr/share/plasma/systemsettings/externalmodules/juno-fancontrol-settings.desktop"
echo
echo "The drop-in takes effect on the next start:"
echo "  sudo systemctl restart fancontrol.service"
echo
echo "Optional — total power draw on AC needs the root-only RAPL counter:"
echo "  sudo install -m644 /usr/local/share/juno-kde-fancontrol/rapl-readable.rules \\"
echo "      /etc/udev/rules.d/99-rapl-readable.rules"
echo "  sudo udevadm control --reload && sudo udevadm trigger -s powercap"
