#!/usr/bin/env bash
# Validation entry point inside the container (started by run-container.sh).
# Collects every result and fails at the end — no step is allowed to stay
# silently green. Target distro: Debian unstable (the dev platform).
set -u
export DEBIAN_FRONTEND=noninteractive
SRC=/src

results=()
check() { # check NAME CMD...
    local name="$1"; shift
    echo
    echo "=== $name"
    if "$@"; then results+=("PASS $name"); else results+=("FAIL $name"); fi
}

apt-get update -qq
if ! apt-get install -y -qq --no-install-recommends \
    python3 python3-pyside6.qtwidgets python3-pytest fonts-dejavu-core \
    fancontrol desktop-file-utils \
    libgl1 libegl1 libglib2.0-0t64 libfontconfig1 libfreetype6 libdbus-1-3 libxkbcommon0 \
    > /tmp/apt.log 2>&1; then
    echo "apt install failed:"; tail -5 /tmp/apt.log; exit 1
fi
export PYTHON=python3

# Install from source into the container so renders reflect production
# (helper found, Apply enabled). Runs as container root; fails loudly.
install_out=$(bash "$SRC/install.sh" 2>&1) || { echo "install.sh failed:"; echo "$install_out"; exit 1; }

# The whole directory, never a list of files: a named list silently left
# tests/test_ktheme.py out of the gate for a whole pass.
check fancore-unit      "$PYTHON" -m pytest -q -p no:cacheprovider "$SRC/tests"
check apply-helper      env PYTHON="$PYTHON" bash "$SRC/tests/test_apply_helper.sh"
check deb-package       bash "$SRC/tests/test_deb.sh"
check render-quiet      "$PYTHON" "$SRC/tests/render_app.py" --out /out/shot-quiet.png       --preset quiet
check render-turbo-dark "$PYTHON" "$SRC/tests/render_app.py" --out /out/shot-turbo-dark.png  --preset turbo --dark
check render-auto       "$PYTHON" "$SRC/tests/render_app.py" --out /out/shot-auto.png        --preset quiet --auto
check render-knobs      "$PYTHON" "$SRC/tests/render_app.py" --out /out/shot-knobs.png \
                            --knobs "40:0 55:60 70:90 80:150 95:255"
check render-gpu-knobs  "$PYTHON" "$SRC/tests/render_app.py" --out /out/shot-gpu-knobs.png \
                            --dgpu --fan gpu \
                            --knobs "40:0 55:60 70:90 80:150 95:255" \
                            --gpu-knobs "35:0 50:70 65:130 80:200"
check render-tray-dashboard "$PYTHON" "$SRC/tests/render_tray.py" --out /out/tray-dashboard.png
check render-tray-dark  "$PYTHON" "$SRC/tests/render_tray.py" --out /out/tray-dark.png --dark
check render-tray-off   "$PYTHON" "$SRC/tests/render_tray.py" --out /out/tray-off.png \
                            --dgpu suspended --battery "Not charging"
# tray-min keeps the legacy "chart" probe key in its store: this shot is where
# the read-side migration (probes/chart -> both new charts off) is rendered.
check render-tray-min   "$PYTHON" "$SRC/tests/render_tray.py" --out /out/tray-min.png \
                            --probes igpu,power,battery,chart
mkdir -p /out/control
check render-control    "$PYTHON" "$SRC/tests/render_app.py"  --out /out/control/nochart.png --preset quiet --hide-chart
# Defect control for the dashboard: both utilization charts removed.
check render-tray-ctl   "$PYTHON" "$SRC/tests/render_tray.py" --out /out/control/tray-nocharts.png --hide-charts

echo
echo "=== summary"
fail=0
for r in "${results[@]}"; do
    echo "$r"
    [[ $r == FAIL* ]] && fail=1
done
exit $fail
