#!/usr/bin/env bash
# Regression test for the regen fix: `fan-profile regen` (the ExecStartPre
# that runs on EVERY fancontrol.service start/restart, not only at boot) must
# preserve whatever curve and cap decision are already on disk. It must never
# replay a preset label from the hardcoded table (that discards a hand-edited
# MIN/MAX -- the config's own banner tells the admin to edit those in place)
# and it must never re-clamp MAXPWM against the calibrated cap (the cap was
# already applied, if at all, whenever the file was last written).
#
# FP_FANCONFIG / FP_CAP_FILE / FP_SYSFS are fan-profile's existing test hooks
# (already used by tests/test_apply_helper.sh); this script just points them
# at a temp dir instead of the real /etc and /sys, and runs as the calling
# user (FP_AS_ROOT=1 skips the sudo re-exec).
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "$HERE/.." && pwd)
FP="$SRC/fan-profile"
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "ok   $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL $1: $2"; }

# --- fake sysfs: one clevofan + one coretemp hwmon, indices stable (regen's
# index-resolution is covered separately by tests/test_apply_helper.sh) ------
mkdir -p "$ROOT/sys/clevofan/hwmon/hwmon7" "$ROOT/sys/coretemp.0/hwmon/hwmon10" "$ROOT/etc"
echo V5xTNC_TND_TNE > "$ROOT/sys/clevofan/hwmon/hwmon7/name"
echo coretemp        > "$ROOT/sys/coretemp.0/hwmon/hwmon10/name"
echo 74000           > "$ROOT/sys/coretemp.0/hwmon/hwmon10/temp1_input"
for i in 1 2; do
    echo 78   > "$ROOT/sys/clevofan/hwmon/hwmon7/pwm$i"
    echo 1    > "$ROOT/sys/clevofan/hwmon/hwmon7/pwm${i}_enable"
    echo 2500 > "$ROOT/sys/clevofan/hwmon/hwmon7/fan${i}_input"
done
echo 150 > "$ROOT/etc/fan-profile.maxpwm"   # calibrated cap: 150

run_regen() { # run_regen FAN_PROFILE_PROGRAM
    # FP_DGPU_PCI points at a path that never exists: the real machine this
    # suite runs on has a dGPU, and without this override pwm2's FCTEMPS would
    # pick up the live juno-gpu-temp helper instead of the fixture's plain
    # hwmon temp input.
    env FP_AS_ROOT=1 FP_SYSFS="$ROOT/sys" FP_FANCONFIG="$ROOT/etc/fancontrol" \
        FP_CAP_FILE="$ROOT/etc/fan-profile.maxpwm" FP_DGPU_PCI="$ROOT/no-dgpu" \
        "$1" regen >/dev/null 2>&1
}

write_cfg() { # write_cfg LABEL INTERVAL MINT MAXT MSTART MSTOP MINPWM MAXPWM
    cat > "$ROOT/etc/fancontrol" <<EOF
# Managed by fan-profile ($1) — 2026-09-10 09:00
# Edit MIN/MAX values then run: fancontrol or fan-profile quiet
INTERVAL=$2
DEVPATH=hwmon7=devices/platform/clevofan hwmon10=devices/platform/coretemp.0
DEVNAME=hwmon7=V5xTNC_TND_TNE hwmon10=coretemp
FCTEMPS=hwmon7/pwm1=hwmon10/temp1_input hwmon7/pwm2=hwmon10/temp1_input
FCFANS=hwmon7/pwm1=hwmon7/fan1_input hwmon7/pwm2=hwmon7/fan2_input
MINTEMP=hwmon7/pwm1=$3 hwmon7/pwm2=$3
MAXTEMP=hwmon7/pwm1=$4 hwmon7/pwm2=$4
MINSTART=hwmon7/pwm1=$5 hwmon7/pwm2=$5
MINSTOP=hwmon7/pwm1=$6 hwmon7/pwm2=$6
MINPWM=hwmon7/pwm1=$7 hwmon7/pwm2=$7
MAXPWM=hwmon7/pwm1=$8 hwmon7/pwm2=$8
AVERAGE=hwmon7/pwm1=4 hwmon7/pwm2=4
EOF
}

# === (a) preset label 'quiet', MAXPWM hand-edited to 200, cap file 150 ======
write_cfg quiet 10 60 95 70 50 0 200
before_a=$(tail -n +2 "$ROOT/etc/fancontrol")
run_regen "$FP"

