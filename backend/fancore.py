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
# The package installs fan-profile in /usr/bin. /usr/local/bin is the legacy
# location from the hand-installed system-fixes era and still wins on PATH, so
# a machine that has both must resolve to whichever one is actually there.
FAN_PROFILE_PATHS = ("/usr/bin/fan-profile", "/usr/local/bin/fan-profile")
DEFAULT_FAN_PROFILE = next((p for p in FAN_PROFILE_PATHS if os.path.exists(p)),
                          FAN_PROFILE_PATHS[0])
DEFAULT_PLATFORM = "/sys/devices/platform"
# The `!`-prefixed FCTEMPS source used in knob mode. Same two-location search as
# fan-profile: packaged in /usr/bin, hand-installed in /usr/local/bin.
FAN_CURVE_PATHS = ("/usr/bin/juno-fan-curve", "/usr/local/bin/juno-fan-curve")
DEFAULT_FAN_CURVE = next((p for p in FAN_CURVE_PATHS if os.path.exists(p)),
                         FAN_CURVE_PATHS[0])
# pwm1 cools the CPU, pwm2 the GPU/chassis (clevofan wiring for this board
# family; the EC labels are the generic "Fan 1"/"Fan 2" and carry nothing).
CPU_PWM, GPU_PWM = "pwm1", "pwm2"
# The dGPU has no hwmon temperature: its sources are executable `!` entries like
# the knob helper. juno-gpu-temp reports plain millidegrees (preset/custom-bands
# mode), juno-gpu-curve the GPU knob curve as a virtual temperature (knob mode).
DEFAULT_DGPU_PCI = "/sys/bus/pci/devices/0000:01:00.0"
GPU_TEMP_PATHS = ("/usr/bin/juno-gpu-temp", "/usr/local/bin/juno-gpu-temp")
DEFAULT_GPU_TEMP = next((p for p in GPU_TEMP_PATHS if os.path.exists(p)),
                        GPU_TEMP_PATHS[0])
GPU_CURVE_PATHS = ("/usr/bin/juno-gpu-curve", "/usr/local/bin/juno-gpu-curve")
DEFAULT_GPU_CURVE = next((p for p in GPU_CURVE_PATHS if os.path.exists(p)),
                         GPU_CURVE_PATHS[0])
# A runtime-suspended dGPU honestly reports no temperature, and asking it would
# wake it (~10 W). The fan it cools belongs at the floor of its curve then, so
# every dGPU source synthesizes 25 C: below any sane first knob/band edge.
COLD_DGPU_MILLIC = 25000
MAX_KNOBS = 16

