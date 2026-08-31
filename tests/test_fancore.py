"""Unit tests for backend.fancore — run in the validation container."""
from __future__ import annotations

import random
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from backend.fancore import (KNOB_XFER, MAX_KNOBS, Curve, HwmonNotFound,
                             discover, parse_config, parse_knobs, parse_presets,
                             pwm_percent, read_cap, read_sensors, render_config)
from mktree import make_platform

HERE = Path(__file__).resolve().parent
LEGACY_SNAPSHOT = HERE / "fixtures" / "juno-fancontrol-legacy"  # old /etc/fancontrol.d tree
FAN_PROFILE_FIXTURE = (HERE / "fixtures" / "fan-profile").read_text()
NOW = "2026-08-31 07:35"

# Byte-exact reference: the same text /usr/local/bin/fan-profile writes for
# `fan-profile quiet` on hwmon7/hwmon10 (captured live 2026-08-30 14:00).
EXPECTED_QUIET = """\
# Managed by fan-profile (quiet) — 2026-08-31 07:35
# Edit MIN/MAX values then run: fancontrol or fan-profile quiet
INTERVAL=10
DEVPATH=hwmon7=devices/platform/clevofan hwmon10=devices/platform/coretemp.0
DEVNAME=hwmon7=V5xTNC_TND_TNE hwmon10=coretemp
FCTEMPS=hwmon7/pwm1=hwmon10/temp1_input hwmon7/pwm2=hwmon10/temp1_input
FCFANS=hwmon7/pwm1=hwmon7/fan1_input hwmon7/pwm2=hwmon7/fan2_input
MINTEMP=hwmon7/pwm1=60 hwmon7/pwm2=60
MAXTEMP=hwmon7/pwm1=95 hwmon7/pwm2=95
MINSTART=hwmon7/pwm1=70 hwmon7/pwm2=70
MINSTOP=hwmon7/pwm1=50 hwmon7/pwm2=50
MINPWM=hwmon7/pwm1=0 hwmon7/pwm2=0
MAXPWM=hwmon7/pwm1=120 hwmon7/pwm2=120
AVERAGE=hwmon7/pwm1=4 hwmon7/pwm2=4
"""

QUIET = Curve(interval=10, mintemp=60, maxtemp=95, minstart=70, minstop=50,
              minpwm=0, maxpwm=120, average=4, label="quiet")


def test_discover_two_fans(tmp_path: Path) -> None:
    platform = make_platform(tmp_path / "platform")
    hw = discover(str(platform))
    assert hw.fan_hwmon == "hwmon7"
    assert hw.temp_hwmon == "hwmon10"
    assert hw.fan_devname == "V5xTNC_TND_TNE"
    assert hw.temp_devname == "coretemp"
    assert hw.pwms == ("pwm1", "pwm2")
    assert hw.fans == ("fan1_input", "fan2_input")


def test_discover_missing_fans(tmp_path: Path) -> None:
    with pytest.raises(HwmonNotFound):
        discover(str(tmp_path / "empty"))


def test_discover_sanitizes_devname(tmp_path: Path) -> None:
    # fancontrol sanitizes names ('s/[[:space:]=]/_/g') before matching
    # DEVNAME; discover() must emit the same form or every config looks stale.
    platform = make_platform(tmp_path / "platform", fan_name="Clevo Fan X=Y",
                             temp_name="core temp=2")
    hw = discover(str(platform))
    assert hw.fan_devname == "Clevo_Fan_X_Y"
    assert hw.temp_devname == "core_temp_2"
    # Both names come from sysfs. A writer that hardcodes either emits a config
    # fancontrol's ValidateDevices rejects, which is why the shell writers are
    # held to the same fixture (T20 in tests/test_apply_helper.sh).
    assert ("DEVNAME=hwmon7=Clevo_Fan_X_Y hwmon10=core_temp_2"
            in render_config(QUIET, hw, NOW))


def test_render_byte_exact(tmp_path: Path) -> None:
    hw = discover(str(make_platform(tmp_path / "platform")))
    assert render_config(QUIET, hw, NOW) == EXPECTED_QUIET


