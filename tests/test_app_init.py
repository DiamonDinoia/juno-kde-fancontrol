"""GUI-construction regression tests (offscreen). Caught live: load_presets()
warned via self.result before the label existed -> AttributeError on any
machine without /usr/local/bin/fan-profile (i.e. the packaged install path)."""
from __future__ import annotations

import argparse
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import app as fanapp  # noqa: E402
from mktree import (make_dgpu, make_platform,  # noqa: E402
                    write_fake_nvidia_smi, write_fake_systemctl)


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
        # No card by default: a stray /sys path away from the fixture, so the
        # GPU branch only appears where a test builds one.
        dgpu_pci=str(tmp_path / "no-dgpu"), smi="nvidia-smi", fan="cpu",
    )
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture(autouse=True)
def no_modals(monkeypatch):
    """`QMessageBox.warning` is modal, so an unexpected invalid curve blocks the
    whole suite forever instead of failing it — a mutation of the knob-insert
    clamp did exactly that. Record the calls and let the test assert on them."""
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(fanapp.QMessageBox, "warning",
                        lambda _parent, title, text, *a, **k: seen.append((title, text)))
    return seen


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


# --- knob editing (the click gesture) --------------------------------------
from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QMouseEvent, QPalette  # noqa: E402

from backend.fancore import MAX_KNOBS  # noqa: E402


def _click(canvas, t: float, pwm: float, button=Qt.MouseButton.LeftButton):
    """Synthesize a press at a curve coordinate rather than a pixel, so the
    test says what it means and survives a layout change."""
    pos = canvas._to_px(t, pwm)
    canvas.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, pos, canvas.mapToGlobal(QPoint(0, 0)),
        button, button, Qt.KeyboardModifier.NoModifier))


