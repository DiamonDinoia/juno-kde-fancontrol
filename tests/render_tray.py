#!/usr/bin/env python3
"""Render the tray monitor panel against a fixture tree offscreen and save a PNG.

The fixture counters are stepped between refreshes to draw a known load ramp,
so the screenshot is deterministic and the vision rubric has something to read.
Exits non-zero if the PNG was not written, so the container gate fails."""
from __future__ import annotations

import argparse
import math
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

import mktree
import test_sysmon
import tray as traymod

SAMPLES = 70
STEP_S = 2.0          # simulated seconds between samples


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--dgpu", default="active", choices=("active", "suspended"))
    ap.add_argument("--battery", default="Discharging",
                    choices=("Discharging", "Charging", "Not charging"))
    ap.add_argument("--style", default="fusion")
    ap.add_argument("--hide-chart", action="store_true",
                    help="defect control: render the panel without its chart")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="jfc-tray-"))
    root = test_sysmon.make_tree(work / "fix", dgpu_runtime=args.dgpu,
                                 battery={"status": args.battery})
    platform = work / "sys"
    mktree.make_platform(platform)

    smi = work / "nvidia-smi"
    smi.write_text("#!/bin/sh\nprintf '%s' '61, 44, 38.5, 1536'\n")
    smi.chmod(0o755)

    qapp = QApplication([sys.argv[0]])
    qapp.setStyle(args.style)
    if args.dark:
        import app as fanapp
        qapp.setPalette(fanapp.dark_palette())

    ns = argparse.Namespace(
        sysfs=str(platform), stat=str(root / "stat"), net=str(root / "net_dev"),
        net_class=str(root / "net_class"), gt=str(root / "gt0"),
        dgpu_pci=str(root / "dgpu"), power_supply=str(root / "power_supply"),
        rapl=str(root / "rapl"), nvidia_smi=str(smi),
        interval=10 ** 6,        # the harness drives refresh, not the timer
        screenshot=None, screenshot_samples=0, dark=args.dark)
    mon = traymod.Monitor(ns, qapp)

    # Deterministic clock so the rates come out of the counter steps alone.
    clock = {"t": 0.0}
    mon.sampler.clock = lambda: clock["t"]

    jiffies = {"idle": 8000, "busy": 1360}
    rc6 = {"ms": 0}
    net = {"rx": 1_000_000, "tx": 300_000}
    state = {"n": 0}

    def step() -> None:
        i = state["n"]
        # CPU sweeps 10..95 %, the GPU trails it by a quarter period.
        cpu = 0.10 + 0.85 * (0.5 - 0.5 * math.cos(i / 9.0))
        gpu = 0.10 + 0.80 * (0.5 - 0.5 * math.cos((i - 5) / 9.0))
        total = 1000                      # jiffies per simulated interval
        jiffies["busy"] += int(total * cpu)
        jiffies["idle"] += total - int(total * cpu)
        (root / "stat").write_text(
            f"cpu  {jiffies['busy']} 0 0 {jiffies['idle']} 0 0 0 0 0 0\n")
        rc6["ms"] += int(STEP_S * 1000 * (1 - gpu))
        (root / "gt0" / "rc6_residency_ms").write_text(f"{rc6['ms']}\n")
        net["rx"] += int(2_500_000 * cpu)
        net["tx"] += int(400_000 * cpu)
        (root / "net_dev").write_text(test_sysmon.NET.replace(
            " 1000000  2000", f"{net['rx']:8d}  2000").replace(
            "   300000   900", f"{net['tx']:9d}   900"))
        clock["t"] += STEP_S
        mon.refresh()
        state["n"] += 1

    ok = {"v": False}

    def shot() -> None:
        if args.hide_chart:
            mon.panel.chart.hide()
        mon.panel.adjustSize()
        mon.panel.show()

        def save() -> None:
            ok["v"] = bool(mon.panel.grab().save(args.out)) and \
                Path(args.out).stat().st_size > 4000
            qapp.quit()
        QTimer.singleShot(150, save)

    for i in range(SAMPLES):
        QTimer.singleShot(i * 5, step)
    QTimer.singleShot(SAMPLES * 5 + 100, shot)
    QTimer.singleShot(20000, qapp.quit)     # watchdog: never hang the container
    qapp.exec()
    print(f"{args.out}: {'ok' if ok['v'] else 'FAILED'}")
    return 0 if ok["v"] else 1


if __name__ == "__main__":
    sys.exit(main())
