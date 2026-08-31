"""System readings for the juno-kde-fancontrol tray monitor.

Pure python, no Qt. Every reader takes explicit paths so the tests can drive
fixture trees, the same contract as fancore. Counters that only make sense as
a rate (CPU jiffies, GPU RC6 residency, network bytes) are turned into rates
by Sampler, which keeps the previous sample.

All sources are readable without privileges except RAPL, which the kernel
restricts to root since the PLATYPUS side channel. Sampler reports package
power as None in that case rather than pretending.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass

DEFAULT_STAT = "/proc/stat"
DEFAULT_NET = "/proc/net/dev"
DEFAULT_I915_GT = "/sys/class/drm/card0/gt/gt0"
DEFAULT_DGPU_PCI = "/sys/bus/pci/devices/0000:01:00.0"
DEFAULT_POWER_SUPPLY = "/sys/class/power_supply"
DEFAULT_RAPL = "/sys/class/powercap/intel-rapl:1"  # psys: whole-platform domain
DEFAULT_NET_CLASS = "/sys/class/net"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    text = _read(path)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


# --- CPU ------------------------------------------------------------------
def read_cpu_jiffies(stat_path: str = DEFAULT_STAT) -> tuple[int, int] | None:
    """(idle_jiffies, total_jiffies) from the aggregate 'cpu' line. idle counts
    idle+iowait, matching what top reports as idle."""
    text = _read(stat_path)
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("cpu "):
            f = [int(v) for v in line.split()[1:]]
            if len(f) < 5:
                return None
            return f[3] + f[4], sum(f)
    return None


# --- integrated GPU -------------------------------------------------------
def read_rc6_ms(gt_dir: str = DEFAULT_I915_GT) -> int | None:
    """RC6 is the render engine's deep-idle residency. Busy time is wall time
    minus the RC6 delta, which is the only unprivileged i915 utilization
    source: the PMU needs CAP_PERFMON at perf_event_paranoid >= 1.

    ponytail: gt0 (render/compute) only. The media tile gt1 idles separately;
    add it when transcode load matters.
    """
    return _read_int(os.path.join(gt_dir, "rc6_residency_ms"))


def read_igpu_freq_mhz(gt_dir: str = DEFAULT_I915_GT) -> tuple[int | None, int | None]:
    return (_read_int(os.path.join(gt_dir, "rps_act_freq_mhz")),
            _read_int(os.path.join(gt_dir, "rps_max_freq_mhz")))


# --- discrete GPU ---------------------------------------------------------
@dataclass(frozen=True)
class Dgpu:
    present: bool
    powered: bool            # False when runtime-suspended (D3cold) or off
    state: str               # 'suspended', 'active', 'absent', 'D3cold', ...
    temp_c: int | None = None
    util_pct: int | None = None
    power_w: float | None = None
    memory_mb: int | None = None


def read_dgpu(pci_dir: str = DEFAULT_DGPU_PCI, nvidia_smi: str = "nvidia-smi") -> Dgpu:
    """Read the runtime PM state from sysfs FIRST and only run nvidia-smi when
    the device is already awake. Querying a suspended GPU resumes it, which
    costs about 10 W and defeats the purpose of asking whether it is off.
    """
    if not os.path.isdir(pci_dir):
        return Dgpu(present=False, powered=False, state="absent")
    runtime = _read(os.path.join(pci_dir, "power/runtime_status")) or "unknown"
    power_state = _read(os.path.join(pci_dir, "power_state")) or ""
    if runtime != "active":
        return Dgpu(present=True, powered=False, state=f"{runtime} ({power_state})".strip())

    fields = "temperature.gpu,utilization.gpu,power.draw,memory.used"
    try:
        out = subprocess.run([nvidia_smi, f"--query-gpu={fields}",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return Dgpu(present=True, powered=True, state=power_state or "active")
    if out.returncode != 0 or not out.stdout.strip():
        return Dgpu(present=True, powered=True, state=power_state or "active")

    def num(raw: str, cast):
        raw = raw.strip()
        try:
            return cast(raw)
        except ValueError:
            return None

    parts = out.stdout.strip().splitlines()[0].split(",")
    parts += [""] * (4 - len(parts))
    return Dgpu(present=True, powered=True, state=power_state or "active",
                temp_c=num(parts[0], lambda v: int(float(v))),
                util_pct=num(parts[1], lambda v: int(float(v))),
                power_w=num(parts[2], float),
                memory_mb=num(parts[3], lambda v: int(float(v))))


# --- network --------------------------------------------------------------
def read_net_bytes(net_path: str = DEFAULT_NET,
                   net_class_dir: str = DEFAULT_NET_CLASS) -> dict[str, tuple[int, int]]:
    """{iface: (rx_bytes, tx_bytes)} for physical interfaces only.

    Only an interface backed by real hardware has a `device` link under
    /sys/class/net. That one rule drops lo, bridges, veth pairs, tun/tap and
    the VPN interfaces in one go, and a VPN counted alongside its carrier
    would double every byte.
    """
    text = _read(net_path)
    if not text:
        return {}
    out = {}
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if not rest or not os.path.exists(os.path.join(net_class_dir, name, "device")):
            continue
        f = rest.split()
        if len(f) >= 9:
            out[name] = (int(f[0]), int(f[8]))
    return out


# --- battery and power ----------------------------------------------------
@dataclass(frozen=True)
class Battery:
    percent: int | None
    status: str               # Charging / Discharging / Full / Not charging
    power_w: float | None     # discharge (>0) or charge (<0) rate
    seconds_left: int | None  # to empty when discharging, to full when charging


def read_battery(supply_dir: str = DEFAULT_POWER_SUPPLY, name: str = "BAT0") -> Battery:
    """Energy-reporting batteries expose ENERGY_* in µWh and POWER_NOW in µW;
    charge-reporting ones (this laptop) expose CHARGE_* in µAh and CURRENT_NOW
    in µA, so power is current x voltage."""
    d = os.path.join(supply_dir, name)
    status = _read(os.path.join(d, "status")) or "unknown"
    percent = _read_int(os.path.join(d, "capacity"))

    energy_now = _read_int(os.path.join(d, "energy_now"))
    power_now = _read_int(os.path.join(d, "power_now"))
    if energy_now is not None and power_now is not None:
        now_uwh, rate_uw, full_uwh = energy_now, power_now, _read_int(os.path.join(d, "energy_full"))
    else:
        charge_now = _read_int(os.path.join(d, "charge_now"))
        current_ua = _read_int(os.path.join(d, "current_now"))
        volt_uv = _read_int(os.path.join(d, "voltage_now"))
        if charge_now is None or current_ua is None or volt_uv is None:
            return Battery(percent=percent, status=status, power_w=None, seconds_left=None)
        volts = volt_uv / 1e6
        now_uwh = int(charge_now * volts)
        rate_uw = int(abs(current_ua) * volts)
        full = _read_int(os.path.join(d, "charge_full"))
        full_uwh = int(full * volts) if full is not None else None

    power_w = rate_uw / 1e6
    seconds = None
    if rate_uw > 0:
        if status == "Discharging":
            seconds = int(now_uwh * 3600 / rate_uw)
        elif status == "Charging" and full_uwh is not None:
            seconds = int(max(full_uwh - now_uwh, 0) * 3600 / rate_uw)
            power_w = -power_w
    return Battery(percent=percent, status=status, power_w=power_w, seconds_left=seconds)


def read_rapl_uj(rapl_dir: str = DEFAULT_RAPL) -> int | None:
    """Platform energy counter. Returns None when the kernel denies the read,
    which is the default: energy_uj is 0400 root-only since PLATYPUS."""
    return _read_int(os.path.join(rapl_dir, "energy_uj"))


def rapl_readable(rapl_dir: str = DEFAULT_RAPL) -> bool:
    return read_rapl_uj(rapl_dir) is not None


# --- one snapshot ---------------------------------------------------------
@dataclass(frozen=True)
class Snapshot:
    cpu_pct: float | None
    igpu_pct: float | None
    igpu_mhz: int | None
    igpu_max_mhz: int | None
    dgpu: Dgpu
    battery: Battery
    net_rx_bps: float | None
    net_tx_bps: float | None
    net_ifaces: tuple[str, ...]
    package_w: float | None   # RAPL psys; None when the counter is root-only


class Sampler:
    """Turns the monotonic counters into rates. The first sample has no
    previous point, so every rate is None until the second call."""

    def __init__(self, stat_path: str = DEFAULT_STAT, net_path: str = DEFAULT_NET,
                 gt_dir: str = DEFAULT_I915_GT, dgpu_pci: str = DEFAULT_DGPU_PCI,
                 supply_dir: str = DEFAULT_POWER_SUPPLY, rapl_dir: str = DEFAULT_RAPL,
                 battery: str = "BAT0", nvidia_smi: str = "nvidia-smi",
                 net_class_dir: str = DEFAULT_NET_CLASS, clock=time.monotonic) -> None:
        self.stat_path, self.net_path, self.gt_dir = stat_path, net_path, gt_dir
        self.net_class_dir = net_class_dir
        self.dgpu_pci, self.supply_dir, self.rapl_dir = dgpu_pci, supply_dir, rapl_dir
        self.battery, self.nvidia_smi, self.clock = battery, nvidia_smi, clock
        self._prev: dict = {}

    def sample(self) -> Snapshot:
        now = self.clock()
        prev, self._prev = self._prev, {}
        dt = now - prev.get("t", now)
        self._prev["t"] = now

        cpu = read_cpu_jiffies(self.stat_path)
        self._prev["cpu"] = cpu
        cpu_pct = None
        if cpu and prev.get("cpu"):
            d_idle, d_total = cpu[0] - prev["cpu"][0], cpu[1] - prev["cpu"][1]
            if d_total > 0:
                cpu_pct = max(0.0, min(100.0, 100.0 * (1 - d_idle / d_total)))

        rc6 = read_rc6_ms(self.gt_dir)
        self._prev["rc6"] = rc6
        igpu_pct = None
        if rc6 is not None and prev.get("rc6") is not None and dt > 0:
            busy_ms = max(0.0, dt * 1000.0 - (rc6 - prev["rc6"]))
            igpu_pct = max(0.0, min(100.0, 100.0 * busy_ms / (dt * 1000.0)))

        net = read_net_bytes(self.net_path, self.net_class_dir)
        self._prev["net"] = net
        rx_bps = tx_bps = None
        if prev.get("net") and dt > 0:
            shared = [i for i in net if i in prev["net"]]
            if shared:
                # A counter that went backwards means the interface reset; drop
                # that sample instead of reporting a negative or huge rate.
                rx = sum(max(0, net[i][0] - prev["net"][i][0]) for i in shared)
                tx = sum(max(0, net[i][1] - prev["net"][i][1]) for i in shared)
                rx_bps, tx_bps = rx / dt, tx / dt

        rapl = read_rapl_uj(self.rapl_dir)
        self._prev["rapl"] = rapl
        package_w = None
        if rapl is not None and prev.get("rapl") is not None and dt > 0:
            d_uj = rapl - prev["rapl"]
            if d_uj >= 0:  # the counter wraps; skip the wrapping interval
                package_w = d_uj / 1e6 / dt

        act, max_mhz = read_igpu_freq_mhz(self.gt_dir)
        return Snapshot(cpu_pct=cpu_pct, igpu_pct=igpu_pct, igpu_mhz=act,
                        igpu_max_mhz=max_mhz,
                        dgpu=read_dgpu(self.dgpu_pci, self.nvidia_smi),
                        battery=read_battery(self.supply_dir, self.battery),
                        net_rx_bps=rx_bps, net_tx_bps=tx_bps,
                        net_ifaces=tuple(sorted(net)), package_w=package_w)


# --- formatting -----------------------------------------------------------
def fmt_rate(bps: float | None) -> str:
    """Network rate in bytes/s with a binary prefix."""
    if bps is None:
        return "n/a"
    for unit, scale in (("GB/s", 1 << 30), ("MB/s", 1 << 20), ("kB/s", 1 << 10)):
        if bps >= scale:
            return f"{bps / scale:.1f} {unit}"
    return f"{bps:.0f} B/s"


def fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "n/a"
    h, m = divmod(seconds // 60, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"