def test_render_parse_roundtrip(tmp_path: Path) -> None:
    hw = discover(str(make_platform(tmp_path / "platform")))
    text = render_config(QUIET, hw, NOW)
    assert parse_config(text) == QUIET


def test_render_one_fan_has_no_pwm2(tmp_path: Path) -> None:
    hw = discover(str(make_platform(tmp_path / "platform", n_fans=1)))
    text = render_config(QUIET, hw, NOW)
    assert "pwm2" not in text
    assert "FCTEMPS=hwmon7/pwm1=hwmon10/temp1_input\n" in text


def test_parse_legacy_snapshot() -> None:
    # The legacy /etc/fancontrol.d snapshot (pre fan-profile daemon layout).
    text = LEGACY_SNAPSHOT.read_text()
    c = parse_config(text)
    assert (c.interval, c.mintemp, c.maxtemp) == (60, 75, 92)
    assert (c.minstart, c.minstop, c.minpwm, c.maxpwm) == (150, 51, 0, 206)
    assert c.label == "custom"  # no "Managed by" header in the legacy file


def test_resync_after_hwmon_drift(tmp_path: Path) -> None:
    # Positive control for the bug class that broke the live box: the stored
    # config names hwmon9 (stale) while clevofan is hwmon7 after a reboot.
    stale = LEGACY_SNAPSHOT.read_text()
    assert "hwmon9" in stale
    c = parse_config(stale)
    hw = discover(str(make_platform(tmp_path / "platform")))  # hwmon7/hwmon10
    rewritten = render_config(c, hw, NOW)
    assert "hwmon7/pwm1=hwmon10/temp1_input" in rewritten
    assert "hwmon9" not in rewritten
    assert parse_config(rewritten) == c


FANCONTROL = "/usr/sbin/fancontrol"

# fancontrol's UpdateFanSpeeds, lifted verbatim from the packaged script:
# `local -i pwmval` is what makes the quoted expression evaluate as arithmetic,
# and mint/maxt are millidegrees (MINTEMP*1000), not degrees.
_LAW_SH = r"""
law() {
    local -i pwmval
    local tval=$1 mint=$2 maxt=$3 minso=$4 minpwm=$5 maxpwm=$6
    if (( tval <= mint )); then pwmval=$minpwm
    elif (( tval >= maxt )); then pwmval=$maxpwm
    else pwmval="(${tval}-${mint})*(${maxpwm}-${minso})/(${maxt}-${mint})+${minso}"
    fi
    echo $pwmval
}
while read -r a b c d e f; do law "$a" "$b" "$c" "$d" "$e" "$f"; done
"""


def test_fancontrol_law_source_unchanged() -> None:
    """_LAW_SH is a copy. Fail loudly when the packaged script stops matching it,
    instead of silently validating pwm_at against a stale transcription."""
    src = Path(FANCONTROL).read_text()
    assert "local -i pwmval" in src, "pwmval is no longer an integer variable"
    assert 'pwmval="(${tval}-${mint})*(${maxpwm}-${minso})/(${maxt}-${mint})+${minso}"' in src
    assert 'let mint="${AFCMINTEMP[$fcvcount]}*1000"' in src, "MINTEMP scaling changed"
    assert 'let maxt="${AFCMAXTEMP[$fcvcount]}*1000"' in src, "MAXTEMP scaling changed"
    # Knob mode rests entirely on the executable FCTEMPS source: a `!` prefix
    # runs the rest as a command and uses its stdout as the reading. Both the
    # validation and the read site must keep doing that.
    assert 'tlastval="$(${tsens:1})"' in src, "the ! temp-source hook is gone"
    assert 'if [ ${tsen::1} == "!" ]' in src, "ValidateDevices no longer accepts !cmd"