# fancontrol interpolates ONE linear segment, so a multi-knob curve cannot be
# written into /etc/fancontrol directly. Knob mode instead feeds fancontrol a
# virtual temperature through an executable FCTEMPS source and calibrates the
# segment into the identity: with MINTEMP=0, MAXTEMP=255, MINSTOP=0, MAXPWM=255
# the law
#     pwm = (tval - mint) * (maxpwm - minso) / (maxt - mint) + minso
# becomes pwm = tval * 255 / 255000 = tval / 1000 in exact integer arithmetic.
# The helper therefore reports pwm_at(real_temp) * 1000 millidegrees and
# fancontrol writes back exactly pwm_at(real_temp).
KNOB_XFER = {"mintemp": 0, "maxtemp": 255, "minstop": 0, "minpwm": 0, "maxpwm": 255}


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
    # Knob mode: (temp_c, pwm) control points, ascending temp, non-decreasing
    # pwm, at least two. Empty means the native single-segment law above, where
    # mintemp..maxpwm are the curve. When knobs is set those fields hold
    # KNOB_XFER instead and carry no user-facing meaning.
    knobs: tuple[tuple[int, int], ...] = ()

    def validate(self) -> None:
        """Rules fancontrol itself enforces (it refuses bad configs), plus the
        MINPWM <= MINSTOP < MAXPWM ordering fan-profile documents. In knob mode
        the single-segment fields are the fixed KNOB_XFER calibration, so the
        knob list is validated in their place."""
        c = self
        for name in ("interval", "mintemp", "maxtemp", "minstart", "minstop",
                     "minpwm", "maxpwm", "average"):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f"{name} must be an integer")
        if not 1 <= c.interval <= 60:
            raise ValueError(f"INTERVAL must be 1..60 s, got {c.interval}")
        if not 1 <= c.average <= 16:
            raise ValueError(f"AVERAGE must be 1..16, got {c.average}")
        if not PWM_MIN <= c.minstart <= PWM_MAX:
            raise ValueError(f"MINSTART must be {PWM_MIN}..{PWM_MAX}, got {c.minstart}")
        if c.knobs:
            c._validate_knobs()
            return
        if not 0 <= c.mintemp <= 120 or not 0 <= c.maxtemp <= 130:
            raise ValueError(f"temps out of range: {c.mintemp}/{c.maxtemp}")
        if c.mintemp >= c.maxtemp:
            raise ValueError(f"MINTEMP ({c.mintemp}) must be < MAXTEMP ({c.maxtemp})")
        for name, v in (("MINSTOP", c.minstop), ("MINPWM", c.minpwm),
                        ("MAXPWM", c.maxpwm)):
            if not PWM_MIN <= v <= PWM_MAX:
                raise ValueError(f"{name} must be {PWM_MIN}..{PWM_MAX}, got {v}")
        if c.minpwm > c.minstop:
            raise ValueError(f"MINPWM ({c.minpwm}) must be <= MINSTOP ({c.minstop})")
        if c.minstop >= c.maxpwm:
            raise ValueError(f"MINSTOP ({c.minstop}) must be < MAXPWM ({c.maxpwm})")
        # MINSTART is the kick-start pulse used to spin a stopped fan back up.
        # Below MINSTOP it cannot do that and the fan stays stopped while the
        # temperature climbs. Every fan-profile preset satisfies this.
        if c.minstart < c.minstop:
            raise ValueError(f"MINSTART ({c.minstart}) must be >= MINSTOP ({c.minstop})")

    def _validate_knobs(self) -> None:
        k = self.knobs
        if len(k) < 2:
            raise ValueError(f"a knob curve needs at least 2 knobs, got {len(k)}")
        if len(k) > MAX_KNOBS:
            raise ValueError(f"at most {MAX_KNOBS} knobs, got {len(k)}")
        for t, pwm in k:
            if not isinstance(t, int) or not isinstance(pwm, int):
                raise ValueError(f"knob ({t}, {pwm}) must be a pair of integers")
            if not 0 <= t <= 130:
                raise ValueError(f"knob temperature must be 0..130 C, got {t}")
            if not PWM_MIN <= pwm <= PWM_MAX:
                raise ValueError(f"knob pwm must be {PWM_MIN}..{PWM_MAX}, got {pwm}")
        for (t0, p0), (t1, p1) in zip(k, k[1:]):
            if t1 <= t0:
                raise ValueError(f"knob temperatures must ascend, got {t0} then {t1}")
            # A fan curve that falls with temperature drives the fan the wrong
            # way as the CPU heats up. Reject it rather than let the GUI emit it.
            if p1 < p0:
                raise ValueError(f"knob pwm must not fall, got {p0} then {p1} at {t1} C")

    def pwm_at(self, temp_c: int) -> int:
        """The commanded pwm at a temperature. In knob mode, piecewise linear
        between the knobs and flat outside the end knobs. Otherwise fancontrol's
        own law (UpdateFanSpeeds): MINPWM below MINTEMP, linear ramp from
        MINSTOP to MAXPWM between MINTEMP and MAXTEMP, MAXPWM above. Integer
        truncation in both cases, matching the shell script."""
        c = self
        if c.knobs:
            k = c.knobs
            if temp_c <= k[0][0]:
                return k[0][1]
            if temp_c >= k[-1][0]:
                return k[-1][1]
            for (t0, p0), (t1, p1) in zip(k, k[1:]):
                if temp_c <= t1:
                    return (temp_c - t0) * (p1 - p0) // (t1 - t0) + p0
            raise AssertionError("unreachable: temp_c is below the last knob")
        if temp_c <= c.mintemp:
            return c.minpwm
        if temp_c >= c.maxtemp:
            return c.maxpwm
        return (temp_c - c.mintemp) * (c.maxpwm - c.minstop) // (c.maxtemp - c.mintemp) + c.minstop

    def as_knobs(self) -> tuple[tuple[int, int], ...]:
        """The native single-segment law expressed as knobs, for handing a
        preset to the knob editor without changing what the fan does.

        The native law jumps from MINPWM to MINSTOP at MINTEMP, which a
        polyline cannot hold. The step goes one degree BELOW MINTEMP so the
        ramp knob keeps the native origin (MINTEMP, MINSTOP) and span, making
        the conversion exact at every integer temperature except MINTEMP
        itself, where the fan runs at MINSTOP rather than MINPWM. Putting the
        step above MINTEMP instead shortens the ramp and costs up to 6 pwm
        across its whole length. When MINPWM and MINSTOP coincide (turbo)
        there is no step and two knobs suffice."""
        if self.knobs:
            return self.knobs
        k = []
        if self.minstop != self.minpwm and self.mintemp > 0:
            k.append((self.mintemp - 1, self.minpwm))
        k.append((self.mintemp, self.minstop if k else self.minpwm))
        k.append((self.maxtemp, self.maxpwm))
        return tuple(k)

    def clamped(self, cap: int | None) -> "Curve":
        """fan-profile's apply_fancontrol: honor the calibrated noise cap
        unless the profile opts out (turbo)."""
        if cap is None or self.ignore_cap:
            return self
        if self.knobs:
            if max(pwm for _, pwm in self.knobs) <= cap:
                return self
            return replace(self, knobs=tuple((t, min(pwm, cap))
                                             for t, pwm in self.knobs))
        if self.maxpwm > cap:
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