def _drag(canvas, t: float, pwm: float):
    pos = canvas._to_px(t, pwm)
    canvas.mouseMoveEvent(QMouseEvent(
        QMouseEvent.Type.MouseMove, pos, canvas.mapToGlobal(QPoint(0, 0)),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _knob_window(tmp_path, qapp):
    make_platform(tmp_path / "sys")
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "fan-profile")
    w = fanapp.MainWindow(_ns(tmp_path, fan_profile=fixture, preset="quiet"))
    w.resize(900, 600)
    w.canvas.resize(520, 380)
    return w


def test_click_on_empty_plot_adds_a_knob(tmp_path, qapp) -> None:
    w = _knob_window(tmp_path, qapp)
    assert not w.canvas.curve.knobs          # a preset starts as a 2-point ramp
    _click(w.canvas, 80, 100)
    k = w.canvas.curve.knobs
    assert k, "the click did not convert the curve to knob mode"
    assert (80, 100) in k
    # The preset's shape survives the conversion as its end knobs.
    assert k[0][0] == 59 and k[-1] == (95, 120)
    w.canvas.curve.validate()
    assert w.active_preset is None and w._dirty


def test_click_on_an_occupied_degree_lands_next_to_it(tmp_path, qapp) -> None:
    """Knob temperatures are whole degrees, so a click can land on one already
    taken. The knob must appear at an adjacent degree rather than the click
    being silently dropped."""
    w = _knob_window(tmp_path, qapp)
    _click(w.canvas, 80, 100)
    w.canvas._drag = -1
    n = len(w.canvas.curve.knobs)
    _click(w.canvas, 80, 60)                 # same degree, different pwm
    k = w.canvas.curve.knobs
    assert len(k) == n + 1, k
    assert 79 in [kt for kt, _ in k] or 81 in [kt for kt, _ in k], k
    w.canvas.curve.validate()


def test_added_knobs_hide_the_single_segment_rows(tmp_path, qapp) -> None:
    w = _knob_window(tmp_path, qapp)
    _click(w.canvas, 80, 100)
    # isHidden(), not isVisible(): the window is never shown in the suite.
    for key in ("mintemp", "maxtemp", "minpwm", "minstop", "maxpwm"):
        assert w.spin[key].isHidden(), key
        assert w.form.labelForField(w.spin[key]).isHidden(), key
    for key in ("minstart", "interval", "average"):
        assert not w.spin[key].isHidden(), key
    assert "knobs" in w.knob_label.text()
    assert w.btn_two_point.isEnabled()


def test_back_to_two_point_restores_the_rows(tmp_path, qapp) -> None:
    w = _knob_window(tmp_path, qapp)
    _click(w.canvas, 80, 100)
    w.on_two_point()
    assert not w.canvas.curve.knobs
    for key in ("mintemp", "maxtemp", "minpwm", "minstop", "maxpwm"):
        assert not w.spin[key].isHidden(), key
        assert w.spin[key].isEnabled(), key
    assert not w.btn_two_point.isEnabled()


def test_right_click_removes_a_knob_but_keeps_two(tmp_path, qapp) -> None:
    w = _knob_window(tmp_path, qapp)
    _click(w.canvas, 80, 100)
    w.canvas._drag = -1
    n = len(w.canvas.curve.knobs)
    _click(w.canvas, 80, 100, Qt.MouseButton.RightButton)
    assert len(w.canvas.curve.knobs) == n - 1
    while len(w.canvas.curve.knobs) > 2:      # strip down to the two end knobs
        _click(w.canvas, *w.canvas.curve.knobs[1], Qt.MouseButton.RightButton)
    last = w.canvas.curve.knobs
    _click(w.canvas, *last[0], Qt.MouseButton.RightButton)
    assert w.canvas.curve.knobs == last, "removed a knob the curve needs"


def test_added_knob_is_clamped_to_stay_monotone(tmp_path, qapp) -> None:
    """A knob outside its neighbours' pwm range would make the curve fall with
    temperature, which Curve.validate rejects. The insert clamps to the nearest
    legal value in both directions instead of refusing the click.

    Clamping down is not a defect: on a non-falling curve the highest reachable
    pwm at any temperature is the last knob's, so 'quiet' (maxing at 120) has to
    have its end knob raised before an interior knob can go above it."""
    w = _knob_window(tmp_path, qapp)
    _click(w.canvas, 70, 100)
    w.canvas._drag = -1
    _click(w.canvas, 80, 10)     # far below the knob to its left (pwm 100)
    w.canvas._drag = -1
    assert dict(w.canvas.curve.knobs)[80] == 100
    _click(w.canvas, 65, 240)    # far above the knob to its right (pwm 100)
    w.canvas._drag = -1
    assert dict(w.canvas.curve.knobs)[65] == 100
    w.canvas.curve.validate()


def test_dragging_a_knob_stays_between_its_neighbours(tmp_path, qapp) -> None:
    w = _knob_window(tmp_path, qapp)
    _click(w.canvas, 70, 80)
    w.canvas._drag = -1
    _click(w.canvas, 80, 100)
    i = [t for t, _ in w.canvas.curve.knobs].index(80)
    w.canvas._drag = i
    _drag(w.canvas, 20, 0)       # yank it left and down, past both neighbours
    t, pwm = w.canvas.curve.knobs[i]
    left = w.canvas.curve.knobs[i - 1]
    assert t == left[0] + 1 and pwm == left[1], (left, (t, pwm))
    w.canvas.curve.validate()


def test_knob_count_is_capped(tmp_path, qapp) -> None:
    w = _knob_window(tmp_path, qapp)
    for t in range(62, 95):      # more clicks than MAX_KNOBS allows
        _click(w.canvas, t, 100 + t)
        w.canvas._drag = -1
    assert len(w.canvas.curve.knobs) == MAX_KNOBS
    w.canvas.curve.validate()


def test_apply_argv_carries_the_knobs(tmp_path, qapp, no_modals) -> None:
    w = _knob_window(tmp_path, qapp)
    _click(w.canvas, 80, 100)
    w.on_apply()                 # no_apply=True: builds argv and stops
    assert no_modals == [], no_modals   # a click must never build an illegal curve
    out = w.result.text()
    assert "--knobs" in out
    assert "80:100" in out
    # All nine positionals: the seven transfer-calibration values, then AVERAGE
    # and the label. Asserting only the first seven left the AVERAGE and label
    # pass-through untested.
    tail = out.split()[-9:]
    assert tail == ["10", "0", "255", "70", "0", "0", "255", "4", "custom"], tail


# --- KDE theming ------------------------------------------------------------
def _scheme(tmp_path, monkeypatch, **roles: str) -> None:
    """Point ktheme at a scheme file with unmistakable colours."""
    f = tmp_path / "kdeglobals"
    f.write_text("[Colors:View]\n" + "".join(f"{k}={v}\n" for k, v in roles.items()))
    fanapp.ktheme.scheme.cache_clear()
    monkeypatch.setattr(fanapp.ktheme, "kdeglobals", lambda: str(f))


def _dominant(img, want: tuple[int, int, int], box=None, slack: int = 60) -> int:
    """Pixels within `slack` of `want` on every channel, inside `box` if given
    as (x0, y0, x1, y1). Antialiasing leaves the line core exact and its edges
    blended, so an exact match would be flaky."""
    x0, y0, x1, y1 = box or (0, 0, img.width(), img.height())
    n = 0
    for y in range(int(y0), min(int(y1), img.height())):
        for x in range(int(x0), min(int(x1), img.width())):
            c = img.pixelColor(x, y)
            if all(abs(a - b) <= slack for a, b in
                   zip((c.red(), c.green(), c.blue()), want)):
                n += 1
    return n


MAGENTA, YELLOW, GREEN = (255, 0, 255), (255, 255, 0), (0, 255, 0)
CYAN = (0, 255, 255)


CAP = 150


def _painted(tmp_path, qapp):
    w = _knob_window(tmp_path, qapp)
    w.canvas.cap = CAP                 # draws the cap line
    w.canvas.set_live(70.0, 90)        # draws the live marker
    w.canvas.resize(520, 380)
    return w.canvas, w.canvas.grab().toImage()


def _cap_band(canvas) -> tuple[float, float, float, float]:
    """The cap line alone: its own y, and only the left third of the plot,
    which its right-aligned text label never reaches. Without the box the
    label's pixels answer for the line and a half-reverted colour passes."""
    r = canvas._plot()
    y = canvas._to_px(fanapp.TEMP_LO, CAP).y()
    return (r.left() + 3, y - 2, r.left() + r.width() * 0.33, y + 3)


def test_the_chart_paints_the_scheme_colours(tmp_path, qapp, monkeypatch) -> None:
    """The cap line, the curve and the live marker must come from the colour
    scheme. A hardcoded hex anywhere in the chart fails this."""
    _scheme(tmp_path, monkeypatch, ForegroundNegative="255,0,255",
            DecorationFocus="255,255,0", ForegroundPositive="0,255,0")
    canvas, img = _painted(tmp_path, qapp)
    assert _dominant(img, MAGENTA, _cap_band(canvas)) > 5, \
        "the cap line itself is not the scheme's negative"
    assert _dominant(img, MAGENTA) > 20, "the cap label is not the scheme's negative"
    assert _dominant(img, YELLOW) > 20, "curve is not the scheme's focus colour"
    assert _dominant(img, GREEN) > 20, "live marker is not the scheme's positive"


def test_without_a_scheme_the_chart_uses_breeze_not_the_test_colours(
        tmp_path, qapp, monkeypatch) -> None:
    """Positive control for the test above: the same rubric on an unthemed run
    must find none of those colours, so a match there means something."""
    fanapp.ktheme.scheme.cache_clear()
    monkeypatch.setattr(fanapp.ktheme, "kdeglobals", lambda: "")
    canvas, img = _painted(tmp_path, qapp)
    assert _dominant(img, MAGENTA, _cap_band(canvas)) == 0
    for want, name in ((MAGENTA, "magenta"), (YELLOW, "yellow"), (GREEN, "green")):
        assert _dominant(img, want) == 0, f"{name} found with no scheme file"


def test_a_scheme_change_repaints_the_chart(tmp_path, qapp, monkeypatch) -> None:
    """Switching the KDE colour scheme while the app runs has to reach the
    chart. The scheme read is memoized, so without ktheme.forget() on the
    palette event the window keeps the colours it started with for the whole
    session."""
    f = tmp_path / "kdeglobals"
    fanapp.ktheme.scheme.cache_clear()
    monkeypatch.setattr(fanapp.ktheme, "kdeglobals", lambda: str(f))

    f.write_text("[Colors:View]\nForegroundNegative=255,0,255\n")
    fanapp.ktheme.scheme.cache_clear()
    canvas, img = _painted(tmp_path, qapp)
    w, band = canvas.window(), _cap_band(canvas)
    assert _dominant(img, MAGENTA, band) > 5

    # The user picks a different scheme: KDE rewrites kdeglobals and Qt turns
    # the app-wide palette change into a PaletteChange on this window.
    f.write_text("[Colors:View]\nForegroundNegative=0,255,255\n")
    QApplication.sendEvent(w, QEvent(QEvent.Type.PaletteChange))
    img = canvas.grab().toImage()
    assert _dominant(img, CYAN, band) > 5, "the cap line kept the old scheme colour"
    assert _dominant(img, MAGENTA, band) == 0, "the old colour is still painted"


def test_qt_still_delivers_a_palette_change_on_a_scheme_switch(qapp) -> None:
    """Pins the Qt behaviour the fix above depends on. Measured on Qt 6:
    QApplication.setPalette sends ApplicationPaletteChange to event() and
    PaletteChange to changeEvent(), so the handler watches the latter. If a Qt
    release moves it, this fails instead of the theming silently going stale."""
    seen: list[str] = []

    class Probe(QWidget):
        def changeEvent(self, e):  # noqa: N802
            seen.append(e.type().name)
            super().changeEvent(e)

    probe = Probe()
    probe.show()
    before = qapp.palette()
    try:
        pal = QPalette(before)
        pal.setColor(QPalette.ColorRole.Window, QColor(1, 2, 3))
        qapp.setPalette(pal)
        qapp.processEvents()
    finally:
        qapp.setPalette(before)
        qapp.processEvents()
    probe.deleteLater()
    assert QEvent.Type.PaletteChange.name in seen, seen


def test_an_error_does_not_rely_on_colour_alone(tmp_path, qapp) -> None:
    """KDE HIG: colour must not be the only carrier of meaning. Red text is
    invisible as an error to a dichromat, so the string has to say so too."""
    w = _knob_window(tmp_path, qapp)
    w.result_warn("something went wrong")
    assert w.result.text().startswith(fanapp.ERROR_PREFIX)
    assert "something went wrong" in w.result.text()
    # and a second warning must not stack a second marker on the same line
    w.result_warn("something went wrong")
    assert w.result.text().count(fanapp.ERROR_PREFIX) == 1


def test_hover_tracks_the_handle_under_the_pointer(tmp_path, qapp) -> None:
    w = _knob_window(tmp_path, qapp)
    _click(w.canvas, 80, 100)
    w.canvas._drag = -1
    i = [t for t, _ in w.canvas.curve.knobs].index(80)
    _drag(w.canvas, 80, 100)                 # pointer over that knob
    assert w.canvas._hover == i
    _drag(w.canvas, 30, 200)                 # empty plot area
    assert w.canvas._hover == -1


def test_painting_survives_a_removed_knob_under_the_drag(tmp_path, qapp) -> None:
    """A right-click removes a knob without ending the drag, so _drag can point
    past the end of the shortened handle list. The painter indexed it directly
    and raised IndexError."""
    w = _knob_window(tmp_path, qapp)
    for t in (70, 80):
        _click(w.canvas, t, 100)
        w.canvas._drag = -1
    k = w.canvas.curve.knobs
    w.canvas._drag = len(k) - 1               # dragging the last handle
    _click(w.canvas, *k[1], Qt.MouseButton.RightButton)
    assert len(w.canvas.curve.knobs) == len(k) - 1
    w.canvas.grab()                           # must not raise


def test_dark_palette_keeps_disabled_text_distinct(qapp) -> None:
    """ktheme falls back to the palette's disabled text for guides and hints,
    so a palette whose disabled text equals its normal text paints them at full
    contrast. QPalette.setColor() without a group sets all groups, which is
    exactly how that happened."""
    pal = fanapp.dark_palette()
    normal = pal.color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText)
    dim = pal.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
    assert dim != normal
    assert fanapp.ktheme.colors(pal, path="").inactive == dim