def test_pwm_at_matches_fancontrol_over_random_curves() -> None:
    """Differential: pwm_at vs the shell arithmetic over the whole chart range.
    The scale mismatch (fancontrol truncates in millidegrees, pwm_at in degrees)
    is the failure this catches."""
    assert shutil.which("bash"), "bash required"
    rng = random.Random(20260831)
    curves = []
    while len(curves) < 300:
        mintemp = rng.randint(0, 100)
        maxtemp = rng.randint(mintemp + 1, 130)
        minstop = rng.randint(0, 254)
        maxpwm = rng.randint(minstop + 1, 255)
        c = Curve(mintemp=mintemp, maxtemp=maxtemp, minstop=minstop, maxpwm=maxpwm,
                  minpwm=rng.randint(0, minstop), minstart=rng.randint(minstop, 255))
        c.validate()
        curves.append(c)

    temps = list(range(-5, 136))   # past both ends of the chart range
    pairs = [(c, t) for c in curves for t in temps]
    stdin = "".join(f"{t * 1000} {c.mintemp * 1000} {c.maxtemp * 1000} "
                    f"{c.minstop} {c.minpwm} {c.maxpwm}\n" for c, t in pairs)
    out = subprocess.run(["bash", "-c", _LAW_SH], input=stdin, text=True,
                         capture_output=True, check=True).stdout.split()
    assert len(out) == len(pairs)
    mismatches = [(c, t, int(ref)) for (c, t), ref in zip(pairs, out)
                  if c.pwm_at(t) != int(ref)]
    assert not mismatches, f"{len(mismatches)} mismatches, first: {mismatches[0]}"


def test_pwm_at_matches_fancontrol_law() -> None:
    # UpdateFanSpeeds(): MINPWM below MINTEMP, ramp MINSTOP->MAXPWM (integer
    # truncation like the shell), MAXPWM from MAXTEMP up.
    assert QUIET.pwm_at(20) == 0
    assert QUIET.pwm_at(60) == 0      # <= MINTEMP -> MINPWM, not MINSTOP
    assert QUIET.pwm_at(61) == 52     # (61-60)*(120-50)//35 + 50
    assert QUIET.pwm_at(77) == 84     # (77-60)*(120-50)//35 + 50
    assert QUIET.pwm_at(95) == 120
    assert QUIET.pwm_at(100) == 120
    balanced = parse_presets(FAN_PROFILE_FIXTURE)["balanced"]
    assert balanced.pwm_at(30) == 40  # MINPWM below MINTEMP (not MINSTOP=45)
    assert balanced.pwm_at(55) == 40


@pytest.mark.parametrize("bad", [
    dict(mintemp=95, maxtemp=60),     # MINTEMP >= MAXTEMP
    dict(minpwm=60),                  # MINPWM > MINSTOP (50)
    dict(minstop=130),                # MINSTOP >= MAXPWM (120)
    dict(maxpwm=300),                 # > 255
    dict(interval=0),
    dict(average=0),
    dict(minstart=40),                # MINSTART < MINSTOP (50): no kick-start
])
def test_validate_rejects_bad_curves(bad: dict) -> None:
    with pytest.raises(ValueError):
        Curve(**{**QUIET.__dict__, **bad}).validate()


def test_validate_accepts_all_presets() -> None:
    for p in parse_presets(FAN_PROFILE_FIXTURE).values():
        p.clamped(150).validate()     # what the helper would write
        p.validate()                  # and the raw table itself


def test_presets_scraped_from_fan_profile() -> None:
    presets = parse_presets(FAN_PROFILE_FIXTURE)
    assert set(presets) == {"quiet", "balanced", "cool", "turbo"}
    q = presets["quiet"]
    assert (q.interval, q.mintemp, q.maxtemp, q.minstart, q.minstop,
            q.minpwm, q.maxpwm) == (10, 60, 95, 70, 50, 0, 120)
    assert presets["turbo"].ignore_cap is True
    assert presets["balanced"].ignore_cap is False
    assert presets["balanced"].maxpwm == 255  # clamped later, not in the table


def test_presets_reject_garbage() -> None:
    with pytest.raises(ValueError):
        parse_presets("# nothing here\n")


