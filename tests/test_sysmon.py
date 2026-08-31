"""Unit tests for backend.sysmon — run in the validation container.

Every reader takes explicit paths, so the whole module is driven off a fixture
tree: no real /proc, /sys or nvidia-smi involved.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest
from backend.sysmon import (Battery, Sampler, fmt_duration, fmt_rate, read_battery,
                            read_cpu_jiffies, read_dgpu, read_igpu_freq_mhz,
                            read_net_bytes, read_rapl_uj, read_rc6_ms)

STAT = """\
cpu  1000 20 300 8000 100 0 40 0 0 0
cpu0 500 10 150 4000 50 0 20 0 0 0
intr 12345
"""

# name: rx_bytes packets errs drop fifo frame compressed multicast tx_bytes ...
NET = """\
Inter-|   Receive                    |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets
    lo: 5000000  1000    0    0    0     0          0         0  5000000  1000
 wlan0: 1000000  2000    0    0    0     0          0         0   300000   900
enp44s0: 4000000  3000    0    0    0     0          0         0   700000  1100
tailscale0: 900000  100   0    0    0     0          0         0   900000   100
"""


def make_tree(root: Path, *, rc6_ms: int = 1000, cpu_stat: str = STAT,
              net: str = NET, dgpu_runtime: str = "active",
              battery: dict | None = None, rapl_uj: int | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "stat").write_text(cpu_stat)
    (root / "net_dev").write_text(net)

    # Only hardware-backed interfaces get a `device` link; lo and tailscale0
    # deliberately get none, which is what read_net_bytes filters on.
    net_class = root / "net_class"
    for iface in ("wlan0", "enp44s0"):
        (net_class / iface / "device").mkdir(parents=True)
    for iface in ("lo", "tailscale0"):
        (net_class / iface).mkdir(parents=True)

    gt = root / "gt0"
    gt.mkdir()
    (gt / "rc6_residency_ms").write_text(f"{rc6_ms}\n")
    (gt / "rps_act_freq_mhz").write_text("900\n")
    (gt / "rps_max_freq_mhz").write_text("2250\n")

    pci = root / "dgpu"
    (pci / "power").mkdir(parents=True)
    (pci / "power" / "runtime_status").write_text(f"{dgpu_runtime}\n")
    (pci / "power_state").write_text("D0\n" if dgpu_runtime == "active" else "D3cold\n")

    bat = root / "power_supply" / "BAT0"
    bat.mkdir(parents=True)
    values = {"status": "Discharging", "capacity": "77", "charge_now": "3187000",
              "charge_full": "4140000", "current_now": "2000000",
              "voltage_now": "16019000"}
    values.update(battery or {})
    for key, value in values.items():
        (bat / key).write_text(f"{value}\n")

    rapl = root / "rapl"
    rapl.mkdir()
    if rapl_uj is not None:
        (rapl / "energy_uj").write_text(f"{rapl_uj}\n")
    return root


def fake_smi(tmp_path: Path, stdout: str, rc: int = 0) -> str:
    path = tmp_path / "nvidia-smi"
    path.write_text(f"#!/bin/sh\nprintf '%s' '{stdout}'\nexit {rc}\n")
    path.chmod(0o755)
    return str(path)


# --- individual readers ---------------------------------------------------
def test_read_cpu_jiffies(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "t")
    idle, total = read_cpu_jiffies(str(root / "stat"))
    assert idle == 8100          # idle + iowait
    assert total == 9460


def test_read_cpu_jiffies_missing(tmp_path: Path) -> None:
    assert read_cpu_jiffies(str(tmp_path / "nope")) is None


def test_read_net_skips_virtual_interfaces(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "t")
    got = read_net_bytes(str(root / "net_dev"), str(root / "net_class"))
    # lo would double every local byte and tailscale0 would double every
    # routed byte; both lack a `device` link.
    assert set(got) == {"wlan0", "enp44s0"}
    assert got["wlan0"] == (1000000, 300000)
    assert got["enp44s0"] == (4000000, 700000)


def test_read_rc6_and_freq(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "t", rc6_ms=4242)
    assert read_rc6_ms(str(root / "gt0")) == 4242
    assert read_igpu_freq_mhz(str(root / "gt0")) == (900, 2250)


def test_read_rapl_denied_reads_as_none(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "t")           # no energy_uj written
    assert read_rapl_uj(str(root / "rapl")) is None


def test_dgpu_suspended_never_runs_nvidia_smi(tmp_path: Path) -> None:
    # Waking a suspended GPU to ask whether it is asleep costs ~10 W. The
    # fake smi writes a marker file: it must never run.
    root = make_tree(tmp_path / "t", dgpu_runtime="suspended")
    marker = tmp_path / "smi-ran"
    smi = tmp_path / "nvidia-smi"
    smi.write_text(f"#!/bin/sh\ntouch {marker}\necho '50, 10, 9.0, 100'\n")
    smi.chmod(0o755)
    d = read_dgpu(str(root / "dgpu"), str(smi))
    assert not d.powered and d.present
    assert "suspended" in d.state and "D3cold" in d.state
    assert not marker.exists()


def test_dgpu_active_parses_smi(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "t")
    d = read_dgpu(str(root / "dgpu"), fake_smi(tmp_path, "55, 37, 12.34, 512\n"))
    assert (d.present, d.powered) == (True, True)
    assert (d.temp_c, d.util_pct, d.power_w, d.memory_mb) == (55, 37, 12.34, 512)


def test_dgpu_smi_broken_still_reports_powered(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "t")
    d = read_dgpu(str(root / "dgpu"), fake_smi(tmp_path, "", rc=9))
    assert d.powered and d.temp_c is None


def test_dgpu_smi_reports_na(tmp_path: Path) -> None:
    # nvidia-smi prints "[N/A]" for power on some laptop SKUs.
    root = make_tree(tmp_path / "t")
    d = read_dgpu(str(root / "dgpu"), fake_smi(tmp_path, "55, 0, [N/A], 47\n"))
    assert d.temp_c == 55 and d.power_w is None and d.memory_mb == 47


def test_dgpu_absent(tmp_path: Path) -> None:
    d = read_dgpu(str(tmp_path / "no-such-device"))
    assert not d.present and d.state == "absent"


# --- battery --------------------------------------------------------------
def test_battery_charge_units(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "t")
    b = read_battery(str(root / "power_supply"))
    # 2.0 A x 16.019 V = 32.0 W; 3.187 Ah x 16.019 V = 51.06 Wh -> 1.594 h
    assert b.percent == 77 and b.status == "Discharging"
    assert b.power_w == pytest.approx(32.04, abs=0.05)
    assert b.seconds_left == pytest.approx(5739, abs=30)


def test_battery_energy_units_preferred(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "t")
    bat = root / "power_supply" / "BAT0"
    (bat / "energy_now").write_text("51000000\n")     # µWh
    (bat / "energy_full").write_text("66000000\n")
    (bat / "power_now").write_text("17000000\n")      # µW
    b = read_battery(str(root / "power_supply"))
    assert b.power_w == pytest.approx(17.0)
    assert b.seconds_left == pytest.approx(51000000 * 3600 // 17000000, abs=2)


def test_battery_charging_counts_down_to_full(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "t", battery={"status": "Charging"})
    b = read_battery(str(root / "power_supply"))
    assert b.power_w < 0                       # charging is not a system draw
    # (4.140 - 3.187) Ah at 2 A = 0.4765 h
    assert b.seconds_left == pytest.approx(1715, abs=30)


def test_battery_idle_on_ac_has_no_estimate(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "t", battery={"status": "Not charging",
                                              "current_now": "0"})
    b = read_battery(str(root / "power_supply"))
    assert b.power_w == 0.0 and b.seconds_left is None


def test_battery_missing(tmp_path: Path) -> None:
    b = read_battery(str(tmp_path), "BAT9")
    assert b == Battery(percent=None, status="unknown", power_w=None, seconds_left=None)


# --- the sampler: rates need two points -----------------------------------
class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def sampler_for(root: Path, clock: FakeClock, smi: str = "/nonexistent") -> Sampler:
    return Sampler(stat_path=str(root / "stat"), net_path=str(root / "net_dev"),
                   net_class_dir=str(root / "net_class"), gt_dir=str(root / "gt0"),
                   dgpu_pci=str(root / "dgpu"), supply_dir=str(root / "power_supply"),
                   rapl_dir=str(root / "rapl"), nvidia_smi=smi, clock=clock)


def test_first_sample_has_no_rates(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "t")
    s = sampler_for(root, FakeClock()).sample()
    assert s.cpu_pct is None and s.igpu_pct is None
    assert s.net_rx_bps is None and s.package_w is None
    # non-rate readings are available immediately
    assert s.battery.percent == 77 and s.igpu_mhz == 900


def test_rates_over_a_known_interval(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "t", rc6_ms=1000, rapl_uj=10_000_000)
    clock = FakeClock()
    sampler = sampler_for(root, clock)
    sampler.sample()

    # 10 s later: 750 more busy jiffies out of 1000, RC6 gained 2 s of the 10 s
    # wall interval (80 % busy), 10 MB down / 1 MB up, 250 J burned.
    clock.t += 10.0
    (root / "stat").write_text("cpu  1250 20 300 8250 100 0 40 0 0 0\n")
    (root / "gt0" / "rc6_residency_ms").write_text("3000\n")
    (root / "net_dev").write_text(
        NET.replace(" 1000000  2000", "11000000  2000").replace("   300000   900",
                                                                "  1300000   900"))
    (root / "rapl" / "energy_uj").write_text("260000000\n")
    s = sampler.sample()

    assert s.cpu_pct == pytest.approx(50.0, abs=0.1)   # 250 idle of 500 jiffies
    assert s.igpu_pct == pytest.approx(80.0, abs=0.1)
    assert s.net_rx_bps == pytest.approx(1_000_000.0, abs=1)
    assert s.net_tx_bps == pytest.approx(100_000.0, abs=1)
    assert s.package_w == pytest.approx(25.0, abs=0.01)


def test_counter_reset_does_not_produce_a_negative_rate(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "t")
    clock = FakeClock()
    sampler = sampler_for(root, clock)
    sampler.sample()
    clock.t += 5.0
    # interface came back up with zeroed counters
    (root / "net_dev").write_text(NET.replace(" 1000000  2000", "       0  2000"))
    s = sampler.sample()
    assert s.net_rx_bps is not None and s.net_rx_bps >= 0


def test_rc6_longer_than_the_interval_clamps_to_zero(tmp_path: Path) -> None:
    # A suspend/resume can advance RC6 past the wall delta; busy must not go
    # negative and the chart must not invert.
    root = make_tree(tmp_path / "t", rc6_ms=0)
    clock = FakeClock()
    sampler = sampler_for(root, clock)
    sampler.sample()
    clock.t += 1.0
    (root / "gt0" / "rc6_residency_ms").write_text("99999\n")
    assert sampler.sample().igpu_pct == 0.0


# --- formatting -----------------------------------------------------------
@pytest.mark.parametrize("bps,want", [
    (None, "n/a"), (0, "0 B/s"), (999, "999 B/s"), (1536, "1.5 kB/s"),
    (5 * 1024 ** 2, "5.0 MB/s"), (3 * 1024 ** 3, "3.0 GB/s"),
])
def test_fmt_rate(bps, want) -> None:
    assert fmt_rate(bps) == want


@pytest.mark.parametrize("seconds,want", [
    (None, "n/a"), (0, "0 min"), (90, "1 min"), (3600, "1 h 00 min"),
    (5739, "1 h 35 min"),
])
def test_fmt_duration(seconds, want) -> None:
    assert fmt_duration(seconds) == want


def test_fixture_tree_matches_the_real_sysfs_layout() -> None:
    """Positive control for the fixture: if the real host has these files, the
    fixture must name them the same way or every test above proves nothing."""
    real = [
        ("/proc/stat", "cpu "),
        ("/proc/net/dev", "Inter-|"),
    ]
    for path, marker in real:
        if os.path.exists(path):
            assert marker in Path(path).read_text()[:200]


# --- the popup follows the colour scheme -------------------------------------
def test_the_panel_rethemes_when_the_scheme_changes(tmp_path: Path) -> None:
    """The hint label carries an inline stylesheet, which wins over the palette,
    so a scheme switch has to re-set it explicitly. Also a regression guard: the
    re-set itself posts a PaletteChange, so a retheme that always assigns would
    recurse until the stack blew."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication

    import tray as traymod

    app = QApplication.instance() or QApplication(["test"])
    # An explicit light window, so the contrast floor in ktheme._inactive gives
    # the same answer whatever palette the runner's platform theme supplies.
    # Both test colours clear 3.0 against it (red 3.53, blue 7.58).
    light = QPalette()
    light.setColor(QPalette.ColorRole.Window, QColor(239, 240, 241))
    light.setColor(QPalette.ColorRole.WindowText, QColor(0, 0, 0))

    f = tmp_path / "kdeglobals"
    f.write_text("[Colors:View]\nForegroundInactive=255,0,0\n")
    traymod.ktheme.scheme.cache_clear()
    orig = traymod.ktheme.kdeglobals
    traymod.ktheme.kdeglobals = lambda: str(f)
    try:
        panel = traymod.Panel(argparse.Namespace())
        panel.setPalette(light)
        panel.retheme()
        assert "#ff0000" in panel.hint.styleSheet()

        f.write_text("[Colors:View]\nForegroundInactive=0,0,255\n")
        QApplication.sendEvent(panel, QEvent(QEvent.Type.PaletteChange))
        assert "#0000ff" in panel.hint.styleSheet()

        # the second delivery must be a no-op, not another assignment
        sheet = panel.hint.styleSheet()
        QApplication.sendEvent(panel, QEvent(QEvent.Type.PaletteChange))
        assert panel.hint.styleSheet() == sheet
        panel.deleteLater()
        app.processEvents()
    finally:
        traymod.ktheme.kdeglobals = orig
        traymod.ktheme.scheme.cache_clear()