def knobs_line(knobs: tuple[tuple[int, int], ...], pwm: str = CPU_PWM) -> str:
    """The knob list as the config comment fancontrol ignores and both
    juno-fan-curve and `fan-profile regen` read back. One line per pwm."""
    return f"# Knobs {pwm}: " + " ".join(f"{t}:{pwm_}" for t, pwm_ in knobs)


_KNOBS_TAGGED_RE = r"^# Knobs {pwm}:((?:[ \t]+\d+:\d+)+)[ \t]*$"
_KNOBS_LEGACY_RE = re.compile(r"^# Knobs:((?:[ \t]+\d+:\d+)+)[ \t]*$", re.M)


def parse_knobs(text: str, pwm: str = CPU_PWM) -> tuple[tuple[int, int], ...]:
    """Knobs for one pwm from a config, or () when that pwm has none (native
    single-segment curve). Never raises: a malformed line reads as 'no knobs'
    and the caller falls back to the single-segment law that is also on disk.

    Configs from before the GPU fan earned its own curve carry one untagged
    `# Knobs:` line; that curve drove both fans off the CPU temperature, so it
    reads back as the CPU pwm's knobs and only there."""
    m = re.search(_KNOBS_TAGGED_RE.format(pwm=pwm), text, re.M)
    if not m and pwm == CPU_PWM:
        m = _KNOBS_LEGACY_RE.search(text)
    if not m:
        return ()
    return tuple((int(a), int(b))
                 for a, b in (kv.split(":") for kv in m.group(1).split()))