def test_cap_clamp() -> None:
    loud = Curve(maxpwm=255)
    assert loud.clamped(150).maxpwm == 150
    assert loud.clamped(None).maxpwm == 255
    turbo = Curve(maxpwm=255, ignore_cap=True)
    assert turbo.clamped(150).maxpwm == 255   # turbo exempt, fan-profile semantics
    assert QUIET.clamped(150).maxpwm == 120   # already below cap: untouched


def test_read_cap(tmp_path: Path) -> None:
    f = tmp_path / "cap"
    assert read_cap(str(f)) is None
    f.write_text("150\n")
    assert read_cap(str(f)) == 150
    f.write_text("max pwm: 130!\n")
    assert read_cap(str(f)) == 130


def test_read_sensors(tmp_path: Path) -> None:
    platform = make_platform(tmp_path / "platform")
    hw = discover(str(platform))
    s = read_sensors(hw, str(platform))
    assert s.cpu_temp_c == 74.0
    assert s.rpms == (2560, 2480)
    assert s.pwms == (78, 78)
    assert s.pwm_enables == (1, 1)
    # a dead tachometer must read as n/a, not crash
    (platform / "clevofan/hwmon/hwmon7/fan2_input").write_text("not-a-number\n")
    s = read_sensors(hw, str(platform))
    assert s.rpms == (2560, None)


def test_pwm_percent() -> None:
    assert pwm_percent(None) == "n/a"
    assert pwm_percent(0) == "0%"
    assert pwm_percent(255) == "100%"
    assert pwm_percent(78) == "31%"


# --- knob curves -----------------------------------------------------------
KNOBS = ((45, 0), (60, 55), (75, 110), (95, 255))
KNOB_CURVE = Curve(interval=10, minstart=70, average=4, label="custom", knobs=KNOBS)

EXPECTED_KNOB = """\
# Managed by fan-profile (custom) — 2026-08-31 07:35
# Knobs: 45:0 60:55 75:110 95:255
# Knob curve: edit the knobs in juno-kde-fancontrol. The MIN/MAX
# values below are the fixed transfer calibration, not the curve.
INTERVAL=10
DEVPATH=hwmon7=devices/platform/clevofan hwmon10=devices/platform/coretemp.0
DEVNAME=hwmon7=V5xTNC_TND_TNE hwmon10=coretemp
FCTEMPS=hwmon7/pwm1=!/usr/bin/juno-fan-curve hwmon7/pwm2=!/usr/bin/juno-fan-curve
FCFANS=hwmon7/pwm1=hwmon7/fan1_input hwmon7/pwm2=hwmon7/fan2_input
MINTEMP=hwmon7/pwm1=0 hwmon7/pwm2=0
MAXTEMP=hwmon7/pwm1=255 hwmon7/pwm2=255
MINSTART=hwmon7/pwm1=70 hwmon7/pwm2=70
MINSTOP=hwmon7/pwm1=0 hwmon7/pwm2=0
MINPWM=hwmon7/pwm1=0 hwmon7/pwm2=0
MAXPWM=hwmon7/pwm1=255 hwmon7/pwm2=255
AVERAGE=hwmon7/pwm1=4 hwmon7/pwm2=4
"""


def test_knob_render_byte_exact(tmp_path: Path) -> None:
    hw = discover(str(make_platform(tmp_path / "platform")))
    got = render_config(KNOB_CURVE, hw, NOW, fan_curve="/usr/bin/juno-fan-curve")
    assert got == EXPECTED_KNOB


def test_knob_render_parse_roundtrip(tmp_path: Path) -> None:
    hw = discover(str(make_platform(tmp_path / "platform")))
    text = render_config(KNOB_CURVE, hw, NOW, fan_curve="/usr/bin/juno-fan-curve")
    back = parse_config(text)
    assert back.knobs == KNOBS
    # The single-segment fields on disk are the transfer calibration, so a
    # round trip must reproduce them and not the caller's stale values.
    for key, want in KNOB_XFER.items():
        assert getattr(back, key) == want, key
    assert back.interval == 10 and back.minstart == 70 and back.average == 4


