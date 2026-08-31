"""Unit tests for fancurve.py — the executable FCTEMPS source for knob curves.

fancontrol treats a non-zero exit from this program as a fatal error and hands
the fans back to the EC, so every test that expects a refusal also asserts the
exit status: a helper that failed quietly would drive the fan from a value
nothing checked.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from backend.fancore import Curve, discover, render_config
from mktree import make_platform

HERE = Path(__file__).resolve().parent
FANCURVE = HERE.parent / "fancurve.py"
NOW = "2026-08-31 07:35"
KNOBS = ((45, 0), (60, 55), (75, 110), (95, 255))


def build(tmp_path: Path, *, knobs=KNOBS, temp_millic: int = 67000,
          temp_input: bool = True) -> tuple[Path, Path]:
    platform = make_platform(tmp_path / "platform", temp_millic=temp_millic)
    if not temp_input:
        (platform / "coretemp.0" / "hwmon" / "hwmon10" / "temp1_input").unlink()
    cfg = tmp_path / "fancontrol"
    cfg.write_text(render_config(
        Curve(label="custom", minstart=70, knobs=knobs), discover(str(platform)),
        NOW, fan_curve="/usr/bin/juno-fan-curve"))
    return cfg, platform


def run(cfg: Path, platform: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(FANCURVE), "--config", str(cfg), "--sysfs", str(platform)],
        capture_output=True, text=True)


def test_reports_the_curve_pwm_as_millidegrees(tmp_path: Path) -> None:
    r = run(*build(tmp_path, temp_millic=67000))
    assert r.returncode == 0, r.stderr
    # 67 C sits on the (60,55)..(75,110) segment: (67-60)*55//15 + 55 = 80.
    assert r.stdout.strip() == "80000"


@pytest.mark.parametrize("temp_millic, want", [
    (20000, "0"),        # left of the first knob, flat
    (45000, "0"),        # on the first knob
    (95000, "255000"),   # on the last knob
    (110000, "255000"),  # right of the last knob, flat
    (85000, "182000"),   # (85-75)*145//20 + 110 = 182
])
def test_tracks_the_temperature(tmp_path: Path, temp_millic: int, want: str) -> None:
    """Positive control for the whole set: the number has to move with the
    fixture temperature, so a helper that printed a constant would fail here."""
    r = run(*build(tmp_path, temp_millic=temp_millic))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == want


def test_refuses_a_native_config(tmp_path: Path) -> None:
    platform = make_platform(tmp_path / "platform")
    cfg = tmp_path / "fancontrol"
    cfg.write_text(render_config(Curve(label="quiet"), discover(str(platform)), NOW))
    r = run(cfg, platform)
    assert r.returncode != 0
    assert "Knobs" in r.stderr


def test_refuses_a_hand_edited_falling_curve(tmp_path: Path) -> None:
    """The knob line is a plain comment any root user can edit, so the helper
    validates it rather than driving the fan from it."""
    cfg, platform = build(tmp_path)
    cfg.write_text(cfg.read_text().replace("# Knobs: 45:0 60:55 75:110 95:255",
                                           "# Knobs: 45:200 60:55 75:110 95:255"))
    r = run(cfg, platform)
    assert r.returncode != 0
    assert "must not fall" in r.stderr


def test_refuses_a_single_knob(tmp_path: Path) -> None:
    cfg, platform = build(tmp_path)
    cfg.write_text(cfg.read_text().replace("# Knobs: 45:0 60:55 75:110 95:255",
                                           "# Knobs: 45:200"))
    r = run(cfg, platform)
    assert r.returncode != 0
    assert "at least 2 knobs" in r.stderr


def test_refuses_an_unreadable_temperature(tmp_path: Path) -> None:
    r = run(*build(tmp_path, temp_input=False))
    assert r.returncode != 0
    assert "temperature" in r.stderr


def test_refuses_a_missing_hwmon_without_a_traceback(tmp_path: Path) -> None:
    """The first call after a resume can land before clevofan re-registers.
    The exit must be non-zero (so fancontrol falls back to the EC) and the
    message one readable line, not a Python traceback in the journal."""
    cfg, platform = build(tmp_path)
    for d in (platform / "clevofan" / "hwmon").iterdir():
        shutil.rmtree(d)
    r = run(cfg, platform)
    assert r.returncode != 0
    assert "no clevofan hwmon" in r.stderr
    assert "Traceback" not in r.stderr


def test_refuses_a_missing_config(tmp_path: Path) -> None:
    platform = make_platform(tmp_path / "platform")
    r = run(tmp_path / "absent", platform)
    assert r.returncode != 0