def render_config(curve: Curve, hw: Hwmon, now: str,
                  fan_curve: str = DEFAULT_FAN_CURVE,
                  *, dgpu: bool = False,
                  gpu_curve: "Curve | None" = None,
                  gpu_helper: str = DEFAULT_GPU_CURVE,
                  gpu_temp: str = DEFAULT_GPU_TEMP) -> str:
    """Byte-compatible with fan-profile's write_curve output: same two-line
    header (so `fan-profile status` parses the label), same key order and
    spacing. `now` is passed in so tests can compare byte-exact.

    A knob curve additionally carries one `# Knobs <pwm>:` line per fan, points
    FCTEMPS at the executable curve sources, and pins the single segment to
    KNOB_XFER so fancontrol writes back the helper's reported value divided by
    1000. With `dgpu` the GPU fan is driven by the dGPU temperature instead of
    coretemp: juno-gpu-curve in knob mode, juno-gpu-temp otherwise.

    In native mode a non-knob `gpu_curve` may carry pwm2's band: when its
    MINTEMP..MAXPWM band differs from pwm1's, those six keys are emitted
    per-pwm (fancontrol's native `KEY=hwmonN/pwm1=v pwm2=v'` format) instead of
    fanned out with one shared value. Identical bands emit exactly the bytes a
    single curve always did."""
    c = curve
    f, t = hw.fan_hwmon, hw.temp_hwmon
    if gpu_curve is not None:
        if not dgpu:
            raise ValueError("a GPU curve without a dGPU would write pwm2 "
                             "onto the CPU temperature")
        if c.knobs and not gpu_curve.knobs:
            raise ValueError("a GPU curve must be knobs, behind a knob CPU curve")
        if gpu_curve.knobs and not c.knobs:
            raise ValueError("a knob GPU curve must sit behind a knob CPU curve")
    if c.knobs and dgpu and gpu_curve is None:
        raise ValueError("a dGPU machine needs the GPU fan's own knobs: the "
                         "CPU curve would drive pwm2 off the CPU temperature")
    # pwm2's own native band: a knob gpu_curve stays in knob mode, where the
    # MIN/MAX keys carry KNOB_XFER for every pwm.
    gpu_band = gpu_curve if gpu_curve is not None and not gpu_curve.knobs else None

    def source_for(p: str) -> str:
        if p == GPU_PWM and dgpu:
            return f"!{gpu_helper}" if c.knobs else f"!{gpu_temp}"
        return f"!{fan_curve}" if c.knobs else f"{t}/{hw.temp_input}"

    def per_pwm(key: str, value: int, gpu_value: int | None = None) -> str:
        out = []
        for p in hw.pwms:
            v = gpu_value if gpu_value is not None and p == GPU_PWM else value
            out.append(f"{f}/{p}={v}")
        return key + "=" + " ".join(out)

    if c.knobs:
        c = replace(c, **KNOB_XFER)
        head = [knobs_line(curve.knobs, CPU_PWM)]
        if gpu_curve is not None:
            head.append(knobs_line(gpu_curve.knobs, GPU_PWM))
        head += ["# Knob curve: edit the knobs in juno-kde-fancontrol. The MIN/MAX",
                 "# values below are the fixed transfer calibration, not the curve."]
    else:
        head = ["# Edit MIN/MAX values then run: fancontrol or fan-profile "
                + (c.label if c.label in ("quiet", "balanced", "cool", "turbo")
                   else "quiet")]

    lines = [
        f"# Managed by fan-profile ({c.label}) — {now}",
        *head,
        f"INTERVAL={c.interval}",
        f"DEVPATH={f}=devices/platform/clevofan {t}=devices/platform/coretemp.0",
        f"DEVNAME={f}={hw.fan_devname} {t}={hw.temp_devname}",
        "FCTEMPS=" + " ".join(f"{f}/{p}={source_for(p)}" for p in hw.pwms),
        "FCFANS=" + " ".join(f"{f}/{p}={f}/{fan}" for p, fan in zip(hw.pwms, hw.fans)),
    ]
    for key, value in (("MINTEMP", c.mintemp), ("MAXTEMP", c.maxtemp),
                       ("MINSTART", c.minstart), ("MINSTOP", c.minstop),
                       ("MINPWM", c.minpwm), ("MAXPWM", c.maxpwm),
                       ("AVERAGE", c.average)):
        # INTERVAL/AVERAGE stay shared (fancontrol treats them per-config in
        # practice); the six band keys fan out per-pwm when gpu_band differs.
        if gpu_band is not None and key not in ("AVERAGE",):
            lines.append(per_pwm(key, value, getattr(gpu_band, key.lower())))
        else:
            lines.append(per_pwm(key, value))
    return "\n".join(lines) + "\n"


