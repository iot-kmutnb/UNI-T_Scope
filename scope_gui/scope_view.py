"""Custom-painted scope screen: 16 x 8 divisions in an 800x480 logical space,
laid out like the instrument display (status bar, grid, channel boxes)."""
from datetime import datetime

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from . import theme

W, H = 800, 480
PLOT = QRectF(0, 40, 800, 400)     # 16 x 8 divs, 50 px each
DIV = 50.0
CX, CY = PLOT.center().x(), PLOT.center().y()


def si(value, unit, digits=3):
    """1.00V, 500µs, 20mV ... Unit prefix is chosen after rounding, so 999.99µs -> 1ms."""
    if value == 0:
        return f"0{unit}"
    sign = "-" if value < 0 else ""

    def fmt(x, pre):
        x = abs(x)
        num = f"{x:.{max(0, digits - 3)}f}" if x >= 100 else f"{x:.{digits}g}"
        if "." in num:
            num = num.rstrip("0").rstrip(".")
        return f"{sign}{num}{pre}{unit}"

    prev = None
    for div, pre in ((1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        xr = float(f"{abs(value / div):.{digits}g}")
        if xr >= 1 or pre == "n":
            if xr < 1:                                    # below 1 nano-unit: effectively zero
                return f"0{unit}"
            if xr >= 1000 and prev is not None:       # rounded up past this unit: use the larger one
                return fmt(xr / 1000, prev)
            return fmt(xr, pre)
        prev = pre
    return f"{value:.3g}{unit}"


def fmt_points(n):
    """32000 -> '32kpts', 1000000 -> '1Mpts'."""
    if n >= 1e6:
        return f"{n / 1e6:.4g}Mpts"
    return f"{n / 1e3:.4g}kpts" if n >= 1e3 else f"{n:g}pts"


def fmt_rate(r):
    """2e6 -> '2MSa/s', 5e8 -> '500MSa/s', 2e5 -> '200kSa/s'."""
    for div, unit in ((1e9, "GSa/s"), (1e6, "MSa/s"), (1e3, "kSa/s")):
        if float(f"{r / div:.4g}") >= 1:
            return f"{r / div:.4g}{unit}"
    return f"{r:.4g}Sa/s"


def envelope(t, v, columns=800):
    """Reduce a long record to about 2 points per column, keeping each column's minimum and maximum in time
    order: the picture looks the same as the full record, and a narrow pulse is never lost. Records of up to
    2 x columns samples are returned unchanged."""
    n = len(v)
    if n <= 2 * columns:
        return t, v
    g = n // columns
    ts, vs = [], []
    for i in range(0, n, g):
        blk = v[i:i + g]
        lo, hi = min(blk), max(blk)
        for k in sorted({blk.index(lo), blk.index(hi)}):
            ts.append(t[i + k])
            vs.append(blk[k])
    return ts, vs


STATE_NAMES = {"TRIGED": "TRIG"}      # short badge text; others show as reported


class ScopeView(QWidget):
    trigger_level_changed = pyqtSignal(float)      # volts, emitted on drop
    offset_changed = pyqtSignal(int, float)        # channel, volts, emitted on drop
    trigger_pos_changed = pyqtSignal(float)        # seconds (TIM:OFFS), emitted on drop
    cursors_changed = pyqtSignal()                 # cursor enabled / moved
    wheel_zoom = pyqtSignal(int)                   # mouse-wheel notches: +1 = zoom in (shorter time/div)
    plot_inset_changed = pyqtSignal(int)           # left gap between widget edge and the drawn scope area

    def __init__(self, parent=None):
        super().__init__(parent)
        self.frame = None
        self.theme = "dark"
        self._ov = {}            # display overrides: "lvl" / "off1" / "off2" -> value
        self._ov_time = {}       # key -> datetime the override was dropped (None while dragging)
        self._drag = None        # active drag key
        self._drag_y0 = 0.0
        self._drag_v0 = None
        self._drag_x0 = 0.0
        # measurement cursors (display-only, not sent to the scope)
        self.cur_x_on = False
        self.cur_y_on = False
        self.cur_x = [-2e-3, 2e-3]     # seconds
        self.cur_y = [2.0, -2.0]       # volts (signal volts of cur_y_src, like the trigger level)
        self.cur_y_src = 1
        self._cx_init = False
        self._cy_init = False
        self._wheel = 0                # wheel delta not yet turned into a whole notch
        self._state = None             # run state reported after a Run/Stop click, until the next frame
        self._dec = {}                 # channel -> (t, volts) to draw: the record, or its envelope when it is long
        self.setMinimumSize(480, 288)
        self.setMouseTracking(True)
        pol = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        pol.setHeightForWidth(True)       # keep the 800:480 shape so nothing sits in letterbox gaps
        self.setSizePolicy(pol)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        s = min(self.width() / W, self.height() / H)
        self.plot_inset_changed.emit(round((self.width() - W * s) / 2))   # letterbox gap when height-limited

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return round(w * H / W)

    def _f(self, family, size, weight=QFont.Weight.Normal):
        """Font for painted text: at least semi-bold in the light theme for contrast."""
        if self.theme == "light" and weight.value < QFont.Weight.DemiBold.value:
            weight = QFont.Weight.DemiBold
        return QFont(family, size, weight)

    def set_theme(self, name):
        self.theme = name
        self.update()

    def set_state(self, state):
        """Update the state badge (TRIG / STOP ...) right away, without waiting for the next capture."""
        self._state = state
        self.update()

    def set_frame(self, frame):
        self.frame = frame
        self._state = None
        self._dec = {cd.channel: envelope(cd.t, cd.volts) for cd in frame.channels} if frame is not None else {}
        # drop overrides once a frame captured after the drop has arrived
        for k in list(self._ov):
            t = self._ov_time.get(k)
            if t is not None and frame is not None and frame.stamp > t:
                del self._ov[k]
                del self._ov_time[k]
        self._init_cursors()
        self.update()

    # --- cursors ----------------------------------------------------------
    def set_cursors(self, x_on, y_on, y_src):
        if y_src != self.cur_y_src:
            self.cur_y_src = y_src
            self._cy_init = False
        self.cur_x_on, self.cur_y_on = x_on, y_on
        self._init_cursors()
        self.cursors_changed.emit()
        self.update()

    def _src_channel(self):
        f = self.frame
        return next((cd for cd in f.channels if cd.channel == self.cur_y_src), None) if f else None

    def _init_cursors(self):
        """Place the cursors at +-2 divisions the first time they are enabled."""
        f = self.frame
        if f is None:
            return
        if self.cur_x_on and not self._cx_init:
            self.cur_x = [-2 * f.time_scale, 2 * f.time_scale]
            self._cx_init = True
        src = self._src_channel()
        if self.cur_y_on and not self._cy_init and src is not None:
            m = src.meas
            if m is not None and m.vpp > 0:
                self.cur_y = [m.vmax, m.vmin]          # start on the signal: delta = Vpp
            else:                                      # otherwise +-2 divisions about screen centre
                off = self._off(src)
                self.cur_y = [2 * src.scale - off, -2 * src.scale - off]
            self._cy_init = True

    def _cx_px(self, t):
        return self._tx(t)

    def _cy_px(self, v):
        src = self._src_channel()
        return CY - (v + self._off(src)) / (src.scale or 1.0) * DIV

    # --- effective values (scope value unless the user is dragging) -------
    def _off(self, cd):
        return self._ov.get(f"off{cd.channel}", cd.offset)

    def _toff(self, f):
        """Horizontal trigger position (s): the scope's value unless the user is dragging the marker."""
        return self._ov.get("toff", getattr(f, "time_offset", 0.0))

    def _tx(self, t):
        """x of time t (relative to the trigger): the screen centre is t = time offset."""
        return CX + (t - self._toff(self.frame)) / (self.frame.time_scale or 5e-4) * DIV

    def _trig_x(self):
        return min(max(self._tx(0.0), PLOT.left() + 6), PLOT.right() - 6)

    def _lvl(self, f):
        return self._ov.get("lvl", f.trigger.level)

    # --- mouse interaction ------------------------------------------------
    def _to_scope(self, pos):
        s = min(self.width() / W, self.height() / H)
        return ((pos.x() - (self.width() - W * s) / 2) / s, (pos.y() - (self.height() - H * s) / 2) / s)

    def _marker_y(self, cd):
        return min(max(CY - self._off(cd) / (cd.scale or 1.0) * DIV, PLOT.top() + 8), PLOT.bottom() - 8)

    def _trig_y(self, f):
        src = next((cd for cd in f.channels if cd.channel == f.trigger.source), None)
        if src is None:
            return None, None
        y = CY - (self._lvl(f) + self._off(src)) / (src.scale or 1.0) * DIV
        return src, min(max(y, PLOT.top() + 1), PLOT.bottom() - 1)

    def _hit(self, x, y):
        """What is under (x, y) in scope coordinates: ('off', ch) / ('lvl',) / None."""
        f = self.frame
        if f is None or not PLOT.adjusted(0, -10, 0, 10).contains(x, y):
            return None
        if y <= PLOT.top() + 18 and abs(x - self._trig_x()) <= 10:
            return ("tpos",)                                    # trigger position marker (top edge)
        if self.cur_x_on:                                       # measurement cursors first
            for i, t in enumerate(self.cur_x):
                if abs(x - self._cx_px(t)) <= 5:
                    return ("cx", i)
        if self.cur_y_on and self._src_channel() is not None:
            for i, v in enumerate(self.cur_y):
                if abs(y - self._cy_px(v)) <= 5:
                    return ("cy", i)
        for cd in f.channels:                                   # ground markers
            if x <= 26 and abs(y - self._marker_y(cd)) <= 10:
                return ("off", cd.channel)
        _, ty = self._trig_y(f)
        if ty is not None and abs(y - ty) <= (10 if x >= PLOT.right() - 24 else 6):
            return ("lvl",)                                     # level line / T tag
        for cd in f.channels:                                   # trace body
            sc, off = cd.scale or 1.0, self._off(cd)
            ts, vs = self._dec.get(cd.channel, (cd.t, cd.volts))
            ys = [CY - (v + off) / sc * DIV for t, v in zip(ts, vs)
                  if abs(self._tx(t) - x) <= 3]
            if ys and min(ys) - 5 <= y <= max(ys) + 5:
                return ("off", cd.channel)
        return None

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        x, y = self._to_scope(e.position())
        hit = self._hit(x, y)
        if hit and hit[0] == "tpos":
            self._drag = "toff"
            self._ov_time["toff"] = None
            self._drag_x0, self._drag_v0 = x, self._toff(self.frame)
        elif hit and hit[0] in ("cx", "cy"):
            self._drag = f"{hit[0]}{hit[1]}"
            self._drag_x0, self._drag_y0 = x, y
            self._drag_v0 = (self.cur_x if hit[0] == "cx" else self.cur_y)[hit[1]]
        elif hit:
            self._drag = "lvl" if hit[0] == "lvl" else f"off{hit[1]}"
            self._ov_time[self._drag] = None
            self._drag_y0, self._drag_v0 = y, self._drag_value()

    def _drag_target(self):
        """(current value, volts per division) of the dragged item."""
        f = self.frame
        if self._drag == "lvl":
            src, _ = self._trig_y(f)
            return (self._lvl(f), src.scale or 1.0) if src else (None, 1.0)
        cd = next(c for c in f.channels if f"off{c.channel}" == self._drag)
        return self._off(cd), cd.scale or 1.0

    def _drag_value(self):
        return self._drag_target()[0]

    def _drag_to(self, y):
        """Move the item by the cursor travel since the press (no jump on grab)."""
        _, sc = self._drag_target()
        if self._drag_v0 is None:
            return
        y = min(max(y, PLOT.top()), PLOT.bottom())
        self._ov[self._drag] = round(self._drag_v0 + (self._drag_y0 - y) / DIV * sc, 3)
        self.update()

    def _cursor_drag(self, x, y):
        i = int(self._drag[2])
        if self._drag[1] == "x":
            x = min(max(x, PLOT.left()), PLOT.right())
            self.cur_x[i] = self._drag_v0 + (x - self._drag_x0) / DIV * (self.frame.time_scale or 5e-4)
        else:
            src = self._src_channel()
            y = min(max(y, PLOT.top()), PLOT.bottom())
            self.cur_y[i] = self._drag_v0 + (self._drag_y0 - y) / DIV * (src.scale or 1.0)
        self.cursors_changed.emit()
        self.update()

    def mouseMoveEvent(self, e):
        x, y = self._to_scope(e.position())
        if self._drag == "toff":
            x = min(max(x, PLOT.left()), PLOT.right())
            self._ov["toff"] = round(self._drag_v0 - (x - self._drag_x0) / DIV * (self.frame.time_scale or 5e-4), 9)
            self.update()
            return
        if self._drag:
            if self._drag[0] == "c":
                self._cursor_drag(x, y)
            else:
                self._drag_to(y)
            return
        hit = self._hit(x, y)
        shape = (Qt.CursorShape.SizeHorCursor if hit and hit[0] in ("cx", "tpos") else
                 Qt.CursorShape.SizeVerCursor if hit else Qt.CursorShape.ArrowCursor)
        self.setCursor(shape)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or not self._drag:
            return
        key, self._drag = self._drag, None
        if key[0] == "c":                    # cursors are display-only
            return
        if key not in self._ov:              # pressed and released without moving: there is nothing to send
            self._ov_time.pop(key, None)
            return
        self._ov_time[key] = datetime.now()
        if key == "toff":
            self.trigger_pos_changed.emit(self._ov[key])
            return
        if key == "lvl":
            self.trigger_level_changed.emit(self._ov[key])
        else:
            self.offset_changed.emit(int(key[3:]), self._ov[key])

    def wheelEvent(self, e):
        """Wheel up = zoom in, wheel down = zoom out (the window owner changes the time/div)."""
        self._wheel += e.angleDelta().y()
        steps = int(self._wheel / 120)          # touchpads send fractions of a notch
        if steps:
            self._wheel -= steps * 120
            self.wheel_zoom.emit(steps)
        e.accept()

    # --- painting -----------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        s = min(self.width() / W, self.height() / H)
        p.translate((self.width() - W * s) / 2, (self.height() - H * s) / 2)
        p.scale(s, s)
        self.paint_scope(p)

    def render_image(self, scale=2):
        img = QImage(W * scale, H * scale, QImage.Format.Format_RGB32)
        p = QPainter(img)
        p.scale(scale, scale)
        self.paint_scope(p)
        p.end()
        return img

    def paint_scope(self, p):
        c = theme.colors(self.theme)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.fillRect(QRectF(0, 0, W, H), QColor(c["face"]))
        self._grid(p, c)
        f = self.frame
        if f is None:
            p.setPen(QColor(c["grid_major"]))
            p.setFont(self._f("Segoe UI", 16))
            p.drawText(PLOT, Qt.AlignmentFlag.AlignCenter, "No data - connect and capture")
        else:
            self._traces(p, c, f)
            self._cursors(p, c, f)
        self._top_bar(p, c, f)
        self._bottom_bar(p, c, f)

    def _cursors(self, p, c, f):
        p.save()
        p.setClipRect(PLOT)
        p.setFont(self._f("Segoe UI", 9, QFont.Weight.DemiBold))
        if self.cur_x_on:
            col = QColor(c["cur_x"])
            for i, t in enumerate(self.cur_x):
                x = self._cx_px(t)
                pen = QPen(col, 1.2, Qt.PenStyle.DashDotLine)
                p.setPen(pen)
                p.drawLine(QPointF(x, PLOT.top()), QPointF(x, PLOT.bottom()))
                left_side = x > PLOT.right() - 110
                box = QRectF(x - 104 if left_side else x + 4, PLOT.top() + 14 + 18 * i, 100, 16)
                p.setPen(col)
                p.drawText(box, (Qt.AlignmentFlag.AlignRight if left_side else Qt.AlignmentFlag.AlignLeft)
                           | Qt.AlignmentFlag.AlignVCenter, f"X{i + 1} {si(t, 's', 4)}")
        if self.cur_y_on and self._src_channel() is not None:
            col = QColor(c["cur_y"])
            for i, v in enumerate(self.cur_y):
                y = self._cy_px(v)
                p.setPen(QPen(col, 1.2, Qt.PenStyle.DashDotLine))
                p.drawLine(QPointF(PLOT.left(), y), QPointF(PLOT.right(), y))
                above = y - 16 if y - 16 > PLOT.top() else y + 2
                p.setPen(col)
                p.drawText(QRectF(PLOT.left() + 30 + 120 * i, above, 118, 16),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           f"Y{i + 1} {si(v, 'V')}")
        p.restore()

    def _grid(self, p, c):
        p.save()
        p.setClipRect(PLOT)
        dot = QPen(QColor(c["grid"]), 1, Qt.PenStyle.DotLine)
        p.setPen(dot)
        for i in range(1, 16):
            x = PLOT.left() + i * DIV
            p.drawLine(QPointF(x, PLOT.top()), QPointF(x, PLOT.bottom()))
        for j in range(1, 8):
            y = PLOT.top() + j * DIV
            p.drawLine(QPointF(PLOT.left(), y), QPointF(PLOT.right(), y))
        # centre axes with 0.2-div ticks
        p.setPen(QPen(QColor(c["grid_major"]), 1))
        p.drawLine(QPointF(CX, PLOT.top()), QPointF(CX, PLOT.bottom()))
        p.drawLine(QPointF(PLOT.left(), CY), QPointF(PLOT.right(), CY))
        step = DIV / 5
        x = PLOT.left()
        while x <= PLOT.right():
            p.drawLine(QPointF(x, CY - 3), QPointF(x, CY + 3))
            x += step
        y = PLOT.top()
        while y <= PLOT.bottom():
            p.drawLine(QPointF(CX - 3, y), QPointF(CX + 3, y))
            y += step
        p.restore()
        p.setPen(QPen(QColor(c["grid_major"]), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(PLOT)

    def _traces(self, p, c, f):
        p.save()
        p.setClipRect(PLOT)
        for cd in f.channels:
            col = QColor(c["ch1"] if cd.channel == 1 else c["ch2"])
            sc = cd.scale or 1.0
            off = self._off(cd)
            path = QPainterPath()
            ts, vs = self._dec.get(cd.channel, (cd.t, cd.volts))
            for k, (t, v) in enumerate(zip(ts, vs)):
                pt = QPointF(self._tx(t), CY - (v + off) / sc * DIV)
                if k == 0:
                    path.moveTo(pt)
                else:
                    path.lineTo(pt)
            pen = QPen(col, 1.4)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        p.restore()
        self._trigger_level(p, c, f)
        # ground markers on the left edge
        p.setFont(self._f("Segoe UI", 8, QFont.Weight.Bold))
        for cd in f.channels:
            col = QColor(c["ch1"] if cd.channel == 1 else c["ch2"])
            y = self._marker_y(cd)
            tri = QPainterPath()
            tri.moveTo(PLOT.left(), y - 7)
            tri.lineTo(PLOT.left() + 16, y)
            tri.lineTo(PLOT.left(), y + 7)
            tri.closeSubpath()
            p.fillPath(tri, col)
            p.setPen(QColor(c["face"]))
            p.drawText(QRectF(PLOT.left(), y - 7, 10, 14), Qt.AlignmentFlag.AlignCenter, str(cd.channel))
        # trigger position marker (time 0): at the centre unless the trigger position was moved
        mx = self._trig_x()
        tri = QPainterPath()
        tri.moveTo(mx - 6, PLOT.top())
        tri.lineTo(mx + 6, PLOT.top())
        tri.lineTo(mx, PLOT.top() + 9)
        tri.closeSubpath()
        p.fillPath(tri, QColor(c["warn"]))

    def _trigger_level(self, p, c, f):
        src, y = self._trig_y(f)
        if src is None:
            return
        col = QColor(c["ch1"] if src.channel == 1 else c["ch2"])
        pen = QPen(col, 1.2, Qt.PenStyle.DashLine)
        pen.setDashPattern([6, 4])
        p.save()
        p.setPen(pen)
        p.drawLine(QPointF(PLOT.left() + 18, y), QPointF(PLOT.right() - 22, y))
        # "T" tag on the right edge
        tag = QRectF(PLOT.right() - 20, y - 8, 20, 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(tag, 3, 3)
        p.setPen(QColor(c["face"]))
        p.setFont(self._f("Segoe UI", 9, QFont.Weight.Bold))
        p.drawText(tag, Qt.AlignmentFlag.AlignCenter, "T")
        p.restore()

    def _top_bar(self, p, c, f):
        p.fillRect(QRectF(0, 0, W, 40), QColor(c["bar"]))
        p.setFont(self._f("Segoe UI", 10, QFont.Weight.DemiBold))
        trig = ((self._state or f.state) if f else "---") or "---"
        col = {"TRIGED": c["ok"], "STOP": c["bad"]}.get(trig.upper(), c["warn"])
        box = QRectF(8, 8, 72, 24)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(col))
        p.drawRoundedRect(box, 4, 4)
        p.setPen(QColor("#ffffff"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, STATE_NAMES.get(trig.upper(), trig.upper()))
        p.setPen(QColor(c["bar_text"]))
        p.setFont(self._f("Segoe UI", 11))
        tb = si(f.time_scale, "s") + "/div" if f else "---"
        p.drawText(QRectF(96, 0, 190, 40), Qt.AlignmentFlag.AlignVCenter, f"Time  {tb}")
        if f:
            slope = {"rising": "↗", "falling": "↘"}.get(f.trigger.slope, "↗↘")
            p.setPen(QColor(c["bar_text"]))
            p.setFont(self._f("Segoe UI", 10))
            p.drawText(QRectF(300, 0, 330, 40), Qt.AlignmentFlag.AlignVCenter,
                       f"Trig  CH{f.trigger.source}  {slope}  {si(self._lvl(f), 'V')}  {f.trigger.sweep.upper()}")
            p.setPen(QColor(c["grid_major"]))
            p.setFont(self._f("Segoe UI", 11))
            p.drawText(QRectF(W - 190, 0, 182, 40), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       f.stamp.strftime("%Y-%m-%d %H:%M:%S"))

    def _bottom_bar(self, p, c, f):
        p.fillRect(QRectF(0, 440, W, 40), QColor(c["bar"]))
        if not f:
            return
        if f.sample_rate > 0 and f.points > 0:        # memory depth and sample rate, like the instrument's screen
            mem = f"{fmt_points(f.points)}   {fmt_rate(f.sample_rate)}"
            n = max((len(cd.volts) for cd in f.channels), default=0)
            cd0 = f.channels[0] if f.channels else None
            right = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight
            p.setPen(QColor(c["bar_text"]))
            if cd0 is not None and 0 < n < 0.9 * f.points and len(cd0.t) > 1 and cd0.t[-1] > cd0.t[0]:
                # the plotted record is a decimation of the memory (screen mode): say both
                rate = (len(cd0.t) - 1) / (cd0.t[-1] - cd0.t[0])
                p.setFont(self._f("Segoe UI", 9))
                p.drawText(QRectF(W - 200, 441, 192, 18), right, f"Record {fmt_points(n)}   {fmt_rate(rate)}")
                p.setPen(QColor(c["grid_major"]))
                p.drawText(QRectF(W - 200, 459, 192, 18), right, f"Memory {mem}")
            else:
                p.setFont(self._f("Segoe UI", 10))
                p.drawText(QRectF(W - 200, 440, 192, 40), right, mem)
        x = 8
        for cd in f.channels:
            col = QColor(c["ch1"] if cd.channel == 1 else c["ch2"])
            p.setPen(QPen(col, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(x, 446, 250, 28), 4, 4)
            tag = QRectF(x, 446, 26, 28)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(tag, 4, 4)
            p.setPen(QColor(c["face"]))
            p.setFont(self._f("Segoe UI", 11, QFont.Weight.Bold))
            p.drawText(tag, Qt.AlignmentFlag.AlignCenter, str(cd.channel))
            p.setPen(QColor(c["bar_text"]))
            p.setFont(self._f("Segoe UI", 10))
            p.drawText(QRectF(x + 34, 446, 214, 28), Qt.AlignmentFlag.AlignVCenter,
                       f"{cd.coupling}   {si(cd.scale, 'V')}/div   {cd.probe:g}X")
            x += 262
