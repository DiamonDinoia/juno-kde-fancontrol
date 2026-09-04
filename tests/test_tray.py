"""Tray monitor dashboard tests: gauges, both charts, the compute-GPU
indicator, probe switches, persistence, and the legacy-key migration.

The Monitor's refresh() feeds the widgets from the sampler fixtures, so these
run offscreen off make_tree fixtures like render_tray, without a display.
"""
from __future__ import annotations

import argparse
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import test_sysmon  # noqa: E402
import tray as traymod  # noqa: E402
from mktree import make_platform  # noqa: E402

# The fake nvidia-smi every awake-dGPU fixture reuses: temp 61 °C, util 44 %.
SMI_CSV = "61, 44, 38.5, 1536"


def _mon(tmp_path, qapp, *, n_fans: int = 2, hw: bool = True, probes_off=(),
         fix="fix", dgpu: str = "active", smi_log: bool = False):
    root = test_sysmon.make_tree(tmp_path / fix, dgpu_runtime=dgpu)
    if hw:
        make_platform(tmp_path / "sys", n_fans=n_fans)
    smi_log_path = tmp_path / "smi.log"
    if smi_log:
        # Every call appends to the log: a never-wake assertion reads it.
        smi = tmp_path / "smi"
        smi.write_text("#!/bin/bash\n"
                       f'echo "$*" >> "{smi_log_path}"\n'
                       f"echo '{SMI_CSV}'\n")
    else:
        smi = tmp_path / "smi"
        smi.write_text(f"#!/bin/sh\nprintf '%s' '{SMI_CSV}'\n")
    smi.chmod(0o755)
    settings = tmp_path / "settings.ini"
    if probes_off:
        # probe keys all live in [probes], including the legacy chart key
        settings.write_text("[probes]\n" + "".join(f"{p}=false\n" for p in probes_off))
    ns = argparse.Namespace(
        sysfs=str(tmp_path / "sys"), stat=str(root / "stat"), net=str(root / "net_dev"),
        net_class=str(root / "net_class"), gt=str(root / "gt0"),
        dgpu_pci=str(root / "dgpu"), power_supply=str(root / "power_supply"),
        rapl=str(root / "rapl"), nvidia_smi=str(smi), settings=str(settings),
        interval=10 ** 9, screenshot=None, screenshot_samples=0, dark=False)
    return traymod.Monitor(ns, qapp), settings, root, smi_log_path


def _wake(root) -> None:
    """Flip the fixture card from suspended to awake, in place."""
    (root / "dgpu" / "power" / "runtime_status").write_text("active\n")
    (root / "dgpu" / "power_state").write_text("D0\n")


def _kill(mon) -> None:
    mon.timer.stop()
    mon.panel.deleteLater()


def _dominant(img, want: tuple[int, int, int], slack: int = 60) -> int:
    """Pixels within `slack` of `want` on every channel. Antialiasing leaves
    the line core exact and its edges blended, so an exact match is flaky."""
    n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if all(abs(a - b) <= slack for a, b in
                   zip((c.red(), c.green(), c.blue()), want)):
                n += 1
    return n


# --- every widget on by default ---------------------------------------------
def test_every_probe_on_by_default(tmp_path, qapp) -> None:
    mon, *_ = _mon(tmp_path, qapp)
    for key, _label in traymod.PROBES:
        name, value = mon.panel.row_widgets[key]
        assert name.isVisibleTo(mon.panel), key
        assert value.isVisibleTo(mon.panel), key
    for key, _label in traymod.GAUGES:
        assert mon.panel.gauges[key].isVisibleTo(mon.panel), key
    for key, _label in traymod.CHARTS:
        assert mon.panel.charts[key].isVisibleTo(mon.panel), key


# --- the gauges ---------------------------------------------------------------
def test_gauges_render_the_fixture_values(tmp_path, qapp) -> None:
    mon, *_ = _mon(tmp_path, qapp)
    # mktree fixture: coretemp 74000 millidegrees; fake smi says 61 °C.
    assert mon.panel.gauges["cpu"].value == pytest.approx(74.0)
    assert mon.panel.gauges["gpu"].value == 61
    assert mon.panel.gauges["gpu"].suspended is None


def test_suspended_dgpu_gauge_is_inactive_and_smi_never_runs(tmp_path, qapp) -> None:
    mon, settings, _root, smi_log = _mon(tmp_path, qapp, dgpu="suspended",
                                         smi_log=True)
    gauge = mon.panel.gauges["gpu"]
    assert gauge.suspended == "suspended"
    assert gauge.value is None
    # Asking a suspended GPU its temperature costs ~10 W: the log must stay
    # empty, i.e. the fake nvidia-smi must never have run.
    assert not smi_log.exists() or not smi_log.read_text().strip()
    _kill(mon)


