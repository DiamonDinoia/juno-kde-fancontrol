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
    echo "${FAN_NAME:-V5xTNC_TND_TNE}" > "$ROOT/sys/clevofan/hwmon/hwmon7/name"
    echo "${TEMP_NAME:-coretemp}" > "$ROOT/sys/coretemp.0/hwmon/hwmon10/name"
    echo "74000" > "$ROOT/sys/coretemp.0/hwmon/hwmon10/temp1_input"
    for ((i = 1; i <= n; i++)); do
        echo 78 > "$ROOT/sys/clevofan/hwmon/hwmon7/pwm$i"
        echo 1 > "$ROOT/sys/clevofan/hwmon/hwmon7/pwm${i}_enable"
        echo 2500 > "$ROOT/sys/clevofan/hwmon/hwmon7/fan${i}_input"
        echo "FAN$i" > "$ROOT/sys/clevofan/hwmon/hwmon7/fan${i}_label"
    done
}

# --- fixture: a dGPU. Default is NO card — the writers' default PCI paths point
# at the real /sys, and this laptop has one, which would leak the GPU branches
# into every dGPU-unaware expectation. Tests that need a card set DGPU_DIR and
# regenerate the wrappers. ---
DGPU_DIR="$ROOT/no-dgpu"
make_dgpu() { # make_dgpu  -> repoints DGPU_DIR at a live (awake, 55 C) fixture
    DGPU_DIR="$ROOT/dgpu"
    mkdir -p "$DGPU_DIR/power"
    echo active > "$DGPU_DIR/power/runtime_status"
    echo D0 > "$DGPU_DIR/power_state"
    export JFC_DGPU_PCI="$DGPU_DIR"
    real_fpwrap
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
FP_LIVE="${FANPROFILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/fan-profile}"
[[ -f "$FP_LIVE" ]] || { echo "FANPROFILE not found: $FP_LIVE" >&2; exit 1; }
real_fpwrap() {
    cat > "$ROOT/bin/fpwrap" <<EOF
#!/bin/bash
exec env FP_AS_ROOT=1 FP_SYSFS="$ROOT/sys" FP_FANCONFIG="$ROOT/etc/fancontrol" \
         FP_CAP_FILE="$ROOT/etc/fan-profile.maxpwm" FP_DGPU_PCI="$DGPU_DIR" \
         FP_GPU_TEMP="$ROOT/bin/juno-gpu-temp" FP_GPU_CURVE="$ROOT/bin/juno-gpu-curve" \
         "$FP_LIVE" "\$@"
EOF
    chmod +x "$ROOT/bin/fpwrap"
}
real_fpwrap

export JFC_PLATFORM_DIR="$ROOT/sys" JFC_FANCONFIG="$ROOT/etc/fancontrol" \
       JFC_CAP_FILE="$ROOT/etc/fan-profile.maxpwm" JFC_SYSTEMCTL="$ROOT/bin/systemctl" \
       JFC_FANCONTROL="$ROOT/bin/fancontrol" JFC_FAN_PROFILE="$ROOT/bin/fan-profile" \
       JFC_NOW="2026-08-31 07:35" JFC_DGPU_PCI="$DGPU_DIR"
# Knob mode needs an executable FCTEMPS source. The real one is python; a stub
# is enough here because nothing in this suite evaluates the curve.
printf '#!/bin/sh\necho 74000\n' > "$ROOT/bin/juno-fan-curve"
printf '#!/bin/sh\necho 55000\n' > "$ROOT/bin/juno-gpu-curve"
printf '#!/bin/sh\necho 55000\n' > "$ROOT/bin/juno-gpu-temp"
chmod +x "$ROOT/bin/"{juno-fan-curve,juno-gpu-curve,juno-gpu-temp}
export JFC_FAN_CURVE="$ROOT/bin/juno-fan-curve" JFC_GPU_CURVE="$ROOT/bin/juno-gpu-curve" \
       JFC_GPU_TEMP="$ROOT/bin/juno-gpu-temp"
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

# T11: a config missing one key must leave regen a no-op. A concatenated -z
# guard let this through and wrote "MINSTART=hwmon7/pwm1=", which
# fancontrol --check accepts and the daemon then reads as 0 (no kick-start).
make_tree 2; reset_state; real_fpwrap
"$APPLY" 12 58 90 65 55 20 100 4 custom >/dev/null 2>&1
sed -i '/^MINSTART=/d' "$ROOT/etc/fancontrol"
before=$(md5sum < "$ROOT/etc/fancontrol")
out=$("$ROOT/bin/fpwrap" regen 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T11-partial-regen-survives || bad T11-partial-regen-survives "rc=$rc $out"
[[ "$before" == "$(md5sum < "$ROOT/etc/fancontrol")" ]] \
    && ok T11-partial-config-untouched || bad T11-partial-config-untouched "$(grep '^MINSTART=' "$ROOT/etc/fancontrol")"
grep -q "cannot parse values" <<< "$out" && ok T11-partial-warns || bad T11-partial-warns "$out"

# T12: one-fan machine, full restart path. The regen replay rewrites the config
# from the live pwm count, so a hardcoded pwm1+pwm2 emit would resurrect pwm2
# here and fancontrol --check would then fail on a device that does not exist.
make_tree 1; reset_state; real_fpwrap
out=$("$APPLY" 12 58 90 65 55 20 100 4 custom 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T12-one-fan-custom || bad T12-one-fan-custom "$out"
grep -q pwm2 "$ROOT/etc/fancontrol" && bad T12-one-fan-regen "pwm2 resurrected by regen" || ok T12-one-fan-regen
grep -qx "MINSTART=hwmon7/pwm1=65" "$ROOT/etc/fancontrol" \
    && ok T12-one-fan-values || bad T12-one-fan-values "$(grep '^MINSTART=' "$ROOT/etc/fancontrol")"

# reference text as backend.fancore.render_config emits it for a knob curve
render_knob_ref() { # render_knob_ref "T:P T:P ..."
    "$PY" - "$ROOT/sys" "$1" "$SRC" "$JFC_FAN_CURVE" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[3])
from backend.fancore import Curve, discover, render_config
knobs = tuple(tuple(map(int, kv.split(":"))) for kv in sys.argv[2].split())
c = Curve(interval=10, minstart=70, average=4, label="custom", knobs=knobs)
c.validate()
sys.stdout.write(render_config(c, discover(sys.argv[1]), "2026-08-31 07:35",
                               fan_curve=sys.argv[4]))
PYEOF
}

# The transfer calibration the GUI sends as positionals in knob mode
# (fancore.KNOB_XFER): INTERVAL MINTEMP MAXTEMP MINSTART MINSTOP MINPWM MAXPWM
# AVERAGE LABEL.
XFER=(10 0 255 70 0 0 255 4 custom)
KN="45:0 60:55 75:110 95:130"

# T13: knob mode writes the knob list, the executable temp source and the
# transfer constants, byte-identical to what fancore.render_config emits.
# NO_RESTART: the restart replays `fan-profile regen`, which stamps a fresh
# date into line 1 and would defeat a byte-exact comparison. T14 covers the
# restart path.
make_tree 2; reset_state; real_fpwrap
out=$(NO_RESTART=1 "$APPLY" --knobs "$KN" "${XFER[@]}" 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T13-knob-apply || bad T13-knob-apply "rc=$rc $out"
diff <(render_knob_ref "$KN") "$ROOT/etc/fancontrol" >/dev/null \
    && ok T13-knob-byte-exact \
    || bad T13-knob-byte-exact "$(diff <(render_knob_ref "$KN") "$ROOT/etc/fancontrol" | head -6)"
grep -qx "# Knobs pwm1: $KN" "$ROOT/etc/fancontrol" \
    && ok T13-knob-line || bad T13-knob-line "$(grep '^# Knobs' "$ROOT/etc/fancontrol")"
grep -q "^FCTEMPS=hwmon7/pwm1=!$JFC_FAN_CURVE " "$ROOT/etc/fancontrol" \
    && ok T13-knob-fctemps || bad T13-knob-fctemps "$(grep '^FCTEMPS=' "$ROOT/etc/fancontrol")"
grep -q "temp1_input" "$ROOT/etc/fancontrol" \
    && bad T13-knob-no-hwmon-temp "FCTEMPS still reads a hwmon temp input" \
    || ok T13-knob-no-hwmon-temp

# T14: the boot contract. The fake systemctl restart replays the REAL
# `fan-profile regen`, so this pins that a reboot does not rewrite the transfer
# calibration. Clamping MAXPWM to the 150 noise cap here would rescale every
# commanded pwm by 150/255 -- a silently 41% slower fan.
make_tree 2; reset_state; real_fpwrap
out=$("$APPLY" --knobs "$KN" "${XFER[@]}" 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T14-knob-apply-restart || bad T14-knob-apply-restart "rc=$rc $out"
grep -qx "MAXPWM=hwmon7/pwm1=255 hwmon7/pwm2=255" "$ROOT/etc/fancontrol" \
    && ok T14-knob-xfer-survived-regen \
    || bad T14-knob-xfer-survived-regen "$(grep '^MAXPWM=' "$ROOT/etc/fancontrol")"
before=$(md5sum < "$ROOT/etc/fancontrol")
out=$("$ROOT/bin/fpwrap" regen 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T14-knob-regen-ok || bad T14-knob-regen-ok "rc=$rc $out"
[[ "$before" == "$(md5sum < "$ROOT/etc/fancontrol")" ]] \
    && ok T14-knob-regen-idempotent \
    || bad T14-knob-regen-idempotent "$(diff <(render_knob_ref "$KN") "$ROOT/etc/fancontrol" | head -6)"

# T14b: same config after an index drift -- knobs kept, fan hwmon refreshed.
mv "$ROOT/sys/clevofan/hwmon/hwmon7" "$ROOT/sys/clevofan/hwmon/hwmon4"
out=$("$ROOT/bin/fpwrap" regen 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T14b-drift-regen-ok || bad T14b-drift-regen-ok "rc=$rc $out"
grep -qx "# Knobs pwm1: $KN" "$ROOT/etc/fancontrol" \
    && ok T14b-drift-keeps-knobs || bad T14b-drift-keeps-knobs "$(grep '^# Knobs' "$ROOT/etc/fancontrol")"
grep -q "^FCTEMPS=hwmon4/pwm1=!$JFC_FAN_CURVE " "$ROOT/etc/fancontrol" \
    && ok T14b-drift-refreshes-index || bad T14b-drift-refreshes-index "$(grep '^FCTEMPS=' "$ROOT/etc/fancontrol")"
mv "$ROOT/sys/clevofan/hwmon/hwmon4" "$ROOT/sys/clevofan/hwmon/hwmon7"

# T15: the helper runs as root on caller-supplied argv, so every malformed knob
# list must be refused BEFORE the config is written.
for spec in "45:0" "45:0 60:55 40:90" "45:200 60:55" "45:0 60:x" "45:0 60:300" \
            "45:0 60:55 75:110 95:130 100:140 105:150 110:160 115:170 120:180 \
             121:190 122:200 123:210 124:220 125:230 126:240 127:250 128:255"; do
    make_tree 2; reset_state; real_fpwrap
    "$APPLY" --knobs "$KN" "${XFER[@]}" >/dev/null 2>&1
    keep=$(md5sum < "$ROOT/etc/fancontrol")
    out=$("$APPLY" --knobs "$spec" "${XFER[@]}" 2>&1); rc=$?
    tag="T15-reject[${spec:0:14}]"
    [[ $rc -ne 0 ]] && ok "$tag" || bad "$tag" "accepted: $out"
    [[ "$keep" == "$(md5sum < "$ROOT/etc/fancontrol")" ]] \
        && ok "$tag-untouched" || bad "$tag-untouched" "config rewritten by a rejected apply"
done

# T16: the calibrated cap (150) has to land on the knob pwms, because in knob
# mode MAXPWM is the transfer constant and clamping it would do nothing.
make_tree 2; reset_state; real_fpwrap
out=$("$APPLY" --knobs "45:0 60:100 95:255" "${XFER[@]}" 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T16-cap-apply || bad T16-cap-apply "rc=$rc $out"
grep -qx "# Knobs pwm1: 45:0 60:100 95:150" "$ROOT/etc/fancontrol" \
    && ok T16-cap-clamps-knobs || bad T16-cap-clamps-knobs "$(grep '^# Knobs' "$ROOT/etc/fancontrol")"
grep -qx "MAXPWM=hwmon7/pwm1=255 hwmon7/pwm2=255" "$ROOT/etc/fancontrol" \
    && ok T16-cap-leaves-xfer || bad T16-cap-leaves-xfer "$(grep '^MAXPWM=' "$ROOT/etc/fancontrol")"

# T17: --ignore-cap cannot be honored for a knob curve -- regen re-applies the
# cap to the knobs at every boot -- so the combination is refused rather than
# accepted and then silently undone. Both option orders must reach the check.
# The knob list is one argument, so each order needs its own array: an
# unquoted string would split "45:0 60:55 ..." across argv.
knobs_first=(--knobs "$KN" --ignore-cap)
cap_first=(--ignore-cap --knobs "$KN")
for tag in knobs-first cap-first; do
    case "$tag" in knobs-first) opts=("${knobs_first[@]}") ;;
                   cap-first)   opts=("${cap_first[@]}") ;; esac
    make_tree 2; reset_state; real_fpwrap
    out=$("$APPLY" "${opts[@]}" "${XFER[@]}" 2>&1); rc=$?
    [[ $rc -ne 0 ]] && ok "T17-refuse[$tag]" || bad "T17-refuse[$tag]" "accepted: $out"
    grep -q "mutually exclusive" <<< "$out" \
        && ok "T17-refuse[$tag]-reason" || bad "T17-refuse[$tag]-reason" "$out"
done

# T17b: the cap is re-applied to the knobs when fan-calibrate lowers it after
# the curve was written, which is the order fan-calibrate --apply actually uses.
make_tree 2; reset_state; real_fpwrap
NO_RESTART=1 "$APPLY" --knobs "45:0 60:100 95:255" "${XFER[@]}" >/dev/null 2>&1
sed -i 's/^# Knobs pwm1:.*/# Knobs pwm1: 45:0 60:100 95:255/' "$ROOT/etc/fancontrol"
echo 90 > "$ROOT/etc/fan-profile.maxpwm"
out=$("$ROOT/bin/fpwrap" regen 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T17b-recap-regen || bad T17b-recap-regen "rc=$rc $out"
grep -qx "# Knobs pwm1: 45:0 60:90 95:90" "$ROOT/etc/fancontrol" \
    && ok T17b-recap-clamps || bad T17b-recap-clamps "$(grep '^# Knobs' "$ROOT/etc/fancontrol")"
echo 150 > "$ROOT/etc/fan-profile.maxpwm"

# T18: no juno-fan-curve means no way to evaluate the curve. Refuse up front
# rather than write a config whose FCTEMPS names a missing program.
make_tree 2; reset_state; real_fpwrap
keep=$(md5sum < "$ROOT/etc/fancontrol")
out=$(JFC_FAN_CURVE="$ROOT/bin/absent-fan-curve" "$APPLY" --knobs "$KN" "${XFER[@]}" 2>&1); rc=$?
[[ $rc -ne 0 ]] && ok T18-no-helper-refused || bad T18-no-helper-refused "accepted: $out"
grep -q "knob curves need" <<< "$out" && ok T18-no-helper-names-it || bad T18-no-helper-names-it "$out"
[[ "$keep" == "$(md5sum < "$ROOT/etc/fancontrol")" ]] \
    && ok T18-no-helper-untouched || bad T18-no-helper-untouched "config rewritten"

# T18b: a knob config whose helper vanished before a reboot. regen must leave
# the file alone so fancontrol aborts into the EC curve, not rewrite it.
make_tree 2; reset_state; real_fpwrap
"$APPLY" --knobs "$KN" "${XFER[@]}" >/dev/null 2>&1
sed -i "s|!$JFC_FAN_CURVE|!$ROOT/bin/absent-fan-curve|g" "$ROOT/etc/fancontrol"
keep=$(md5sum < "$ROOT/etc/fancontrol")
out=$("$ROOT/bin/fpwrap" regen 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T18b-regen-survives || bad T18b-regen-survives "rc=$rc $out"
[[ "$keep" == "$(md5sum < "$ROOT/etc/fancontrol")" ]] \
    && ok T18b-regen-untouched || bad T18b-regen-untouched "regen rewrote a config it cannot evaluate"
grep -q "knob helper" <<< "$out" && ok T18b-regen-warns || bad T18b-regen-warns "$out"

# T19: one-fan machine. The regen replay rewrites from the live pwm count, so a
# hardcoded pwm1+pwm2 knob emit would resurrect pwm2 here.
make_tree 1; reset_state; real_fpwrap
out=$("$APPLY" --knobs "$KN" "${XFER[@]}" 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T19-one-fan-knobs || bad T19-one-fan-knobs "rc=$rc $out"
grep -q pwm2 "$ROOT/etc/fancontrol" && bad T19-one-fan-no-pwm2 "pwm2 resurrected" || ok T19-one-fan-no-pwm2
grep -qx "# Knobs pwm1: $KN" "$ROOT/etc/fancontrol" \
    && ok T19-one-fan-knob-line || bad T19-one-fan-knob-line "$(grep '^# Knobs' "$ROOT/etc/fancontrol")"

# T20: DEVNAME must come from sysfs in BOTH shell writers, not from this
# board's names. fancontrol matches DEVNAME against the sanitized contents of
# <hwmon>/name, so a hardcoded name is a config ValidateDevices rejects. The
# other checks all run on a fixture that reports the real names, so they pass
# whether the writers read sysfs or not -- only a renamed fixture can tell.
FAN_NAME="Clevo Fan X=Y" TEMP_NAME="core temp=2" make_tree 2
reset_state; real_fpwrap
# NO_RESTART: the restart replays `fan-profile regen`, which rewrites DEVNAME
# and would mask a hardcoded name in the helper. Each writer is checked alone.
out=$(NO_RESTART=1 "$APPLY" 10 60 95 70 50 0 120 4 quiet 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T20-renamed-applied || bad T20-renamed-applied "rc=$rc $out"
grep -qx "DEVNAME=hwmon7=Clevo_Fan_X_Y hwmon10=core_temp_2" "$ROOT/etc/fancontrol" \
    && ok T20-apply-reads-devnames \
    || bad T20-apply-reads-devnames "$(grep '^DEVNAME' "$ROOT/etc/fancontrol")"
# Now regen, which rewrites the same line from fan-profile.
out=$("$ROOT/bin/fpwrap" regen 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T20-regen-ran || bad T20-regen-ran "rc=$rc $out"
grep -qx "DEVNAME=hwmon7=Clevo_Fan_X_Y hwmon10=core_temp_2" "$ROOT/etc/fancontrol" \
    && ok T20-regen-reads-devnames \
    || bad T20-regen-reads-devnames "$(grep '^DEVNAME' "$ROOT/etc/fancontrol")"

# --- T21: the GPU fan ------------------------------------------------------------
# The cap fixture is 150; a ref at 255 would be clamped by the apply itself, so
# the dual-knob fixtures stay under it. Values above the cap are T21e's job.
GKN="40:0 60:80 85:150"

# reference text with a card: render_config(dgpu=True, gpu_curve=...)
render_dual_ref() { # render_dual_ref "active knobs" as below
    "$PY" - "$ROOT/sys" "$SRC" "$JFC_FAN_CURVE" "$JFC_GPU_CURVE" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[2])
from backend.fancore import Curve, discover, render_config
hw = discover(sys.argv[1])
cpu = Curve(interval=10, minstart=70, average=4, label="custom",
            knobs=((45, 0), (60, 55), (75, 110), (95, 130)))
gpu = Curve(interval=10, minstart=70, average=4, label="custom",
            knobs=((40, 0), (60, 80), (85, 150)))
sys.stdout.write(render_config(cpu, hw, "2026-08-31 07:35", fan_curve=sys.argv[3],
                               dgpu=True, gpu_curve=gpu, gpu_helper=sys.argv[4]))
PYEOF
}

# T21a: native preset on a dGPU machine: pwm1 coretemp, pwm2 the gpu source,
# byte-identical to render_config(dgpu=True).
make_tree 2; make_dgpu; reset_state
out=$(NO_RESTART=1 "$APPLY" 10 60 95 70 50 0 120 4 quiet 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T21a-native-apply || bad T21a-native-apply "rc=$rc $out"
"$PY" - "$ROOT/sys" "$SRC" "$JFC_GPU_TEMP" > "$ROOT/ref-gpu.txt" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[2])
from backend.fancore import Curve, discover, render_config
c = Curve(interval=10, mintemp=60, maxtemp=95, minstart=70, minstop=50,
          minpwm=0, maxpwm=120, average=4, label="quiet")
sys.stdout.write(render_config(c, discover(sys.argv[1]), "2026-08-31 07:35",
                               dgpu=True, gpu_temp=sys.argv[3]))
PYEOF
diff -q "$ROOT/ref-gpu.txt" "$ROOT/etc/fancontrol" >/dev/null \
    && ok T21a-native-byte-exact \
    || bad T21a-native-byte-exact "$(diff "$ROOT/ref-gpu.txt" "$ROOT/etc/fancontrol" | head -6)"

# T21b: knob mode with a card but no --gpu-knobs is refused, before any write.
make_tree 2; make_dgpu; reset_state
"$APPLY" --knobs "$KN" --gpu-knobs "$GKN" "${XFER[@]}" >/dev/null 2>&1
keep=$(md5sum < "$ROOT/etc/fancontrol")
out=$("$APPLY" --knobs "$KN" "${XFER[@]}" 2>&1); rc=$?
[[ $rc -ne 0 ]] && grep -q "gpu-knobs" <<<"$out" \
    && ok T21b-half-split-refused || bad T21b-half-split-refused "rc=$rc $out"
[[ "$keep" == "$(md5sum < "$ROOT/etc/fancontrol")" ]] \
    && ok T21b-config-untouched || bad T21b-config-untouched "rewritten by a refused apply"

# T21c: dual knob apply, byte-identical to render_config(dgpu=True, gpu_curve).
make_tree 2; make_dgpu; reset_state
out=$(NO_RESTART=1 "$APPLY" --knobs "$KN" --gpu-knobs "$GKN" "${XFER[@]}" 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T21c-dual-apply || bad T21c-dual-apply "rc=$rc $out"
diff <(render_dual_ref) "$ROOT/etc/fancontrol" >/dev/null \
    && ok T21c-dual-byte-exact \
    || bad T21c-dual-byte-exact "$(diff <(render_dual_ref) "$ROOT/etc/fancontrol" | head -8)"
grep -qx "# Knobs pwm1: $KN" "$ROOT/etc/fancontrol" && ok T21c-cpu-line \
    || bad T21c-cpu-line "$(grep '^# Knobs' "$ROOT/etc/fancontrol")"
grep -qx "# Knobs pwm2: $GKN" "$ROOT/etc/fancontrol" && ok T21c-gpu-line \
    || bad T21c-gpu-line "$(grep '^# Knobs' "$ROOT/etc/fancontrol")"

# T21d: boot contract with two curves: regen keeps both lines and refreshes BOTH
# executable sources after an index drift, byte-identical each time.
make_tree 2; make_dgpu; reset_state
"$APPLY" --knobs "$KN" --gpu-knobs "$GKN" "${XFER[@]}" >/dev/null 2>&1
grep -qx "MAXPWM=hwmon7/pwm1=255 hwmon7/pwm2=255" "$ROOT/etc/fancontrol" \
    && ok T21d-xfer-survived-restart \
    || bad T21d-xfer-survived-restart "$(grep '^MAXPWM=' "$ROOT/etc/fancontrol")"
mv "$ROOT/sys/clevofan/hwmon/hwmon7" "$ROOT/sys/clevofan/hwmon/hwmon4"
out=$("$ROOT/bin/fpwrap" regen 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T21d-drift-regen || bad T21d-drift-regen "rc=$rc $out"
grep -q "^FCTEMPS=hwmon4/pwm1=!$JFC_FAN_CURVE hwmon4/pwm2=!$JFC_GPU_CURVE\$" \
    "$ROOT/etc/fancontrol" \
    && ok T21d-drift-splits-sources \
    || bad T21d-drift-splits-sources "$(grep '^FCTEMPS=' "$ROOT/etc/fancontrol")"
grep -qx "# Knobs pwm2: $GKN" "$ROOT/etc/fancontrol" \
    && ok T21d-drift-keeps-gpu-knobs \
    || bad T21d-drift-keeps-gpu-knobs "$(grep '^# Knobs' "$ROOT/etc/fancontrol")"

# T21e: the cap lands on BOTH knob lines.
make_tree 2; make_dgpu; reset_state
"$APPLY" --knobs "45:0 60:100 95:200" --gpu-knobs "40:0 95:255" \
    "${XFER[@]}" >/dev/null 2>&1
grep -qx "# Knobs pwm1: 45:0 60:100 95:150" "$ROOT/etc/fancontrol" \
    && ok T21e-cap-clamps-cpu \
    || bad T21e-cap-clamps-cpu "$(grep '^# Knobs pwm1' "$ROOT/etc/fancontrol")"
grep -qx "# Knobs pwm2: 40:0 95:150" "$ROOT/etc/fancontrol" \
    && ok T21e-cap-clamps-gpu \
    || bad T21e-cap-clamps-gpu "$(grep '^# Knobs pwm2' "$ROOT/etc/fancontrol")"

# T21f: --gpu-knobs without a card is refused (wrong machine, not silently kept).
DGPU_DIR="$ROOT/no-dgpu"; export JFC_DGPU_PCI="$DGPU_DIR"
make_tree 2; reset_state; real_fpwrap
"$APPLY" --knobs "$KN" --gpu-knobs "$GKN" "${XFER[@]}" >/dev/null 2>&1
keep=$(md5sum < "$ROOT/etc/fancontrol")
out=$(NO_RESTART=1 "$APPLY" --knobs "$KN" --gpu-knobs "$GKN" "${XFER[@]}" 2>&1); rc=$?
[[ $rc -ne 0 ]] && grep -q "no dGPU" <<<"$out" \
    && ok T21f-gpu-knobs-no-card || bad T21f-gpu-knobs-no-card "rc=$rc $out"
[[ "$keep" == "$(md5sum < "$ROOT/etc/fancontrol")" ]] \
    && ok T21f-config-untouched || bad T21f-config-untouched "rewritten by a refused apply"

# T21g: a dual config whose gpu helper vanished before reboot: regen leaves the
# file alone so fancontrol aborts into the EC curve.
make_tree 2; make_dgpu; reset_state
"$APPLY" --knobs "$KN" --gpu-knobs "$GKN" "${XFER[@]}" >/dev/null 2>&1
sed -i "s|!$JFC_GPU_CURVE|!$ROOT/bin/absent-gpu-curve|g" "$ROOT/etc/fancontrol"
keep=$(md5sum < "$ROOT/etc/fancontrol")
out=$("$ROOT/bin/fpwrap" regen 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T21g-regen-survives || bad T21g-regen-survives "rc=$rc $out"
[[ "$keep" == "$(md5sum < "$ROOT/etc/fancontrol")" ]] \
    && ok T21g-regen-untouched || bad T21g-regen-untouched "regen rewrote a config it cannot evaluate"
grep -q "knob helper" <<< "$out" && ok T21g-regen-names-helper \
    || bad T21g-regen-names-helper "$out"

# T21h: a dGPU machine running a native preset keeps pwm2 on the gpu source
# after a reboot replay (fpwrap regen writes fan_header itself).
make_tree 2; make_dgpu; reset_state
"$APPLY" 10 60 95 70 50 0 120 4 quiet >/dev/null 2>&1
grep -q "pwm2=!$JFC_GPU_TEMP" "$ROOT/etc/fancontrol" \
    && ok T21h-restart-keeps-gpu-source \
    || bad T21h-restart-keeps-gpu-source "$(grep '^FCTEMPS=' "$ROOT/etc/fancontrol")"

# T21i: fan-calibrate lowers the cap after a dual curve is written, so regen
# re-caps BOTH knob lines — the boot path, distinct from T21e's apply-time
# clamp. Skipping the gpu line here is exactly what M31 measures.
make_tree 2; make_dgpu; reset_state
NO_RESTART=1 "$APPLY" --knobs "$KN" --gpu-knobs "$GKN" "${XFER[@]}" >/dev/null 2>&1
sed -i 's/^# Knobs pwm1:.*/# Knobs pwm1: 45:0 60:100 95:200/
        s/^# Knobs pwm2:.*/# Knobs pwm2: 40:0 95:255/' "$ROOT/etc/fancontrol"
echo 90 > "$ROOT/etc/fan-profile.maxpwm"
out=$("$ROOT/bin/fpwrap" regen 2>&1); rc=$?
[[ $rc -eq 0 ]] && ok T21i-recap-regen || bad T21i-recap-regen "rc=$rc $out"
grep -qx "# Knobs pwm1: 45:0 60:90 95:90" "$ROOT/etc/fancontrol" \
    && ok T21i-recap-cpu || bad T21i-recap-cpu "$(grep '^# Knobs pwm1' "$ROOT/etc/fancontrol")"
grep -qx "# Knobs pwm2: 40:0 95:90" "$ROOT/etc/fancontrol" \
    && ok T21i-recap-gpu || bad T21i-recap-gpu "$(grep '^# Knobs pwm2' "$ROOT/etc/fancontrol")"
echo 150 > "$ROOT/etc/fan-profile.maxpwm"

echo
echo "helper tests: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
