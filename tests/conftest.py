import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import os

import pytest

from backend import sysmon


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole session. Module-local copies collide on
    the singleton the moment tests from two Qt modules run together."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(["test"])



@pytest.fixture(autouse=True)
def hermetic_proc(tmp_path, monkeypatch):
    """smi_hung scans /proc by default; a host with a wedged NVIDIA driver would
    turn every awake-dGPU test into a skip of nvidia-smi."""
    proc = tmp_path / "empty-proc"
    proc.mkdir()
    monkeypatch.setattr(sysmon, "DEFAULT_PROC", str(proc))
