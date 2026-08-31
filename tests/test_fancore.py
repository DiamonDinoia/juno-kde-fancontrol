"""Unit tests for backend.fancore — run in the validation container."""
from __future__ import annotations

from pathlib import Path

import pytest
from backend.fancore import (Curve, HwmonNotFound, discover, parse_config,
                             parse_presets, pwm_percent, read_cap, read_sensors,
                             render_config)
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
    platform = make_platform(tmp_path / "platform", fan_name="Clevo Fan X=Y")
    hw = discover(str(platform))
    assert hw.fan_devname == "Clevo_Fan_X_Y"
    assert "DEVNAME=hwmon7=Clevo_Fan_X_Y hwmon10=coretemp" in render_config(QUIET, hw, NOW)


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
])
def test_validate_rejects_bad_curves(bad: dict) -> None:
    with pytest.raises(ValueError):
        QUIET
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
