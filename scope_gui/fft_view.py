"""FFT display: amplitude spectrum of CH1 or CH2 from the last capture, 0 Hz .. a fraction of the Nyquist frequency."""
import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from unit_scope import spectrum as sp

from . import theme
from .scope_view import fmt_rate, si

W, H = 800, 480
LEFT, RIGHT, TOP, BOTTOM = 66, 16, 30, 34
DB_SPAN = 80.0                       # dB shown below the top of the plot


def fmt_hz(f):
    """1000 -> '1kHz', 62.5 -> '62.5Hz'."""
    for div, unit in ((1e6, "MHz"), (1e3, "kHz")):
        if abs(f) >= div * 0.9995:
            return f"{f / div:.4g}{unit}"
    return f"{f:.4g}Hz"


def nice_step(span, target):
    """A 1-2-5 step that gives about ``target`` divisions over ``span``."""
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


class FftView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.frame = None
        self.theme = "dark"
        self.source = 1
        self.window = "hann"
        self.scale = "dbv"                 # "dbv" (dB re 1 V rms) or "v" (volts peak)
        self.data = "mem"                  # "mem": the scope's acquisition memory (32-64 kpts), "screen": the 1600-pt record
        self.span_hz = "auto"              # "auto" (0 .. 10 x the peak), "full" (0 .. Nyquist) or a frequency in Hz
        self._used = "screen"              # what the current spectrum was computed from
        self._spec = None
        self._dirty = True
        self._hover = None                 # x in widget pixels
        self.setMinimumSize(480, 288)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # --- data ------------------------------------------------------------------
    def set_theme(self, name):
        self.theme = name
        self.update()

    def set_frame(self, frame):
        self.frame = frame
        self._dirty = True
        if self.isVisible():
            self.update()

    def set_options(self, **kw):
        """source (1|2), window, scale, data ('mem'|'screen'), span_hz (number or None)."""
        for key, value in kw.items():
            if not hasattr(self, key):
                raise AttributeError(key)
            setattr(self, key, value)
        if {"source", "window", "data"} & kw.keys():
            self._dirty = True
        self.update()

    def spectrum(self):
        """The spectrum of the selected channel (computed when needed, i.e. only while this tab is shown)."""
        if self._dirty:
            self._dirty = False
            self._spec = None
            cd = None
            if self.frame is not None:
                mem = getattr(self.frame, "memory", {}).get(self.source) if self.data == "mem" else None
                cd = mem or next((c for c in self.frame.channels if c.channel == self.source), None)
                self._used = "memory" if (mem or (cd is not None and len(cd.t) > 2000)) else "screen"
            if cd is not None and len(cd.t) >= 8:
                dt = (cd.t[-1] - cd.t[0]) / (len(cd.t) - 1)
                self._spec = sp.spectrum(cd.volts, dt, self.window)
        return self._spec

    def span_limit(self, s):
        """Highest frequency shown. Auto: ten times the strongest line (so its harmonics are visible), but at least
        40 resolution steps, so a line near 0 Hz is not squeezed against the edge of a wide span."""
        if isinstance(self.span_hz, (int, float)):
            return min(float(self.span_hz), s.nyquist)
        if self.span_hz == "full":
            return s.nyquist
        f0, _ = sp.find_peak(s)
        return min(s.nyquist, max(10.0 * f0, 40.0 * s.rbw)) if f0 > 0 else s.nyquist

    def _value(self, amp):
        """Spectrum value in the displayed unit."""
        return sp.to_dbv(amp / math.sqrt(2)) if self.scale == "dbv" else amp

    # --- mouse -----------------------------------------------------------------
    def mouseMoveEvent(self, e):
        self._hover = e.position().x()
        self.update()

    def leaveEvent(self, e):
        self._hover = None
        self.update()

    # --- painting --------------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.scale(self.width() / W, self.height() / H)
        self.paint_fft(p, hover=None if self._hover is None else self._hover * W / self.width())

    def render_image(self, scale=2):
        img = QImage(W * scale, H * scale, QImage.Format.Format_RGB32)
        p = QPainter(img)
        p.scale(scale, scale)
        self.paint_fft(p)
        p.end()
        return img

    def _f(self, size, weight=QFont.Weight.Normal):
        if self.theme == "light" and weight.value < QFont.Weight.DemiBold.value:
            weight = QFont.Weight.DemiBold
        return QFont("Segoe UI", size, weight)

    def paint_fft(self, p, hover=None):
        c = theme.colors(self.theme)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.fillRect(QRectF(0, 0, W, H), QColor(c["face"]))
        plot = QRectF(LEFT, TOP, W - LEFT - RIGHT, H - TOP - BOTTOM)
        s = self.spectrum()
        p.setFont(self._f(10))
        if s is None:
            p.setPen(QColor(c["grid_major"]))
            p.setFont(self._f(14))
            msg = "No data - connect and capture" if self.frame is None else f"CH{self.source} has no data"
            p.drawText(plot, Qt.AlignmentFlag.AlignCenter, msg)
            p.setPen(QPen(QColor(c["grid_major"]), 1))
            p.drawRect(plot)
            return

        fmax = self.span_limit(s)
        nbin = max(2, min(len(s.freqs), int(fmax / s.df) + 2))
        vals = [self._value(a) for a in s.amps[:nbin]]
        pk_freq, pk_amp = sp.find_peak(s, fmax)               # the peak within the shown span
        peak = self._value(pk_amp)
        if self.scale == "dbv":
            top = math.ceil((peak + 3) / 10.0) * 10.0
            lo, hi, unit, vstep = top - DB_SPAN, top, "dBV", 10.0
        else:
            hi = max(max(vals), 1e-6) * 1.1
            vstep = nice_step(hi, 5)
            lo, hi, unit = 0.0, math.ceil(hi / vstep) * vstep, "V"

        def xy(f, v):
            return QPointF(plot.left() + f / fmax * plot.width(),
                           plot.bottom() - (min(max(v, lo), hi) - lo) / (hi - lo) * plot.height())

        # grid and axes
        p.save()
        p.setClipRect(plot)
        p.setPen(QPen(QColor(c["grid"]), 1, Qt.PenStyle.DotLine))
        fstep = nice_step(fmax, 8)
        f = 0.0
        while f <= fmax * 1.0001:
            x = xy(f, lo).x()
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            f += fstep
        v = lo
        while v <= hi + 1e-9:
            y = xy(0, v).y()
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            v += vstep
        p.restore()
        p.setPen(QPen(QColor(c["grid_major"]), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)
        p.setFont(self._f(9))
        p.setPen(QColor(c["bar_text"]))
        f = 0.0
        while f <= fmax * 1.0001:
            x = xy(f, lo).x()
            p.drawText(QRectF(x - 40, plot.bottom() + 3, 80, 16), Qt.AlignmentFlag.AlignCenter, fmt_hz(f))
            f += fstep
        v = lo
        while v <= hi + 1e-9:
            y = xy(0, v).y()
            label = f"{v:g}" if self.scale == "dbv" else si(v, "V", 3)
            p.drawText(QRectF(2, y - 8, LEFT - 8, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
            v += vstep
        p.drawText(QRectF(2, 4, LEFT, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, unit)

        # trace
        col = QColor(c["ch1"] if self.source == 1 else c["ch2"])
        path = QPainterPath()
        group = max(1, nbin // int(2 * plot.width()))           # long spectra: keep the highest bin of each group
        first = True                                            # (peak decimation: a narrow line is never lost)
        for k0 in range(0, nbin, group):
            k = max(range(k0, min(nbin, k0 + group)), key=lambda i: vals[i]) if group > 1 else k0
            pt = xy(s.freqs[k], vals[k])
            path.moveTo(pt) if first else path.lineTo(pt)
            first = False
        p.save()
        p.setClipRect(plot)
        pen = QPen(col, 1.4)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        # peak marker
        if pk_amp > 0:
            m = xy(pk_freq, peak)
            p.setPen(QPen(QColor(c["warn"]), 1.5))
            p.drawEllipse(m, 4, 4)
        p.restore()

        # readouts
        p.setFont(self._f(10, QFont.Weight.DemiBold))
        p.setPen(QColor(c["warn"]))
        rms = pk_amp / math.sqrt(2)
        p.drawText(QRectF(plot.right() - 428, TOP + 4, 420, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"Peak {fmt_hz(pk_freq)}   {sp.to_dbv(rms):.1f} dBV   {si(pk_amp, 'V')} pk")
        if 0 < pk_freq < 4 * s.rbw:                # fewer than a few periods in the record: the peak is unreliable
            p.setFont(self._f(9))
            p.setPen(QColor(c["bad"]))
            p.drawText(QRectF(LEFT + 8, TOP + 22, 520, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       "Peak is within 4 resolution steps of 0 Hz - increase the time/div to fit more periods")
        p.setFont(self._f(9))
        p.setPen(QColor(c["grid_major"]))
        info = (f"CH{self.source} {self._used}   {self.window}   fs {fmt_rate(s.fs)}   resolution {fmt_hz(s.rbw)}   "
                f"span 0-{fmt_hz(fmax)}   {s.n} samples, {s.nfft}-pt FFT")
        p.drawText(QRectF(LEFT, H - 17, W - LEFT - RIGHT, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, info)
        if hover is not None and plot.left() <= hover <= plot.right():
            fh = (hover - plot.left()) / plot.width() * fmax
            k = min(nbin - 1, max(0, int(round(fh / s.df))))
            p.setPen(QPen(QColor(c["cur_x"]), 1, Qt.PenStyle.DashLine))
            x = xy(s.freqs[k], lo).x()
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setFont(self._f(10, QFont.Weight.DemiBold))
            p.setPen(QColor(c["cur_x"]))
            right = x < plot.right() - 190
            box = QRectF(x + 6 if right else x - 196, plot.top() + 40, 190, 18)
            p.drawText(box, (Qt.AlignmentFlag.AlignLeft if right else Qt.AlignmentFlag.AlignRight)
                       | Qt.AlignmentFlag.AlignVCenter,
                       f"{fmt_hz(s.freqs[k])}   {sp.to_dbv(s.amps[k] / math.sqrt(2)):.1f} dBV   {si(s.amps[k], 'V')}")
