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
from mktree import make_dgpu, make_platform, write_fake_nvidia_smi

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
    cfg.write_text(cfg.read_text().replace("# Knobs pwm1: 45:0 60:55 75:110 95:255",
                                           "# Knobs pwm1: 45:200 60:55 75:110 95:255"))
    r = run(cfg, platform)
    assert r.returncode != 0
    assert "must not fall" in r.stderr


def test_refuses_a_single_knob(tmp_path: Path) -> None:
    cfg, platform = build(tmp_path)
    cfg.write_text(cfg.read_text().replace("# Knobs pwm1: 45:0 60:55 75:110 95:255",
                                           "# Knobs pwm1: 45:200"))
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


# ---- --fan gpu: the GPU fan's curve off the dGPU temperature ----------------

GPU_KNOBS = ((40, 0), (60, 80), (85, 255))
GPUTEMP = HERE.parent / "gputemp.py"


def build_gpu(tmp_path: Path, *, awake: bool = True, temp_c: int = 67,
              smi_fail: bool = False):
    """A dual-curve config plus the dGPU fixtures; the platform tree carries a
    71 C coretemp as the fallback reading."""
    platform = make_platform(tmp_path / "platform", temp_millic=71000)
    cfg = tmp_path / "fancontrol"
    cfg.write_text(render_config(
        Curve(label="custom", minstart=70, knobs=KNOBS), discover(str(platform)),
        NOW, fan_curve="/usr/bin/juno-fan-curve", dgpu=True,
        gpu_curve=Curve(label="custom", minstart=70, knobs=GPU_KNOBS),
        gpu_helper="/usr/bin/juno-gpu-curve"))
    pci = make_dgpu(tmp_path / "pci", awake=awake, temp_c=temp_c)
    smi = write_fake_nvidia_smi(tmp_path / "nvidia-smi", tmp_path / "smi.log",
                                temp_c=temp_c, fail=smi_fail)
    return cfg, platform, pci, smi


def run_gpu(cfg: Path, platform: Path, pci: Path, smi: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(FANCURVE), "--fan", "gpu", "--config", str(cfg),
         "--sysfs", str(platform), "--pci", str(pci), "--smi", str(smi)],
        capture_output=True, text=True)


def test_gpu_curve_tracks_the_dgpu_temperature(tmp_path: Path) -> None:
    r = run_gpu(*build_gpu(tmp_path, temp_c=67))
    assert r.returncode == 0, r.stderr
    # 67 C on the (60,80)..(85,255) segment: (67-60)*175//25 + 80 = 129.
    assert r.stdout.strip() == "129000"


def test_gpu_curve_suspended_card_commands_the_idle_point(tmp_path: Path) -> None:
    """Suspension synthesizes 25 C, which is flat-left of the first knob, so
    the fan sits at the curve floor — never awake, never fast for no load."""
    pci_args = build_gpu(tmp_path, awake=False)
    r = run_gpu(*pci_args)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0"          # GPU_KNOBS floor pwm is 0
    log = tmp_path / "smi.log"
    assert not log.exists() or not log.read_text()   # and the card was not asked


def test_gpu_curve_ignores_the_cpu_knob_line(tmp_path: Path) -> None:
    """Same config, wrong line: at 67 C the CPU curve would answer 80000, so
    a split bug where the GPU fan reads the pwm1 knobs is caught outright."""
    r = run_gpu(*build_gpu(tmp_path, temp_c=67))
    assert r.stdout.strip() == "129000"     # the pwm2 knobs, not the pwm1 ones
    # and the CPU fan on the same config still answers its own curve
    r2 = run(*build(tmp_path, temp_millic=67000))
    assert r2.stdout.strip() == "80000"


def test_gpu_curve_refuses_a_config_without_gpu_knobs(tmp_path: Path) -> None:
    cfg, platform, pci, smi = build_gpu(tmp_path)
    cfg.write_text(render_config(Curve(label="custom", minstart=70, knobs=KNOBS),
                                 discover(str(platform)), NOW,
                                 fan_curve="/usr/bin/juno-fan-curve"))
    r = run_gpu(cfg, platform, pci, smi)
    assert r.returncode != 0
    assert "pwm2" in r.stderr


def test_gpu_curve_errors_without_a_card(tmp_path: Path) -> None:
    cfg, platform, pci, smi = build_gpu(tmp_path)
    r = run_gpu(cfg, platform, tmp_path / "no-card", smi)
    assert r.returncode != 0
    assert "dGPU" in r.stderr
    assert "Traceback" not in r.stderr


def test_gputemp_reports_millidegrees(tmp_path: Path) -> None:
    _, platform, pci, smi = build_gpu(tmp_path, temp_c=52)
    r = subprocess.run([sys.executable, str(GPUTEMP), "--pci", str(pci),
                        "--smi", str(smi), "--sysfs", str(platform)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "52000"


def test_gputemp_suspended_is_cold_and_silent(tmp_path: Path) -> None:
    _, platform, pci, _smi = build_gpu(tmp_path, awake=False)
    r = subprocess.run([sys.executable, str(GPUTEMP), "--pci", str(pci),
                        "--smi", str(_smi), "--sysfs", str(platform)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "25000"
