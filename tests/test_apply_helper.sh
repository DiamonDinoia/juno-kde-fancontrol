#!/usr/bin/env bash
# Integration test for juno-fancontrol-apply against a fake sysfs + fake
# systemd/fancontrol. The fake systemctl replays the live 20-resync.conf
# ExecStartPre chain (the REAL fan-profile via fpwrap + fancontrol --check),
# which is what enforces the label contract the GUI relies on.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "$HERE/.." && pwd)
PY="${PYTHON:-python3}"
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "PASS $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL $1: $2"; }

# --- fixture: fake sysfs platform tree (drifted indices, like after a reboot) ---
make_tree() { # make_tree N_FANS
    local n="${1:-2}" i
    rm -rf "$ROOT/sys"
    mkdir -p "$ROOT/sys/clevofan/hwmon/hwmon7" "$ROOT/sys/coretemp.0/hwmon/hwmon10"
    echo "V5xTNC_TND_TNE" > "$ROOT/sys/clevofan/hwmon/hwmon7/name"
    echo "coretemp" > "$ROOT/sys/coretemp.0/hwmon/hwmon10/name"
    echo "74000" > "$ROOT/sys/coretemp.0/hwmon/hwmon10/temp1_input"
    for ((i = 1; i <= n; i++)); do
        echo 78 > "$ROOT/sys/clevofan/hwmon/hwmon7/pwm$i"
        echo 1 > "$ROOT/sys/clevofan/hwmon/hwmon7/pwm${i}_enable"
        echo 2500 > "$ROOT/sys/clevofan/hwmon/hwmon7/fan${i}_input"
        echo "FAN$i" > "$ROOT/sys/clevofan/hwmon/hwmon7/fan${i}_label"
    done
}

# --- fixture: fake tools + service state -------------------------------------
mkdir -p "$ROOT/bin" "$ROOT/etc" "$ROOT/state"
cat > "$ROOT/bin/systemctl" <<EOF
#!/bin/bash
echo "\$*" >> "$ROOT/state/systemctl.log"
STATE="$ROOT/state/unit.state"
pre() {  # the live 20-resync.conf ExecStartPre chain
    "$ROOT/bin/fpwrap" regen >/dev/null 2>&1 || return 1
    "$ROOT/bin/fancontrol" --check "$ROOT/etc/fancontrol" >/dev/null 2>&1
}
case "\$1" in
    restart) pre || { echo failed > "\$STATE"; exit 1; } ;;
    start)   pre || exit 1 ;;
esac
case "\$1" in
    restart|start) echo active > "\$STATE" ;;
    is-active)     [[ \$(cat "\$STATE") == active ]] || exit 3 ;;
    is-enabled)    exit 0 ;;
esac
exit 0
EOF
cat > "$ROOT/bin/fancontrol" <<'EOF'
#!/bin/bash
[[ "${FAKE_FANCONTROL_FAIL:-0}" == "1" ]] && exit 1
cfg="${2:-/dev/null}"
[[ "$1" == "--check" && -f "$cfg" ]] || exit 1
grep -q '^INTERVAL=' "$cfg" && grep -q '^FCTEMPS=' "$cfg"
EOF
cat > "$ROOT/bin/fan-profile" <<EOF
#!/bin/bash
echo "\$*" >> "$ROOT/state/fan-profile.log"
exit 0
EOF
chmod +x "$ROOT/bin/"*
echo 150 > "$ROOT/etc/fan-profile.maxpwm"

# The REAL fan-profile under test (defaults to the patched source of truth);
# fpwrap hands it the fake tree/config.
FP_LIVE="${FANPROFILE:-$HOME/system-fixes/fan-profile}"
[[ -f "$FP_LIVE" ]] || { echo "FANPROFILE not found: $FP_LIVE" >&2; exit 1; }
cat > "$ROOT/bin/fpwrap" <<EOF
#!/bin/bash
exec env FP_AS_ROOT=1 FP_SYSFS="$ROOT/sys" FP_FANCONFIG="$ROOT/etc/fancontrol" \
         FP_CAP_FILE="$ROOT/etc/fan-profile.maxpwm" "$FP_LIVE" "\$@"
EOF
chmod +x "$ROOT/bin/fpwrap"

export JFC_PLATFORM_DIR="$ROOT/sys" JFC_FANCONFIG="$ROOT/etc/fancontrol" \
       JFC_CAP_FILE="$ROOT/etc/fan-profile.maxpwm" JFC_SYSTEMCTL="$ROOT/bin/systemctl" \
       JFC_FANCONTROL="$ROOT/bin/fancontrol" JFC_FAN_PROFILE="$ROOT/bin/fan-profile" \
       JFC_NOW="2026-08-31 07:35"