# ---- the GPU fan -----------------------------------------------------------

def _knob_window_dgpu(tmp_path, qapp, *, preset="quiet", awake=True, temp_c=67):
    """A MainWindow on a machine with a (fake) dGPU: sysfs PCI fixture plus a
    fake nvidia-smi, and the applied-config fixtures the knob tests use."""
    make_platform(tmp_path / "sys")
    pci = make_dgpu(tmp_path / "pci", awake=awake)
    smi = write_fake_nvidia_smi(tmp_path / "smi", tmp_path / "smi.log", temp_c=temp_c)
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "fan-profile")
    w = fanapp.MainWindow(_ns(tmp_path, fan_profile=fixture, preset=preset,
                              dgpu_pci=str(pci), smi=str(smi)))
    w.resize(900, 600)
    w.canvas.resize(520, 380)
    return w


def test_gpu_selector_only_with_a_card(tmp_path, qapp) -> None:
    w = _knob_window(tmp_path, qapp)
    assert not hasattr(w, "fan_buttons")
    w = _knob_window_dgpu(tmp_path, qapp)
    assert set(w.fan_buttons) == {"cpu", "gpu"}
    # A one-canvas-per-role bug would be invisible on the CPU default.
    assert w.sel == "cpu"


def test_gpu_curve_is_independent_of_the_cpu_curve(tmp_path, qapp) -> None:
    w = _knob_window_dgpu(tmp_path, qapp)
    w.select_fan("gpu")
    _click(w.canvas, 70, 120)
    assert w.canvas.curve.knobs, "the GPU click did not add a knob"
    # The CPU curve must not have moved: it was stowed, not shared.
    assert not w.curves["cpu"].knobs
    assert w.curves["gpu"] is None or not w.curves["gpu"].knobs  # stow happens on read
    assert w.curve_from_editor("gpu").knobs