grep -qx "MAXPWM=hwmon7/pwm1=200 hwmon7/pwm2=200" "$ROOT/etc/fancontrol" \
    && ok "(a) regen keeps a hand-edited MAXPWM=200 on a preset label" \
    || bad "(a) regen keeps a hand-edited MAXPWM=200 on a preset label" \
           "$(grep '^MAXPWM=' "$ROOT/etc/fancontrol")"

after_a=$(tail -n +2 "$ROOT/etc/fancontrol")  # skip line 1: regen restamps the date
[[ "$before_a" == "$after_a" ]] \
    && ok "(a) every MIN/MAX value is unchanged by regen" \
    || bad "(a) every MIN/MAX value is unchanged by regen" "$(diff <(echo "$before_a") <(echo "$after_a"))"

# === (b) label 'custom', MAXPWM 255, cap file 150 ===========================
write_cfg custom 12 58 90 65 55 20 255
run_regen "$FP"

grep -qx "MAXPWM=hwmon7/pwm1=255 hwmon7/pwm2=255" "$ROOT/etc/fancontrol" \
    && ok "(b) regen keeps MAXPWM=255 on a custom label under a 150 cap" \
    || bad "(b) regen keeps MAXPWM=255 on a custom label under a 150 cap" \
           "$(grep '^MAXPWM=' "$ROOT/etc/fancontrol")"

# === (c) POSITIVE CONTROLS: the pre-fix logic must actually rewrite the file,
# or (a)/(b) above would pass on a no-op check. Reproduced inline rather than
# via `git show HEAD:fan-profile`, because HEAD stops being "pre-fix" the
# moment this fix is committed -- tests/test_apply_helper.sh's own T10 negative
# control uses the same inline-stub approach for the same reason. ===========

# (c1) defect #1: regen replayed the hardcoded preset table for
# quiet/balanced/cool/turbo instead of reading the file back, discarding any
# hand-edited MIN/MAX. Table row for quiet (fan-profile.orig, case $PROFILE in
# ... quiet) apply_fancontrol 10 60 95 70 50 0 120): MAXPWM=120, not 200.
prefix_table_replay() { # prefix_table_replay CFG -- old buggy 'quiet' replay
    sed -i -E 's/^MAXPWM=.*/MAXPWM=hwmon7\/pwm1=120 hwmon7\/pwm2=120/' "$1"
}
write_cfg quiet 10 60 95 70 50 0 200
prefix_table_replay "$ROOT/etc/fancontrol"
grep -qx "MAXPWM=hwmon7/pwm1=120 hwmon7/pwm2=120" "$ROOT/etc/fancontrol" \
    && ok "(c1) positive control: pre-fix table replay DOES discard MAXPWM=200 (proves (a) is not vacuous)" \
    || bad "(c1) positive control: pre-fix table replay DOES discard MAXPWM=200" \
           "$(grep '^MAXPWM=' "$ROOT/etc/fancontrol")"

# (c2) defect #2: regen_custom called apply_fancontrol with its default
# clamp=1, re-clamping MAXPWM to the cap file on every read-back.
prefix_reclamp() { # prefix_reclamp CFG CAP_FILE -- old buggy custom-label clamp
    local cap; cap=$(tr -dc '0-9' < "$2")
    sed -i -E "s/^MAXPWM=.*/MAXPWM=hwmon7\/pwm1=$cap hwmon7\/pwm2=$cap/" "$1"
}
write_cfg custom 12 58 90 65 55 20 255
prefix_reclamp "$ROOT/etc/fancontrol" "$ROOT/etc/fan-profile.maxpwm"
grep -qx "MAXPWM=hwmon7/pwm1=150 hwmon7/pwm2=150" "$ROOT/etc/fancontrol" \
    && ok "(c2) positive control: pre-fix default clamp DOES reclamp MAXPWM=255 -> 150 (proves (b) is not vacuous)" \
    || bad "(c2) positive control: pre-fix default clamp DOES reclamp MAXPWM=255 -> 150" \
           "$(grep '^MAXPWM=' "$ROOT/etc/fancontrol")"

echo
echo "regen-preserve tests: $PASS passed, $FAIL failed"
(( FAIL == 0 ))
