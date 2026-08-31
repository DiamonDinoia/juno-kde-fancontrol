"""GUI-construction regression tests (offscreen). Caught live: load_presets()
warned via self.result before the label existed -> AttributeError on any
machine without /usr/local/bin/fan-profile (i.e. the packaged install path)."""
from __future__ import annotations

import argparse
import os
import sys

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import app as fanapp  # noqa: E402
from mktree import make_platform, write_fake_systemctl  # noqa: E402


def _ns(tmp_path, **over):
    work = tmp_path / "etc"
    work.mkdir(exist_ok=True)
    base = dict(
        sysfs=str(tmp_path / "sys"),
        config=str(work / "fancontrol"),
        fan_profile=str(tmp_path / "no-such-fan-profile"),
        cap=str(work / "fan-profile.maxpwm"),
        systemctl=str(write_fake_systemctl(tmp_path / "systemctl", tmp_path / "ctl.log")),
        screenshot=None, dark=False, preset=None, auto=False, no_apply=True,
    )
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture(scope="session")
def qapp():
    return QApplication(["test"])


def test_mainwindow_constructs_without_fan_profile(tmp_path, qapp) -> None:
    make_platform(tmp_path / "sys")  # only the fan-profile warning should remain
    ns = _ns(tmp_path)
    w = fanapp.MainWindow(ns)  # must not raise
    assert w.presets  # fallback preset present
    assert "could not parse" in w.result.text()
    assert "no clevofan" not in w.result.text()  # tree exists, so only the parse warning


def test_mainwindow_accumulates_warnings(tmp_path, qapp) -> None:
    # Both problems must stay visible (covers the overwrite class that hid the
    # parse warning behind the hwmon error).
    ns = _ns(tmp_path)  # no sysfs tree + missing fan-profile
    w = fanapp.MainWindow(ns)
    assert "could not parse" in w.result.text()
    assert "no clevofan" in w.result.text()


def test_mainwindow_preset_and_auto_flags(tmp_path, qapp) -> None:
    make_platform(tmp_path / "sys")
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "fan-profile")
    ns = _ns(tmp_path, fan_profile=fixture, preset="turbo", auto=True)
    w = fanapp.MainWindow(ns)
    assert w.active_preset == "turbo"
    assert w.rb_auto.isChecked()
    s = w.spin["maxpwm"].value()
    assert s == 255  # turbo raw value, unclamped in the editor