def test_apply_sends_both_knob_lists(tmp_path, qapp) -> None:
    w = _knob_window_dgpu(tmp_path, qapp)
    _click(w.canvas, 80, 100)                  # cpu fan: knob mode engaged
    w.select_fan("gpu")
    _click(w.canvas, 70, 110)                  # gpu fan: own curve
    w.on_apply()
    out = w.result.text()
    assert "--knobs" in out and "--gpu-knobs" in out
    assert "70:110" in out and "80:100" in out


def test_apply_converts_the_native_side(tmp_path, qapp) -> None:
    """Knob mode is per-config: one fan in knobs sends the other as its exact
    as_knobs() conversion, because the helper writes them together."""
    w = _knob_window_dgpu(tmp_path, qapp)
    w.select_fan("gpu")
    _click(w.canvas, 70, 110)                  # gpu knobs; cpu stays 2-point ramp
    w.on_apply()
    out = w.result.text()
    assert "--gpu-knobs" in out
    # quiet is 60C off .. 95C at 120; the step knob sits one degree below MINTEMP.
    assert "59:0 60:50 95:120" in out


def test_status_line_reports_the_card(tmp_path, qapp) -> None:
    w = _knob_window_dgpu(tmp_path, qapp, awake=True, temp_c=67)
    w.refresh_sensors()
    assert "GPU 67 °C" in w.status.text()
    w2 = _knob_window_dgpu(tmp_path / "s", qapp, awake=False)
    w2.refresh_sensors()
    assert "GPU suspended" in w2.status.text()
    assert not (tmp_path / "s" / "smi.log").exists()  # the card was not woken