_CONFIG_KEYS = ("MINTEMP", "MAXTEMP", "MINSTART", "MINSTOP", "MINPWM", "MAXPWM", "AVERAGE")


def parse_config(text: str, pwm: str = CPU_PWM) -> Curve:
    """Read a fancontrol config back into a Curve, from pwm1's values by
    default; pass pwm="pwm2" for the GPU fan's band (parse_knobs' shape).
    Configs with one shared band read back identical for either pwm."""
    interval = re.search(r"^INTERVAL=(\d+)", text, re.M)
    if not interval:
        raise ValueError("config has no INTERVAL line")
    values = {}
    for key in _CONFIG_KEYS:
        # .* not \S+: pwm2's slot sits after a space on the same line.
        m = re.search(rf"^{key}=.*{pwm}=(\d+)", text, re.M)
        if not m:
            raise ValueError(f"config has no {key} entry for {pwm}")
        values[key.lower()] = int(m.group(1))
    label = "custom"
    m = re.search(r"^# Managed by fan-profile \((\w+)\)", text, re.M)
    if m:
        label = m.group(1)
    return Curve(interval=int(interval.group(1)), **values, label=label,
                 knobs=parse_knobs(text, pwm))


_PRESET_RE = re.compile(
    r"^\s*(quiet|balanced|cool|turbo)\)\s+apply_fancontrol\s+([^;]+);;", re.M)


def parse_presets(fan_profile_text: str) -> dict[str, Curve]:
    """Scrape the profile table out of the fan-profile script so the GUI
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


def dgpu_present(pci_dir: str = DEFAULT_DGPU_PCI) -> bool:
    return os.path.isdir(pci_dir)


def gpu_temp_millic(pci_dir: str = DEFAULT_DGPU_PCI,
                    nvidia_smi: str = "nvidia-smi",
                    platform_dir: str = DEFAULT_PLATFORM) -> int:
    """dGPU temperature in millidegrees for the executable FCTEMPS sources.

    Suspended → COLD_DGPU_MILLIC without touching the GPU: querying a suspended
    card wakes it (~10 W), and the fan it cools belongs at its curve floor
    then. Awake → nvidia-smi. A missing or failing nvidia-smi falls back to
    coretemp instead of exiting non-zero: a permanently failing FCTEMPS source
    aborts fancontrol into restorefans every INTERVAL, which cools worse than
    following the CPU temperature on a machine whose driver stack is broken."""
    from backend.sysmon import read_dgpu  # local: keeps the sampler out of the fan path
    d = read_dgpu(pci_dir, nvidia_smi)
    if not d.present:
        raise HwmonNotFound(f"no dGPU at {pci_dir}")
    if not d.powered:
        return COLD_DGPU_MILLIC
    if d.temp_c is not None:
        return d.temp_c * 1000
    try:
        temp = read_sensors(discover(platform_dir), platform_dir).cpu_temp_c
    except HwmonNotFound:
        temp = None
    if temp is None:
        raise HwmonNotFound("nvidia-smi gave no temperature, coretemp also unreadable")
    return int(round(temp * 1000))


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
