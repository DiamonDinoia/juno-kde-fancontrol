"""Core logic for juno-kde-fancontrol.

Pure python, no Qt: parse/emit fancontrol configs, discover hwmon devices,
read live sensors, compute the fancontrol control law. Everything takes
explicit paths so the container tests can run against fixture trees.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
from dataclasses import dataclass, replace

PWM_MIN, PWM_MAX = 0, 255
DEFAULT_CONFIG = "/etc/fancontrol"
DEFAULT_CAP = "/etc/fan-profile.maxpwm"
DEFAULT_FAN_PROFILE = "/usr/local/bin/fan-profile"
DEFAULT_PLATFORM = "/sys/devices/platform"


class HwmonNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class Curve:
    # Same field order as fan-profile's write_curve args.
    interval: int = 10
    mintemp: int = 60
    maxtemp: int = 95
    minstart: int = 70
    minstop: int = 50
    minpwm: int = 0
    maxpwm: int = 120
    average: int = 4
    label: str = "custom"
    ignore_cap: bool = False  # fan-profile: only turbo ignores the calibrated cap

    def validate(self) -> None:
        """Rules fancontrol itself enforces (it refuses bad configs), plus the
        MINPWM <= MINSTOP < MAXPWM ordering fan-profile documents."""
        c = self
        for name in ("interval", "mintemp", "maxtemp", "minstart", "minstop",
                     "minpwm", "maxpwm", "average"):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f"{name} must be an integer")
        if not 1 <= c.interval <= 60:
            raise ValueError(f"INTERVAL must be 1..60 s, got {c.interval}")
        if not 0 <= c.mintemp <= 120 or not 0 <= c.maxtemp <= 130:
            raise ValueError(f"temps out of range: {c.mintemp}/{c.maxtemp}")
        if c.mintemp >= c.maxtemp:
            raise ValueError(f"MINTEMP ({c.mintemp}) must be < MAXTEMP ({c.maxtemp})")
        for name, v in (("MINSTART", c.minstart), ("MINSTOP", c.minstop),
                        ("MINPWM", c.minpwm), ("MAXPWM", c.maxpwm)):
            if not PWM_MIN <= v <= PWM_MAX:
                raise ValueError(f"{name} must be {PWM_MIN}..{PWM_MAX}, got {v}")
        if c.minpwm > c.minstop:
            raise ValueError(f"MINPWM ({c.minpwm}) must be <= MINSTOP ({c.minstop})")
        if c.minstop >= c.maxpwm:
            raise ValueError(f"MINSTOP ({c.minstop}) must be < MAXPWM ({c.maxpwm})")
        if not 1 <= c.average <= 16:
            raise ValueError(f"AVERAGE must be 1..16, got {c.average}")

    def pwm_at(self, temp_c: int) -> int:
        """fancontrol's control law (UpdateFanSpeeds): MINPWM below MINTEMP,
        linear ramp from MINSTOP to MAXPWM between MINTEMP and MAXTEMP,
        MAXPWM above. Integer math like the shell script (truncate)."""
        c = self
        if temp_c <= c.mintemp:
            return c.minpwm
        if temp_c >= c.maxtemp:
            return c.maxpwm
        return (temp_c - c.mintemp) * (c.maxpwm - c.minstop) // (c.maxtemp - c.mintemp) + c.minstop

    def clamped(self, cap: int | None) -> "Curve":
        """fan-profile's apply_fancontrol: honor the calibrated noise cap
        unless the profile opts out (turbo)."""
        if cap is not None and not self.ignore_cap and self.maxpwm > cap:
            return replace(self, maxpwm=cap)
        return self


@dataclass(frozen=True)
class Hwmon:
    fan_hwmon: str       # e.g. hwmon7  (clevofan: pwm*, fan*_input)
    temp_hwmon: str      # e.g. hwmon10 (coretemp: temp*_input)
    fan_devname: str     # contents of <fan hwmon>/name, e.g. V5xTNC_TND_TNE
    temp_devname: str    # contents of <temp hwmon>/name, e.g. coretemp
    pwms: tuple[str, ...]    # ('pwm1', 'pwm2')
    fans: tuple[str, ...]    # ('fan1_input', 'fan2_input')
    temp_input: str = "temp1_input"


def _glob1(pattern: str) -> str | None:
    matches = sorted(glob.glob(pattern))
    return matches[0] if matches else None


def discover(platform_dir: str = DEFAULT_PLATFORM) -> Hwmon:
    """Locate the clevofan and coretemp hwmon devices by device path — hwmon
    indices drift between boots, never trust a cached hwmonN."""
    fan_dir = _glob1(os.path.join(platform_dir, "clevofan/hwmon/hwmon*"))
    temp_dir = _glob1(os.path.join(platform_dir, "coretemp.0/hwmon/hwmon*"))
    if fan_dir is None:
        raise HwmonNotFound(f"no clevofan hwmon under {platform_dir} (module loaded?)")
    if temp_dir is None:
        raise HwmonNotFound(f"no coretemp hwmon under {platform_dir}")

    def name_of(d: str) -> str:
        # fancontrol sanitizes device names (sed 's/[[:space:]=]/_/g') before
        # matching DEVNAME — emit the same form.
        with open(os.path.join(d, "name"), encoding="ascii") as f:
            return re.sub(r"[\s=]", "_", f.read().strip())

    pwms = tuple(sorted(
        (os.path.basename(p) for p in glob.glob(os.path.join(fan_dir, "pwm[0-9]"))
         if not os.path.basename(p).endswith("_enable")),
        key=lambda s: int(s[3:])))
    if not pwms:
        raise HwmonNotFound(f"{fan_dir} exposes no pwmN files")
    fans = tuple(f"fan{p[3:]}_input" for p in pwms)
    return Hwmon(fan_hwmon=os.path.basename(fan_dir),
                 temp_hwmon=os.path.basename(temp_dir),
                 fan_devname=name_of(fan_dir),
                 temp_devname=name_of(temp_dir),
                 pwms=pwms, fans=fans)


def render_config(curve: Curve, hw: Hwmon, now: str) -> str:
    """Byte-compatible with fan-profile's write_curve output: same two-line
    header (so `fan-profile status` parses the label), same key order and
    spacing. `now` is passed in so tests can compare byte-exact."""
    c = curve
    f, t = hw.fan_hwmon, hw.temp_hwmon

    def per_pwm(key: str, value: int) -> str:
        return key + "=" + " ".join(f"{f}/{p}={value}" for p in hw.pwms)

    lines = [
        f"# Managed by fan-profile ({c.label}) — {now}",
        "# Edit MIN/MAX values then run: fancontrol or fan-profile "
        + (c.label if c.label in ("quiet", "balanced", "cool", "turbo") else "quiet"),
        f"INTERVAL={c.interval}",
        f"DEVPATH={f}=devices/platform/clevofan {t}=devices/platform/coretemp.0",
        f"DEVNAME={f}={hw.fan_devname} {t}={hw.temp_devname}",
        "FCTEMPS=" + " ".join(f"{f}/{p}={t}/{hw.temp_input}" for p in hw.pwms),
        "FCFANS=" + " ".join(f"{f}/{p}={f}/{fan}" for p, fan in zip(hw.pwms, hw.fans)),
    ]
    for key, value in (("MINTEMP", c.mintemp), ("MAXTEMP", c.maxtemp),
                       ("MINSTART", c.minstart), ("MINSTOP", c.minstop),
                       ("MINPWM", c.minpwm), ("MAXPWM", c.maxpwm),
                       ("AVERAGE", c.average)):
        lines.append(per_pwm(key, value))
    return "\n".join(lines) + "\n"


_CONFIG_KEYS = ("MINTEMP", "MAXTEMP", "MINSTART", "MINSTOP", "MINPWM", "MAXPWM", "AVERAGE")


def parse_config(text: str) -> Curve:
    """Read a fancontrol config back into a Curve (per-fan pwm1 values; all
    fans share one curve on every profile this tool emits)."""
    interval = re.search(r"^INTERVAL=(\d+)", text, re.M)
    if not interval:
        raise ValueError("config has no INTERVAL line")
    values = {}
    for key in _CONFIG_KEYS:
        m = re.search(rf"^{key}=\S+/pwm1=(\d+)", text, re.M)
        if not m:
            raise ValueError(f"config has no {key} entry for pwm1")
        values[key.lower()] = int(m.group(1))
    label = "custom"
    m = re.search(r"^# Managed by fan-profile \((\w+)\)", text, re.M)
    if m:
        label = m.group(1)
    return Curve(interval=int(interval.group(1)), **values, label=label)


_PRESET_RE = re.compile(
    r"^\s*(quiet|balanced|cool|turbo)\)\s+apply_fancontrol\s+([^;]+);;", re.M)


def parse_presets(fan_profile_text: str) -> dict[str, Curve]:
    """Scrape the profile table out of /usr/local/bin/fan-profile so the GUI
    presets can never drift from the CLI. 8th arg (when present) is the clamp
    flag: 0 means 'ignore the calibrated cap' (turbo)."""
    presets: dict[str, Curve] = {}
    for name, args in _PRESET_RE.findall(fan_profile_text):
        nums = [int(a) for a in args.split()]
        if len(nums) not in (7, 8):
            raise ValueError(f"profile {name}: expected 7..8 args, got {len(nums)}")
        ignore_cap = len(nums) == 8 and nums[7] == 0
        nums = nums[:7]
        presets[name] = Curve(interval=nums[0], mintemp=nums[1], maxtemp=nums[2],
                              minstart=nums[3], minstop=nums[4], minpwm=nums[5],
                              maxpwm=nums[6], average=4, label=name,
                              ignore_cap=ignore_cap)
    if not presets:
        raise ValueError("no apply_fancontrol profiles found")
    return presets


def read_cap(cap_path: str = DEFAULT_CAP) -> int | None:
    try:
        with open(cap_path, encoding="ascii") as f:
            digits = re.sub(r"[^0-9]", "", f.read())
        return int(digits) if digits else None
    except OSError:
        return None


@dataclass(frozen=True)
class Sensors:
    cpu_temp_c: float | None
    rpms: tuple[int | None, ...]
    pwms: tuple[int | None, ...]
    pwm_enables: tuple[int | None, ...]  # 1=manual (fancontrol), 2=EC auto


def _read_int(path: str) -> int | None:
    try:
        with open(path, encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_sensors(hw: Hwmon, platform_dir: str = DEFAULT_PLATFORM) -> Sensors:
    fan_dir = os.path.join(platform_dir, "clevofan/hwmon", hw.fan_hwmon)
    temp_dir = os.path.join(platform_dir, "coretemp.0/hwmon", hw.temp_hwmon)
    t_milli = _read_int(os.path.join(temp_dir, hw.temp_input))
    return Sensors(
        cpu_temp_c=(t_milli / 1000.0) if t_milli is not None else None,
        rpms=tuple(_read_int(os.path.join(fan_dir, fan)) for fan in hw.fans),
        pwms=tuple(_read_int(os.path.join(fan_dir, pwm)) for pwm in hw.pwms),
        pwm_enables=tuple(_read_int(os.path.join(fan_dir, pwm + "_enable")) for pwm in hw.pwms))


def service_state(systemctl: str = "systemctl") -> tuple[bool, bool]:
    """(active, enabled) for fancontrol.service; (False, False) if systemctl
    is unusable."""
    def q(verb: str) -> bool:
        try:
            return subprocess.run([systemctl, verb, "--quiet", "fancontrol.service"],
                                  timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    return q("is-active"), q("is-enabled")


def pwm_percent(raw: int | None) -> str:
    return "n/a" if raw is None else f"{round(raw * 100 / PWM_MAX)}%"
