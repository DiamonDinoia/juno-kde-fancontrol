import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import os

import pytest


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole session. Module-local copies collide on
    the singleton the moment tests from two Qt modules run together."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(["test"])
