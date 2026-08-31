#!/usr/bin/env python3
"""juno-fan-curve — the executable FCTEMPS source behind knob-mode fan curves.

fancontrol interpolates exactly one linear segment, so a multi-knob curve
reaches it as a virtual temperature instead: this script reads the real CPU
temperature, evaluates the knob curve stored in /etc/fancontrol, and prints
pwm * 1000 millidegrees. Under the KNOB_XFER calibration fancontrol writes that
back as exactly pwm. See backend.fancore.KNOB_XFER for the derivation.

fancontrol calls restorefans() when an FCTEMPS command exits non-zero, which
hands the fans back to the EC curve or to full speed. Failing loudly is
therefore the safe behavior, and every error path here exits non-zero rather
than guessing a temperature.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.fancore import (DEFAULT_CONFIG, DEFAULT_PLATFORM, HwmonNotFound,
                             discover, parse_config, read_sensors)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="print the knob-curve pwm as millidegrees")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--sysfs", default=DEFAULT_PLATFORM)
    args = ap.parse_args(argv[1:])

    with open(args.config, encoding="utf-8") as f:
        curve = parse_config(f.read())
    if not curve.knobs:
        raise SystemExit(f"{args.config} carries no '# Knobs:' line")
    # A hand-edited knob line must abort the daemon, not drive the fan from
    # values nothing has checked.
    curve.validate()

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