APPLY="${APPLY:-$SRC/juno-fancontrol-apply}"   # APPLY env: test the packaged helper

reset_state() {
    mkdir -p "$ROOT/state"
    : > "$ROOT/state/systemctl.log"; : > "$ROOT/state/fan-profile.log"
    echo active > "$ROOT/state/unit.state"
}

# reference text as backend.fancore.render_config would emit it for the same args
render_ref() { # render_ref MAXPWM_OUT
    "$PY" - "$ROOT/sys" "$1" "$SRC" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[3])
from backend.fancore import Curve, discover, render_config
hw = discover(sys.argv[1])
c = Curve(interval=10, mintemp=60, maxtemp=95, minstart=70, minstop=50,
          minpwm=0, maxpwm=int(sys.argv[2]), average=4, label="quiet")
sys.stdout.write(render_config(c, hw, "2026-08-31 07:35"))
PYEOF
}

# T1: preset label => the on-disk result IS the preset table (regen replays it
# at restart — this test pins that contract). quiet table maxpwm is 120, so the
# helper's 255 arg is clamp-reported but the table wins after the restart replay.
make_tree 2; reset_state
out=$("$APPLY" 10 60 95 70 50 0 255 4 quiet 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T1-apply || bad T1-apply "$out"
[[ -f $ROOT/etc/fancontrol ]] || bad T1-config-missing ""
render_ref 120 > "$ROOT/ref.txt"
# line 1 carries a wall-clock date from fan-profile's regen replay: compare the rest
if diff -q <(tail -n +2 "$ROOT/ref.txt") <(tail -n +2 "$ROOT/etc/fancontrol") >/dev/null 2>&1; then
    ok T1-table-parity
else
    bad T1-table-parity "$(diff -u <(tail -n +2 "$ROOT/ref.txt") <(tail -n +2 "$ROOT/etc/fancontrol") 2>&1 | head -20)"
fi
grep -q "restart fancontrol.service" "$ROOT/state/systemctl.log" && ok T1-restarted || bad T1-restarted ""
grep -q "enable fancontrol.service"  "$ROOT/state/systemctl.log" && ok T1-enabled   || bad T1-enabled ""
grep -q "clamping MAXPWM 255 -> 150" <<< "$out" && ok T1-clamp-msg || bad T1-clamp-msg "$out"

# T2: --ignore-cap reaches the turbo table: regen replay keeps MAXPWM=255
reset_state
"$APPLY" --ignore-cap 5 40 80 100 100 100 255 4 turbo >/dev/null 2>&1
grep -qx "MAXPWM=hwmon7/pwm1=255 hwmon7/pwm2=255" "$ROOT/etc/fancontrol" \
    && ok T2-ignore-cap || bad T2-ignore-cap "$(grep '^MAXPWM=' "$ROOT/etc/fancontrol")"

# T3: invalid curves rejected, existing config untouched
"$APPLY" 10 60 95 70 50 0 120 4 weird >/dev/null 2>&1   # known-good baseline (custom label)
"$APPLY" 10 95 60 70 50 0 120 4 bad >/dev/null 2>&1; rc=$?
[[ $rc -ne 0 ]] && ok T3-mintemp-rejected || bad T3-mintemp-rejected "rc=0"
"$APPLY" 10 60 95 70 50 60 120 4 bad >/dev/null 2>&1; rc=$?  # MINPWM > MINSTOP
[[ $rc -ne 0 ]] && ok T3-minpwm-rejected || bad T3-minpwm-rejected "rc=0"
"$APPLY" 10 60 95 70 50 0 300 4 bad >/dev/null 2>&1; rc=$?   # MAXPWM > 255
[[ $rc -ne 0 ]] && ok T3-maxpwm-rejected || bad T3-maxpwm-rejected "rc=0"
grep -q "weird" "$ROOT/etc/fancontrol" && ok T3-config-preserved || bad T3-config-preserved ""

# T4: fancontrol --check failure -> non-zero rc AND previous config restored
reset_state
FAKE_FANCONTROL_FAIL=1 "$APPLY" 10 55 90 60 45 40 100 4 balanced >/dev/null 2>&1; rc=$?
[[ $rc -ne 0 ]] && ok T4-check-fails || bad T4-check-fails "rc=0"
grep -q "weird" "$ROOT/etc/fancontrol" && ok T4-restored || bad T4-restored "$(head -1 "$ROOT/etc/fancontrol")"

# T5: --auto delegates to fan-profile
reset_state
"$APPLY" --auto >/dev/null 2>&1
grep -qx "auto" "$ROOT/state/fan-profile.log" && ok T5-auto-delegate || bad T5-auto-delegate "$(cat "$ROOT/state/fan-profile.log")"

# T6: NO_RESTART skips systemctl entirely
reset_state
NO_RESTART=1 "$APPLY" 10 60 95 70 50 0 120 4 quiet >/dev/null 2>&1
[[ ! -s "$ROOT/state/systemctl.log" ]] && ok T6-no-restart || bad T6-no-restart "$(cat "$ROOT/state/systemctl.log")"

# T7: one-fan machine: helper writer emits no pwm2 lines (NO_RESTART: the
# 2-fan fan-profile regen replay is out of scope here and covered elsewhere)
make_tree 1; reset_state
NO_RESTART=1 "$APPLY" 10 60 95 70 50 0 120 4 quiet >/dev/null 2>&1
grep -q pwm2 "$ROOT/etc/fancontrol" && bad T7-one-fan "pwm2 leaked" || ok T7-one-fan

# T8: missing clevofan -> clear failure
rm -rf "$ROOT/sys"; mkdir -p "$ROOT/sys/coretemp.0/hwmon/hwmon10"; echo coretemp > "$ROOT/sys/coretemp.0/hwmon/hwmon10/name"
out=$("$APPLY" 10 60 95 70 50 0 120 4 quiet 2>&1); rc=$?
{ [[ $rc -ne 0 ]] && grep -q "clevofan" <<< "$out"; } && ok T8-missing-hwmon || bad T8-missing-hwmon "rc=$rc $out"
make_tree 2

# --- the label contract: custom curves must survive the 20-resync.conf drop-in ---
# T9: custom label + patched fan-profile regen -> restart succeeds end to end,
# values (incl. non-default AVERAGE) preserved, fresh indices, label kept.
reset_state
out=$("$APPLY" 12 58 90 65 55 20 100 6 custom 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T9-custom-apply || bad T9-custom-apply "$out"
grep -q "(custom)" "$ROOT/etc/fancontrol" && ok T9-label-kept || bad T9-label-kept "$(head -1 "$ROOT/etc/fancontrol")"
grep -qx "MINTEMP=hwmon7/pwm1=58 hwmon7/pwm2=58" "$ROOT/etc/fancontrol" \
    && ok T9-values-kept || bad T9-values-kept "$(grep '^MINTEMP=' "$ROOT/etc/fancontrol")"
grep -qx "AVERAGE=hwmon7/pwm1=6 hwmon7/pwm2=6" "$ROOT/etc/fancontrol" \
    && ok T9-average-kept || bad T9-average-kept "$(grep '^AVERAGE=' "$ROOT/etc/fancontrol")"
grep -q "^restart fancontrol.service" "$ROOT/state/systemctl.log" && ok T9-restart-ran || bad T9-restart-ran ""

# T10: NEGATIVE CONTROL — pre-fix regen (rejects non-table labels) must fail the
# apply, roll the file back, AND restart the previous daemon.
reset_state
"$APPLY" 10 60 95 70 50 0 120 4 quiet >/dev/null 2>&1   # known-good baseline
: > "$ROOT/state/systemctl.log"
cat > "$ROOT/bin/fpwrap" <<EOF
#!/bin/bash
# pre-fix regen semantics: labels outside the preset table are fatal
label=\$(grep -m1 '^# Managed by fan-profile' "$ROOT/etc/fancontrol" | sed 's/.*(//; s/).*//')
case "\$label" in quiet|balanced|cool|turbo|auto) exit 0 ;; *) exit 1 ;; esac
EOF
chmod +x "$ROOT/bin/fpwrap"
out=$("$APPLY" 12 58 90 65 55 20 100 4 custom 2>&1); rc=$?
[[ $rc -ne 0 ]] && ok T10-broken-regen-fails || bad T10-broken-regen-fails "rc=0"
grep -q "(quiet)" "$ROOT/etc/fancontrol" && ok T10-rolled-back || bad T10-rolled-back "$(head -1 "$ROOT/etc/fancontrol")"
grep -q "^start fancontrol.service" "$ROOT/state/systemctl.log" \
    && ok T10-daemon-restarted || bad T10-daemon-restarted "$(cat "$ROOT/state/systemctl.log")"

echo
echo "helper tests: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
