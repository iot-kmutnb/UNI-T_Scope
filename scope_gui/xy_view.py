"""XY display: CH1 on the horizontal axis, CH2 on the vertical axis, 8 x 8 divisions."""
from datetime import datetime

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from . import theme
from .scope_view import si

SIZE = 480.0          # logical square, 60 px per division
DIV = SIZE / 8
PAD = 10.0


class XyView(QWidget):
    offset_changed = pyqtSignal(int, float)        # channel, GND level in volts, emitted on drop

    def __init__(self, parent=None):
        super().__init__(parent)
        self.frame = None
        self.theme = "dark"
        self._ov = {}              # channel -> GND level while dragging / until a newer frame arrives
        self._ov_time = {}         # channel -> datetime of the drop (None while dragging)
        self._drag = None          # channel being dragged
        self._drag_p0 = 0.0
        self._drag_v0 = 0.0
        self.setMouseTracking(True)
        self.setMinimumSize(320, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_theme(self, name):
        self.theme = name
        self.update()

    def set_frame(self, frame):
        self.frame = frame
        for ch in list(self._ov):          # drop overrides once a frame captured after the drop arrives
            t = self._ov_time.get(ch)
            if t is not None and frame is not None and frame.stamp > t:
                del self._ov[ch]
                del self._ov_time[ch]
        self.update()

    def _off(self, cd):
        """GND level (signal volts): the scope's value unless the user is dragging / has just dropped it."""
        return self._ov.get(cd.channel, cd.offset)

    def _gnd_pos(self):
        """Marker positions in plot coordinates: CH1 GND is a vertical line (x), CH2 GND a horizontal line (y)."""
        x, y = self._channel(1), self._channel(2)
        gx = SIZE / 2 + self._off(x) / (x.scale or 1.0) * DIV if x else None
        gy = SIZE / 2 - self._off(y) / (y.scale or 1.0) * DIV if y else None
        return gx, gy

    # --- mouse: drag the GND markers ----------------------------------------
    def _to_plot(self, pos):
        total = SIZE + 2 * PAD
        s = min(self.width(), self.height()) / total
        return ((pos.x() - (self.width() - total * s) / 2) / s - PAD,
                (pos.y() - (self.height() - total * s) / 2) / s - PAD)

    def _hit(self, x, y):
        """Channel whose GND marker is under (x, y): CH1 on the bottom edge, CH2 on the left edge."""
        gx, gy = self._gnd_pos()
        if gx is not None and y >= SIZE - 18 and abs(x - min(max(gx, 8), SIZE - 8)) <= 10:
            return 1
        if gy is not None and x <= 18 and abs(y - min(max(gy, 8), SIZE - 8)) <= 10:
            return 2
        return None

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        x, y = self._to_plot(e.position())
        ch = self._hit(x, y)
        if ch:
            self._drag = ch
            self._ov_time[ch] = None
            self._drag_p0 = x if ch == 1 else y
            self._drag_v0 = self._off(self._channel(ch))

    def mouseMoveEvent(self, e):
        x, y = self._to_plot(e.position())
        if self._drag:
            cd = self._channel(self._drag)
            sc = cd.scale or 1.0
            if self._drag == 1:
                p = min(max(x, 0.0), SIZE)
                self._ov[1] = round(self._drag_v0 + (p - self._drag_p0) / DIV * sc, 3)
            else:
                p = min(max(y, 0.0), SIZE)
                self._ov[2] = round(self._drag_v0 + (self._drag_p0 - p) / DIV * sc, 3)
            self.update()
            return
        ch = self._hit(x, y)
        self.setCursor(Qt.CursorShape.SizeHorCursor if ch == 1 else
                       Qt.CursorShape.SizeVerCursor if ch == 2 else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or not self._drag:
            return
        ch, self._drag = self._drag, None
        if ch not in self._ov:               # pressed and released without moving: there is nothing to send
            self._ov_time.pop(ch, None)
            return
        self._ov_time[ch] = datetime.now()
        self.offset_changed.emit(ch, self._ov[ch])

    def _channel(self, n):
        return next((cd for cd in self.frame.channels if cd.channel == n), None) if self.frame else None

    def paintEvent(self, _):
        p = QPainter(self)
        total = SIZE + 2 * PAD
        s = min(self.width(), self.height()) / total
        p.translate((self.width() - total * s) / 2, (self.height() - total * s) / 2)
        p.scale(s, s)
        self.paint_xy(p, fill_bg=True)

    def render_image(self, scale=2):
        total = SIZE + 2 * PAD
        img = QImage(int(total * scale), int(total * scale), QImage.Format.Format_RGB32)
        p = QPainter(img)
        p.scale(scale, scale)
        self.paint_xy(p, fill_bg=True)
        p.end()
        return img

    def paint_xy(self, p, fill_bg=False):
        c = theme.colors(self.theme)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        total = SIZE + 2 * PAD
        if fill_bg:
            p.fillRect(QRectF(-1000, -1000, 3000, 3000), QColor(c["face"]))
        p.translate(PAD, PAD)
        box = QRectF(0, 0, SIZE, SIZE)
        c0 = SIZE / 2

        p.save()
        p.setClipRect(box)
        p.setPen(QPen(QColor(c["grid"]), 1, Qt.PenStyle.DotLine))
        for i in range(1, 8):
            p.drawLine(QPointF(i * DIV, 0), QPointF(i * DIV, SIZE))
            p.drawLine(QPointF(0, i * DIV), QPointF(SIZE, i * DIV))
        p.setPen(QPen(QColor(c["grid_major"]), 1))
        p.drawLine(QPointF(c0, 0), QPointF(c0, SIZE))
        p.drawLine(QPointF(0, c0), QPointF(SIZE, c0))
        for k in range(41):
            p.drawLine(QPointF(k * DIV / 5, c0 - 3), QPointF(k * DIV / 5, c0 + 3))
            p.drawLine(QPointF(c0 - 3, k * DIV / 5), QPointF(c0 + 3, k * DIV / 5))
        p.restore()
        p.setPen(QPen(QColor(c["grid_major"]), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(box)

        x, y = self._channel(1), self._channel(2)
        p.setFont(QFont("Segoe UI", 11))
        if x is None or y is None:
            p.setPen(QColor(c["grid_major"]))
            msg = "No data - connect and capture" if self.frame is None else "XY needs CH1 and CH2 enabled"
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, msg)
            return

        if len(x.volts) != len(y.volts) and self.frame.state.upper() != "SCAN":
            # the scope sent a truncated record for one channel: the two cannot be paired point by point
            # (while a slow SCAN sweep fills, both start at the same time, so the common start is plotted instead)
            short, full = (x, y) if len(x.volts) < len(y.volts) else (y, x)
            p.setPen(QColor(c["bad"]))
            p.drawText(box.adjusted(30, 0, -30, 0), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       f"Incomplete record: CH{short.channel} returned {len(short.volts)} of {len(full.volts)} samples. "
                       "The scope truncates a trace that is clipped at the ADC range. "
                       "Use a larger V/div or move the GND level so the whole signal fits.")
            self._gnd_markers(p, c)
            return

        n = min(len(x.volts), len(y.volts))
        xs, ys = x.scale or 1.0, y.scale or 1.0
        ox, oy = self._off(x), self._off(y)
        path = QPainterPath()
        for k in range(0, n, max(1, n // 4000)):          # a 32k-point memory record: about 4000 points are plenty
            pt = QPointF(c0 + (x.volts[k] + ox) / xs * DIV, c0 - (y.volts[k] + oy) / ys * DIV)
            path.moveTo(pt) if k == 0 else path.lineTo(pt)
        p.save()
        p.setClipRect(box)
        pen = QPen(QColor(c["ok"]), 1.4)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.restore()

        p.setPen(QColor(c["ch1"]))
        p.drawText(QRectF(6, 4, 220, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                   f"X: CH1  {si(xs, 'V')}/div")
        p.setPen(QColor(c["ch2"]))
        p.drawText(QRectF(SIZE - 226, 4, 220, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                   f"Y: CH2  {si(ys, 'V')}/div")
        self._gnd_markers(p, c)

    def _gnd_markers(self, p, c):
        """GND lines through the plot and draggable tags: CH1 on the bottom edge, CH2 on the left edge."""
        gx, gy = self._gnd_pos()
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        for ch, g in ((1, gx), (2, gy)):
            if g is None:
                continue
            col = QColor(c["ch1"] if ch == 1 else c["ch2"])
            pen = QPen(col, 1.0, Qt.PenStyle.DashLine)
            pen.setDashPattern([5, 4])
            p.save()
            p.setClipRect(QRectF(0, 0, SIZE, SIZE))
            p.setPen(pen)
            if ch == 1:
                p.drawLine(QPointF(g, 0), QPointF(g, SIZE))
            else:
                p.drawLine(QPointF(0, g), QPointF(SIZE, g))
            p.restore()
            pos = min(max(g, 8), SIZE - 8)
            tri = QPainterPath()
            if ch == 1:      # points up from the bottom edge
                tri.moveTo(pos - 8, SIZE)
                tri.lineTo(pos + 8, SIZE)
                tri.lineTo(pos, SIZE - 15)
                lab = QRectF(pos - 8, SIZE - 12, 16, 12)
            else:            # points right from the left edge
                tri.moveTo(0, pos - 8)
                tri.lineTo(0, pos + 8)
                tri.lineTo(15, pos)
                lab = QRectF(0, pos - 7, 10, 14)
            tri.closeSubpath()
            p.fillPath(tri, col)
            p.setPen(QColor(c["face"]))
            p.drawText(lab, Qt.AlignmentFlag.AlignCenter, str(ch))
