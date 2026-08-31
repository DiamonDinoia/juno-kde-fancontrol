#!/usr/bin/env python3
"""juno-fan-monitor — system tray readout for the Juno (Clevo) laptop.

The icon carries the CPU temperature. Clicking it opens a panel with fan
speeds, both GPUs, network rates, power draw and battery runtime, plus a
rolling chart of CPU and GPU utilization.

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

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QAction, QColor, QFont, QFontMetrics, QIcon, QPainter,
                           QPalette, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QGridLayout, QLabel, QMenu,
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
    """CPU / iGPU / dGPU utilization, 0..100 %, oldest sample on the left."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(260, 90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.series: dict[str, deque[float]] = {}
        self.colors: dict[str, str] = {}

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
        r = QRectF(gutter, 4, self.width() - gutter - 6, self.height() - 20)

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


class Panel(QWidget):
    """The popup. A plain Popup window so it closes when focus moves away."""

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(None, Qt.WindowType.Popup)
        self.args = args
        self.setWindowTitle("Juno system monitor")

        root = QVBoxLayout(self)
        self.chart = Sparkline()
        root.addWidget(self.chart)

        self.rows: dict[str, QLabel] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        root.addLayout(grid)
        for i, key in enumerate(("cpu", "fans", "igpu", "dgpu", "net", "power", "battery")):
            name = QLabel(key.upper())
            name.setStyleSheet("font-weight: bold")
            value = QLabel("—")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(name, i, 0, Qt.AlignmentFlag.AlignRight)
            grid.addWidget(value, i, 1)
            self.rows[key] = value

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)
        self.retheme()

    def retheme(self) -> None:
        """Re-resolve the scheme colours and repaint. An inline stylesheet wins
        over the palette, so the hint has to be re-set explicitly: a palette
        change alone would leave it the old grey. setStyleSheet itself posts a
        PaletteChange, so only a changed sheet is assigned -- otherwise this
        re-enters through changeEvent forever."""
        ktheme.forget()
        sheet = f"color: {ktheme.colors(self.palette()).inactive.name()}"
        if sheet != self.hint.styleSheet():
            self.hint.setStyleSheet(sheet)
        self.chart.update()
        self.update()

    def changeEvent(self, e) -> None:  # noqa: N802
        # Plasma switching the colour scheme reaches a running Qt app as an
        # app-wide palette change, which Qt delivers here as PaletteChange; the
        # three series and the hint all follow the scheme.
        if e.type() == QEvent.Type.PaletteChange:
            self.retheme()
        super().changeEvent(e)

    def set_row(self, key: str, text: str) -> None:
        self.rows[key].setText(text)


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
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(app.quit)
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

    # -- data ---------------------------------------------------------------
    def refresh(self) -> None:
        s = self.sampler.sample()
        temp_c, fan_text = None, "clevofan not found"
        if self.hw is not None:
            fan = read_sensors(self.hw, self.args.sysfs)
            temp_c = fan.cpu_temp_c
            rpms = "  ".join(f"fan{i + 1} {r if r is not None else 'n/a'} RPM"
                             for i, r in enumerate(fan.rpms))
            pwms = "  ".join(pwm_percent(v) for v in fan.pwms)
            fan_text = f"{rpms}   pwm {pwms}"

        self.tray.setIcon(self.paint_icon(temp_c))
        k = ktheme.colors(self.panel.palette())
        self.panel.chart.add("CPU", getattr(k, SERIES["CPU"]), s.cpu_pct)
        self.panel.chart.add("iGPU", getattr(k, SERIES["iGPU"]), s.igpu_pct)
        # The dGPU only joins the chart once it is awake; a suspended card has
        # no utilization to plot and must not be woken to invent one.
        if s.dgpu.powered and s.dgpu.util_pct is not None:
            self.panel.chart.add("dGPU", getattr(k, SERIES["dGPU"]), s.dgpu.util_pct)
        self.panel.chart.update()

        cpu_txt = "n/a" if s.cpu_pct is None else f"{s.cpu_pct:.0f}% busy"
        temp_txt = "n/a" if temp_c is None else f"{temp_c:.0f} °C"
        self.panel.set_row("cpu", f"{temp_txt}   {cpu_txt}")
        self.panel.set_row("fans", fan_text)

        igpu = "n/a" if s.igpu_pct is None else f"{s.igpu_pct:.0f}% busy"
        # rps_act_freq reads 0 whenever the sample lands inside an RC6 window,
        # which contradicts a non-zero busy figure. Show it only when running.
        if s.igpu_mhz:
            igpu += f"   {s.igpu_mhz} / {s.igpu_max_mhz} MHz"
        self.panel.set_row("igpu", f"Intel Arc   {igpu}")

        d = s.dgpu
        if not d.present:
            dgpu_txt = "absent"
        elif not d.powered:
            dgpu_txt = f"off — {d.state}"
        elif d.temp_c is None:
            dgpu_txt = f"on ({d.state}), nvidia-smi unavailable"
        else:
            dgpu_txt = (f"{d.temp_c} °C   {d.util_pct}% busy   {d.power_w:.1f} W"
                        f"   {d.memory_mb} MiB")
        self.panel.set_row("dgpu", f"NVIDIA   {dgpu_txt}")

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

        self.tray.setToolTip(f"CPU {temp_txt}  {cpu_txt}\n{fan_text}\n"
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