def test_gauge_never_divides_a_zero_span(qapp) -> None:
    """A degenerate range still paints, empty, instead of dividing by zero."""
    g = traymod.TempGauge("X", lo=50, hi=50)
    g.set_value(50)
    assert g._frac() == 0.0
    g.resize(200, 26)
    g.grab()                            # must not raise
    g.deleteLater()


def test_gauge_fraction_clamps_to_the_range(qapp) -> None:
    g = traymod.TempGauge("CPU")        # the panel range: 20..110 °C
    g.set_value(65)
    assert g._frac() == pytest.approx((65 - 20) / 90)
    g.set_value(10)
    assert g._frac() == 0.0
    g.set_value(200)
    assert g._frac() == 1.0
    g.set_value(None)
    assert g._frac() == 0.0
    g.deleteLater()


# --- the two charts -------------------------------------------------------------
def test_two_distinct_charts_with_the_right_series(tmp_path, qapp) -> None:
    mon, *_ = _mon(tmp_path, qapp)
    assert mon.panel.chart_cpu is not mon.panel.chart_gpu
    assert set(mon.panel.chart_cpu.series) == {"CPU"}
    # dGPU awake in this fixture: all three series, split over the two charts.
    assert set(mon.panel.chart_gpu.series) == {"iGPU", "dGPU"}


def test_dgpu_series_joins_only_after_the_card_wakes(tmp_path, qapp) -> None:
    mon, settings, root, smi_log = _mon(tmp_path, qapp, dgpu="suspended",
                                        smi_log=True)
    assert set(mon.panel.chart_gpu.series) == {"iGPU"}   # no dGPU pre-wake
    _wake(root)
    mon.refresh()
    assert set(mon.panel.chart_gpu.series) == {"iGPU", "dGPU"}
    # Awake now: querying is allowed, so the log must have filled.
    assert smi_log.exists() and smi_log.read_text().strip()
    _kill(mon)


def test_charts_paint_the_scheme_colours(tmp_path, qapp, monkeypatch) -> None:
    """Every series colour must come from the scheme, and the tests paint with
    colours nothing else uses, so a hardcoded hex cannot pass by luck."""
    f = tmp_path / "kdeglobals"
    f.write_text("[Colors:View]\nForegroundNeutral=255,0,255\n"
                 "DecorationFocus=255,255,0\nForegroundPositive=0,255,0\n")
    traymod.ktheme.scheme.cache_clear()
    monkeypatch.setattr(traymod.ktheme, "kdeglobals", lambda: str(f))
    try:
        mon, *_ = _mon(tmp_path, qapp)
        # Two refreshes so every series has a segment, not just a point.
        mon.refresh()
        mon.panel.adjustSize()
        cpu_img = mon.panel.chart_cpu.grab().toImage()
        gpu_img = mon.panel.chart_gpu.grab().toImage()
        assert _dominant(cpu_img, (255, 255, 0)) > 10, \
            "the CPU chart is not in the scheme's focus colour"
        assert _dominant(gpu_img, (0, 255, 0)) > 10, \
            "the iGPU series is not the scheme's positive"
        assert _dominant(gpu_img, (255, 0, 255)) > 10, \
            "the dGPU series is not the scheme's neutral"
        _kill(mon)
    finally:
        traymod.ktheme.scheme.cache_clear()


# --- the compute-GPU indicator -------------------------------------------------
def test_indicator_names_the_running_gpu(tmp_path, qapp) -> None:
    mon, settings, root, smi_log = _mon(tmp_path, qapp, dgpu="suspended",
                                        smi_log=True)
    row = mon.panel.row_widgets["compute-gpu"][1]
    assert row.text() == "iGPU (Intel Arc)"
    # ... and it must not have woken the card to say so.
    assert not smi_log.exists() or not smi_log.read_text().strip()
    suspended_sheet = row.styleSheet()
    _wake(root)
    mon.refresh()
    assert row.text() == "dGPU (NVIDIA)"
    # The state colour rides on an inline stylesheet, which wins over the
    # palette, so the two states must differ.
    assert "color:" in row.styleSheet()
    assert row.styleSheet() != suspended_sheet
    _kill(mon)


# --- fan rows unchanged ----------------------------------------------------------
def test_fans_split_per_fan_with_pwm(tmp_path, qapp) -> None:
    mon, *_ = _mon(tmp_path, qapp)
    # mktree fixture: rpms 2560/2480, pwms 78/78 -> 31%
    assert "2560 RPM" in mon.panel.row_widgets["fan-cpu"][1].text()
    assert "31%" in mon.panel.row_widgets["fan-cpu"][1].text()
    assert "2480 RPM" in mon.panel.row_widgets["fan-gpu"][1].text()


def test_one_fan_machine_says_absent(tmp_path, qapp) -> None:
    mon, *_ = _mon(tmp_path, qapp, n_fans=1)
    assert mon.panel.row_widgets["fan-gpu"][1].text() == "absent"


