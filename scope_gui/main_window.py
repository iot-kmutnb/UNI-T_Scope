import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QMetaObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                             QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
                             QMessageBox, QPushButton, QSizePolicy, QSpacerItem, QTableWidget,
                             QTabWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from unit_scope import __version__

from . import prefs, theme
from unit_scope import export as export_mod
from unit_scope.errors import ScopeError
from unit_scope.measure import value_at
from .scope_view import ScopeView, si
from .worker import ScopeWorker
from .xy_view import XyView
from .fft_view import FftView, fmt_hz


def _125(lo_exp, hi_exp):
    """1-2-5 sequence from 1e<lo_exp> to 5e<hi_exp>."""
    return [m * 10.0 ** e for e in range(lo_exp, hi_exp + 1) for m in (1, 2, 5)]


MEAS_COLS = ["Vpp", "Vmax", "Vmin", "Vrms", "Freq", "Period"]
VOLT_COLS = (1, 2, 3, 4)          # column 0 is the row label
TIME_COLS = (5, 6)
LABEL_W, COL_W, ROW_H = 96, 112, 28   # same label width in both tables; fixed data columns


def _make_table(headers, rows):
    """Read-only table: label column + fixed-width centred data columns, no stretching."""
    t = QTableWidget(rows, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.verticalHeader().setDefaultSectionSize(ROW_H)
    hh = t.horizontalHeader()
    hh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
    hh.setStretchLastSection(False)
    hh.setHighlightSections(False)
    hh.setFixedHeight(ROW_H)
    hh.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
    t.setColumnWidth(0, LABEL_W)
    for c in range(1, len(headers)):
        t.setColumnWidth(c, COL_W)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    t.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    t.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    _fit_table(t)
    return t


def _fit_table(t):
    """Size to exactly the visible columns/rows (1 px border each side)."""
    t.setFixedWidth(sum(t.columnWidth(c) for c in range(t.columnCount()) if not t.isColumnHidden(c)) + 2)
    t.setFixedHeight(ROW_H * (t.rowCount() + 1) + 2)


def _cell(t, r, c, text, color, left=False):
    it = t.item(r, c)
    if it is None:                          # only insert new items; re-inserting one the table owns warns
        it = QTableWidgetItem()
        t.setItem(r, c, it)
    it.setText(text)
    align = Qt.AlignmentFlag.AlignLeft if left else Qt.AlignmentFlag.AlignHCenter
    it.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
    it.setForeground(QBrush(QColor(color)))


def fmt_freq(f):
    for div, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz"), (1, "Hz")):
        if float(f"{f / div:.4g}") >= 1:      # unit chosen after rounding (999.98 Hz -> 1 kHz)
            return f"{f / div:.4g} {unit}"
    return f"{f * 1e3:.4g} mHz"


def fmt_idn(idn):
    """'UNI-TREND TECHNOLOGY,DSO,ADO2726190811,1.10.0060' -> 'DSO   S/N ADO2726190811'."""
    parts = [p.strip() for p in idn.split(",")]
    return f"{parts[1]}   S/N {parts[2]}" if len(parts) >= 3 and parts[1] and parts[2] else idn


TIME_DIVS = _125(-9, 1)       # 1 ns .. 50 s
VOLT_DIVS = _125(-3, 2)       # 1 mV .. 500 V  (scope clamps to its real range; a 10X probe multiplies V/div)
AUTO_INTERVALS = [1, 2, 5, 10]
FFT_SPANS = [50, 100, 200, 500, 1e3, 2e3, 5e3, 1e4, 2e4, 5e4, 1e5, 2e5, 5e5]   # Hz
FFT_MAX_TDIV = 0.05          # s/div: longest time base chosen for a span (a 0.8 s record, 1.25 Hz resolution)
SLOPES = [("Rising", "rising"), ("Falling", "falling"), ("Both", "both")]
SWEEPS = [("Auto", "auto"), ("Normal", "normal"), ("Single", "single")]
COUPLINGS = [("DC", "DC"), ("AC", "AC"), ("GND", "GND")]
PROBES = [("1X", 1.0), ("10X", 10.0)]       # others the scope may report are added when detected


def _fill(combo, items):
    for label, data in items:
        combo.addItem(label, data)


class MainWindow(QMainWindow):
    request_connect = pyqtSignal()
    request_disconnect = pyqtSignal()
    request_capture = pyqtSignal()
    request_run = pyqtSignal(bool)
    request_apply = pyqtSignal(list)
    request_display_type = pyqtSignal(str)
    request_memory_channel = pyqtSignal(int)
    request_record_mode = pyqtSignal(str)

    def __init__(self, app, settings=None):
        super().__init__()
        self.app = app
        self.settings = settings if settings is not None else theme.settings()
        self.theme_name = theme.saved_name()
        self.setWindowTitle(f"UNI-T Scope Panel {__version__}")
        self.resize(1180, 860)
        self._busy = False
        self._connected = False
        self._last_frame = None
        self._loading = True           # saved settings are being applied: do not write them back half-loaded
        self._had_connection = False   # a connection was up (so a disconnect without a click is a lost connection)
        self._user_disconnect = False  # the last disconnect was asked for by the user
        self._reconnecting = False     # retrying after a lost connection
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.setInterval(3000)
        self._reconnect_timer.timeout.connect(self._try_reconnect)
        self._mem_ch = 0              # channel whose deep memory the worker reads for the FFT (0 = none)
        self._xy_mode = False         # whether the scope's own display is believed to be in XY mode
        self._pending = {}            # changes made while the scope was busy, merged: key -> change
        self._zoom_target = None      # time/div the wheel asked for, until the scope has confirmed it
        self._fill_len = -1           # samples in the last partial record (slow sweep still filling), -1 = none
        self._fill_timer = QTimer(self)             # re-capture while a slow sweep fills up
        self._fill_timer.setSingleShot(True)
        self._fill_timer.setInterval(1000)
        self._fill_timer.timeout.connect(self._refresh_partial)
        self._zoom_deadline = 0.0
        self._zoom_timer = QTimer(self)             # wheel debounce: send only the value the spinning ends on
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.setInterval(120)
        self._zoom_timer.timeout.connect(lambda: self._send([("time_scale", self.tdiv.currentData())]))

        # worker thread ---------------------------------------------------
        self.thread = QThread(self)
        self.worker = ScopeWorker()
        self.worker.moveToThread(self.thread)
        self.request_connect.connect(self.worker.connect_scope)
        self.request_disconnect.connect(self.worker.disconnect_scope)
        self.request_capture.connect(self.worker.capture)
        self.request_run.connect(self.worker.set_running)
        self.request_apply.connect(self.worker.apply)
        self.request_display_type.connect(self.worker.set_display_type)
        self.request_memory_channel.connect(self.worker.set_memory_channel)
        self.request_record_mode.connect(self.worker.set_record_mode)
        self.worker.display_type.connect(self._on_display_type)
        self.worker.display_synced.connect(self._on_display_synced)
        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.frame.connect(self._on_frame)
        self.worker.error.connect(self._on_error)
        self.worker.run_state.connect(self._on_run_state)
        self.thread.start()

        # UI --------------------------------------------------------------
        self.view = ScopeView()
        self.view.set_theme(self.theme_name)
        self.xy_view = XyView()
        self.xy_view.set_theme(self.theme_name)
        self.fft_view = FftView()
        self.fft_view.set_theme(self.theme_name)
        self.view.trigger_level_changed.connect(lambda v: self._send([("trigger", {"level": v})]))
        self.view.offset_changed.connect(lambda ch, v: self._send([("channel", ch, {"offset": v})]))
        self.view.wheel_zoom.connect(self._wheel_zoom)
        self.view.trigger_pos_changed.connect(lambda v: self._send([("time_offset", v)]))
        self.xy_view.offset_changed.connect(lambda ch, v: self._send([("channel", ch, {"offset": v})]))

        # measurements ------------------------------------------------------
        self.meas = _make_table(["", *MEAS_COLS], 2)
        for r, label in enumerate(("CH1", "CH2")):
            _cell(self.meas, r, 0, label, "#000000", left=True)
            for c in range(len(MEAS_COLS)):
                _cell(self.meas, r, c + 1, "---", "#000000")
        self._color_meas()

        # measurement / cursor toggles ---------------------------------------
        def chip(text, tip, checked=False):
            b = QPushButton(text)
            b.setObjectName("chip")
            b.setCheckable(True)
            b.setChecked(checked)
            b.setToolTip(tip)
            return b
        self.vm_btn = chip("Voltages", "Show/hide Vpp, Vmax, Vmin, Vrms", True)
        self.tm_btn = chip("Timing", "Show/hide Freq and Period", True)
        self.cx_btn = chip("Time cursors", "Vertical cursor pair (X1, X2): time difference and values at the cursors")
        self.cy_btn = chip("Voltage cursors", "Horizontal cursor pair (Y1, Y2): voltage difference")
        self.cy_src = QComboBox()
        _fill(self.cy_src, [("CH1", 1), ("CH2", 2)])
        self.cy_src.setEnabled(False)
        self.vm_btn.toggled.connect(self._apply_meas_visibility)
        self.tm_btn.toggled.connect(self._apply_meas_visibility)
        self.cx_btn.toggled.connect(self._cursors_toggled)
        self.cy_btn.toggled.connect(self._cursors_toggled)
        self.cy_src.activated.connect(lambda _: self._cursors_toggled())
        self.view.cursors_changed.connect(self._update_cursor_table)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.addWidget(QLabel("Measure:"))
        bar.addWidget(self.vm_btn)
        bar.addWidget(self.tm_btn)
        bar.addSpacing(14)
        bar.addWidget(QLabel("Cursors:"))
        bar.addWidget(self.cx_btn)
        bar.addWidget(self.cy_btn)
        bar.addWidget(QLabel("on"))
        bar.addWidget(self.cy_src)
        bar.addStretch(1)

        self.cur_table = _make_table(["", "1", "2", "Δ", "1/Δ"], 0)
        self.cur_table.hide()

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("connectBtn")
        self.connect_btn.setCheckable(True)
        self.connect_btn.setMinimumWidth(88)         # fits "Disconnect" so the row does not shift
        self.connect_btn.clicked.connect(self._toggle_connect)
        self.idn_label = QLabel("Not connected")
        self.idn_label.setWordWrap(True)
        self.idn_label.setObjectName("muted")
        g_conn = QGroupBox("Connection")
        lay = QVBoxLayout(g_conn)
        lay.setSpacing(6)
        conn_row = QHBoxLayout()
        conn_row.setSpacing(8)
        conn_row.addWidget(self.connect_btn)
        conn_row.addWidget(self.idn_label, 1)
        lay.addLayout(conn_row)
        self.reconnect_chk = QCheckBox("Auto-reconnect")
        self.reconnect_chk.setToolTip("If the connection to the scope is lost (cable, power, a communication error), "
                                      "try to connect again every 3 s until it works or you click Disconnect")
        self.reconnect_chk.toggled.connect(self._reconnect_toggled)
        lay.addWidget(self.reconnect_chk)

        self.run_btn = QPushButton()
        self.run_btn.setObjectName("runBtn")         # green "Run" / red-orange "Stop" (see theme.py)
        self._set_run_label("Run")
        self.run_btn.setToolTip("Shows the scope's mode (green Run / red-orange Stop); click to toggle")
        self.run_btn.clicked.connect(self._toggle_run)
        self.capture_btn = QPushButton("Capture")
        self.capture_btn.clicked.connect(self._capture_once)
        self.auto_chk = QCheckBox("Auto capture")
        self.auto_chk.toggled.connect(self._auto_toggled)
        self.interval = QComboBox()
        _fill(self.interval, [(f"{v} s", v) for v in AUTO_INTERVALS])   # default: first = 1 s
        self.interval.activated.connect(self._interval_changed)
        g_acq = QGroupBox("Acquisition")
        form = QFormLayout(g_acq)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.capture_btn)
        form.addRow(btn_row)
        auto_row = QHBoxLayout()
        auto_row.setSpacing(6)
        auto_row.addWidget(self.auto_chk)
        auto_row.addStretch(1)
        auto_row.addWidget(QLabel("Interval"))
        auto_row.addWidget(self.interval)
        form.addRow(auto_row)
        self.record = QComboBox()
        _fill(self.record, [("Screen (1600 pts)", "screen"), ("Memory (full depth)", "mem")])
        self.record.setToolTip("Record plotted in Y-T / XY and used for the measurements: the 1600-point screen record "
                               "(fast), or the scope's raw acquisition memory (64 kpts with one channel on, 32 kpts each with both; every sample at the full rate, "
                               "narrow pulses stay visible; about 1 s slower per capture)")
        self.record.activated.connect(lambda _: self._record_changed())
        rec_row = QHBoxLayout()
        rec_row.setSpacing(6)
        rec_row.addWidget(QLabel("Record"))
        rec_row.addWidget(self.record, 1)
        form.addRow(rec_row)

        # trigger -----------------------------------------------------------
        self.trig_src = QComboBox()
        _fill(self.trig_src, [("CH1", 1), ("CH2", 2)])
        self.trig_src.activated.connect(lambda _: self._send([("trigger", {"source": self.trig_src.currentData()})]))
        self.trig_slope = QComboBox()
        _fill(self.trig_slope, SLOPES)
        self.trig_slope.activated.connect(lambda _: self._send([("trigger", {"slope": self.trig_slope.currentData()})]))
        self.trig_sweep = QComboBox()
        _fill(self.trig_sweep, SWEEPS)
        self.trig_sweep.activated.connect(lambda _: self._send([("trigger", {"sweep": self.trig_sweep.currentData()})]))
        self.trig_level = QDoubleSpinBox()
        self.trig_level.setRange(-50.0, 50.0)
        self.trig_level.setDecimals(2)
        self.trig_level.setSingleStep(0.04)
        self.trig_level.setSuffix(" V")
        self.trig_level.setKeyboardTracking(False)
        self.trig_level.valueChanged.connect(lambda v: self._send([("trigger", {"level": v})]))
        g_trig = QGroupBox("Trigger")
        f2 = QGridLayout(g_trig)           # two pairs per row, each label above its control
        f2.setHorizontalSpacing(10)
        f2.setVerticalSpacing(3)
        f2.addWidget(QLabel("Source"), 0, 0)
        f2.addWidget(QLabel("Edge"), 0, 1)
        f2.addWidget(self.trig_src, 1, 0)
        f2.addWidget(self.trig_slope, 1, 1)
        f2.addWidget(QLabel("Level"), 2, 0)
        f2.addWidget(QLabel("Mode"), 2, 1)
        f2.addWidget(self.trig_level, 3, 0)
        f2.addWidget(self.trig_sweep, 3, 1)
        f2.setColumnStretch(0, 1)
        f2.setColumnStretch(1, 1)

        # horizontal / vertical -------------------------------------------
        self.tdiv = QComboBox()
        _fill(self.tdiv, [(si(v, "s") + "/div", v) for v in TIME_DIVS])
        self.tdiv.activated.connect(lambda _: self._send([("time_scale", self.tdiv.currentData())]))
        self.vdiv = {}
        self.coup = {}
        self.probe = {}
        self.ch_on = {}                # channel -> tick box that enables/disables it on the scope
        self._ch_target = {}           # channel -> (wanted state, deadline): a click the scope has not confirmed yet
        g_scale = QGroupBox("Scale / coupling")
        f3 = QGridLayout(g_scale)               # one grid, so the Time box is as wide as the V/div boxes
        f3.setHorizontalSpacing(6)
        f3.addWidget(QLabel("Time"), 0, 0)
        f3.addWidget(self.tdiv, 0, 1)
        for row, ch in enumerate((1, 2), start=1):
            cb = QComboBox()
            _fill(cb, [(si(v, "V") + "/div", v) for v in VOLT_DIVS])
            cb.activated.connect(lambda _, ch=ch, cb=cb: self._send([("channel", ch, {"scale": cb.currentData()})]))
            self.vdiv[ch] = cb
            cc = QComboBox()
            _fill(cc, COUPLINGS)
            cc.setToolTip(f"CH{ch} input coupling: DC, AC (blocks DC) or GND (input grounded)")
            cc.activated.connect(lambda _, ch=ch, cc=cc: self._send([("channel", ch, {"coupling": cc.currentData()})]))
            self.coup[ch] = cc
            pb = QComboBox()
            _fill(pb, PROBES)
            pb.setToolTip(f"CH{ch} probe attenuation: read from the scope, change it here to match the probe's 1X/10X switch")
            pb.activated.connect(lambda _, ch=ch, pb=pb: self._send([("channel", ch, {"probe": pb.currentData()})]))
            self.probe[ch] = pb
            on = QCheckBox(f"CH{ch}")
            on.setChecked(True)
            on.setToolTip(f"Show CH{ch}. Turning a channel off gives the other one the whole acquisition memory "
                          "(64 kpts instead of 32 kpts each). At least one channel stays on")
            on.clicked.connect(lambda checked, ch=ch: self._channel_tick_clicked(ch, checked))
            self.ch_on[ch] = on
            f3.addWidget(on, row, 0)
            f3.addWidget(cb, row, 1)
            f3.addWidget(cc, row, 2)
            f3.addWidget(pb, row, 3)
        f3.setColumnStretch(1, 1)
        self.ctrl_widgets = [*self.ch_on.values(), self.record, self.trig_src, self.trig_slope, self.trig_sweep, self.trig_level, self.tdiv,
                             *self.vdiv.values(), *self.coup.values(), *self.probe.values()]

        self.save_btn = QPushButton("Save")
        self.save_btn.setToolTip("Save the display as a PNG image")
        self.save_btn.clicked.connect(self._save_png)
        self.export_btn = QPushButton("Export")
        self.export_btn.setToolTip("Export the captured waveforms as VCD (GTKWave / Surfer) or CSV")
        self.export_btn.clicked.connect(self._export_wave)
        self.vcd_digital = QCheckBox("VCD: add digital traces")
        self.vcd_digital.setChecked(False)
        self.vcd_digital.setToolTip("Also write each channel thresholded to a 0/1 wire (CHn_dig)")
        g_exp = QGroupBox("Export")
        lay_exp = QVBoxLayout(g_exp)
        exp_row = QHBoxLayout()
        exp_row.setSpacing(6)
        exp_row.addWidget(self.save_btn)
        exp_row.addWidget(self.export_btn)
        lay_exp.addLayout(exp_row)
        lay_exp.addWidget(self.vcd_digital)

        self.theme_btn = QPushButton()
        self.theme_btn.clicked.connect(self._toggle_theme)

        side = QVBoxLayout()
        side.addWidget(g_conn)
        side.addWidget(g_acq)
        side.addWidget(g_trig)
        side.addWidget(g_scale)
        side.addWidget(g_exp)
        side.addStretch(1)
        side.addWidget(self.theme_btn, 0, Qt.AlignmentFlag.AlignHCenter)     # natural width, centred
        side_w = QWidget()
        side_w.setLayout(side)
        side_w.setFixedWidth(305)

        root = QHBoxLayout()
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(self.view)
        # Everything under the display lines up with the drawn waveform area: when the window is wide and
        # short the display is letterboxed, so the left margin follows the display's actual inset.
        under = QWidget()
        under_h = QHBoxLayout(under)
        under_h.setContentsMargins(0, 0, 0, 0)
        under_h.setSpacing(0)
        # Maximum policy: may shrink to nothing, so the inset never raises the window's minimum width
        self._inset_spacer = QSpacerItem(0, 0, QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)
        under_h.addSpacerItem(self._inset_spacer)
        self.under_layout = QVBoxLayout()
        self.under_layout.setContentsMargins(0, 0, 0, 0)
        self.under_layout.setSpacing(8)
        under_h.addLayout(self.under_layout, 1)
        self._under_h = under_h
        self.under_layout.addLayout(bar)
        self.under_layout.addWidget(self.meas, 0, Qt.AlignmentFlag.AlignLeft)
        self.under_layout.addWidget(self.cur_table, 0, Qt.AlignmentFlag.AlignLeft)
        left.addWidget(under)
        left.addStretch(1)
        self._inset = 0
        self.view.plot_inset_changed.connect(self._on_plot_inset)
        yt_page = QWidget()
        yt_page.setLayout(left)
        self.tabs = QTabWidget()
        self.tabs.addTab(yt_page, "Y-T")
        # XY tab: GND levels can be typed here or dragged on the plot (X axis = CH1, Y axis = CH2)
        xy_page = QWidget()
        xy_lay = QVBoxLayout(xy_page)
        xy_lay.setContentsMargins(0, 6, 0, 0)
        gnd_bar = QHBoxLayout()
        gnd_bar.setSpacing(6)
        self.gnd_spin = {}
        for ch, axis in ((1, "X"), (2, "Y")):
            sp = QDoubleSpinBox()
            sp.setRange(-50.0, 50.0)
            sp.setDecimals(3)
            sp.setSingleStep(0.1)
            sp.setSuffix(" V")
            sp.setKeyboardTracking(False)
            sp.setToolTip(f"CH{ch} ground level ({axis} axis): enter a value or drag the CH{ch} marker on the plot")
            sp.valueChanged.connect(lambda v, ch=ch: self._send([("channel", ch, {"offset": v})]))
            self.gnd_spin[ch] = sp
            gnd_bar.addWidget(QLabel(f"CH{ch} GND ({axis}):"))
            gnd_bar.addWidget(sp)
            gnd_bar.addSpacing(10)
        gnd_bar.addStretch(1)
        xy_lay.addLayout(gnd_bar)
        xy_lay.addWidget(self.xy_view, 1)
        self.ctrl_widgets += list(self.gnd_spin.values())
        self.tabs.addTab(xy_page, "XY")

        # FFT tab: spectrum of CH1 or CH2 from the last capture; time/div (sidebar) sets span and resolution
        fft_page = QWidget()
        fft_lay = QVBoxLayout(fft_page)
        fft_lay.setContentsMargins(0, 6, 0, 0)
        fft_bar = QHBoxLayout()
        fft_bar.setSpacing(6)
        self.fft_src = QComboBox()
        _fill(self.fft_src, [("CH1", 1), ("CH2", 2)])
        self.fft_win = QComboBox()
        _fill(self.fft_win, [("Hann", "hann"), ("Hamming", "hamming"), ("Blackman", "blackman"),
                             ("Flat top", "flattop"), ("Rectangular", "rect")])
        self.fft_win.setToolTip("Window: Hann is a good default; Flat top reads amplitudes most accurately; "
                                "Rectangular leaks unless the record holds whole periods")
        self.fft_scale = QComboBox()
        _fill(self.fft_scale, [("dBV (rms)", "dbv"), ("Volts (peak)", "v")])
        self.fft_data = QComboBox()
        _fill(self.fft_data, [("Memory (full depth)", "mem"), ("Screen (1600 pts)", "screen")])
        self.fft_data.setToolTip("Memory: the scope's raw acquisition memory (up to 64 kpts, 32 kpts per channel with both on; full sample rate, no decimation) - "
                                 "a much wider span and accurate amplitudes. Screen: the 1600-point record that is drawn on the scope")
        self.fft_span = QComboBox()
        _fill(self.fft_span, [("Auto", "auto"), ("Full", "full")] + [(fmt_hz(f), f) for f in FFT_SPANS])
        self.fft_span.setToolTip("Highest frequency shown. Auto: 10 x the strongest line. Full: up to the Nyquist frequency "
                                 "(half the sample rate - a fast time base makes this very wide). A frequency also sets the "
                                 "time/div (side panel): the longest time base whose Nyquist frequency covers it, so the "
                                 "resolution 1 / (16 x time/div) is as fine as possible. Auto and Full leave the time/div alone")
        for label, box in (("Source", self.fft_src), ("Window", self.fft_win), ("Scale", self.fft_scale),
                           ("Data", self.fft_data), ("Span", self.fft_span)):
            fft_bar.addWidget(QLabel(label))
            fft_bar.addWidget(box)
            fft_bar.addSpacing(8)
        fft_bar.addStretch(1)
        self.fft_src.activated.connect(lambda _: self._fft_options(source=self.fft_src.currentData()))
        self.fft_win.activated.connect(lambda _: self._fft_options(window=self.fft_win.currentData()))
        self.fft_scale.activated.connect(lambda _: self._fft_options(scale=self.fft_scale.currentData()))
        self.fft_data.activated.connect(lambda _: self._fft_options(data=self.fft_data.currentData(), retune=True))
        self.fft_span.activated.connect(lambda _: self._fft_options(span_hz=self.fft_span.currentData(), retune=True))
        fft_lay.addLayout(fft_bar)
        fft_lay.addWidget(self.fft_view, 1)
        self.tabs.addTab(fft_page, "FFT")
        self.tabs.setToolTip("Selecting a tab also switches the scope's own display (Y-T / XY) when connected")
        self.tabs.currentChanged.connect(self._tab_changed)
        root.addWidget(self.tabs, 1)
        root.addWidget(side_w)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        self.status_led = QLabel("●")
        self.status_msg = QLabel("Ready")
        self.statusBar().addWidget(self.status_led)
        self.statusBar().addWidget(self.status_msg, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._status = ("Ready", None)
        self._update_theme_ui()
        self._color_ticks()
        self._update_enabled()
        prefs.load(self, self.settings)            # saved choices of the last session
        self._loading = False
        self.interval.activated.connect(lambda _: self._save_prefs())
        for chip in (self.vm_btn, self.tm_btn, self.vcd_digital):
            chip.toggled.connect(lambda _: self._save_prefs())

    # --- helpers ---------------------------------------------------------
    def _on_plot_inset(self, x):
        """Line the measure area up with the drawn waveform (the display is letterboxed when the window
        is wide and short, so its left edge is inset from the widget edge)."""
        if x != self._inset:
            self._inset = x
            self._inset_spacer.changeSize(x, 0, QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)
            self._under_h.invalidate()

    def _set_status(self, msg, ok=None):
        self._status = (msg, ok)
        c = theme.colors(self.theme_name)
        col = c["muted"] if ok is None else (c["ok"] if ok else c["bad"])
        self.status_led.setStyleSheet(f"color: {col};")
        self.status_msg.setText(msg)

    def _set_run_label(self, text):
        """The button shows the scope's mode: green Run while running, red-orange Stop while stopped."""
        self.run_btn.setText(text)
        self.run_btn.setProperty("action", text.lower())
        self.run_btn.style().unpolish(self.run_btn)      # re-evaluate the [action=...] style rule
        self.run_btn.style().polish(self.run_btn)

    def _update_enabled(self):
        on = self._connected
        for w in (self.capture_btn, self.auto_chk, self.run_btn, *self.ctrl_widgets):
            w.setEnabled(on)
        self.save_btn.setEnabled(self._last_frame is not None)
        self.export_btn.setEnabled(self._last_frame is not None)
        self._apply_channel_states()

    def _apply_channel_states(self):
        """The V/div, coupling, probe and GND controls of a channel that is off are greyed out."""
        for ch, tick in self.ch_on.items():
            live = self._connected and tick.isChecked()
            for w in (self.vdiv[ch], self.coup[ch], self.probe[ch], self.gnd_spin[ch]):
                w.setEnabled(live)

    def _channel_tick_clicked(self, ch, checked):
        """Tick box clicked: enable/disable the channel on the scope. The last channel cannot be turned off, and
        turning off the trigger channel moves the trigger to the other one (a trigger on a channel that is off
        leaves the display free-running)."""
        others = [c for c, t in self.ch_on.items() if c != ch and t.isChecked()]
        if not checked and not others:
            self.ch_on[ch].setChecked(True)
            self._set_status("At least one channel must stay on", False)
            return
        changes = [("channel", ch, {"enabled": checked})]
        if not checked and self.trig_src.currentData() == ch:
            changes.append(("trigger", {"source": others[0]}))
            self.trig_src.setCurrentIndex(self.trig_src.findData(others[0]))
        self._ch_target[ch] = (checked, time.monotonic() + 3.0)
        self._apply_channel_states()
        self._send(changes)

    def _color_ticks(self):
        c = theme.colors(self.theme_name)
        for ch, tick in self.ch_on.items():
            tick.setStyleSheet(f"QCheckBox {{ color: {c['ch1' if ch == 1 else 'ch2']}; font-weight: 600; }}"
                               f"QCheckBox:disabled {{ color: {c['muted']}; }}")

    def _update_theme_ui(self):
        self.theme_btn.setText("Switch to light theme" if self.theme_name == "dark" else "Switch to dark theme")

    # --- connection ------------------------------------------------------
    def _toggle_connect(self, checked):
        self._reconnect_timer.stop()
        self._reconnecting = False
        self._user_disconnect = not checked        # a click, not a lost connection
        self.connect_btn.setEnabled(False)
        self._set_status("Connecting..." if checked else "Disconnecting...")
        if checked:
            self.request_connect.emit()
        else:
            self.auto_chk.setChecked(False)
            self.request_disconnect.emit()

    def _reconnect_toggled(self, on):
        if not on:
            self._reconnect_timer.stop()
            self._reconnecting = False
        self._save_prefs()

    def _try_reconnect(self):
        if self._connected or not self._reconnecting:
            return
        self.connect_btn.setEnabled(False)
        self._set_status("Reconnecting...")
        self.request_connect.emit()

    def _on_connected(self, idn):
        again = self._reconnecting
        self._had_connection = True
        self._reconnecting = False
        self._reconnect_timer.stop()
        self._connected = True
        self.connect_btn.setEnabled(True)
        self.connect_btn.setChecked(True)
        self.connect_btn.setText("Disconnect")
        self.idn_label.setText(fmt_idn(idn))
        self.idn_label.setToolTip(idn)             # the full *IDN? reply
        self._set_status("Reconnected" if again else "Connected", True)
        self._update_enabled()
        self._fft_sync_memory()
        self._capture_once()

    def _on_disconnected(self):
        self._connected = False
        self._busy = False
        self.timer.stop()
        self._pending.clear()
        self._mem_ch = 0
        self.request_memory_channel.emit(0)
        self._zoom_timer.stop()
        self._fill_timer.stop()
        self._fill_len = -1
        self._zoom_target = None
        self.auto_chk.setChecked(False)
        self.connect_btn.setEnabled(True)
        self.connect_btn.setChecked(False)
        self.connect_btn.setText("Connect")
        self.idn_label.setText("Not connected")
        self.idn_label.setToolTip("")
        if not self.status_msg.text().startswith(("Connect failed", "Capture failed", "Run/Stop failed", "Setting failed",
                                                      "Unexpected error", "Display type failed")):
            self._set_status("Disconnected")
        lost = self._had_connection and not self._user_disconnect
        self._had_connection = False
        if self.reconnect_chk.isChecked() and not self._user_disconnect and (lost or self._reconnecting):
            self._reconnecting = True              # keep trying until it works or the user disconnects
            self._reconnect_timer.start()
            self._set_status("Connection lost - reconnecting in 3 s..." if lost
                             else "Could not reconnect - trying again in 3 s...", False)
        else:
            self._reconnecting = False
        self._update_enabled()

    def _on_error(self, msg):
        self._set_status(msg, False)

    # --- capture ---------------------------------------------------------
    def _capture_once(self):
        if self._connected and not self._busy:
            self._busy = True
            self.request_capture.emit()

    def _refresh_partial(self):
        if not self._connected:
            return
        if self._busy:                               # try again shortly rather than queueing
            self._fill_timer.start()
        else:
            self._capture_once()

    def _tick(self):
        self._capture_once()

    def _on_frame(self, frame):
        self._busy = False
        if frame is None:
            return
        self._last_frame = frame
        self.view.set_frame(frame)
        self.xy_view.set_frame(frame)
        self.fft_view.set_frame(frame)
        self._sync_controls(frame)
        self._show_meas(frame)
        self._update_cursor_table()
        self._set_run_label("Stop" if frame.state.upper() == "STOP" else "Run")
        lens = [len(cd.volts) for cd in frame.channels]
        stopped = frame.state.upper() == "STOP"
        incomplete = any(cd.missing for cd in frame.channels)
        total = max((len(cd.volts) + cd.missing for cd in frame.channels), default=0)
        have = sum(lens)
        # A running scope that has not finished the sweep (a slow time base, or one just changed) returns part of the
        # record, and the channels are read one after another so their lengths differ a little. Look again while the
        # record keeps growing; a record that stays short is a clipped trace, not a sweep in progress.
        if incomplete and not stopped and have > self._fill_len:
            self._fill_len = have
            if not self.timer.isActive():            # auto capture, if on, looks again by itself
                self._fill_timer.start()
            self._set_status(f"Acquiring the sweep: {min(lens)} of {total} samples ({100 * min(lens) // max(1, total)} %)", None)
        else:
            self._fill_len = -1
            full = max(lens, default=0)
            short = [f"CH{cd.channel} {len(cd.volts)}/{full}" for cd in frame.channels if len(cd.volts) < full]
            if incomplete and stopped and len(set(lens)) == 1 and lens[0] > 0:
                self._set_status(f"Stopped: only the stored part of the record is shown ({lens[0]} of {total} samples)", None)
            elif short:       # the scope sends a truncated record for a trace that is clipped at the ADC range
                self._set_status("Incomplete record from scope (" + ", ".join(short) + " samples): trace clipped? "
                                 "use a larger V/div or move GND", False)
            else:
                self._set_status(f"Captured {frame.stamp:%H:%M:%S}  -  {len(frame.channels)} channel(s)", True)
        self._update_enabled()
        self._flush_pending()

    def _auto_toggled(self, on):
        if on and self._connected:
            self.timer.start(self.interval.currentData() * 1000)
            self._capture_once()
        else:
            self.timer.stop()

    def _interval_changed(self, _):
        if self.timer.isActive():
            self.timer.start(self.interval.currentData() * 1000)

    def _send(self, changes):
        """Hand setting changes to the worker (see ScopeWorker.apply); it refreshes the display afterwards.
        While the scope is busy with an earlier request the changes are merged into one pending set, newest
        value per setting, and sent when the frame arrives - so a burst of events cannot build a queue."""
        if not self._connected:
            return
        if self._busy:
            for change in changes:
                self._merge_pending(change)
            return
        self._busy = True
        self._fill_len = -1                          # new settings: a new record starts
        self.request_apply.emit(changes)

    def _merge_pending(self, change):
        kind = change[0]
        if kind == "channel":                       # ("channel", ch, {settings}): settings of one channel merge
            key = ("channel", change[1])
            old = self._pending.get(key)
            change = ("channel", change[1], {**(old[2] if old else {}), **change[2]})
        elif kind == "trigger":
            old = self._pending.get(("trigger",))
            key, change = ("trigger",), ("trigger", {**(old[1] if old else {}), **change[1]})
        else:
            key = (kind,)                           # time_scale, time_offset: the newest value wins
        self._pending[key] = change

    def _flush_pending(self):
        if self._pending and self._connected and not self._busy:
            changes = list(self._pending.values())
            self._pending.clear()
            self._send(changes)

    def _color_meas(self):
        c = theme.colors(self.theme_name)
        for r, key in enumerate(("ch1", "ch2")):
            for col in range(self.meas.columnCount()):
                self.meas.item(r, col).setForeground(QBrush(QColor(c[key])))

    def _apply_meas_visibility(self):
        v, t = self.vm_btn.isChecked(), self.tm_btn.isChecked()
        for c in VOLT_COLS:
            self.meas.setColumnHidden(c, not v)
        for c in TIME_COLS:
            self.meas.setColumnHidden(c, not t)
        _fit_table(self.meas)
        self.meas.setVisible(v or t)

    def _cursors_toggled(self):
        self.cy_src.setEnabled(self.cy_btn.isChecked())
        self.view.set_cursors(self.cx_btn.isChecked(), self.cy_btn.isChecked(), self.cy_src.currentData())

    def _update_cursor_table(self):
        v, f = self.view, self._last_frame
        rows = []
        if f is not None:
            if v.cur_x_on:
                x1, x2 = v.cur_x
                dt = x2 - x1
                rows.append(("X (time)", [si(x1, "s", 4), si(x2, "s", 4), si(dt, "s", 4),
                                          fmt_freq(1 / abs(dt)) if dt else "---"]))
                for cd in f.channels:
                    a, b = value_at(cd.t, cd.volts, x1), value_at(cd.t, cd.volts, x2)
                    ok = a is not None and b is not None
                    rows.append((f"CH{cd.channel} @ X", [si(a, "V") if a is not None else "---",
                                                    si(b, "V") if b is not None else "---",
                                                    si(b - a, "V") if ok else "---", ""]))
            if v.cur_y_on and v._src_channel() is not None:
                y1, y2 = v.cur_y
                rows.append((f"Y (CH{v.cur_y_src})", [si(y1, "V"), si(y2, "V"), si(y2 - y1, "V"), ""]))
        t = self.cur_table
        t.setRowCount(len(rows))
        c = theme.colors(self.theme_name)
        for r, (label, vals) in enumerate(rows):
            col = c["cur_y"] if label.startswith("Y") else c["cur_x"] if label.startswith("X") else \
                c["ch1"] if label.startswith("CH1") else c["ch2"]
            _cell(t, r, 0, label, col, left=True)
            for k, text in enumerate(vals):
                _cell(t, r, k + 1, text, col)
        _fit_table(t)
        t.setVisible(bool(rows))

    def _show_meas(self, f):
        by_ch = {cd.channel: cd.meas for cd in f.channels}
        for r, ch in enumerate((1, 2)):
            m = by_ch.get(ch)
            if m is None:
                vals = ["---"] * len(MEAS_COLS)
            else:
                vals = [si(m.vpp, "V"), si(m.vmax, "V"), si(m.vmin, "V"), si(m.vrms, "V"),
                        fmt_freq(m.freq) if m.freq else "---",
                        si(m.period, "s", 4) if m.period else "---"]
            for col, text in enumerate(vals):
                self.meas.item(r, col + 1).setText(text)

    def _show_probe(self, ch, factor):
        """Show the detected probe attenuation; an unusual factor (e.g. 100X) gets its own entry."""
        cb = self.probe[ch]
        k = next((i for i in range(cb.count()) if abs(cb.itemData(i) - factor) < 1e-9), -1)
        if k < 0:
            cb.addItem(f"{factor:g}X", factor)
            k = cb.count() - 1
        cb.blockSignals(True)
        cb.setCurrentIndex(k)
        cb.blockSignals(False)

    def _sync_controls(self, f):
        """Show the scope's actual settings in the controls without re-sending them."""
        def set_combo(cb, pred):
            for k in range(cb.count()):
                if pred(cb.itemData(k)):
                    cb.blockSignals(True)
                    cb.setCurrentIndex(k)
                    cb.blockSignals(False)
                    return
        close = lambda a: (lambda b: abs(a - b) <= 1e-6 * max(abs(a), abs(b), 1e-30))
        present = {cd.channel for cd in f.channels}
        for ch, tick in self.ch_on.items():
            wanted = self._ch_target.get(ch)
            if wanted is not None and time.monotonic() < wanted[1] and (ch in present) != wanted[0]:
                continue                                    # a click the scope has not caught up with yet
            self._ch_target.pop(ch, None)
            tick.blockSignals(True)
            tick.setChecked(ch in present)
            tick.blockSignals(False)
        self._apply_channel_states()
        set_combo(self.trig_src, lambda d: d == f.trigger.source)
        set_combo(self.trig_slope, lambda d: d == f.trigger.slope)
        set_combo(self.trig_sweep, lambda d: d == f.trigger.sweep)
        if self._zoom_target is not None and time.monotonic() < self._zoom_deadline                 and not close(self._zoom_target)(f.time_scale):
            pass                                    # a wheel zoom is still on its way: keep what the user chose
        else:
            self._zoom_target = None
            set_combo(self.tdiv, close(f.time_scale))
        for cd in f.channels:
            set_combo(self.vdiv[cd.channel], close(cd.scale))
            set_combo(self.coup[cd.channel], lambda d, cd=cd: d == cd.coupling.upper())
            self._show_probe(cd.channel, cd.probe)
        for cd in f.channels:
            sp = self.gnd_spin[cd.channel]
            if not sp.hasFocus():                  # do not fight the user while typing
                sp.blockSignals(True)
                sp.setValue(cd.offset)
                sp.blockSignals(False)
        self.trig_level.blockSignals(True)
        self.trig_level.setValue(f.trigger.level)
        self.trig_level.blockSignals(False)

    def _wheel_zoom(self, steps):
        """Mouse wheel over the Y-T display: step the time/div one 1-2-5 value per notch (up = zoom in)."""
        if not self._connected:
            return
        i = min(max(self.tdiv.currentIndex() - steps, 0), self.tdiv.count() - 1)
        if i != self.tdiv.currentIndex():
            self.tdiv.setCurrentIndex(i)                       # the box follows every notch at once ...
            self._zoom_target = self.tdiv.itemData(i)
            self._zoom_deadline = time.monotonic() + 3.0
            self._zoom_timer.start()                           # ... the scope only gets the final value

    def _save_prefs(self):
        if not self._loading:
            prefs.save(self, self.settings)

    def _record_changed(self):
        """Screen record or the acquisition memory for Y-T / XY / measurements: tell the worker and capture again."""
        self.request_record_mode.emit(self.record.currentData())
        self._capture_once()
        self._save_prefs()

    def _fft_options(self, retune=False, **kw):
        """FFT tab control changed: pass it on, and (re)configure the scope's memory read and time/div."""
        self.fft_view.set_options(**kw)
        self._save_prefs()
        if retune:
            self._fft_retune()
        self._fft_sync_memory()

    def _fft_retune(self):
        """A span in Hz picks the time/div: the longest time base whose Nyquist frequency (50 / tdiv for the
        screen record, 1280 / tdiv for the memory, which leaves a margin above the span) covers the span, but
        not longer than FFT_MAX_TDIV, so the resolution 1 / (16 x tdiv) is as fine as the data allow."""
        span = self.fft_span.currentData()
        if not isinstance(span, (int, float)) or not self._connected:       # Auto / Full: leave the time/div alone
            return
        k = 1280.0 if self.fft_data.currentData() == "mem" else 40.0
        limit = min(FFT_MAX_TDIV, k / span)
        tdiv = max((d for d in TIME_DIVS if d <= limit * (1 + 1e-9)), default=TIME_DIVS[0])
        i = self.tdiv.findData(tdiv)
        if i >= 0 and i != self.tdiv.currentIndex():
            self.tdiv.setCurrentIndex(i)
            self._zoom_target = tdiv                       # same path as the mouse wheel: debounced, newest wins
            self._zoom_deadline = time.monotonic() + 3.0
            self._zoom_timer.start()
        self._set_status(f"FFT span 0-{fmt_hz(span)}: time/div {si(tdiv, 's')}/div, resolution {fmt_hz(1 / (16 * tdiv))}", None)

    def _fft_sync_memory(self):
        """Read the deep memory of the FFT channel with every capture, but only while it is being shown."""
        want = 0
        if self._connected and self.tabs.currentIndex() == 2 and self.fft_data.currentData() == "mem":
            want = self.fft_src.currentData()
        if want != self._mem_ch:
            self._mem_ch = want
            self.request_memory_channel.emit(want)
            if want:
                self._capture_once()

    def _tab_changed(self, index):
        """Tab selected: the XY tab puts the scope's own display in XY mode, Y-T and FFT (both work on the
        time-domain record) put it in Y-T mode. Nothing is sent when the mode would not change."""
        self._fft_sync_memory()
        want_xy = index == 1
        if self._connected and want_xy != self._xy_mode:
            self._xy_mode = want_xy
            self.request_display_type.emit("XY12" if want_xy else "YT")

    def _show_mode_tab(self, display_type):
        """Select the tab for the scope's display type without sending it back (Roll/unknown: unchanged)."""
        index = {"YT": 0, "XY12": 1}.get(display_type)
        if index is not None:
            self._xy_mode = index == 1
            if index == 0 and self.tabs.currentIndex() == 2:
                return                                  # the FFT tab also needs the Y-T mode: stay on it
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(index)
            self.tabs.blockSignals(False)

    def _on_display_synced(self, t):
        self._show_mode_tab(t)

    def _on_display_type(self, t):
        """Scope confirmed a mode change. The tab is not touched: it is what the user chose, and a confirmation can
        arrive after a later click. If the scope refused (empty) the error is already in the status bar and the tab
        stays: the XY plot is drawn from the captured data either way."""
        if t:
            self._set_status(f"Scope display: {t}", True)
            self._capture_once()

    def _toggle_run(self):
        stopped = self.run_btn.text() == "Stop"   # the label is the scope's current mode
        self.request_run.emit(stopped)             # stopped -> run, running -> stop

    def _on_run_state(self, state):
        self._set_run_label("Stop" if state.upper() == "STOP" else "Run")
        self.view.set_state(state)           # badge now; the capture below brings the frame itself up to date
        self._capture_once()
        self._set_status(f"Scope state: {state}", True)

    # --- export / theme --------------------------------------------------
    def _save_png(self):
        if self._last_frame is None:
            return
        Path("screenshots").mkdir(exist_ok=True)
        default = str(Path("screenshots") / datetime.now().strftime("%Y-%m-%d_%H-%M-%S.png"))
        path, _ = QFileDialog.getSaveFileName(self, "Save waveform PNG", default, "PNG image (*.png)")
        if not path:
            return
        active = (self.view, self.xy_view, self.fft_view)[self.tabs.currentIndex()]
        if not active.render_image(2).save(path, "PNG"):
            QMessageBox.warning(self, "Save PNG", f"Could not write {path}")
            return
        self._set_status(f"Saved {path}", True)

    def _export_wave(self):
        if self._last_frame is None:
            return
        Path("screenshots").mkdir(exist_ok=True)
        filters = ";;".join(f"{label} (*{ext})" for ext, label in export_mod.FORMATS.items())
        default = str(Path("screenshots") / export_mod.default_filename(".vcd"))
        path, chosen = QFileDialog.getSaveFileName(self, "Export waveform", default, filters)
        if not path:
            return
        p = Path(path)
        if p.suffix.lower() not in export_mod.FORMATS:          # no/unknown extension: follow the chosen filter
            p = p.with_suffix(".csv" if "csv" in chosen.lower() else ".vcd")
        try:
            opts = {"digital": self.vcd_digital.isChecked()} if p.suffix.lower() == ".vcd" else {}
            export_mod.export(self._last_frame, p, **opts)
        except (ScopeError, OSError) as e:
            QMessageBox.warning(self, "Export waveform", f"Could not write {p}:\n{e}")
            return
        self._set_status(f"Exported {p}", True)

    def _toggle_theme(self):
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        theme.apply(self.app, self.theme_name)
        self.view.set_theme(self.theme_name)
        self.xy_view.set_theme(self.theme_name)
        self.fft_view.set_theme(self.theme_name)
        self._color_meas()
        self._color_ticks()
        self._update_cursor_table()
        self._set_status(*self._status)      # LED colour is theme-dependent
        self._update_theme_ui()

    # --- shutdown --------------------------------------------------------
    def closeEvent(self, e):
        if getattr(self, "_shut_down", False):          # closed before: the worker thread is already gone
            return super().closeEvent(e)
        self._shut_down = True
        self._reconnect_timer.stop()
        self._reconnecting = False
        self._save_prefs()
        self.timer.stop()
        QMetaObject.invokeMethod(self.worker, "disconnect_scope", Qt.ConnectionType.BlockingQueuedConnection)
        self.thread.quit()
        self.thread.wait(3000)
        super().closeEvent(e)