def test_knob_resync_after_hwmon_drift(tmp_path: Path) -> None:
    """The boot-time regen path: a knob config re-rendered onto this boot's
    indices keeps the knobs and the executable temp source."""
    stale = render_config(KNOB_CURVE,
                          discover(str(make_platform(tmp_path / "old",
                                                     fan_hw="hwmon9", temp_hw="hwmon3"))),
                          NOW, fan_curve="/usr/bin/juno-fan-curve")
    c = parse_config(stale)
    fresh = render_config(c, discover(str(make_platform(tmp_path / "new"))), NOW,
                          fan_curve="/usr/bin/juno-fan-curve")
    assert "hwmon9" not in fresh and "hwmon3" not in fresh
    assert "FCTEMPS=hwmon7/pwm1=!/usr/bin/juno-fan-curve" in fresh
    assert parse_config(fresh).knobs == KNOBS


@pytest.mark.parametrize("text, want", [
    ("# Knobs: 45:0 95:255\n", ((45, 0), (95, 255))),
    ("# Knobs:\t40:10\t80:200\n", ((40, 10), (80, 200))),
    ("# Knobs: nonsense\n", ()),
    ("# Knobs: 45:0 95\n", ()),          # a bare temperature is not a knob
    ("# Knobs: 45:0 -60:55\n", ()),      # negatives are not knobs
    ("#Knobs: 45:0 95:255\n", ()),       # fancontrol's own comment style differs
    ("nothing here\n", ()),
])
def test_parse_knobs(text: str, want: tuple) -> None:
    """A malformed knob line reads as 'no knobs', so the caller falls back to
    the single-segment law that is also on disk rather than crashing."""
    assert parse_knobs(text) == want


def test_knob_interpolation() -> None:
    c = KNOB_CURVE
    assert c.pwm_at(20) == 0 and c.pwm_at(45) == 0        # flat left of knob 0
    assert c.pwm_at(95) == 255 and c.pwm_at(120) == 255   # flat right of the last
    assert c.pwm_at(60) == 55 and c.pwm_at(75) == 110     # on the knobs
    assert c.pwm_at(52) == 25    # (52-45)*55//15 + 0
    assert c.pwm_at(67) == 80    # (67-60)*55//15 + 55
    assert c.pwm_at(85) == 182   # (85-75)*145//20 + 110


@pytest.mark.parametrize("bad", [
    ((60, 100),),                          # one knob is not a curve
    tuple((40 + i, i) for i in range(MAX_KNOBS + 1)),   # too many
    ((60, 50), (60, 90)),                  # duplicate temperature
    ((60, 50), (55, 90)),                  # descending temperature
    ((60, 90), (80, 50)),                  # falling pwm drives the fan backwards
    ((60, 50), (131, 90)),                 # temperature out of range
    ((60, 50), (80, 256)),                 # pwm out of range
    ((60, 50), (80, 90.0)),                # non-integer pwm
])
def test_validate_rejects_bad_knobs(bad: tuple) -> None:
    with pytest.raises(ValueError):
        Curve(label="custom", minstart=70, knobs=bad).validate()


def test_as_knobs_reproduces_every_preset() -> None:
    """Handing a preset to the knob editor must not change what the fan does.
    The native law jumps at MINTEMP and a polyline cannot, so exactly that one
    degree may differ, and only upward (more cooling)."""
    for name, native in parse_presets(FAN_PROFILE_FIXTURE).items():
        knob = Curve(knobs=native.as_knobs(), minstart=native.minstart, label=name)
        knob.validate()
        differ = [t for t in range(0, 131) if native.pwm_at(t) != knob.pwm_at(t)]
        assert differ in ([], [native.mintemp]), f"{name}: differs at {differ}"
        for t in differ:
            assert knob.pwm_at(t) > native.pwm_at(t), f"{name}: less cooling at {t}"


def test_as_knobs_is_identity_on_a_knob_curve() -> None:
    assert KNOB_CURVE.as_knobs() == KNOBS


