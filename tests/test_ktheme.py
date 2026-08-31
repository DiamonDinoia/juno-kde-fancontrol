"""backend.ktheme: the semantic colours have to come out of the user's colour
scheme, not out of the source. Every check here fails if the scheme file stops
being read, is read from the wrong section, or a corrupt entry reaches the
painter."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from backend import ktheme  # noqa: E402

# Deliberately nothing like Breeze: a hardcoded fallback cannot pass by luck.
SCHEME = """[Colors:View]
ForegroundPositive=1,2,3
ForegroundNegative=4,5,6
ForegroundNeutral=7,8,9
ForegroundInactive=10,11,12
DecorationFocus=13,14,15
DecorationHover=16,17,18

[Colors:Window]
ForegroundPositive=200,200,200
"""


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication(["test"])


def _light_palette() -> QPalette:
    """An explicit light window. The tests below assert the scheme's own values
    come through, and _inactive only returns those if they are legible on the
    window -- so a runner whose platform theme hands out a dark palette would
    fail them for a reason that is not a regression."""
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(239, 240, 241))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(0, 0, 0))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText,
                 QColor(112, 125, 138))
    return pal


LIGHT_PALETTE = _light_palette()


def write(tmp_path, text: str) -> str:
    f = tmp_path / "kdeglobals"
    f.write_text(text)
    ktheme.scheme.cache_clear()      # the read is memoized per path
    return str(f)


def test_reads_the_active_scheme(tmp_path, qapp) -> None:
    """Also pins the section: Colors:Window carries the same key names with
    different values, so reading the wrong set fails this."""
    c = ktheme.colors(LIGHT_PALETTE, path=write(tmp_path, SCHEME))
    assert c.positive.getRgb()[:3] == (1, 2, 3)
    assert c.negative.getRgb()[:3] == (4, 5, 6)
    assert c.neutral.getRgb()[:3] == (7, 8, 9)
    assert c.inactive.getRgb()[:3] == (10, 11, 12)
    assert c.focus.getRgb()[:3] == (13, 14, 15)
    assert c.hover.getRgb()[:3] == (16, 17, 18)


def test_no_scheme_file_falls_back_to_breeze(qapp) -> None:
    c = ktheme.colors(LIGHT_PALETTE, path="")
    assert c.positive.getRgb()[:3] == (39, 174, 96)
    assert c.negative.getRgb()[:3] == (218, 68, 83)
    assert c.neutral.getRgb()[:3] == (246, 116, 0)
    assert c.focus.getRgb()[:3] == (61, 174, 233)


@pytest.mark.parametrize("bad", ["", "1,2", "1,2,3,4", "a,b,c", "300,0,0",
                                "-1,0,0", "39;174;96"])
def test_a_corrupt_entry_falls_back_instead_of_painting_garbage(
        tmp_path, qapp, bad: str) -> None:
    path = write(tmp_path, f"[Colors:View]\nForegroundNegative={bad}\n")
    c = ktheme.colors(LIGHT_PALETTE, path=path)
    assert c.negative.isValid()
    assert c.negative.getRgb()[:3] == (218, 68, 83)


def test_missing_keys_fall_back_one_at_a_time(tmp_path, qapp) -> None:
    """A partial scheme file is normal: kdeglobals only holds what differs."""
    path = write(tmp_path, "[Colors:View]\nForegroundPositive=1,2,3\n")
    c = ktheme.colors(LIGHT_PALETTE, path=path)
    assert c.positive.getRgb()[:3] == (1, 2, 3)
    assert c.negative.getRgb()[:3] == (218, 68, 83)


def test_whitespace_around_the_components_is_tolerated(tmp_path, qapp) -> None:
    path = write(tmp_path, "[Colors:View]\nForegroundNeutral= 7 , 8 , 9 \n")
    assert ktheme.colors(LIGHT_PALETTE, path=path).neutral.getRgb()[:3] == (7, 8, 9)


# --- the inactive role: legible under every palette/scheme combination ------
def _pal(window: tuple[int, int, int], dim: tuple[int, int, int]) -> QPalette:
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(*window))
    pal.setColor(QPalette.ColorRole.WindowText,
                 QColor(0, 0, 0) if sum(window) > 380 else QColor(255, 255, 255))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText,
                 QColor(*dim))
    return pal


LIGHT_WINDOW, DARK_WINDOW = (239, 240, 241), (32, 35, 38)
BREEZE_DARK_INACTIVE, BREEZE_LIGHT_INACTIVE = "161,169,177", "112,125,138"


@pytest.mark.parametrize("window", [LIGHT_WINDOW, DARK_WINDOW])
@pytest.mark.parametrize("dim", [(190, 190, 190), (120, 120, 120), (10, 10, 10)])
@pytest.mark.parametrize("from_scheme", ["", BREEZE_DARK_INACTIVE,
                                         BREEZE_LIGHT_INACTIVE])
def test_inactive_always_clears_the_contrast_floor(
        tmp_path, qapp, window, dim, from_scheme) -> None:
    """Guides and hints are the one role with no safe constant. Whatever the
    scheme says and whatever the style's disabled text is, the result has to
    stay readable on this window: a light-scheme grey on a dark window, or
    Fusion's #bebebe on its own #efefef window (ratio 1.6), must not survive."""
    body = f"ForegroundInactive={from_scheme}\n" if from_scheme else ""
    path = write(tmp_path, "[Colors:View]\n" + body)
    pal = _pal(window, dim)
    c = ktheme.colors(pal, path=path)
    got = ktheme.contrast(c.inactive, QColor(*window))
    assert got >= ktheme.MIN_CONTRAST, f"{c.inactive.name()} on {window}: {got:.2f}"


def test_a_legible_scheme_value_is_kept_verbatim(tmp_path, qapp) -> None:
    """Positive control for the test above: the floor must not be an excuse to
    ignore the scheme when the scheme is fine."""
    path = write(tmp_path, f"[Colors:View]\nForegroundInactive={BREEZE_DARK_INACTIVE}\n")
    c = ktheme.colors(_pal(DARK_WINDOW, (10, 10, 10)), path=path)
    assert c.inactive.getRgb()[:3] == (161, 169, 177)


def test_contrast_matches_the_wcag_reference_pairs(qapp) -> None:
    """Two ends of the WCAG scale, so a wrong luminance formula cannot pass."""
    white, black = QColor(255, 255, 255), QColor(0, 0, 0)
    assert ktheme.contrast(black, white) == pytest.approx(21.0, abs=0.01)
    assert ktheme.contrast(white, white) == pytest.approx(1.0, abs=0.001)
    # #767676 on white is the canonical 4.54:1 boundary case for normal text.
    assert ktheme.contrast(QColor(0x76, 0x76, 0x76), white) == pytest.approx(4.54, abs=0.02)
