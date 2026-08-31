"""Build a fake /sys/devices/platform tree for tests and offscreen renders."""
from __future__ import annotations

from pathlib import Path


def make_platform(root, *, fan_hw: str = "hwmon7", temp_hw: str = "hwmon10",
                  n_fans: int = 2, fan_name: str = "V5xTNC_TND_TNE",
                  temp_millic: int = 74000, rpms=(2560, 2480), pwms=(78, 78),
                  enables=(1, 1)) -> Path:
    root = Path(root)
    fan_dir = root / "clevofan" / "hwmon" / fan_hw
    temp_dir = root / "coretemp.0" / "hwmon" / temp_hw
    fan_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    (fan_dir / "name").write_text(fan_name + "\n")
    (temp_dir / "name").write_text("coretemp\n")
    (temp_dir / "temp1_input").write_text(f"{temp_millic}\n")
    for i in range(1, n_fans + 1):
        (fan_dir / f"pwm{i}").write_text(f"{pwms[i - 1]}\n")
        (fan_dir / f"pwm{i}_enable").write_text(f"{enables[i - 1]}\n")
        (fan_dir / f"fan{i}_input").write_text(f"{rpms[i - 1]}\n")
        (fan_dir / f"fan{i}_label").write_text(f"FAN{i}\n")
    return root


def write_fake_systemctl(path: Path, log: Path, *, active: bool = True) -> Path:
    script = (
        "#!/bin/bash\n"
        f'echo "$*" >> "{log}"\n'
        'case "$1" in\n'
        f'  is-active) {"exit 0" if active else "exit 3"} ;;\n'
        '  is-enabled) exit 0 ;;\n'
        "esac\n"
        "exit 0\n")
    path.write_text(script)
    path.chmod(0o755)
    return path
