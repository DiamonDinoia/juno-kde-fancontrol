#!/usr/bin/env python3
"""juno-gpu-temp — the executable FCTEMPS source putting the GPU fan on dGPU
temperature in native (preset / single-band) configs. Prints millidegrees.

The semantics (suspended card synthesizes COLD_DGPU_MILLIC rather than being
woken; a broken nvidia-smi falls back to coretemp rather than aborting
fancontrol every INTERVAL) live in backend.fancore.gpu_temp_millic.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.fancore import (DEFAULT_DGPU_PCI, DEFAULT_PLATFORM, HwmonNotFound,
                             gpu_temp_millic)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="print the dGPU temperature in millidegrees")
    ap.add_argument("--pci", default=DEFAULT_DGPU_PCI, help="dGPU sysfs dir")
    ap.add_argument("--smi", default="nvidia-smi", help="nvidia-smi path")
    ap.add_argument("--sysfs", default=DEFAULT_PLATFORM,
                    help="platform dir for the coretemp fallback")
    args = ap.parse_args(argv[1:])
    try:
        print(gpu_temp_millic(args.pci, args.smi, args.sysfs))
    except HwmonNotFound as e:
        # One line, non-zero: fancontrol hands the fans to the EC, and the
        # journal gets a message, not a traceback.
        raise SystemExit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
