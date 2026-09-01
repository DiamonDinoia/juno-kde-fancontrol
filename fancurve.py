#!/usr/bin/env python3
"""juno-fan-curve / juno-gpu-curve — executable FCTEMPS sources for knob curves.

fancontrol interpolates exactly one linear segment, so a multi-knob curve
reaches it as a virtual temperature instead: this script reads the real
temperature (coretemp for --fan cpu, the dGPU for --fan gpu), evaluates that
pwm's knob curve stored in /etc/fancontrol, and prints pwm * 1000 millidegrees.
Under the KNOB_XFER calibration fancontrol writes that back as exactly pwm.
See backend.fancore.KNOB_XFER for the derivation.

fancontrol calls restorefans() when an FCTEMPS command exits non-zero, which
hands the fans back to the EC curve or to full speed. Failing loudly is
therefore the safe behavior, and every error path here exits non-zero rather
than guessing a temperature. The dGPU source is the one deliberate exception a
level down (backend.fancore.gpu_temp_millic): a suspended card synthesizes a
cold reading instead of being woken.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.fancore import (CPU_PWM, DEFAULT_CONFIG, DEFAULT_DGPU_PCI,
                             DEFAULT_PLATFORM, GPU_PWM, Curve, HwmonNotFound,
                             discover, gpu_temp_millic, parse_knobs,
                             read_sensors)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="print the knob-curve pwm as millidegrees")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--sysfs", default=DEFAULT_PLATFORM)
    ap.add_argument("--fan", choices=("cpu", "gpu"), default="cpu",
                    help="which fan's curve and temperature to evaluate")
    ap.add_argument("--pci", default=DEFAULT_DGPU_PCI, help="dGPU sysfs dir (--fan gpu)")
    ap.add_argument("--smi", default="nvidia-smi", help="nvidia-smi path (--fan gpu)")
    args = ap.parse_args(argv[1:])
    pwm = CPU_PWM if args.fan == "cpu" else GPU_PWM

    with open(args.config, encoding="utf-8") as f:
        text = f.read()
    knobs = parse_knobs(text, pwm)
    if not knobs:
        raise SystemExit(f"{args.config} carries no '# Knobs {pwm}:' line")
    # A hand-edited knob line must abort the daemon, not drive the fan from
    # values nothing has checked.
    curve = Curve(label="custom", minstart=70, knobs=knobs)
    curve.validate()

    if args.fan == "gpu":
        try:
            print(curve.pwm_at(gpu_temp_millic(args.pci, args.smi,
                                               args.sysfs) // 1000) * 1000)
        except HwmonNotFound as e:
            raise SystemExit(str(e))
        return 0

    # A missing hwmon means the module is not loaded yet, which happens on the
    # first call after a resume. Report it as one line, not a traceback: this
    # runs from fancontrol every INTERVAL and the journal is where it lands.
    try:
        hw = discover(args.sysfs)
    except HwmonNotFound as e:
        raise SystemExit(str(e))
    temp = read_sensors(hw, args.sysfs).cpu_temp_c
    if temp is None:
        raise SystemExit("cannot read the CPU temperature")
    print(curve.pwm_at(int(round(temp))) * 1000)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
