#!/usr/bin/env bash
# Gate for the ksystemstats plugin: build it plus a probe against fixtures and
# compare sensor values, including the never-wake rule for a suspended dGPU.
# A container lane; all hardware paths are env-hooked.
set -u
SRC=${SRC:-/src}
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "PASS $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL $1: $2"; }

export DEBIAN_FRONTEND=noninteractive
# apt failures must be visible: burying them once cost a debugging session
# tracing a downstream "cmake: command not found".
apt-get update -qq
if ! apt-get install -y -qq --no-install-recommends build-essential cmake extra-cmake-modules \
    qt6-base-dev libkf6coreaddons-dev libkf6i18n-dev libksysguard-dev libsensors-dev \
    >/tmp/apt.ks.log 2>&1; then
    echo "apt install failed:"; tail -5 /tmp/apt.ks.log; exit 1
fi

PBUILD=$(mktemp -d); PROBE_BUILD=$(mktemp -d)
if ( cd "$PBUILD" && cmake "$SRC/ksystemstats" >/tmp/pcfg.log 2>&1 && make -s >/tmp/pmk.log 2>&1 ); then
    ok plugin-build
else
    bad plugin-build "$(tail -8 /tmp/pcfg.log /tmp/pmk.log 2>/dev/null)"
    echo "plugin tests: $PASS/$((PASS+FAIL))"; exit 1
fi
if ( cd "$PROBE_BUILD" && cmake "$SRC/tests/ksystemstats-probe" >/tmp/qcfg.log 2>&1 && make -s >/tmp/qmk.log 2>&1 ); then
    ok probe-build
else
    bad probe-build "$(tail -8 /tmp/qcfg.log /tmp/qmk.log 2>/dev/null)"
    echo "plugin tests: $PASS/$((PASS+FAIL))"; exit 1
fi
SO="$PBUILD/ksystemstats_plugin_juno.so"
PROBE="$PROBE_BUILD/junoprobe"
[[ -f "$SO" && -x "$PROBE" ]] && ok binaries || { bad binaries ""; exit 1; }

R=$(mktemp -d)

# --- fixture: real hardware, dGPU awake -------------------------------------
python3 - "$R" "$SRC/tests" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from mktree import make_dgpu, make_platform, write_fake_nvidia_smi
root = Path(sys.argv[1])
make_platform(root / "sys/devices/platform", temp_millic=67000)
make_dgpu(root / "sys/bus/pci/devices/0000:01:00.0", awake=True)
write_fake_nvidia_smi(root / "nvidia-smi", root / "smi.log", temp_c=67,
                      util_pct=44, power_w=38.5, memory_mb=1536)
(root / "sys/devices/platform/clevofan/hwmon/hwmon7/pwm1").write_text("128\n")
(root / "sys/devices/platform/clevofan/hwmon/hwmon7/fan1_input").write_text("2400\n")
(root / "sys/devices/platform/clevofan/hwmon/hwmon7/pwm2").write_text("64\n")
(root / "sys/devices/platform/clevofan/hwmon/hwmon7/fan2_input").write_text("2200\n")
gt = root / "sys/class/drm/card0/gt/gt0"; gt.mkdir(parents=True)
(gt / "rc6_residency_ms").write_text("1000\n")
(gt / "rps_act_freq_mhz").write_text("600\n")
nd = root / "sys/class/net"
(nd / "wlan0/device").mkdir(parents=True)          # physical marker
(nd / "lo").mkdir(parents=True)                    # loopback: no device link
(nd / "wlan0/statistics").mkdir(parents=True)
(nd / "wlan0/statistics/rx_bytes").write_text("1000000\n")
(nd / "wlan0/statistics/tx_bytes").write_text("400000\n")
r = root / "sys/class/powercap/intel-rapl/intel-rapl:0"; r.mkdir(parents=True)
(r / "name").write_text("psys\n"); (r / "energy_uj").write_text("100000000\n")
b = root / "sys/class/power_supply/BAT1"; b.mkdir(parents=True)
(b / "type").write_text("Battery\n"); (b / "status").write_text("Discharging\n")
(b / "capacity").write_text("55\n")
(b / "energy_now").write_text("27500000\n"); (b / "energy_full").write_text("50000000\n")
(root / "stat").write_text("cpu  2000 0 0 4000 0 0 0 0 0 0\n")
PYEOF

# Counters need to advance between ticks; every probe window must carry exactly
# one step, or windowed rates (W, %, B/s) read zero or double depending on when
# the step lands relative to the tick — that racing boundary cost a debugging
# session. Probe ticks land at 0.652 s + i·0.65; steps at 0.45 + i·0.65 sit
# safely inside each window.
step_counters() {
    local R="$1"
    local rx=$(( $(cat "$R/sys/class/net/wlan0/statistics/rx_bytes") + 5200 ))
    local tx=$(( $(cat "$R/sys/class/net/wlan0/statistics/tx_bytes") + 1300 ))
    echo "$rx" > "$R/sys/class/net/wlan0/statistics/rx_bytes"
    echo "$tx" > "$R/sys/class/net/wlan0/statistics/tx_bytes"
    local uj=$(( $(cat "$R/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj") + 45500000 ))
    echo "$uj" > "$R/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj"
    local rc6=$(( $(cat "$R/sys/class/drm/card0/gt/gt0/rc6_residency_ms") + 650 ))
    echo "$rc6" > "$R/sys/class/drm/card0/gt/gt0/rc6_residency_ms"
    # stat: 5 busy jiffies + 5 idle jiffies per ~0.65 s tick -> 50% busy
    read -r _ busy _ rest idle _ < "$R/stat"
    echo "cpu  $((busy + 5)) 0 0 $((idle + 5)) 0 0 0 0 0 0" > "$R/stat"
}
stepper() {
    sleep 0.45
    for i in 1 2 3 4 5; do step_counters "$1"; sleep 0.65; done
}
stepper() {
    step_counters "$1"; sleep 0.65; step_counters "$1"; sleep 0.65; step_counters "$1"
    sleep 0.65; step_counters "$1"; sleep 0.75; step_counters "$1"
}

