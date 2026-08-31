#!/usr/bin/env python3
"""Render the MainWindow against a fixture tree offscreen and save a PNG.
Exits non-zero if the PNG was not written, so the container gate fails."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# A render must not depend on whoever runs it: without this, ktheme reads the
# developer's own kdeglobals and a light render picks up a dark scheme's greys.
# An empty config home makes QStandardPaths find no scheme file, which is also
# the container's state.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="jfc-noscheme-")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import app as fanapp
import mktree
from backend.fancore import Curve, discover, parse_presets, render_config

FIXTURE_FP = Path(__file__).resolve().parent / "fixtures" / "fan-profile"
NOW = "2026-08-31 07:35"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--preset", default="quiet")
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--style", default="fusion")
    ap.add_argument("--knobs", default="",
                    help='knob curve as "T:P T:P ...": renders the multi-point '
                         "editor instead of the two-point ramp")
    ap.add_argument("--hide-chart", action="store_true",
                    help="render without the curve chart: the defect control the "
                         "vision rubric must flag (tools/vision_check.py)")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="jfc-render-"))
    platform = work / "sys"
    mktree.make_platform(platform, enables=(2, 2) if args.auto else (1, 1))
    etc = work / "etc"
    etc.mkdir()
    (etc / "fan-profile.maxpwm").write_text("150\n")

    curve = parse_presets(FIXTURE_FP.read_text())[args.preset]
    if args.knobs:
        knobs = tuple(tuple(map(int, kv.split(":"))) for kv in args.knobs.split())
        curve = Curve(interval=curve.interval, minstart=curve.minstart,
                      average=curve.average, label="custom", knobs=knobs)
        curve.validate()
    hw = discover(str(platform))
    config = etc / "fancontrol"
    config.write_text(render_config(curve.clamped(None if curve.ignore_cap else 150),
                                    hw, NOW, fan_curve="/usr/bin/juno-fan-curve"))

    fakectl = mktree.write_fake_systemctl(work / "systemctl", work / "systemctl.log")

    qapp = QApplication([sys.argv[0]])
    qapp.setStyle(args.style)
    if args.dark:
        qapp.setPalette(fanapp.dark_palette())

    ns = argparse.Namespace(sysfs=str(platform), config=str(config),
                            fan_profile=str(FIXTURE_FP), cap=str(etc / "fan-profile.maxpwm"),
                            systemctl=str(fakectl), screenshot=None, dark=args.dark,
                            preset=None, auto=args.auto, no_apply=True)
    win = fanapp.MainWindow(ns)
    if args.hide_chart:
        win.canvas.hide()
    win.show()

    state: dict[str, bool] = {}

    def shot() -> None:
        pixmap = win.grab()
        state["ok"] = bool(pixmap.save(args.out)) and Path(args.out).stat().st_size > 4000
        qapp.quit()

    QTimer.singleShot(700, shot)
    QTimer.singleShot(10000, qapp.quit)  # watchdog: never hang the container
    qapp.exec()
    print(f"{args.out}: {'ok' if state.get('ok') else 'FAILED'}")
    return 0 if state.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