def test_no_hwmon_degrades(tmp_path, qapp) -> None:
    mon, *_ = _mon(tmp_path, qapp, hw=False)
    assert mon.panel.row_widgets["fan-cpu"][1].text() == "clevofan not found"
    assert mon.panel.row_widgets["fan-gpu"][1].text() == "n/a"


# --- probe switches persist -------------------------------------------------------
def test_disable_persists_across_instances(tmp_path, qapp) -> None:
    mon, settings, _root, _log = _mon(tmp_path, qapp)
    mon.panel.set_probe("battery", False)
    name, value = mon.panel.row_widgets["battery"]
    assert not name.isVisibleTo(mon.panel)
    content = settings.read_text()
    assert "battery=false" in content
    # A fresh Monitor against the same store reads the choice back. The second
    # fixture tree takes another directory; the settings file is the shared one.
    _kill(mon)
    mon2, *_ = _mon(tmp_path, qapp, fix="fix2")
    assert not mon2.panel.probe_on("battery")
    name2, _ = mon2.panel.row_widgets["battery"]
    assert not name2.isVisibleTo(mon2.panel)
    _kill(mon2)


def test_gauge_and_chart_toggles_persist(tmp_path, qapp) -> None:
    mon, settings, _root, _log = _mon(tmp_path, qapp)
    for key, widget in (("cpu", mon.panel.gauges["cpu"]),
                        ("gpu", mon.panel.gauges["gpu"]),
                        ("chart-cpu", mon.panel.chart_cpu),
                        ("chart-gpu", mon.panel.chart_gpu)):
        mon.panel.set_probe(key, False)
        assert not widget.isVisibleTo(mon.panel), key
    content = settings.read_text()
    for key in ("cpu", "gpu", "chart-cpu", "chart-gpu"):
        assert f"{key}=false" in content
    _kill(mon)
    mon2, *_ = _mon(tmp_path, qapp, fix="fix2")
    assert not mon2.panel.gauges["cpu"].isVisibleTo(mon2.panel)
    assert not mon2.panel.gauges["gpu"].isVisibleTo(mon2.panel)
    assert not mon2.panel.chart_cpu.isVisibleTo(mon2.panel)
    assert not mon2.panel.chart_gpu.isVisibleTo(mon2.panel)
    _kill(mon2)


def test_legacy_chart_key_hides_both_charts(tmp_path, qapp) -> None:
    """A pre-dashboard store knows only probes/chart: both new charts follow it
    until their own keys are written."""
    mon, *_ = _mon(tmp_path, qapp, probes_off=("chart",))
    assert not mon.panel.chart_cpu.isVisibleTo(mon.panel)
    assert not mon.panel.chart_gpu.isVisibleTo(mon.panel)
    # The gauges are not the legacy chart: they stay on.
    assert mon.panel.gauges["cpu"].isVisibleTo(mon.panel)
    _kill(mon)


def test_legacy_row_keys_now_address_the_gauges(tmp_path, qapp) -> None:
    """probes/cpu addressed the old CPU text row; the gauge replaced that row,
    so the key hides the gauge."""
    mon, *_ = _mon(tmp_path, qapp, probes_off=("cpu",))
    assert not mon.panel.gauges["cpu"].isVisibleTo(mon.panel)
    assert mon.panel.gauges["gpu"].isVisibleTo(mon.panel)
    _kill(mon)


def test_a_new_chart_key_overrides_the_legacy_one(tmp_path, qapp) -> None:
    """Half-migrated store: chart=false from before, chart-gpu=true written
    after. The new key wins for its chart only."""
    (tmp_path / "settings.ini").write_text("[probes]\nchart=false\nchart-gpu=true\n")
    mon, *_ = _mon(tmp_path, qapp)
    assert not mon.panel.chart_cpu.isVisibleTo(mon.panel)
    assert mon.panel.chart_gpu.isVisibleTo(mon.panel)
    _kill(mon)


def test_dialog_toggles_the_panel(tmp_path, qapp) -> None:
    mon, *_ = _mon(tmp_path, qapp)
    dlg = traymod.ProbesDialog(mon.panel)
    cbs = {cb.text(): cb for cb in dlg.findChildren(traymod.QCheckBox)}
    assert set(cbs) == ({l for _, l in traymod.PROBES}
                        | {l for _, l in traymod.GAUGES}
                        | {l for _, l in traymod.CHARTS})
    cbs["NET"].setChecked(False)
    assert not mon.panel.probe_on("net")
    assert not mon.panel.row_widgets["net"][1].isVisibleTo(mon.panel)
    cbs["GPU utilization chart"].setChecked(False)
    assert not mon.panel.chart_gpu.isVisibleTo(mon.panel)
    cbs["CPU temperature gauge"].setChecked(False)
    assert not mon.panel.gauges["cpu"].isVisibleTo(mon.panel)
    dlg.deleteLater()
    _kill(mon)