run_probe() { # run_probe ROOT TICKS [NOCOUNTERS]
    if [[ "${3:-}" != nocounters ]]; then
        stepper "$1" &
        stepper_pid=$!
    fi
    env JUNO_KSS_SYSFS="$1/sys" JUNO_KSS_PROC_STAT="$1/stat" \
        JUNO_KSS_DGPU_PCI="$1/sys/bus/pci/devices/0000:01:00.0" \
        JUNO_KSS_NVIDIA_SMI="$1/nvidia-smi" \
        "$PROBE" "$SO" "${2:-4}" 2>/dev/null
    [[ "${3:-}" != nocounters ]] && wait $stepper_pid 2>/dev/null
    return 0
}

OUT=$(run_probe "$R" 4)
grep -q 'juno/cpu/temperature = 67'      <<<"$OUT" && ok cpu-temp || bad cpu-temp "$(grep temperature <<<"$OUT")"
[[ $(sed -n 's|juno/cpu/usage = ||p' <<<"$OUT" | cut -d. -f1) == 50 ]] && ok cpu-busy-50 \
    || bad cpu-busy-50 "$(grep usage <<<"$OUT")"
grep -q 'juno/fan-cpu/rpm = 2400'        <<<"$OUT" && ok fan-cpu-rpm || bad fan-cpu-rpm ""
grep -q 'juno/fan-cpu/duty = 50.19'      <<<"$OUT" && ok fan-cpu-duty || bad fan-cpu-duty "$(grep duty <<<"$OUT")"
grep -q 'juno/fan-gpu/rpm = 2200'        <<<"$OUT" && ok fan-gpu-rpm || bad fan-gpu-rpm ""
grep -q 'juno/dgpu/temperature = 67'     <<<"$OUT" && ok dgpu-temp-awake || bad dgpu-temp-awake "$(grep 'dgpu/temp' <<<"$OUT")"
grep -q 'juno/dgpu/usage = 44'           <<<"$OUT" && ok dgpu-util || bad dgpu-util "$(grep 'dgpu/usage' <<<"$OUT")"
grep -q 'juno/dgpu/activeGpu = dGPU'     <<<"$OUT" && ok active-gpu-dgpu || bad active-gpu-dgpu ""
grep -q 'juno/igpu/usage = '             <<<"$OUT" && ok igpu-busy-present || bad igpu-busy-present ""
w=$(sed -n 's|juno/power/system = ||p' <<<"$OUT")
awk "BEGIN { exit !(\"$w\"+0 > 50 && \"$w\"+0 < 90) }" \
    && ok power-rapl || bad power-rapl "system W off 45.5J/0.65s=70: '$w'"
grep -q 'juno/power/batteryPercentage = 55' <<<"$OUT" && ok battery-pct || bad battery-pct "$(grep battery <<<"$OUT")"
[[ $(sed -n 's|juno/network/download = ||p' <<<"$OUT" | cut -d. -f1) -ge 5000 ]] && ok net-rate \
    || bad net-rate "$(grep network <<<"$OUT")"

# --- dGPU suspended: never woken, honest state ------------------------------
echo suspended > "$R/sys/bus/pci/devices/0000:01:00.0/power/runtime_status"
echo D3cold > "$R/sys/bus/pci/devices/0000:01:00.0/power_state"
: > "$R/smi.log"
OUT=$(run_probe "$R" 3)
[[ ! -s "$R/smi.log" ]] && ok never-wake || bad never-wake "$(cat "$R/smi.log")"
grep -q 'juno/dgpu/state = D3cold' <<<"$OUT" && ok dgpu-state-suspended \
    || bad dgpu-state-suspended "$(grep 'state =' <<<"$OUT")"
grep -q 'juno/dgpu/activeGpu = iGPU' <<<"$OUT" && ok active-gpu-igpu || bad active-gpu-igpu ""
grep -q 'juno/dgpu/temperature = ' <<<"$OUT" && ok dgpu-temp-suspended-blank \
    || bad dgpu-temp-suspended-blank "$(grep 'dgpu/temp' <<<"$OUT")"
dgpu_temp_blank=$(sed -n 's|juno/dgpu/temperature = ||p' <<<"$OUT")
[[ "$dgpu_temp_blank" == "unset" || -z "$dgpu_temp_blank" ]] \
    && ok no-fake-temp || bad no-fake-temp "printed '$dgpu_temp_blank'"

# --- no card at all -----------------------------------------------------------
R2=$(mktemp -d)
python3 - "$R2" "$SRC/tests" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from mktree import make_platform
root = Path(sys.argv[1])
make_platform(root / "sys/devices/platform", temp_millic=67000)
PYEOF
OUT=$(run_probe "$R2" 2 nocounters)
! grep -q 'juno/dgpu/' <<<"$OUT" && ok dgpu-absent-drop-object || bad dgpu-absent-drop-object "$(grep 'dgpu' <<<"$OUT")"

rm -rf "$R" "$R2"
echo
echo "ksystemstats plugin tests: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
