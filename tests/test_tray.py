"""Tray monitor panel tests: probe switches, persistence, and the per-fan rows.

The Monitor's refresh() writes the rows from the sampler fixtures, so these
run offscreen off make_tree fixtures like render_tray, without a display.
"""
from __future__ import annotations

import argparse
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import test_sysmon  # noqa: E402
import tray as traymod  # noqa: E402
from mktree import make_platform  # noqa: E402


def _mon(tmp_path, qapp, *, n_fans: int = 2, hw: bool = True, probes_off=(),
         fix="fix"):
    root = test_sysmon.make_tree(tmp_path / fix)
    if hw:
        make_platform(tmp_path / "sys", n_fans=n_fans)
    smi = tmp_path / "smi"
    smi.write_text("#!/bin/sh\nprintf '%s' '61, 44, 38.5, 1536'\n")
    smi.chmod(0o755)
    settings = tmp_path / "settings.ini"
    if probes_off:
        settings.write_text("[probes]\n" + "".join(f"{p}=false\n" for p in probes_off))
    ns = argparse.Namespace(
        sysfs=str(tmp_path / "sys"), stat=str(root / "stat"), net=str(root / "net_dev"),
        net_class=str(root / "net_class"), gt=str(root / "gt0"),
        dgpu_pci=str(root / "dgpu"), power_supply=str(root / "power_supply"),
        rapl=str(root / "rapl"), nvidia_smi=str(smi), settings=str(settings),
        interval=10 ** 9, screenshot=None, screenshot_samples=0, dark=False)
    return traymod.Monitor(ns, qapp), settings


def test_every_probe_on_by_default(tmp_path, qapp) -> None:
    mon, _ = _mon(tmp_path, qapp)
    for key, _label in traymod.PROBES:
        name, value = mon.panel.row_widgets[key]
        assert name.isVisibleTo(mon.panel), key
        assert value.isVisibleTo(mon.panel), key
    assert mon.panel.chart.isVisibleTo(mon.panel)


def test_gpu_row_carries_the_card_temperature(tmp_path, qapp) -> None:
    mon, _ = _mon(tmp_path, qapp)
    assert "61 °C" in mon.panel.row_widgets["gpu"][1].text()


def test_fans_split_per_fan_with_pwm(tmp_path, qapp) -> None:
    mon, _ = _mon(tmp_path, qapp)
    # mktree fixture: rpms 2560/2480, pwms 78/78 -> 31%
    assert "2560 RPM" in mon.panel.row_widgets["fan-cpu"][1].text()
    assert "31%" in mon.panel.row_widgets["fan-cpu"][1].text()
    assert "2480 RPM" in mon.panel.row_widgets["fan-gpu"][1].text()


def test_one_fan_machine_says_absent(tmp_path, qapp) -> None:
    mon, _ = _mon(tmp_path, qapp, n_fans=1)
    assert mon.panel.row_widgets["fan-gpu"][1].text() == "absent"


def test_no_hwmon_degrades(tmp_path, qapp) -> None:
    mon, _ = _mon(tmp_path, qapp, hw=False)
    assert mon.panel.row_widgets["fan-cpu"][1].text() == "clevofan not found"
    assert mon.panel.row_widgets["fan-gpu"][1].text() == "n/a"


def test_disable_persists_across_instances(tmp_path, qapp) -> None:
    mon, settings = _mon(tmp_path, qapp)
    mon.panel.set_probe("battery", False)
    name, value = mon.panel.row_widgets["battery"]
    assert not name.isVisibleTo(mon.panel)
    content = settings.read_text()
    assert "battery=false" in content
    # A fresh Monitor against the same store reads the choice back. The second
    # fixture tree takes another directory; the settings file is the shared one.
    _kill(mon)
    mon2, _ = _mon(tmp_path, qapp, fix="fix2")
    assert not mon2.panel.probe_on("battery")
    name2, _ = mon2.panel.row_widgets["battery"]
    assert not name2.isVisibleTo(mon2.panel)


def _kill(mon) -> None:
    mon.timer.stop()
    mon.panel.deleteLater()


