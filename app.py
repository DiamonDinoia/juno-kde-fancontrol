#!/usr/bin/env python3
"""juno-kde-fancontrol — KDE/Qt6 frontend for fan-profile/fancontrol.

Reads the live /etc/fancontrol, shows sensors from sysfs, edits the curve in
a draggable chart, and applies through pkexec + juno-fancontrol-apply.

Test/debug entry points (used by tests/render_app.py in the container):
    --sysfs DIR --config FILE --fan-profile FILE --cap FILE --systemctl CMD
    --screenshot FILE [--dark] --no-apply
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import (QEvent, QFileSystemWatcher, QPointF, QProcess,
                            QRectF, Qt, QTimer, Signal)
from PySide6.QtGui import (QColor, QFont, QIcon, QMouseEvent, QPainter,
                           QPalette, QPen)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QRadioButton,
                               QSizePolicy, QSpinBox, QVBoxLayout, QWidget)

from backend import ktheme
from backend.fancore import (DEFAULT_CAP, DEFAULT_CONFIG, DEFAULT_DGPU_PCI,
                             DEFAULT_FAN_PROFILE, DEFAULT_PLATFORM, KNOB_XFER,
                             MAX_KNOBS, PWM_MAX, Curve,
                             Hwmon, HwmonNotFound, discover, dgpu_present,
                             parse_config, parse_knobs, parse_presets,
                             pwm_percent, read_cap, read_sensors, service_state)
from backend.sysmon import read_dgpu

TEMP_LO, TEMP_HI = 20, 110        # chart x range, °C
HANDLE_R = 9                      # drag handle radius, px
# Only installed, root-owned helper paths: the polkit policy pins the
# packaged one; a repo-local fallback would run a user-writable file via pkexec.
HELPER_CANDIDATES = ("/usr/sbin/juno-fancontrol-apply",        # .deb
                     "/usr/local/sbin/juno-fancontrol-apply")  # install.sh
# Both the launcher entry and the System Settings external module name this
# file, so Plasma matches the window to it: the Wayland app_id, the task
# manager icon and the System Settings page all come from one place.
DESKTOP_FILE = "juno-kde-fancontrol"
APP_ICON = "preferences-system-power-management"
# An error must not be red-only text. \u26a0 is in every KDE font and needs no
# icon theme, which the offscreen renders do not have.
ERROR_PREFIX = "\u26a0 "


def find_helper() -> str | None:
    for p in HELPER_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def curve_defaults() -> Curve:
    return Curve(label="quiet")


class CurveWidget(QWidget):
    """The draggable pwm = f(temp) chart; the drawn law is fancore.Curve.pwm_at.

    A native curve has two handles, (MINTEMP, MINPWM) and (MAXTEMP, MAXPWM). A
    left click on empty plot area adds a knob and switches the curve to knob
    mode, where every knob is a handle, right-clicking one removes it, and the
    law is the polyline through them."""

    curveChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(430, 330)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.curve = Curve()
        self.cap: int | None = None
        self.live_temp: float | None = None
        self.live_pwm: int | None = None
        self._drag: int = -1   # index into _handles(), -1 while not dragging
        self._hover: int = -1  # handle under the pointer, -1 for none
        self._auto = False    # EC mode: the curve is inert, handles are not draggable

    def _handles(self) -> tuple[tuple[int, int], ...]:
        """The draggable points in draw and hit-test order. One list serves
        both modes, so painting and hit-testing cannot disagree about them."""
        c = self.curve
        return c.knobs or ((c.mintemp, c.minpwm), (c.maxtemp, c.maxpwm))

    # -- coordinate mapping -------------------------------------------------
    def _plot(self) -> QRectF:
        return QRectF(48, 14, self.width() - 62, self.height() - 52)

    def _to_px(self, t: float, pwm: float) -> QPointF:
        r = self._plot()
        x = r.left() + (t - TEMP_LO) / (TEMP_HI - TEMP_LO) * r.width()
        y = r.bottom() - pwm / PWM_MAX * r.height()
        return QPointF(x, y)

    def _from_px(self, p: QPointF) -> tuple[float, float]:
        r = self._plot()
        t = TEMP_LO + (p.x() - r.left()) / r.width() * (TEMP_HI - TEMP_LO)
        pwm = (r.bottom() - p.y()) / r.height() * PWM_MAX
        return t, pwm

    def set_live(self, temp: float | None, pwm: int | None) -> None:
        if temp != self.live_temp or pwm != self.live_pwm:
            self.live_temp, self.live_pwm = temp, pwm
            self.update()

    def set_curve(self, curve: Curve) -> None:
        self.curve = curve
        self.update()

    # -- painting -----------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt name)
        c = self.curve
        pal = self.palette()
        fg = pal.color(QPalette.ColorRole.WindowText)
        k = ktheme.colors(pal)
        accent = k.focus
        grid = QColor(fg)
        grid.setAlpha(50)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._plot()

        def_font = QFont(p.font())
        small = QFont(def_font)
        small.setPointSizeF(max(7.5, def_font.pointSizeF() - 1.5))
        p.setFont(small)

        # grid + axes
        p.setPen(QPen(grid, 1))
        for t in range(TEMP_LO + 10 - TEMP_LO % 10, TEMP_HI + 1, 10):
            x = self._to_px(t, 0).x()
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        for pct in range(0, 101, 25):
            y = self._to_px(TEMP_LO, pct * PWM_MAX / 100).y()
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(QPen(fg, 1))
        p.drawRect(r)
        for t in range(TEMP_LO + 10 - TEMP_LO % 10, TEMP_HI + 1, 20):
            p.drawText(QRectF(self._to_px(t, 0).x() - 20, r.bottom() + 4, 40, 16),
                       Qt.AlignmentFlag.AlignHCenter, f"{t}")
        p.drawText(QRectF(r.left(), r.bottom() + 18, r.width(), 16),
                   Qt.AlignmentFlag.AlignHCenter, "CPU temperature (°C)")
        for pct in range(0, 101, 25):
            y = self._to_px(TEMP_LO, pct * PWM_MAX / 100).y()
            p.drawText(QRectF(0, y - 8, r.left() - 6, 16),
                       Qt.AlignmentFlag.AlignRight, f"{pct}%")

        # calibrated noise cap
        if self.cap is not None:
            y = self._to_px(TEMP_LO, self.cap).y()
            p.setPen(QPen(k.negative, 1.4, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(k.negative)
            p.drawText(QRectF(r.right() - 156, y - 15, 148, 13),
                       Qt.AlignmentFlag.AlignRight, f"calibrated cap {self.cap}")

        # MINSTOP/MINSTART guides; one shared label when they coincide (turbo).
        # In knob mode MINSTOP is part of the transfer calibration and says
        # nothing about the fan, so only the kick-start pulse is worth drawing.
        if c.knobs:
            guides = [(c.minstart, f"MINSTART {c.minstart}")]
        elif c.minstart == c.minstop:
            guides = [(c.minstop, f"MINSTOP = MINSTART {c.minstop}")]
        else:
            guides = [(c.minstop, f"MINSTOP {c.minstop}"),
                      (c.minstart, f"MINSTART {c.minstart}")]
        for val, text in guides:
            y = self._to_px(TEMP_LO, val).y()
            p.setPen(QPen(k.inactive, 1, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(k.inactive)
            p.drawText(QRectF(r.right() - 178, y - 13, 170, 12),
                       Qt.AlignmentFlag.AlignRight, text)

        # the control law, same integer math as fancontrol
        p.setPen(QPen(accent, 2.6))
        pts = [self._to_px(TEMP_LO, c.pwm_at(TEMP_LO))] + \
              [self._to_px(t, c.pwm_at(t)) for t in range(TEMP_LO + 1, TEMP_HI + 1)]
        for a, b in zip(pts, pts[1:]):
            p.drawLine(a, b)

        # live marker: temp line + actual pwm dot; the label goes to the half
        # of the plot the curve is NOT in at this temperature
        if self.live_temp is not None:
            x = self._to_px(min(max(self.live_temp, TEMP_LO), TEMP_HI), 0).x()
            live = k.positive
            p.setPen(QPen(live, 1.4, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(live)
            curve_here = c.pwm_at(int(round(self.live_temp)))
            label_y = r.bottom() - 24 if curve_here > PWM_MAX / 2 else r.top() + 4
            label_x = min(x + 5, r.right() - 100)
            p.drawText(QRectF(label_x, label_y, 100, 14),
                       f"{self.live_temp:.0f} °C now")
            if self.live_pwm is not None:
                p.setBrush(live)
                dot = QPointF(x, self._to_px(TEMP_LO, self.live_pwm).y())
                p.drawEllipse(dot, 5, 5)

        # handles: hollow in EC mode, where dragging is refused. The one in
        # play grows and takes the hover decoration, so the pointer says which
        # knob a click will move before it moves one.
        handles = self._handles()
        act = -1 if self._auto else (self._drag if self._drag >= 0 else self._hover)
        if act >= len(handles):
            act = -1        # its knob was removed before this repaint
        for i, (t, pwm) in enumerate(handles):
            center = self._to_px(t, pwm)
            live = i == act
            p.setPen(QPen(k.hover if live else fg, 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush if self._auto else accent)
            rad = HANDLE_R if live else HANDLE_R - 2
            p.drawEllipse(center, rad, rad)

        # The axes cannot be read to a degree while dragging, so the handle in
        # play carries its own numbers, as every editor of this kind does.
        if act >= 0:
            t, pwm = handles[act]
            at = self._to_px(t, pwm)
            p.setPen(k.hover)
            p.drawText(QRectF(min(at.x() + 11, r.right() - 96),
                              max(at.y() - 25, r.top() + 2), 94, 15),
                       Qt.AlignmentFlag.AlignLeft,
                       f"{t} \u00b0C  \u2192  {pwm_percent(pwm)}")

        if not self._auto:
            p.setPen(QPen(k.inactive))
            hint = ("click to add a knob, right-click a knob to remove"
                    if c.knobs else "click the plot to add a knob")
            p.drawText(QRectF(r.left() + 4, r.top() + 2, r.width() - 8, 14), hint)
        p.end()

    # -- dragging -------------------------------------------------------------
    def _handle_at(self, pos: QPointF) -> int:
        for i, (t, pwm) in enumerate(self._handles()):
            d = self._to_px(t, pwm) - pos
            if d.manhattanLength() <= HANDLE_R * 2.2:  # x+y close enough near the center
                return i
        return -1

    def _add_knob(self, pos: QPointF) -> None:
        """Insert a knob under the cursor, converting a native curve first. The
        new pwm is clamped between its neighbours because Curve.validate
        rejects a curve that falls with temperature."""
        t, pwm = self._from_px(pos)
        t = int(round(max(TEMP_LO + 1, min(t, TEMP_HI - 1))))
        pwm = int(round(max(0, min(pwm, PWM_MAX))))
        k = list(self.curve.as_knobs())
        if len(k) >= MAX_KNOBS:
            return
        # Knob temperatures are whole degrees, so a click can land on one a knob
        # already owns. Take the nearest free degree instead of dropping the
        # click: the user asked for a knob here, not for nothing to happen.
        taken = {kt for kt, _ in k}
        if t in taken:
            free = [c for d in range(1, 4) for c in (t - d, t + d)
                    if c not in taken and TEMP_LO < c < TEMP_HI]
            if not free:
                return
            t = free[0]
        k.append((t, pwm))
        k.sort()
        i = k.index((t, pwm))
        lo = k[i - 1][1] if i else 0
        hi = k[i + 1][1] if i + 1 < len(k) else PWM_MAX
        k[i] = (t, max(lo, min(pwm, hi)))
        self._drag = i                  # the same gesture keeps dragging it
        self._apply_edit(replace(self.curve, knobs=tuple(k)))

    def _remove_knob(self, i: int) -> None:
        k = self.curve.knobs
        if len(k) <= 2:                 # a curve still needs two end knobs
            return
        self._apply_edit(replace(self.curve, knobs=k[:i] + k[i + 1:]))

    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self._auto:
            return
        i = self._handle_at(e.position())
        if e.button() == Qt.MouseButton.RightButton:
            if i >= 0 and self.curve.knobs:
                self._remove_knob(i)
        elif e.button() == Qt.MouseButton.LeftButton:
            if i >= 0:
                self._drag = i
            elif self._plot().contains(e.position()):
                self._add_knob(e.position())

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self._drag >= len(self._handles()):
            self._drag = -1             # the knob under the cursor was removed
        if self._drag < 0:
            hov = -1 if self._auto else self._handle_at(e.position())
            self.setCursor(Qt.CursorShape.SizeAllCursor if hov >= 0
                           else Qt.CursorShape.ArrowCursor)
            if hov != self._hover:
                self._hover = hov
                self.update()
            return
        t, pwm = self._from_px(e.position())
        pwm = int(round(pwm / 2) * 2)
        t = int(round(t))
        c = self.curve
        if c.knobs:
            # Each knob stays between its neighbours in both axes, so the
            # curve validate() accepts cannot be dragged into an illegal one.
            k, i = list(c.knobs), self._drag
            t_lo = k[i - 1][0] + 1 if i else TEMP_LO
            t_hi = k[i + 1][0] - 1 if i + 1 < len(k) else TEMP_HI
            p_lo = k[i - 1][1] if i else 0
            p_hi = k[i + 1][1] if i + 1 < len(k) else PWM_MAX
            k[i] = (max(t_lo, min(t, t_hi)), max(p_lo, min(pwm, p_hi)))
            self._apply_edit(replace(c, knobs=tuple(k)))
        elif self._drag == 0:
            t = max(25, min(t, c.maxtemp - 5))
            pwm = max(0, min(pwm, c.minstop))
            self._apply_edit(replace(c, mintemp=t, minpwm=pwm))
        else:
            t = min(105, max(t, c.mintemp + 5))
            # validate(): MINSTOP < MAXPWM — clamp the drag to stay legal
            pwm = min(pwm, PWM_MAX)
            pwm = max(pwm, min(c.minstop + 1, PWM_MAX))
            self._apply_edit(replace(c, maxtemp=t, maxpwm=pwm))

    def mouseReleaseEvent(self, _e: QMouseEvent) -> None:  # noqa: N802
        self._drag = -1
        self.update()

    def leaveEvent(self, _e) -> None:  # noqa: N802
        # Without this the readout of the last hovered handle stays painted
        # after the pointer has left the plot.
        if self._hover != -1:
            self._hover = -1
            self.update()

    def _apply_edit(self, curve: Curve) -> None:
        if curve != self.curve:
            self.curve = curve
            self.update()
            self.curveChanged.emit()


class MainWindow(QWidget):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        self.args = args
        self.hw: Hwmon | None = None
        self.presets: dict[str, Curve] = {}
        self.active_preset: str | None = None  # untouched preset name, if any
        self.apply_proc: QProcess | None = None
        self._dirty = False
        self._radio_sync = False  # True while refresh_sensors flips the radio

        self.setWindowTitle("Juno Fan Control")
        self.resize(1020, 620)

        root = QVBoxLayout(self)

        # status strip
        self.status = QLabel("—")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.status)
        # created before load_presets() (it can warn); added to the layout later
        self.result = QLabel("")
        self.result.setWordWrap(True)

        # The GPU fan gets its own curve where a card exists. One canvas, one
        # curve per role; the selector swaps which one is being edited.
        # fancontrol shares INTERVAL/AVERAGE across pwms, so the shared fields
        # live on both Curves and are synced at apply time.
        self.dgpu = dgpu_present(args.dgpu_pci)
        self.curves: dict[str, Curve | None] = {"cpu": curve_defaults(),
                                                "gpu": curve_defaults() if self.dgpu else None}
        self.sel = "cpu"
        if self.dgpu:
            sel_row = QHBoxLayout()
            sel_row.addWidget(QLabel("Editing:"))
            self.fan_buttons: dict[str, QPushButton] = {}
            group = QButtonGroup(self)
            for role, text in (("cpu", "CPU fan (pwm1)"), ("gpu", "GPU fan (pwm2)")):
                b = QPushButton(text)
                b.setCheckable(True)
                b.clicked.connect(lambda _c=False, r=role: self.select_fan(r))
                group.addButton(b)
                sel_row.addWidget(b)
                self.fan_buttons[role] = b
            self.fan_buttons["cpu"].setChecked(True)
            sel_row.addStretch(1)
            root.addLayout(sel_row)

        body = QHBoxLayout()
        root.addLayout(body, 1)

        self.canvas = CurveWidget()
        self.canvas.curveChanged.connect(self.on_canvas_edit)
        body.addWidget(self.canvas, 1)

        side = QVBoxLayout()
        body.addLayout(side)

        # mode
        mode_box = QGroupBox("Control mode")
        mode_l = QHBoxLayout(mode_box)
        self.rb_manual = QRadioButton("Curve (fancontrol)")
        self.rb_auto = QRadioButton("Automatic (EC firmware)")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.rb_manual)
        self.mode_group.addButton(self.rb_auto)
        self.rb_manual.setChecked(True)
        self.rb_auto.toggled.connect(self.on_mode)
        mode_l.addWidget(self.rb_manual)
        mode_l.addWidget(self.rb_auto)
        side.addWidget(mode_box)

        # presets — scraped from fan-profile at runtime
        preset_box = QGroupBox("Presets (same table as fan-profile CLI)")
        preset_l = QGridLayout(preset_box)
        self.preset_buttons: dict[str, QPushButton] = {}
        names = self.load_presets()
        for i, name in enumerate(("quiet", "balanced", "cool", "turbo")):
            btn = QPushButton(name.capitalize())
            btn.setCheckable(True)
            if name not in names:
                btn.setEnabled(False)
                btn.setToolTip(f"'{name}' not found in {self.args.fan_profile}")
            btn.clicked.connect(lambda _c=False, n=name: self.on_preset(n))
            preset_l.addWidget(btn, i // 2, i % 2)
            self.preset_buttons[name] = btn
        side.addWidget(preset_box)

        # numeric editor
        edit_box = QGroupBox("Curve")
        form = self.form = QFormLayout(edit_box)
        self.spin: dict[str, QSpinBox] = {}
        self.form_labels: dict[str, QLabel] = {}

        def add_spin(key: str, lo: int, hi: int, unit: str) -> QSpinBox:
            sb = QSpinBox()
            sb.setRange(lo, hi)
            sb.setSuffix(f" {unit}")
            sb.valueChanged.connect(lambda _v, k=key: self.on_spin_edit(k))
            lab = QLabel()
            self.form_labels[key] = lab
            form.addRow(lab, sb)
            self.spin[key] = sb
            return sb

        add_spin("mintemp", 25, 105, "°C")
        add_spin("maxtemp", 30, 110, "°C")
        add_spin("minpwm", 0, 255, "/255")
        add_spin("minstop", 0, 255, "/255")
        add_spin("minstart", 0, 255, "/255")
        add_spin("maxpwm", 0, 255, "/255")
        add_spin("interval", 1, 60, "s")
        add_spin("average", 1, 16, "")
        for key, text in (("mintemp", "Fan starts (MINTEMP)"),
                          ("maxtemp", "Full speed (MAXTEMP)"),
                          ("minpwm", "Idle PWM (MINPWM)"),
                          ("minstop", "Lowest running PWM (MINSTOP)"),
                          ("minstart", "Kick-start PWM (MINSTART)"),
                          ("maxpwm", "Max PWM (MAXPWM)"),
                          ("interval", "Sample interval"),
                          ("average", "Temp averaging")):
            self.form_labels[key].setText(text)

        # Knob mode retires MINTEMP..MAXPWM (they become the fixed transfer
        # calibration), so the row says which mode the curve is in and offers
        # the way back. Inserted first so it reads as a header for the spins.
        self.knob_label = QLabel()
        self.btn_two_point = QPushButton("Back to 2-point")
        self.btn_two_point.setToolTip(
            "Drop the knobs and edit MINTEMP/MAXTEMP again (discards the knob curve)")
        self.btn_two_point.clicked.connect(self.on_two_point)
        form.insertRow(0, self.knob_label, self.btn_two_point)
        side.addWidget(edit_box)

        # service / apply
        svc_box = QGroupBox("Service")
        svc_l = QVBoxLayout(svc_box)
        self.svc_label = QLabel("—")
        self.svc_label.setWordWrap(True)
        self.svc_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        svc_l.addWidget(self.svc_label)
        btn_row = QHBoxLayout()
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setDefault(True)
        self.btn_revert = QPushButton("Revert")
        self.btn_apply.clicked.connect(self.on_apply)
        self.btn_revert.clicked.connect(self.on_revert)
        btn_row.addWidget(self.btn_apply)
        btn_row.addWidget(self.btn_revert)
        svc_l.addLayout(btn_row)
        svc_l.addWidget(self.result)
        side.addWidget(svc_box)
        side.addStretch(1)

        if find_helper() is None:
            self.btn_apply.setEnabled(False)
            self.btn_apply.setToolTip("juno-fancontrol-apply not installed — run install.sh")

        self.cap = read_cap(args.cap)
        self.canvas.cap = self.cap

        # live refresh
        self.sensors_timer = QTimer(self)
        self.sensors_timer.timeout.connect(self.refresh_sensors)
        self.sensors_timer.start(1000)

        # pick up edits made by the fan-profile CLI / fan-calibrate while we run
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self.on_watched_changed)
        self._ensure_watches()

        self.load_all()
        # debug/test entry points (used by tests/render_app.py)
        if self.args.preset in self.presets:
            self.on_preset(self.args.preset)
        if self.args.fan == "gpu" and self.dgpu:
            self.select_fan("gpu")
        if self.args.auto:
            self.rb_auto.setChecked(True)

    # -- data plumbing --------------------------------------------------------
    @property
    def dirty_label(self) -> str:
        return self.active_preset or "custom"

    def select_fan(self, role: str) -> None:
        """Swap which fan's curve the canvas + spins edit. The departing fan's
        canvas state (knobs) is stowed in self.curves first; shared fields are
        the spins', and are re-read from the arriving fan's curve."""
        self.curves[self.sel] = self.canvas.curve
        self.sel = role
        for r, b in self.fan_buttons.items():
            b.blockSignals(True)
            b.setChecked(r == role)
            b.blockSignals(False)
        self.reflect()

    def reflect(self) -> None:
        c = self.curves[self.sel]
        for key in ("mintemp", "maxtemp", "minpwm", "minstop", "minstart",
                    "maxpwm", "interval", "average"):
            sb = self.spin[key]
            sb.blockSignals(True)
            sb.setValue(getattr(c, key))
            sb.blockSignals(False)
        self.canvas.blockSignals(True)
        self.canvas.set_curve(c)
        self.canvas.blockSignals(False)
        self.sync_knob_ui()

    def curve_from_editor(self, role: str | None = None) -> Curve:
        role = role or self.sel
        base = self.canvas.curve if role == self.sel else self.curves[role]
        if role != self.sel:
            return base      # stowed fan: the spins describe the selected one
        return replace(base,
            interval=self.spin["interval"].value(),
            mintemp=self.spin["mintemp"].value(),
            maxtemp=self.spin["maxtemp"].value(),
            minstart=self.spin["minstart"].value(),
            minstop=self.spin["minstop"].value(),
            minpwm=self.spin["minpwm"].value(),
            maxpwm=self.spin["maxpwm"].value(),
            average=self.spin["average"].value(),
            label=self.dirty_label,
            ignore_cap=self.active_preset == "turbo")

    def editor_from_curve(self, c: Curve, mark_preset: str | None = None) -> None:
        self.active_preset = mark_preset
        self.curves[self.sel] = c
        self.reflect()
        for name, btn in self.preset_buttons.items():
            btn.setChecked(name == mark_preset)

    def sync_knob_ui(self) -> None:
        """Enable only the spin boxes that mean something in the current mode.
        In knob mode MINTEMP..MAXPWM hold fancore.KNOB_XFER, so their rows are
        hidden: editing them would change nothing the fan can feel."""
        knobs = self.canvas.curve.knobs
        auto = self.rb_auto.isChecked()
        # In knob mode these five hold KNOB_XFER, which the spin ranges clamp
        # into numbers that mean nothing. Hide the rows instead of greying a
        # wrong value. MINSTART, INTERVAL and AVERAGE still apply, so they stay.
        for key in ("mintemp", "maxtemp", "minpwm", "minstop", "maxpwm"):
            self.form.setRowVisible(self.spin[key], not knobs)
            self.spin[key].setEnabled(not auto)
        self.btn_two_point.setEnabled(not auto and bool(knobs))
        self.knob_label.setText(f"{len(knobs)} knobs / {MAX_KNOBS}" if knobs
                                else "2-point ramp")
        # The curve as text, which is also how it lands in the config comment.
        # A count alone leaves no way to read a knob back off the chart.
        self.knob_label.setToolTip(" ".join(f"{t}:{v}" for t, v in knobs)
                                   if knobs else "")

    def on_two_point(self) -> None:
        c = self.canvas.curve
        self.editor_from_curve(replace(c, knobs=()), self.active_preset)
        self._dirty = True
        self.result.setText("knobs dropped — Apply to activate the 2-point ramp")

    def load_presets(self) -> dict[str, Curve]:
        try:
            with open(self.args.fan_profile, encoding="utf-8") as f:
                self.presets = parse_presets(f.read())
        except (OSError, ValueError) as e:
            self.presets = {"quiet": Curve(label="quiet")}
            self.result_warn(f"could not parse {self.args.fan_profile}: {e}")
        return self.presets

    def load_all(self) -> None:
        # hardware
        try:
            self.hw = discover(self.args.sysfs)
        except HwmonNotFound as e:
            self.hw = None
            self.result_warn(str(e))

        # active config
        marked = None
        try:
            with open(self.args.config, encoding="utf-8") as f:
                text = f.read()
            current = parse_config(text)
            if self.dgpu:
                gk = parse_knobs(text, "pwm2")
                self.curves["gpu"] = replace(current, knobs=gk)
            marked = current.label if current.label in self.presets and \
                self.curve_matches_preset(current, current.label) else None
            self.editor_from_curve(current, marked)
        except (OSError, ValueError):
            fallback = self.presets.get("quiet", Curve(label="quiet"))
            self.editor_from_curve(fallback, "quiet" if "quiet" in self.presets else None)

        self._dirty = False
        self.refresh_service()
        self.refresh_sensors()

    def curve_matches_preset(self, c: Curve, name: str) -> bool:
        p = self.presets.get(name)
        if p is None:
            return False
        eff = p.clamped(self.cap if not p.ignore_cap else None)
        return all(getattr(c, k) == getattr(eff, k) for k in
                   ("interval", "mintemp", "maxtemp", "minstart", "minstop",
                    "minpwm", "maxpwm", "average"))

    def refresh_sensors(self) -> None:
        if self.hw is None:
            self.status.setText("clevofan/coretemp not found under " + self.args.sysfs)
            self.canvas.set_live(None, None)
            return
        s = read_sensors(self.hw, self.args.sysfs)
        temp_txt = f"{s.cpu_temp_c:.0f} °C" if s.cpu_temp_c is not None else "n/a"
        fans = "  ".join(f"fan{i+1} {r if r is not None else 'n/a'} RPM"
                          for i, r in enumerate(s.rpms))
        pwms = "  ".join(f"pwm{i+1} {pwm_percent(v)}" for i, v in enumerate(s.pwms))
        enable = next((e for e in s.pwm_enables if e is not None), None)
        mode = {1: "manual", 2: "EC auto"}.get(enable, "n/a")
        status = f"CPU {temp_txt}    {fans}    {pwms}    mode {mode}"
        gpu_temp = None
        if self.dgpu:
            # Asking a suspended card its temperature would wake it; show the
            # state instead. The marker then sits the call out entirely.
            d = read_dgpu(self.args.dgpu_pci, self.args.smi)
            if d.present and d.powered and d.temp_c is not None:
                gpu_temp = float(d.temp_c)
                status += f"    GPU {d.temp_c:.0f} °C"
            else:
                status += f"    GPU {d.state}"
        self.status.setText(status)
        if self.sel == "gpu":
            pwm = s.pwms[1] if len(s.pwms) > 1 else None
            self.canvas.set_live(gpu_temp, pwm)
        else:
            live_pwm = next((v for v in s.pwms if v is not None), None)
            self.canvas.set_live(s.cpu_temp_c, live_pwm)
        # If the EC took over (or fancontrol grabbed the fan) outside this app,
        # follow the mode — unless the user has unsaved edits.
        live_auto = enable == 2
        if not self._dirty and live_auto != self.rb_auto.isChecked():
            self._radio_sync = True
            self.rb_auto.setChecked(live_auto)
            self._radio_sync = False

    def refresh_service(self) -> None:
        active, enabled = service_state(self.args.systemctl)
        state = "active" if active else "inactive"
        en = "enabled" if enabled else "disabled"
        cap_txt = f" — noise cap {self.cap}" if self.cap is not None else " — no cap file"
        self.svc_label.setText(
            f"fancontrol.service: {state}, {en}{cap_txt}\n"
            f"config: {self.args.config}")

    # -- user actions ---------------------------------------------------------
    def on_revert(self) -> None:
        manual = self.rb_manual.isChecked()  # user's mode choice is sticky
        self.load_all()
        if manual and self.rb_auto.isChecked():
            self.rb_manual.setChecked(True)

    def on_mode(self) -> None:
        # User-initiated (programmatic syncs wrap setChecked in _radio_sync):
        # a mode choice is a pending edit the sensor tick must not undo.
        if not self._radio_sync:
            self._dirty = True
        auto = self.rb_auto.isChecked()
        self.canvas._auto = auto
        for sb in self.spin.values():
            sb.setEnabled(not auto)
        for name, btn in self.preset_buttons.items():
            btn.setEnabled(not auto and name in self.presets)
        self.sync_knob_ui()
        self.canvas.update()

    def on_preset(self, name: str) -> None:
        p = self.presets[name]
        self.rb_manual.setChecked(True)
        if self.dgpu:
            # A preset is the same shape seeded on both fans; per-fan edits
            # from there are the knob workflow.
            self.curves["gpu"] = replace(p, knobs=())
        self.editor_from_curve(p, name)
        self._dirty = True
        self.result.setText(f"loaded preset '{name}' — Apply to activate")

    def on_spin_edit(self, _key: str) -> None:
        self._dirty = True
        if self.active_preset is not None and \
                not self.curve_matches_preset(self.curve_from_editor(), self.active_preset):
            self.active_preset = None
            for btn in self.preset_buttons.values():
                btn.setChecked(False)
        self.canvas.blockSignals(True)
        self.canvas.set_curve(self.curve_from_editor())
        self.canvas.blockSignals(False)

    def on_canvas_edit(self) -> None:
        self.active_preset = None
        self._dirty = True
        for btn in self.preset_buttons.values():
            btn.setChecked(False)
        c = self.canvas.curve
        if c.knobs:
            self.sync_knob_ui()   # a knob edit moves no spin box, only the count
            return
        for key in ("mintemp", "maxtemp", "minpwm", "maxpwm"):
            sb = self.spin[key]
            sb.blockSignals(True)
            sb.setValue(getattr(c, key))
            sb.blockSignals(False)

    def _ensure_watches(self) -> None:
        # mv-based atomic replace drops the watch; re-add on every (re)load.
        # kdeglobals is watched for the same reason: KDE rewrites it in place
        # when the colour scheme changes, and the watch does not survive that.
        for path in (self.args.config, self.args.cap, ktheme.kdeglobals()):
            if path and os.path.exists(path) and path not in self.watcher.files():
                self.watcher.addPath(path)

    def on_watched_changed(self, path: str) -> None:
        self._ensure_watches()
        if path == ktheme.kdeglobals():
            self.retheme()
            return
        if path == self.args.cap:
            self.cap = read_cap(self.args.cap)
            self.canvas.cap = self.cap
            self.canvas.update()
            self.refresh_service()
            self.result.setText("noise cap changed on disk — reloaded")
            return
        if self._dirty:
            self.result.setText("config changed on disk — keeping your edits (Revert to load)")
        else:
            self.load_all()
            self.result.setText("config changed on disk — reloaded")

    def on_apply(self) -> None:
        helper = find_helper()
        if helper is None:
            return
        if self.rb_auto.isChecked():
            argv = [helper, "--auto"]
        else:
            self.curves[self.sel] = self.canvas.curve
            c = self.curve_from_editor("cpu").clamped(
                None if self.active_preset == "turbo" else self.cap)
            g = self.curve_from_editor("gpu") if self.dgpu else None
            # Knob mode is per-config, not per-fan: one fan in knobs puts the
            # whole file in knob mode, so the native side converts exactly.
            knob_mode = bool(c.knobs) or bool(g and g.knobs)
            if knob_mode:
                if not c.knobs:
                    c = replace(c, knobs=c.as_knobs())
                if g is not None and not g.knobs:
                    g = replace(g, knobs=g.as_knobs())
                g = None if g is None else g.clamped(
                    None if self.active_preset == "turbo" else self.cap)
            try:
                c.validate()
                if g is not None:
                    g.validate()
            except ValueError as e:
                QMessageBox.warning(self, "Invalid curve", str(e))
                return
            opts = ["--ignore-cap"] if c.ignore_cap else []
            if knob_mode:
                opts += ["--knobs", " ".join(f"{t}:{pwm}" for t, pwm in c.knobs)]
                if g is not None:
                    opts += ["--gpu-knobs",
                             " ".join(f"{t}:{pwm}" for t, pwm in g.knobs)]
                # The positionals become the transfer calibration; the curves
                # travel in --knobs/--gpu-knobs. See fancore.KNOB_XFER.
                c = replace(c, **KNOB_XFER)
            argv = [helper] + opts + [
                str(c.interval), str(c.mintemp), str(c.maxtemp), str(c.minstart),
                str(c.minstop), str(c.minpwm), str(c.maxpwm), str(c.average),
                c.label]

        if self.args.no_apply:  # render test: exercise arg building only
            self.result.setText("no-apply: " + " ".join(argv))
            return

        self.btn_apply.setEnabled(False)
        self.result.setText("applying… (polkit may ask for your password)")
        proc = QProcess(self)
        self.apply_proc = proc
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.finished.connect(lambda code, _s, a=argv: self.on_apply_done(code, a))
        proc.start("pkexec", argv)
        if not proc.waitForStarted(3000):
            self.result_warn("could not start pkexec")
            self.btn_apply.setEnabled(True)
            self.apply_proc = None

    def on_apply_done(self, code: int, argv: list[str]) -> None:
        proc = self.apply_proc
        self.apply_proc = None
        out = bytes(proc.readAll()).decode(errors="replace").strip() if proc else ""
        self.btn_apply.setEnabled(True)
        if code == 0:
            self.result.setText("applied — " + (out.splitlines() or [""])[-1])
            self.load_all()
        elif code == 126:
            self.result.setText("cancelled (polkit authentication dismissed)")
        else:
            self.result_warn(f"apply failed (rc={code}): {out or 'see journal'}")

    def retheme(self) -> None:
        """Re-resolve every scheme colour and repaint. Reached from the palette
        event and from the kdeglobals watch; see ktheme.forget."""
        ktheme.forget()
        self.canvas.update()
        self.update()

    def changeEvent(self, e) -> None:  # noqa: N802
        # Plasma switching the colour scheme reaches a running Qt app as an
        # app-wide palette change, and Qt turns that into PaletteChange on every
        # widget (ApplicationPaletteChange goes to event(), not here -- measured,
        # see test_qt_still_delivers_a_palette_change_on_a_scheme_switch).
        # Without this the chart keeps the colours it started with all session.
        if e.type() == QEvent.Type.PaletteChange:
            self.retheme()
        super().changeEvent(e)

    def result_warn(self, msg: str) -> None:
        # Multiple problems stay visible (the label shows only the last one otherwise)
        current = self.result.text()
        text = msg if not current or msg in current else f"{current}\n{msg}"
        neg = ktheme.colors(self.palette()).negative
        self.result.setStyleSheet(f"color: {neg.name()}")
        # KDE HIG: colour must not be the only carrier of meaning, so the text
        # says it is an error too. Red alone is invisible to a dichromat.
        self.result.setText(text if text.startswith(ERROR_PREFIX)
                            else f"{ERROR_PREFIX}{text}")
        QTimer.singleShot(4000, lambda: self.result.setStyleSheet(""))


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Juno KDE fan control")
    ap.add_argument("--sysfs", default=DEFAULT_PLATFORM)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--fan-profile", default=DEFAULT_FAN_PROFILE)
    ap.add_argument("--cap", default=DEFAULT_CAP)
    ap.add_argument("--systemctl", default="systemctl")
    ap.add_argument("--dgpu-pci", default=DEFAULT_DGPU_PCI,
                    help="dGPU sysfs dir; absent means no GPU fan tab")
    ap.add_argument("--smi", default="nvidia-smi", help="nvidia-smi command")
    ap.add_argument("--fan", choices=("cpu", "gpu"), default="cpu",
                    help="fan curve shown at startup (renders)")
    ap.add_argument("--screenshot", help="render and save PNG, then exit")
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--preset", help="load this preset in the editor at startup")
    ap.add_argument("--auto", action="store_true", help="start in EC-auto mode")
    ap.add_argument("--no-apply", action="store_true")
    return ap.parse_args(argv)


def dark_palette() -> QPalette:
    """Breeze Dark for a session that supplies no palette: the offscreen
    renders and any non-Plasma desktop run with --dark. On Plasma the platform
    theme already carries the user's scheme, so this is never applied there.
    Values are BreezeDark.colors: Window/View/Button/Selection BackgroundNormal
    and ForegroundNormal."""
    pal = QPalette()
    base, window = QColor(20, 22, 24), QColor(32, 35, 38)
    button, text, hi = QColor(41, 44, 48), QColor(252, 252, 252), QColor(61, 174, 233)
    pal.setColor(QPalette.ColorRole.Base, base)
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(29, 31, 34))
    pal.setColor(QPalette.ColorRole.Window, window)
    pal.setColor(QPalette.ColorRole.Button, button)
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        pal.setColor(role, text)
    pal.setColor(QPalette.ColorRole.Highlight, hi)
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(252, 252, 252))
    # setColor() without a group sets every group, so the disabled text came
    # out equal to the normal text and ktheme's inactive fallback collapsed
    # onto it. BreezeDark ForegroundInactive is the value the scheme would
    # have supplied.
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(161, 169, 177))
    return pal


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    app = QApplication(sys.argv[:1])
    # No setStyle(): the platform theme picks Breeze on a Plasma session, and
    # forcing Fusion there is exactly what made this look unthemed. The
    # renders pass --style explicitly instead (tests/render_app.py).
    app.setApplicationName(DESKTOP_FILE)
    # Same string as the window title and the launcher entry's Name. Qt shows
    # this in task switchers, so a third variant would be a third name for one
    # app. The System Settings entry is "Fan Control" on purpose: it is already
    # inside a settings context.
    app.setApplicationDisplayName("Juno Fan Control")
    app.setDesktopFileName(DESKTOP_FILE)
    app.setWindowIcon(QIcon.fromTheme(APP_ICON))
    if args.dark:
        app.setPalette(dark_palette())
    w = MainWindow(args)
    w.show()
    if args.screenshot:
        def shot() -> None:
            w.grab().save(args.screenshot)
            app.exit(0)
        QTimer.singleShot(1200, shot)  # let a couple of sensor ticks land
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
