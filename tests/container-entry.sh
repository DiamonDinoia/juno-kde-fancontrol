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
    libgl1 libegl1 libglib2.0-0t64 libfontconfig1 libfreetype6 libdbus-1-3 libxkbcommon0 \
    > /tmp/apt.log 2>&1; then
    echo "apt install failed:"; tail -5 /tmp/apt.log; exit 1
fi
export PYTHON=python3

# Install from source into the container so renders reflect production
# (helper found, Apply enabled). Runs as container root; fails loudly.
install_out=$(bash "$SRC/install.sh" 2>&1) || { echo "install.sh failed:"; echo "$install_out"; exit 1; }

check fancore-unit      "$PYTHON" -m pytest -q -p no:cacheprovider "$SRC/tests/test_fancore.py" "$SRC/tests/test_app_init.py"
check apply-helper      env PYTHON="$PYTHON" bash "$SRC/tests/test_apply_helper.sh"
check deb-package       bash "$SRC/tests/test_deb.sh"
check render-quiet      "$PYTHON" "$SRC/tests/render_app.py" --out /out/shot-quiet.png       --preset quiet
check render-turbo-dark "$PYTHON" "$SRC/tests/render_app.py" --out /out/shot-turbo-dark.png  --preset turbo --dark
check render-auto       "$PYTHON" "$SRC/tests/render_app.py" --out /out/shot-auto.png        --preset quiet --auto

echo
echo "=== summary"
fail=0
for r in "${results[@]}"; do
    echo "$r"
    [[ $r == FAIL* ]] && fail=1
done
exit $fail