def test_knob_cap_clamp() -> None:
    capped = KNOB_CURVE.clamped(120)
    assert capped.knobs == ((45, 0), (60, 55), (75, 110), (95, 120))
    capped.validate()          # clamping must not break the non-falling order
    assert KNOB_CURVE.clamped(255) is KNOB_CURVE       # nothing to do
    assert Curve(label="turbo", minstart=70, knobs=KNOBS,
                 ignore_cap=True).clamped(120).knobs == KNOBS


def test_knob_transfer_is_exact_through_the_real_fancontrol_law() -> None:
    """The decisive differential for knob mode: feed pwm_at(t)*1000 millidegrees
    through fancontrol's own arithmetic under the KNOB_XFER calibration and
    require the commanded pwm back, exactly, for random curves over the whole
    range. A truncation or an off-by-one in the encoding shows up here."""
    assert shutil.which("bash"), "bash required"
    rng = random.Random(20260831)
    curves = []
    while len(curves) < 120:
        temps = sorted(rng.sample(range(0, 131), rng.randint(2, MAX_KNOBS)))
        pwms = sorted(rng.randint(0, 255) for _ in temps)
        c = Curve(label="custom", minstart=rng.randint(0, 255),
                  knobs=tuple(zip(temps, pwms)))
        c.validate()
        curves.append(c)

    temps = list(range(-5, 136))
    pairs = [(c, t) for c in curves for t in temps]
    x = KNOB_XFER
    stdin = "".join(
        f"{c.pwm_at(t) * 1000} {x['mintemp'] * 1000} {x['maxtemp'] * 1000} "
        f"{x['minstop']} {x['minpwm']} {x['maxpwm']}\n" for c, t in pairs)
    out = subprocess.run(["bash", "-c", _LAW_SH], input=stdin, text=True,
                         capture_output=True, check=True).stdout.split()
    assert len(out) == len(pairs)
    bad = [(c.knobs, t, c.pwm_at(t), int(ref))
           for (c, t), ref in zip(pairs, out) if c.pwm_at(t) != int(ref)]
    assert not bad, f"{len(bad)} mismatches, first: {bad[0]}"


def test_max_knobs_agrees_with_the_privileged_helper() -> None:
    """juno-fancontrol-apply re-validates knobs as root and must not accept a
    longer list than fancore builds, nor reject one fancore considers legal."""
    src = (HERE.parent / "juno-fancontrol-apply").read_text()
    m = re.search(r"^MAX_KNOBS=(\d+)", src, re.M)
    assert m, "juno-fancontrol-apply no longer declares MAX_KNOBS"
    assert int(m.group(1)) == MAX_KNOBS


def test_averaging_lag_is_the_same_in_both_modes() -> None:
    """README claims knob mode neither adds nor removes AVERAGE's lag, and
    prints both settling sequences. fancontrol averages the last AVERAGE
    reported values (line 642: `tval=$(( ( ${prevtemp[@]/%/+}0 ) / N ))`), which
    in native mode are millidegrees and in knob mode are pwm*1000. Reproduce
    both here so the numbers in the README come from a check, not from prose."""
    def sma(vals: list[int], n: int) -> list[int]:
        w: list[int] = []
        out = []
        for v in vals:
            w.append(v)
            if len(w) > n:
                w.pop(0)
            out.append(sum(w) // len(w))
        return out

    native = QUIET
    knob = replace(native, label="custom", knobs=native.as_knobs(), **KNOB_XFER)
    step = [60] * 3 + [95] * 8          # a load spike, degrees C

    nat = [native.pwm_at(m // 1000) for m in sma([c * 1000 for c in step], 4)]
    kno = [m // 1000 for m in sma([knob.pwm_at(c) * 1000 for c in step], 4)]
    assert nat == [0, 0, 0, 66, 84, 102, 120, 120, 120, 120, 120], nat
    assert kno == [50, 50, 50, 67, 85, 102, 120, 120, 120, 120, 120], kno
    # Same settling time in both modes: four intervals after the step.
    assert nat.index(nat[-1]) == kno.index(kno[-1]) == 6
    # The only real gap is at MINTEMP itself, where as_knobs() documents that
    # the fan runs at MINSTOP rather than MINPWM.
    assert nat[0] == native.minpwm and kno[0] == native.minstop