def test_gpu_knobs_load_from_disk(tmp_path, qapp) -> None:
    """A dual config on disk must come back as two curves, one per fan."""
    import backend.fancore as fc
    platform = make_platform(tmp_path / "sys")
    text = fc.render_config(
        fc.Curve(label="custom", minstart=70, knobs=((45, 0), (95, 130))),
        fc.discover(str(platform)), "2026-08-31 07:35",
        fan_curve="/usr/bin/juno-fan-curve", dgpu=True,
        gpu_curve=fc.Curve(label="custom", minstart=70, knobs=((40, 0), (85, 255))),
        gpu_helper="/usr/bin/juno-gpu-curve")
    w = _knob_window_dgpu(tmp_path, qapp)
    (tmp_path / "etc" / "fancontrol").write_text(text)
    w.load_all()
    assert w.curve_from_editor("cpu").knobs == ((45, 0), (95, 130))
    assert w.curve_from_editor("gpu").knobs == ((40, 0), (85, 255))
    w.select_fan("gpu")
    assert [t for t, _ in w.canvas.curve.knobs] == [40, 85]


def test_live_marker_follows_the_selected_fan(tmp_path, qapp) -> None:
    w = _knob_window_dgpu(tmp_path, qapp, temp_c=67)
    w.refresh_sensors()
    assert w.canvas.live_temp == pytest.approx(74.0)   # coretemp fixture
    w.select_fan("gpu")
    w.refresh_sensors()
    assert w.canvas.live_temp == pytest.approx(67.0)
