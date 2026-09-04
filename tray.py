#!/usr/bin/env python3
"""juno-fan-monitor — system tray readout for the Juno (Clevo) laptop.

The icon carries the CPU temperature. Clicking it opens a panel with a CPU
and a GPU utilization chart, two temperature gauges, the compute-GPU
indicator, fan speeds, network rates, power draw and battery runtime.

Test/debug entry points (used by tests/render_tray.py):
    --sysfs DIR --stat FILE --net FILE --net-class DIR --gt DIR --dgpu-pci DIR
    --power-supply DIR --rapl DIR --nvidia-smi CMD
    --screenshot FILE [--dark] [--interval MS]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QEvent, QPointF, QRectF, QSettings, Qt, QTimer
from PySide6.QtGui import (QAction, QColor, QFont, QFontMetrics, QIcon, QPainter,
                           QPalette, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox,
                               QGridLayout, QLabel, QMenu,
                               QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget)

from backend import ktheme
from backend.fancore import (DEFAULT_PLATFORM, HwmonNotFound, discover,
                             pwm_percent, read_sensors)
from backend.sysmon import (DEFAULT_DGPU_PCI, DEFAULT_I915_GT, DEFAULT_NET,
                            DEFAULT_NET_CLASS, DEFAULT_POWER_SUPPLY,
                            DEFAULT_RAPL, DEFAULT_STAT, Sampler, fmt_duration,
                            fmt_rate)

HISTORY = 90              # samples kept in the chart
ICON_PX = 64              # painted at 64 px; the tray scales it down
CPU_WARN_C = 85           # icon turns red above this
# The three series and the warning share the plot's semantic colours, so a
# scheme switch moves them all: see backend/ktheme.py.
SERIES = {"CPU": "focus", "iGPU": "positive", "dGPU": "neutral"}
RAPL_RULES = "/usr/share/juno-kde-fancontrol/rapl-readable.rules"    # shipped, not installed: see the panel hint


class Sparkline(QWidget):
    """Utilization over time, 0..100 %, oldest sample on the left. `title`
    names the chart on the panel ("CPU", "GPU") so the two are told apart."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title
        self.setMinimumSize(260, 90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.series: dict[str, deque[float]] = {}
        self.colors: dict[str, QColor] = {}

    def add(self, name: str, color: QColor, value: float | None) -> None:
        if name not in self.series:
            self.series[name] = deque(maxlen=HISTORY)
            self.colors[name] = color
        self.series[name].append(0.0 if value is None else value)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt name)
        pal = self.palette()
        fg = pal.color(QPalette.ColorRole.WindowText)
        grid = QColor(fg)
        grid.setAlpha(45)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        small = QFont(p.font())
        small.setPointSizeF(max(8.0, small.pointSizeF() - 1.0))
        p.setFont(small)
        # The tick-label gutter has to come from the metrics: a fixed 30 px
        # clipped "100%" to "loo%" at this size.
        fm = QFontMetrics(small)
        gutter = fm.horizontalAdvance("100%") + 8
        # A 14 px strip above the plot carries the chart title.
        r = QRectF(gutter, 18, self.width() - gutter - 6, self.height() - 34)
        bold = QFont(small)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(QPen(fg, 1))
        p.drawText(QRectF(r.left(), 2, r.width(), 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.title)
        p.setFont(small)

        p.setPen(QPen(grid, 1))
        for pct in (0, 50, 100):
            y = r.bottom() - pct / 100 * r.height()
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(QPen(fg, 1))
        p.drawRect(r)
        for pct in (0, 50, 100):
            y = r.bottom() - pct / 100 * r.height()
            p.drawText(QRectF(0, y - 8, r.left() - 4, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{pct}%")

        for name, values in self.series.items():
            pen = QPen(QColor(self.colors[name]), 1.8)
            p.setPen(pen)
            # newest sample pinned to the right edge, so a partly filled
            # history grows leftwards instead of faking a flat line at zero
            n = len(values)
            step = r.width() / (HISTORY - 1)
            pts = [QPointF(r.right() - (n - 1 - i) * step,
                           r.bottom() - min(v, 100.0) / 100 * r.height())
                   for i, v in enumerate(values)]
            for a, b in zip(pts, pts[1:]):
                p.drawLine(a, b)

        legend = r.left() + 4
        for name, values in self.series.items():
            p.setPen(QColor(self.colors[name]))
            text = f"{name} {values[-1]:.0f}%" if values else f"{name} —"
            p.drawText(QRectF(legend, r.bottom() + 2, 120, 14), text)
            legend += fm.horizontalAdvance(text) + 12
        p.end()


class TempGauge(QWidget):
    """Mini temperature gauge: a labelled bar filling from `lo` to `hi` °C.

    A suspended source paints no fill and its reason in the scheme's inactive
    colour, which keeps "off" distinct from any real reading without inventing
    a temperature. All colours come from the palette and ktheme at paint time.
    """

    def __init__(self, label: str, lo: float = 20.0, hi: float = 110.0,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label, self.lo, self.hi = label, lo, hi
        self.value: float | None = None
        self.suspended: str | None = None     # the reason text while off
        self.setMinimumSize(200, 26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, temp_c: float | None) -> None:
        self.value, self.suspended = temp_c, None
        self.update()

    def set_suspended(self, reason: str) -> None:
        self.value, self.suspended = None, reason
        self.update()

    def _frac(self) -> float:
        """Fill fraction of the range. A degenerate range divides by nothing:
        the widget still paints, empty."""
        span = self.hi - self.lo
        if span <= 0:
            return 0.0
        if self.value is None:
            return 0.0
        return min(1.0, max(0.0, (self.value - self.lo) / span))

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt name)
        pal = self.palette()
        k = ktheme.colors(pal)
        fg = pal.color(QPalette.ColorRole.WindowText)
        edge = QColor(fg)
        edge.setAlpha(90)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)

        p.setPen(QPen(edge, 1))
        p.setBrush(pal.brush(QPalette.ColorRole.Base))
        p.drawRoundedRect(r, 4, 4)

        if self.suspended is None:
            frac = self._frac()
            if frac > 0:
                hot = (self.value or 0) >= CPU_WARN_C
                fill = k.negative if hot else k.focus
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(fill)
                w = max(2.0, frac * r.width())
                p.drawRoundedRect(QRectF(r.left() + 1, r.top() + 1,
                                         min(w, r.width() - 2), r.height() - 2), 3, 3)
            text_r = f"n/a" if self.value is None else f"{self.value:.0f} °C"
            ink = fg
        else:
            text_r = self.suspended
            ink = k.inactive

        p.setPen(QPen(ink, 1))
        outer = r.adjusted(8, 0, -8, 0)
        p.drawText(outer, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.label)
        p.drawText(outer, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   text_r)
        p.end()


# The dashboard widgets and rows the panel can show, in panel order. Every one
# is individually switchable; the choice persists in QSettings (tray context
# menu -> Probes...). The text rows, then the gauges, then the charts:
PROBES: tuple[tuple[str, str], ...] = (
    ("compute-gpu", "Compute GPU"),
    ("fan-cpu", "CPU fan"),
    ("fan-gpu", "GPU fan"),
    ("igpu", "iGPU"),
    ("net", "NET"),
    ("power", "POWER"),
    ("battery", "BATTERY"),
)
GAUGES: tuple[tuple[str, str], ...] = (
    ("cpu", "CPU temperature gauge"),
    ("gpu", "GPU temperature gauge"),
)
CHARTS: tuple[tuple[str, str], ...] = (
    ("chart-cpu", "CPU utilization chart"),
    ("chart-gpu", "GPU utilization chart"),
)


def probe_settings(args: argparse.Namespace) -> QSettings:
    """--settings exists so tests and renders never touch the real store.

    Read-side legacy migration of pre-dashboard stores: the old panel had one
    "chart" key and plain cpu/gpu text rows. A store that only has the legacy
    keys maps "chart" onto BOTH new charts and keeps "cpu"/"gpu" addressing
    what replaced those rows — the gauges. New-key writes always win; nothing
    is rewritten, so the fixture-mode store (--settings) works unchanged."""
    if args.settings:
        return QSettings(args.settings, QSettings.Format.IniFormat)
    return QSettings("juno", "juno-fan-monitor")


# New key -> legacy keys it falls back to when the store has no new-key entry.
LEGACY_PROBES: dict[str, tuple[str, ...]] = {
    "chart-cpu": ("chart",),
    "chart-gpu": ("chart",),
}


class ProbesDialog(QDialog):
    """Non-modal checkbox list; each toggle writes through immediately and the
    panel reflows on the next tick. Stays above the always-on-top tray popup."""

    def __init__(self, panel: "Panel") -> None:
        super().__init__(None, Qt.WindowType.Dialog)
        self.setWindowTitle("Probes — Juno system monitor")
        self.panel = panel
        lay = QVBoxLayout(self)
        for key, label in (*CHARTS, *GAUGES, *PROBES):
            cb = QCheckBox(label)
            cb.setChecked(panel.probe_on(key))
            cb.toggled.connect(lambda on, k=key: panel.set_probe(k, on))
            lay.addWidget(cb)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class Panel(QWidget):
    """The popup. A plain Popup window so it closes when focus moves away."""

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(None, Qt.WindowType.Popup)
        self.args = args
        self.setWindowTitle("Juno system monitor")
        self.settings = probe_settings(args)

        root = QVBoxLayout(self)
        self.charts: dict[str, Sparkline] = {}
        for key, _label in CHARTS:
            chart = Sparkline(title=key.removeprefix("chart-").upper())
            root.addWidget(chart)
            self.charts[key] = chart
        self.chart_cpu = self.charts["chart-cpu"]
        self.chart_gpu = self.charts["chart-gpu"]

        self.gauges: dict[str, TempGauge] = {}
        for key, label in GAUGES:
            gauge = TempGauge(label.split()[0])      # "CPU" / "GPU"
            root.addWidget(gauge)
            self.gauges[key] = gauge

        self.row_widgets: dict[str, tuple[QLabel, QLabel]] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        root.addLayout(grid)
        for i, (key, label) in enumerate(PROBES):
            name = QLabel(label)
            name.setStyleSheet("font-weight: bold")
            value = QLabel("—")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(name, i, 0, Qt.AlignmentFlag.AlignRight)
            grid.addWidget(value, i, 1)
            self.row_widgets[key] = (name, value)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)
        # The indicator row's state colour: a ktheme role name, re-resolved on
        # every retheme so a scheme switch moves it like everything else.
        self._indicator_role = "inactive"
        self.apply_probe_visibility()
        self.retheme()

    def probe_on(self, key: str) -> bool:
        """New key when the store has it, else the legacy fallback (see
        probe_settings), else on. `contains` first: QSettings reads an ini
        "false" as the string 'false' unless asked for a bool, and a truthy
        string would resurrect a switched-off probe."""
        skey = f"probes/{key}"
        if self.settings.contains(skey):
            return self.settings.value(skey, True, type=bool)
        for legacy in LEGACY_PROBES.get(key, ()):
            lkey = f"probes/{legacy}"
            if self.settings.contains(lkey):
                return self.settings.value(lkey, True, type=bool)
        return True

    def set_probe(self, key: str, on: bool) -> None:
        self.settings.setValue(f"probes/{key}", on)
        self.settings.sync()
        self.apply_probe_visibility()

    def apply_probe_visibility(self) -> None:
        for key, (name, value) in self.row_widgets.items():
            name.setVisible(self.probe_on(key))
            value.setVisible(self.probe_on(key))
        for group in (self.charts, self.gauges):
            for key, widget in group.items():
                widget.setVisible(self.probe_on(key))
        self.adjustSize()

    def set_indicator(self, text: str, role: str) -> None:
        """The compute-GPU row. `role` names a ktheme colour: neutral while the
        dGPU runs (it costs power), inactive while the iGPU does."""
        self._indicator_role = role
        self.set_row("compute-gpu", text)
        self._style_indicator()

    def _style_indicator(self) -> None:
        k = ktheme.colors(self.palette())
        sheet = f"color: {getattr(k, self._indicator_role).name()}"
        label = self.row_widgets["compute-gpu"][1]
        # Same no-recursion guard as the hint: assigning an unchanged sheet
        # posts a PaletteChange and re-enters here forever.
        if sheet != label.styleSheet():
            label.setStyleSheet(sheet)

    def retheme(self) -> None:
        """Re-resolve the scheme colours and repaint. An inline stylesheet wins
        over the palette, so the hint and the indicator have to be re-set
        explicitly: a palette change alone would leave them the old colour.
        setStyleSheet itself posts a PaletteChange, so only a changed sheet is
        assigned -- otherwise this re-enters through changeEvent forever."""
        ktheme.forget()
        sheet = f"color: {ktheme.colors(self.palette()).inactive.name()}"
        if sheet != self.hint.styleSheet():
            self.hint.setStyleSheet(sheet)
        self._style_indicator()
        for chart in self.charts.values():
            chart.update()
        for gauge in self.gauges.values():
            gauge.update()
        self.update()

    def changeEvent(self, e) -> None:  # noqa: N802
        # Plasma switching the colour scheme reaches a running Qt app as an
        # app-wide palette change, which Qt delivers here as PaletteChange; the
        # three series and the hint all follow the scheme.
        if e.type() == QEvent.Type.PaletteChange:
            self.retheme()
        super().changeEvent(e)

    def set_row(self, key: str, text: str) -> None:
        self.row_widgets[key][1].setText(text)


class Monitor:
    def __init__(self, args: argparse.Namespace, app: QApplication) -> None:
        self.args = args
        self.app = app
        self.sampler = Sampler(stat_path=args.stat, net_path=args.net,
                               gt_dir=args.gt, dgpu_pci=args.dgpu_pci,
                               supply_dir=args.power_supply, rapl_dir=args.rapl,
                               nvidia_smi=args.nvidia_smi, net_class_dir=args.net_class)
        try:
            self.hw = discover(args.sysfs)
        except HwmonNotFound:
            self.hw = None

        self.panel = Panel(args)
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(self.paint_icon(None))
        self.tray.activated.connect(self.on_activated)

        menu = QMenu()
        fan_gui = QAction("Fan control…", menu)
        fan_gui.triggered.connect(self.launch_fan_gui)
        probes = QAction("Probes…", menu)
        probes.triggered.connect(self.show_probes)
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(app.quit)
        menu.addAction(probes)
        menu.addAction(fan_gui)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.menu = menu             # keep a reference: setContextMenu does not own it
        self.tray.setContextMenu(menu)

        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(args.interval)
        self.refresh()

    # -- rendering ----------------------------------------------------------
    def paint_icon(self, temp_c: float | None) -> QIcon:
        pix = QPixmap(ICON_PX, ICON_PX)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = "--" if temp_c is None else f"{temp_c:.0f}"
        pal = self.panel.palette()
        color = (ktheme.colors(pal).negative if (temp_c or 0) >= CPU_WARN_C
                 else pal.color(QPalette.ColorRole.WindowText))
        font = QFont(p.font())
        font.setPixelSize(int(ICON_PX * 0.62))
        font.setBold(True)
        p.setFont(font)
        p.setPen(color)
        p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, text)
        p.end()
        return QIcon(pix)

    def on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        if self.panel.isVisible():
            self.panel.hide()
            return
        geo = self.tray.geometry()
        self.panel.adjustSize()
        screen = QApplication.primaryScreen().availableGeometry()
        x = min(max(geo.center().x() - self.panel.width() // 2, screen.left()),
                screen.right() - self.panel.width())
        below = geo.bottom() + 4
        y = below if below + self.panel.height() < screen.bottom() \
            else geo.top() - self.panel.height() - 4
        self.panel.move(x, y)
        self.panel.show()

    def launch_fan_gui(self) -> None:
        from PySide6.QtCore import QProcess
        QProcess.startDetached("juno-kde-fancontrol", [])

    def show_probes(self) -> None:
        # One dialog instance, raised on repeat activations.
        if getattr(self, "_probes", None) is None:
            self._probes = ProbesDialog(self.panel)
            self._probes.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._probes.show()
        self._probes.raise_()
        self._probes.activateWindow()

    # -- data ---------------------------------------------------------------
    def refresh(self) -> None:
        s = self.sampler.sample()
        temp_c = None
        fan_rows = ("clevofan not found", "n/a")
        if self.hw is not None:
            fan = read_sensors(self.hw, self.args.sysfs)
            temp_c = fan.cpu_temp_c

            def fan_row(i: int) -> str:
                if i >= len(fan.rpms):
                    return "absent"
                rpm = fan.rpms[i]
                pwm = fan.pwms[i] if i < len(fan.pwms) else None
                return f"{rpm if rpm is not None else 'n/a'} RPM   {pwm_percent(pwm)}"

            fan_rows = (fan_row(0), fan_row(1))

        self.tray.setIcon(self.paint_icon(temp_c))
        k = ktheme.colors(self.panel.palette())
        self.panel.chart_cpu.add("CPU", getattr(k, SERIES["CPU"]), s.cpu_pct)
        self.panel.chart_gpu.add("iGPU", getattr(k, SERIES["iGPU"]), s.igpu_pct)
        # The dGPU only joins the chart once it is awake; a suspended card has
        # no utilization to plot and must not be woken to invent one.
        if s.dgpu.powered and s.dgpu.util_pct is not None:
            self.panel.chart_gpu.add("dGPU", getattr(k, SERIES["dGPU"]), s.dgpu.util_pct)
        self.panel.chart_cpu.update()
        self.panel.chart_gpu.update()

        # The gauges replace the old cpu/gpu text rows. A suspended dGPU shows
        # its state, not a made-up temperature.
        temp_txt = "n/a" if temp_c is None else f"{temp_c:.0f} °C"
        self.panel.gauges["cpu"].set_value(temp_c)
        d = s.dgpu
        if not d.present:
            self.panel.gauges["gpu"].set_suspended("absent")
        elif not d.powered:
            self.panel.gauges["gpu"].set_suspended("suspended")
        elif d.temp_c is None:
            self.panel.gauges["gpu"].set_value(None)   # awake, smi unreadable
        else:
            self.panel.gauges["gpu"].set_value(d.temp_c)

        # Which GPU the compute load sits on, in a colour that names the cost:
        # the dGPU drawing power is the noteworthy state.
        if d.powered and d.util_pct is not None:
            self.panel.set_indicator("dGPU (NVIDIA)", "neutral")
        else:
            self.panel.set_indicator("iGPU (Intel Arc)", "inactive")

        cpu_txt = "n/a" if s.cpu_pct is None else f"{s.cpu_pct:.0f}% busy"
        self.panel.set_row("fan-cpu", fan_rows[0])
        self.panel.set_row("fan-gpu", fan_rows[1])

        igpu = "n/a" if s.igpu_pct is None else f"{s.igpu_pct:.0f}% busy"
        # rps_act_freq reads 0 whenever the sample lands inside an RC6 window,
        # which contradicts a non-zero busy figure. Show it only when running.
        if s.igpu_mhz:
            igpu += f"   {s.igpu_mhz} / {s.igpu_max_mhz} MHz"
        self.panel.set_row("igpu", f"Intel Arc   {igpu}")

        if not d.present:
            dgpu_txt = "absent"
        elif not d.powered:
            dgpu_txt = f"off — {d.state}"
        elif d.temp_c is None:
            dgpu_txt = f"on ({d.state}), nvidia-smi unavailable"
        else:
            dgpu_txt = (f"{d.temp_c} °C   {d.util_pct}% busy   {d.power_w:.1f} W"
                        f"   {d.memory_mb} MiB")

        self.panel.set_row("net", f"down {fmt_rate(s.net_rx_bps)}   "
                                  f"up {fmt_rate(s.net_tx_bps)}"
                                  f"   ({', '.join(s.net_ifaces) or 'no interface'})")

        b = s.battery
        if s.package_w is not None:
            power_txt = f"{s.package_w:.1f} W platform (RAPL psys)"
        elif b.status == "Discharging" and b.power_w:
            power_txt = f"{b.power_w:.1f} W from battery (system total)"
        else:
            power_txt = "on AC — total draw needs RAPL"
        self.panel.set_row("power", power_txt)

        pct = "n/a" if b.percent is None else f"{b.percent}%"
        left = fmt_duration(b.seconds_left)
        if b.status == "Discharging":
            batt_txt = f"{pct}   {left} left"
        elif b.status == "Charging":
            batt_txt = f"{pct}   {left} to full"
        else:
            batt_txt = f"{pct}   {b.status.lower()}"
        self.panel.set_row("battery", batt_txt)

        if s.package_w is None and b.status != "Discharging":
            self.panel.hint.setText(
                "Total draw on AC needs the RAPL counter, root-only since PLATYPUS. "
                "Grant it once:\n"
                f"  sudo install -m644 {RAPL_RULES} "
                "/etc/udev/rules.d/99-rapl-readable.rules\n"
                "  sudo udevadm control --reload && sudo udevadm trigger -s powercap")
        else:
            self.panel.hint.setText("")

        self.tray.setToolTip(f"CPU {temp_txt}  {cpu_txt}\n"
                             f"CPU fan {fan_rows[0]}\nGPU fan {fan_rows[1]}\n"
                             f"GPU {dgpu_txt}\n{power_txt}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Juno tray system monitor")
    ap.add_argument("--sysfs", default=DEFAULT_PLATFORM)
    ap.add_argument("--stat", default=DEFAULT_STAT)
    ap.add_argument("--net", default=DEFAULT_NET)
    ap.add_argument("--net-class", default=DEFAULT_NET_CLASS)
    ap.add_argument("--gt", default=DEFAULT_I915_GT)
    ap.add_argument("--dgpu-pci", default=DEFAULT_DGPU_PCI)
    ap.add_argument("--power-supply", default=DEFAULT_POWER_SUPPLY)
    ap.add_argument("--rapl", default=DEFAULT_RAPL)
    ap.add_argument("--nvidia-smi", default="nvidia-smi")
    ap.add_argument("--interval", type=int, default=2000, help="refresh period, ms")
    ap.add_argument("--settings", default=None,
                    help="QSettings ini file override (tests/renders; default: platform store)")
    ap.add_argument("--screenshot", help="render the panel and exit")
    ap.add_argument("--screenshot-samples", type=int, default=40,
                    help="samples to collect before the screenshot, at --interval")
    ap.add_argument("--dark", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    from app import dark_palette
    args = parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)   # the tray outlives the popup
    # No setStyle(): Plasma's platform theme supplies Breeze and the user's
    # colour scheme. tests/render_tray.py passes --style for the renders.
    app.setApplicationName("juno-fan-monitor")
    app.setApplicationDisplayName("Fan Monitor")
    app.setDesktopFileName("juno-fan-monitor")
    if args.dark:
        app.setPalette(dark_palette())

    mon = Monitor(args, app)
    if args.screenshot:
        # The timer is already sampling; wait for the history to fill so the
        # chart shows a real trace, not the single point of a cold start.
        def shot() -> None:
            mon.panel.adjustSize()
            mon.panel.show()
            QTimer.singleShot(200, lambda: (mon.panel.grab().save(args.screenshot),
                                            app.exit(0)))
        QTimer.singleShot(args.interval * args.screenshot_samples + 500, shot)
    else:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            # Without a status area the process would run invisible and forever.
            print("no system tray available on this desktop", file=sys.stderr)
            return 1
        mon.tray.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