def test_dialog_toggles_the_panel(tmp_path, qapp) -> None:
    mon, _ = _mon(tmp_path, qapp)
    dlg = traymod.ProbesDialog(mon.panel)
    cbs = {cb.text(): cb for cb in dlg.findChildren(traymod.QCheckBox)}
    assert set(cbs) == {l for _, l in traymod.PROBES} | {"Utilization chart"}
    cbs["NET"].setChecked(False)
    assert not mon.panel.probe_on("net")
    assert not mon.panel.row_widgets["net"][1].isVisibleTo(mon.panel)
    cbs["Utilization chart"].setChecked(False)
    assert not mon.panel.chart.isVisibleTo(mon.panel)
    dlg.deleteLater()


# ---------------------------------------------------------------------------
# Autostart robustness: the single-instance lock and the tray wait/retry loop.

import subprocess  # noqa: E402
import sys  # noqa: E402


def _mon_locked(tmp_path, qapp, lock):
    """The _mon shape with an explicit lock path: the default one is per-user
    and would make independent tests fight over a lock they never see."""
    root = test_sysmon.make_tree(tmp_path / "sysmon")
    make_platform(tmp_path / "sys")
    smi = tmp_path / "smi"
    smi.write_text("#!/bin/sh\nprintf '%s' '61, 44, 38.5, 1536'\n")
    smi.chmod(0o755)
    ns = argparse.Namespace(
        sysfs=str(tmp_path / "sys"), stat=str(root / "stat"), net=str(root / "net_dev"),
        net_class=str(root / "net_class"), gt=str(root / "gt0"),
        dgpu_pci=str(root / "dgpu"), power_supply=str(root / "power_supply"),
        rapl=str(root / "rapl"), nvidia_smi=str(smi),
        settings=str(tmp_path / "settings.ini"), interval=10 ** 9,
        screenshot=None, screenshot_samples=0, dark=False, lock=str(lock))
    return traymod.Monitor(ns, qapp)


def _spawn_lock_holder(lock):
    """Hold `lock` in a separate process so the guard meets a foreign pid;
    the first stdout line reports whether it took the lock."""
    code = ("import sys, time\n"
            "from PySide6.QtCore import QLockFile\n"
            "l = QLockFile(sys.argv[1])\n"
            "print(l.tryLock(0), flush=True)\n"
            "time.sleep(30)\n")
    return subprocess.Popen([sys.executable, "-c", code, str(lock)],
                            stdout=subprocess.PIPE, text=True)


def test_two_monitors_in_one_process_are_not_two_instances(tmp_path, qapp) -> None:
    # The whole _mon suite builds several Monitors per process; that must not
    # trip the guard (tryLock alone cannot tell it from a foreign holder).
    lock = tmp_path / "juno.lock"
    assert _mon_locked(tmp_path / "a", qapp, lock) is not None
    assert _mon_locked(tmp_path / "b", qapp, lock) is not None


def test_instance_held_by_another_process_exits_zero(tmp_path, qapp, capsys) -> None:
    lock = tmp_path / "juno.lock"
    holder = _spawn_lock_holder(lock)
    try:
        assert holder.stdout.readline().strip() == "True"   # premise: lock held
        with pytest.raises(SystemExit) as e:
            _mon_locked(tmp_path, qapp, lock)
        # A manual-launch duplicate is not an error; two panels are.
        assert e.value.code == 0
        assert "already running" in capsys.readouterr().err
    finally:
        holder.kill()
        holder.wait()


def test_tray_wait_retries_then_fails_nonzero(qapp, monkeypatch, capsys) -> None:
    calls = 0

    def _no_tray() -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(traymod.QSystemTrayIcon, "isSystemTrayAvailable",
                        staticmethod(_no_tray))
    assert traymod.ensure_tray(qapp, timeout_s=0.4, every_s=0.05) != 0
    assert calls >= 2, "a single check then exit is the old startup-only behaviour"
    assert "no system tray" in capsys.readouterr().err


def test_tray_wait_succeeds_once_the_shell_is_up(qapp, monkeypatch) -> None:
    answers = iter((False, False, True))
    monkeypatch.setattr(traymod.QSystemTrayIcon, "isSystemTrayAvailable",
                        staticmethod(lambda: next(answers)))
    assert traymod.ensure_tray(qapp, timeout_s=5, every_s=0.01) == 0


def test_tray_wait_is_free_when_already_available(qapp, monkeypatch) -> None:
    monkeypatch.setattr(traymod.QSystemTrayIcon, "isSystemTrayAvailable",
                        staticmethod(lambda: True))
    assert traymod.ensure_tray(qapp, timeout_s=30, every_s=1) == 0
