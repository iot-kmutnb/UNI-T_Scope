"""GUI smoke tests: the real window and worker thread against a fake scope (no hardware, offscreen Qt).

They cover the bugs found while using the panel: mouse-wheel time/div (a burst of notches must become one request and
the display must not jump back), partial records from a sweep that is still filling, tab switching and the scope's
display mode, errors that must not close the program, auto-reconnect and saved settings.
"""
import math
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")      # no screen needed

try:
    from PyQt6.QtCore import QEvent, QEventLoop, QPoint, QPointF, QSettings, Qt, QTimer
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QApplication
except ImportError:                                           # the GUI needs PyQt6; the library tests do not
    QApplication = None

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if QApplication is not None:
    from scope_gui import errors, theme
    from scope_gui.fft_view import FftView
    from scope_gui.main_window import MainWindow
    from scope_gui.scope_view import envelope
    from unit_scope import ScopeCommandError
    from unit_scope.models import Capture, TriggerSettings, Waveform

_app = None
_orig_settings = None


def setUpModule():
    global _app, _orig_settings
    if QApplication is None:
        raise unittest.SkipTest("PyQt6 is not installed")
    _app = QApplication.instance() or QApplication([])
    _orig_settings = theme.settings()


def tearDownModule():
    if _orig_settings is not None:
        theme.use_settings(_orig_settings)


def spin(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_for(cond, limit=10.0, step=15):
    end = time.monotonic() + limit
    while not cond() and time.monotonic() < end:
        spin(step)
    return cond()


def wheel(widget, dy):
    pt = QPointF(100, 100)
    widget.wheelEvent(QWheelEvent(pt, widget.mapToGlobal(pt), QPoint(0, 0), QPoint(0, dy), Qt.MouseButton.NoButton,
                                  Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))


def _wave(ch, n, tdiv, toff, valid=None):
    valid = n if valid is None else valid
    dt = 16 * tdiv / n
    t = [toff - 8 * tdiv + k * dt for k in range(valid)]
    v = [1.0 + math.sin(2 * math.pi * 100 * x + ch) for x in t]
    return Waveform(channel=ch, t=t, volts=v, scale=0.5, offset=-1.0, coupling="DC", probe=1.0, missing=n - valid)


class FakeScope:
    """Just enough of UniTScope for the worker. ``behaviour`` decides what captures return:
    "normal" complete records; "filling" CH1 is 40 % filled for the next ``fill`` captures after a time/div change (the
    sweep is still being acquired, CH2 is read later and is complete); "clipped" CH1 always has 36 samples;
    "stopped" both channels hold the same 20 % (the scope is stopped and zoomed out past its memory)."""
    instances = []

    def __init__(self, *a, **kw):
        self.tdiv, self.toff, self.state_name, self.display = 1e-3, 0.0, "TRIGED", "YT"
        self.calls, self.captures, self.latency, self.fill, self.behaviour = [], 0, 0.02, 0, "normal"
        self.partial_after_change = 2
        self.enabled = {1: True, 2: True}               # which channels are on
        self.trig_source = 1
        self.fail = None
        FakeScope.instances.append(self)

    def connect(self):
        return self

    def idn(self):
        return "UNI-TREND TECHNOLOGY,DSO,FAKE0001,1.10.0060"

    def disconnect(self):
        pass

    def set_time_scale(self, v):
        self.calls.append(("time_scale", v))
        time.sleep(self.latency)
        self.tdiv = v
        if self.behaviour == "filling":
            self.fill = self.partial_after_change

    def set_time_offset(self, v):
        self.calls.append(("time_offset", v))
        self.toff = v

    def set_trigger(self, **kw):
        self.calls.append(("trigger", kw))
        self.trig_source = kw.get("source", self.trig_source)

    def set_channel(self, ch, **kw):
        self.calls.append(("channel", ch, kw))
        if "enabled" in kw:
            self.enabled[ch] = kw["enabled"]

    def run(self):
        self.state_name = "TRIGED"

    def stop(self):
        self.state_name = "STOP"

    def state(self):
        return self.state_name

    def get_display_type(self):
        return self.display

    def set_display_type(self, t):
        self.calls.append(("display", t))
        self.display = t

    def capture(self, memory=False):
        if self.fail:
            raise self.fail
        time.sleep(self.latency)
        self.captures += 1
        n = 32000 if memory else 1600
        valid = {"normal": (n, n), "filling": (n, n), "clipped": (36, n), "stopped": (n // 5, n // 5)}[self.behaviour]
        if self.behaviour == "filling" and self.fill > 0:
            valid = (int(n * (1 - 0.3 * self.fill)), n)      # a real sweep grows: 40 %, 70 %, then complete
            self.fill -= 1
        cap = Capture(time_scale=self.tdiv, time_offset=self.toff, state="STOP" if self.behaviour == "stopped" else
                      self.state_name, trigger=TriggerSettings(self.trig_source, "rising", 1.0, "auto"),
                      points=32000 if self.enabled[2] else 64000, sample_rate=2e6)
        cap.channels = [_wave(ch, n, self.tdiv, self.toff, valid[ch - 1]) for ch in (1, 2) if self.enabled[ch]]
        return cap

    def read_memory(self, ch):
        return _wave(ch, 32000, self.tdiv, self.toff)


class GuiCase(unittest.TestCase):
    """A window connected to a FakeScope, with its own temporary settings file."""

    def make_window(self, ini=None, connect=True, **scope_attrs):
        FakeScope.instances.clear()
        self._tmp = self._tmp if hasattr(self, "_tmp") else tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ini = ini or str(Path(self._tmp.name) / "gui.ini")
        settings = QSettings(self.ini, QSettings.Format.IniFormat)
        theme.use_settings(settings)
        patcher = mock.patch("scope_gui.worker.UniTScope", FakeScope)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmp.name})      # the error log goes to the temp dir
        env.start()
        self.addCleanup(env.stop)
        w = MainWindow(_app, settings=settings)
        w._fill_timer.setInterval(60)                  # do not wait a second between looks in the tests
        w._reconnect_timer.setInterval(60)
        self.addCleanup(self._close, w)
        self.w = w
        w.show()
        if connect:
            w.request_connect.emit()
            self.assertTrue(wait_for(lambda: w._connected and w._last_frame is not None), "did not connect")
            self.scope = FakeScope.instances[-1]
            for k, v in scope_attrs.items():
                setattr(self.scope, k, v)
            self.assertTrue(wait_for(lambda: not w._busy))
        return w

    @staticmethod
    def _close(w):
        w._connected = False
        w.close()

    def settle(self):
        wait_for(lambda: not self.w._busy and not self.w._zoom_timer.isActive() and not self.w._fill_timer.isActive()
                 and not self.w._pending, 15)
        spin(50)


class WheelZoomTests(GuiCase):
    def test_a_fast_spin_is_one_request_with_the_final_value(self):
        w = self.make_window()
        self.scope.calls.clear()
        for _ in range(17):
            wheel(w.view, 120)                           # 17 notches of zoom in (1 ms/div -> 10 ns/div clamps)
            spin(8)
        expected = w.tdiv.currentData()
        self.settle()
        zooms = [c for c in self.scope.calls if c[0] == "time_scale"]
        self.assertEqual(len(zooms), 1, zooms)           # not one request per notch
        self.assertAlmostEqual(zooms[0][1], expected)
        self.assertAlmostEqual(w._last_frame.time_scale, expected)
        self.assertAlmostEqual(w.tdiv.currentData(), expected)

    def test_old_frames_do_not_reset_the_box_while_zooming(self):
        w = self.make_window()
        self.scope.latency = 0.3                         # slow scope: frames of older requests arrive mid-spin
        w._capture_once()
        wheel(w.view, -120)
        spin(10)
        wheel(w.view, -120)                              # 1 ms -> 2 ms -> 5 ms/div
        target = w.tdiv.currentData()
        for _ in range(8):                               # frames keep arriving with the OLD time base
            spin(60)
            self.assertAlmostEqual(w.tdiv.currentData(), target)
        self.settle()
        self.assertAlmostEqual(w._last_frame.time_scale, target)

    def test_bursts_during_a_busy_scope_are_merged(self):
        w = self.make_window()
        self.scope.latency = 0.35
        self.scope.calls.clear()
        for burst in (3, 4, 2):                          # bursts that end while the previous request is running
            for _ in range(burst):
                wheel(w.view, -120)
                spin(8)
            spin(160)
        expected = w.tdiv.currentData()
        self.settle()
        zooms = [c[1] for c in self.scope.calls if c[0] == "time_scale"]
        self.assertLessEqual(len(zooms), 3, zooms)
        self.assertAlmostEqual(zooms[-1], expected)
        self.assertAlmostEqual(w._last_frame.time_scale, expected)

    def test_other_settings_are_merged_while_busy(self):
        w = self.make_window()
        self.scope.latency = 0.3
        self.scope.calls.clear()
        w._send([("trigger", {"level": 1.0})])           # goes out at once
        for level in (1.1, 1.2):
            w._send([("trigger", {"level": level})])
        w._send([("trigger", {"slope": "falling"})])
        w._send([("channel", 1, {"scale": 0.5})])
        w._send([("channel", 1, {"probe": 10.0})])
        self.settle()
        trig = [c[1] for c in self.scope.calls if c[0] == "trigger"]
        chan = [c for c in self.scope.calls if c[0] == "channel"]
        self.assertEqual(trig, [{"level": 1.0}, {"level": 1.2, "slope": "falling"}])
        self.assertEqual(chan, [("channel", 1, {"scale": 0.5, "probe": 10.0})])


class PartialRecordTests(GuiCase):
    def test_a_filling_sweep_is_refreshed_until_it_is_complete(self):
        w = self.make_window(behaviour="filling")
        statuses = []
        orig = w._set_status
        w._set_status = lambda msg, ok=None: (statuses.append(msg), orig(msg, ok))
        self.scope.captures = 0
        wheel(w.view, -120)                              # a time/div change: the next 2 captures are partial
        spin(10)
        self.settle()
        self.assertGreaterEqual(self.scope.captures, 3)  # partial, partial, complete - nobody pressed Capture
        lens = [(len(c.volts), c.missing) for c in w._last_frame.channels]
        self.assertEqual(lens, [(1600, 0), (1600, 0)])
        self.assertTrue(any(s.startswith("Acquiring the sweep") for s in statuses), statuses)
        self.assertFalse(any("clipped" in s for s in statuses), statuses)     # a sweep in progress is not a clipped trace
        self.assertTrue(statuses[-1].startswith("Captured"), statuses[-1])

    def test_a_clipped_trace_is_looked_at_once_and_then_reported(self):
        w = self.make_window(behaviour="clipped")
        self.scope.captures = 0
        w._capture_once()
        self.settle()
        self.assertLessEqual(self.scope.captures, 3)     # no endless re-capturing
        self.assertIn("clipped", w.status_msg.text())

    def test_a_stopped_scope_shows_the_stored_part_without_refreshing(self):
        w = self.make_window(behaviour="stopped")
        self.scope.captures = 0
        w._capture_once()
        self.settle()
        self.assertEqual(self.scope.captures, 1)
        self.assertTrue(w.status_msg.text().startswith("Stopped: only the stored part"), w.status_msg.text())
        self.assertFalse(w._fill_timer.isActive())

    def test_memory_record_mode_captures_32k_samples(self):
        w = self.make_window()
        w.record.setCurrentIndex(w.record.findData("mem"))
        w._record_changed()
        self.settle()
        wait_for(lambda: len(w._last_frame.channels[0].volts) > 2000)
        self.assertEqual([len(c.volts) for c in w._last_frame.channels], [32000, 32000])
        self.assertLess(len(w.view._dec[1][0]), 2000)    # drawn as an envelope, not 32000 points

    def test_envelope_keeps_a_one_sample_pulse(self):
        n = 32000
        t = [k * 1e-6 for k in range(n)]
        v = [0.0] * n
        v[12345] = 2.0
        ts, vs = envelope(t, v)
        self.assertLess(len(vs), 1800)
        self.assertEqual(max(vs), 2.0)
        self.assertEqual(envelope(t[:1600], v[:1600])[1], v[:1600])      # short records are untouched


class TabTests(GuiCase):
    def test_tabs_switch_the_scope_display_only_when_the_mode_changes(self):
        w = self.make_window()
        self.scope.calls.clear()
        w.tabs.setCurrentIndex(2)                        # FFT works on the Y-T record: no change
        w.tabs.setCurrentIndex(0)
        w.tabs.setCurrentIndex(1)                        # XY
        w.tabs.setCurrentIndex(2)                        # back to Y-T mode for the FFT
        w.tabs.setCurrentIndex(0)
        self.settle()
        self.assertEqual([c[1] for c in self.scope.calls if c[0] == "display"], ["XY12", "YT"])

    def test_the_display_mode_sync_does_not_pull_you_off_the_fft_tab(self):
        w = self.make_window()
        w.tabs.setCurrentIndex(2)
        w._on_display_synced("YT")
        self.assertEqual(w.tabs.currentIndex(), 2)
        w._on_display_synced("XY12")                     # the scope really is in XY: follow it
        self.assertEqual(w.tabs.currentIndex(), 1)

    def test_the_fft_tab_reads_memory_only_while_it_is_shown(self):
        w = self.make_window()
        self.assertEqual(w.worker.memory_channel, 0)
        w.tabs.setCurrentIndex(2)
        wait_for(lambda: w.worker.memory_channel == 1)
        self.assertEqual(w.worker.memory_channel, 1)
        w.fft_src.setCurrentIndex(1)
        w._fft_options(source=2)
        wait_for(lambda: w.worker.memory_channel == 2)
        w.tabs.setCurrentIndex(0)
        wait_for(lambda: w.worker.memory_channel == 0)
        self.assertEqual(w.worker.memory_channel, 0)

    def test_fft_auto_span_shows_the_peak(self):
        n, fs = 32000, 2e6
        t = [k / fs for k in range(n)]
        wf = Waveform(channel=1, t=t, volts=[1 + math.sin(2 * math.pi * 1000 * x) for x in t])
        view = FftView()
        view.set_frame(type("F", (), {"channels": [wf], "memory": {1: wf}})())
        s = view.spectrum()
        self.assertAlmostEqual(s.nyquist, 1e6)
        span = view.span_limit(s)
        self.assertLess(span, 20000)                     # not 0-1 MHz, where a 1 kHz line is under one pixel
        self.assertGreater(span, 5000)
        from unit_scope import spectrum as sp
        self.assertAlmostEqual(sp.find_peak(s, span)[0], 1000, delta=2)


class ErrorTests(GuiCase):
    def test_an_exception_in_a_slot_is_reported_and_the_program_keeps_running(self):
        w = self.make_window(connect=False)
        log = Path(self._tmp.name) / "errors.log"
        rep = errors.ErrorReporter(w, log_path=log, show_dialog=False).install()
        self.addCleanup(rep.uninstall)

        def boom():
            1 / 0
        QTimer.singleShot(0, boom)
        self.assertTrue(wait_for(lambda: rep.count == 1))
        spin(50)
        self.assertIn("ZeroDivisionError", rep.last[0])
        self.assertIn("ZeroDivisionError", log.read_text(encoding="utf-8"))
        self.assertIn("Unexpected error", w.statusBar().currentMessage())
        done = []
        QTimer.singleShot(0, lambda: done.append(1))     # the event loop still runs
        self.assertTrue(wait_for(lambda: done))

    def test_an_unexpected_error_in_the_worker_drops_the_connection_gracefully(self):
        w = self.make_window()
        self.scope.fail = RuntimeError("malformed reply")
        w._capture_once()
        self.assertTrue(wait_for(lambda: not w._connected))
        self.assertIn("Unexpected error in capture", w.status_msg.text())
        self.assertTrue(w.connect_btn.isEnabled())


class ReconnectTests(GuiCase):
    def test_a_lost_connection_is_re_established_when_auto_reconnect_is_on(self):
        w = self.make_window()
        statuses = []
        orig = w._set_status
        w._set_status = lambda msg, ok=None: (statuses.append(msg), orig(msg, ok))
        w.reconnect_chk.setChecked(True)
        self.scope.fail = ScopeCommandError("USB hiccup")
        w._capture_once()
        self.assertTrue(wait_for(lambda: not w._connected))
        self.assertTrue(w._reconnecting)
        self.assertTrue(wait_for(lambda: w._connected and len(FakeScope.instances) == 2), 10)
        self.assertIn("Connection lost - reconnecting in 3 s...", statuses)
        self.assertIn("Reconnected", statuses)
        self.assertTrue(wait_for(lambda: w._last_frame is not None and not w._busy))
        self.assertTrue(w.connect_btn.isChecked())
        self.assertFalse(w._reconnecting)

    def test_no_retry_when_off_or_when_the_user_disconnects(self):
        w = self.make_window()
        self.scope.fail = ScopeCommandError("USB hiccup")
        w._capture_once()                                # auto-reconnect is off: stays disconnected
        self.assertTrue(wait_for(lambda: not w._connected))
        spin(250)
        self.assertFalse(w._connected)
        self.assertEqual(len(FakeScope.instances), 1)
        w.reconnect_chk.setChecked(True)
        w.connect_btn.click()                            # connect
        self.assertTrue(wait_for(lambda: w._connected))
        w.connect_btn.click()                            # the user disconnects: no reconnect
        self.assertTrue(wait_for(lambda: not w._connected))
        spin(300)
        self.assertFalse(w._connected)
        self.assertFalse(w._reconnect_timer.isActive())


class SettingsTests(GuiCase):
    def test_choices_survive_a_restart(self):
        w = self.make_window(connect=False)
        w.record.setCurrentIndex(w.record.findData("mem"))
        w._record_changed()
        w.interval.setCurrentIndex(2)
        w._save_prefs()
        w.reconnect_chk.setChecked(True)
        w.tm_btn.setChecked(False)
        w.fft_win.setCurrentIndex(w.fft_win.findData("blackman"))
        w._fft_options(window="blackman")
        w.fft_span.setCurrentIndex(w.fft_span.findData(5000))
        w._fft_options(span_hz=5000)
        w.fft_scale.setCurrentIndex(w.fft_scale.findData("v"))
        w._fft_options(scale="v")
        w.tabs.setCurrentIndex(2)
        w.close()
        ini = self.ini
        w2 = self.make_window(ini=ini, connect=False)
        self.assertEqual(w2.record.currentData(), "mem")
        self.assertEqual(w2.interval.currentIndex(), 2)
        self.assertTrue(w2.reconnect_chk.isChecked())
        self.assertFalse(w2.tm_btn.isChecked())
        self.assertTrue(w2.vm_btn.isChecked())
        self.assertEqual((w2.fft_view.window, w2.fft_view.scale, w2.fft_view.span_hz), ("blackman", "v", 5000.0))
        self.assertEqual(w2.tabs.currentIndex(), 2)
        wait_for(lambda: w2.worker.record_mode == "mem")
        self.assertEqual(w2.worker.record_mode, "mem")

    def test_defaults_on_a_fresh_start_and_garbage_is_ignored(self):
        w = self.make_window(connect=False)
        self.assertEqual((w.record.currentData(), w.fft_view.span_hz, w.tabs.currentIndex()), ("screen", "auto", 0))
        s = QSettings(self.ini, QSettings.Format.IniFormat)
        s.setValue("record", "nonsense")
        s.setValue("fft_span", "abc")
        s.setValue("tab", 99)
        s.sync()
        w.close()
        w2 = self.make_window(ini=self.ini, connect=False)
        self.assertEqual((w2.record.currentData(), w2.fft_view.span_hz, w2.tabs.currentIndex()), ("screen", "auto", 0))


class MouseTests(GuiCase):
    """A click on a marker without dragging it used to raise KeyError (and, before the error handler, close the program)."""

    def _view_point(self, view, sx, sy):
        from scope_gui import scope_view as V
        s = min(view.width() / V.W, view.height() / V.H)
        return QPointF((view.width() - V.W * s) / 2 + sx * s, (view.height() - V.H * s) / 2 + sy * s)

    @staticmethod
    def _mouse(view, kind, pt):
        from PyQt6.QtGui import QMouseEvent
        btn = Qt.MouseButton.LeftButton
        down = kind != "release"
        typ = {"press": QEvent.Type.MouseButtonPress, "move": QEvent.Type.MouseMove,
               "release": QEvent.Type.MouseButtonRelease}[kind]
        return QMouseEvent(typ, pt, view.mapToGlobal(pt), btn, btn if down else Qt.MouseButton.NoButton,
                           Qt.KeyboardModifier.NoModifier)

    def _click_and_drag(self, view, spots):
        """For every (name, signal, x, y) in scope coordinates: click without moving, then a real drag."""
        from scope_gui import scope_view as V
        for name, signal, x, y, dx, dy in spots:
            got = []
            signal.connect(lambda *a, got=got: got.append(a))
            p0 = self._view_point(view, x, y)
            view.mousePressEvent(self._mouse(view, "press", p0))
            view.mouseReleaseEvent(self._mouse(view, "release", p0))
            self.assertEqual(got, [], f"{name}: a click sent a value")
            view.mousePressEvent(self._mouse(view, "press", p0))
            p1 = self._view_point(view, x + dx, y + dy)
            view.mouseMoveEvent(self._mouse(view, "move", p1))
            view.mouseReleaseEvent(self._mouse(view, "release", p1))
            self.assertEqual(len(got), 1, f"{name}: a real drag should send exactly one value")

    def test_clicking_markers_without_dragging_is_harmless(self):
        from scope_gui import scope_view as V
        w = self.make_window()
        rep = errors.ErrorReporter(w, log_path=Path(self._tmp.name) / "e.log", show_dialog=False).install()
        self.addCleanup(rep.uninstall)
        w.tabs.setCurrentIndex(0)
        w.resize(1100, 800)
        spin(100)
        view, f = w.view, w._last_frame
        ch1 = f.channels[0]
        ty = view._trig_y(f)[1]
        self._click_and_drag(view, [
            ("trigger level", view.trigger_level_changed, V.PLOT.right() - 10, ty, 0, 30),
            ("ground marker CH1", view.offset_changed, 5, view._marker_y(ch1), 0, 20),
            ("trigger position", view.trigger_pos_changed, view._trig_x(), V.PLOT.top() + 4, 40, 0)])
        self.assertEqual(rep.count, 0, rep.last)

    def test_clicking_the_xy_markers_without_dragging_is_harmless(self):
        from scope_gui import xy_view as X
        w = self.make_window()
        rep = errors.ErrorReporter(w, log_path=Path(self._tmp.name) / "e.log", show_dialog=False).install()
        self.addCleanup(rep.uninstall)
        w.tabs.setCurrentIndex(1)
        w.resize(1100, 800)
        spin(100)
        v = w.xy_view
        gx, gy = v._gnd_pos()
        total = X.SIZE + 2 * X.PAD
        s = min(v.width(), v.height()) / total

        def at(px, py):
            return QPointF((v.width() - total * s) / 2 + (px + X.PAD) * s, (v.height() - total * s) / 2 + (py + X.PAD) * s)
        for ch, (px, py, dx, dy) in ((1, (gx, X.SIZE - 5, 30, 0)), (2, (5, gy, 0, -30))):
            got = []
            v.offset_changed.connect(lambda c, val, got=got: got.append((c, val)))
            v.mousePressEvent(self._mouse(v, "press", at(px, py)))
            v.mouseReleaseEvent(self._mouse(v, "release", at(px, py)))
            self.assertEqual(got, [], f"CH{ch}: a click sent a value")
            v.mousePressEvent(self._mouse(v, "press", at(px, py)))
            v.mouseMoveEvent(self._mouse(v, "move", at(px + dx, py + dy)))
            v.mouseReleaseEvent(self._mouse(v, "release", at(px + dx, py + dy)))
            self.assertEqual([c for c, _ in got], [ch])
        self.assertEqual(rep.count, 0, rep.last)


class ChannelTickTests(GuiCase):
    def test_tick_boxes_turn_channels_on_and_off(self):
        w = self.make_window()
        self.assertTrue(w.ch_on[1].isChecked() and w.ch_on[2].isChecked())
        self.scope.calls.clear()
        w.ch_on[2].click()                                # untick CH2
        self.settle()
        self.assertEqual([c for c in self.scope.calls if c[0] == "channel"], [("channel", 2, {"enabled": False})])
        self.assertEqual([c.channel for c in w._last_frame.channels], [1])
        self.assertFalse(w.ch_on[2].isChecked())
        self.assertFalse(w.vdiv[2].isEnabled() or w.coup[2].isEnabled() or w.probe[2].isEnabled())   # greyed out
        self.assertTrue(w.vdiv[1].isEnabled())
        w.ch_on[2].click()                                # tick again
        self.settle()
        self.assertEqual([c.channel for c in w._last_frame.channels], [1, 2])
        self.assertTrue(w.ch_on[2].isChecked() and w.vdiv[2].isEnabled())

    def test_the_last_channel_cannot_be_turned_off(self):
        w = self.make_window()
        w.ch_on[2].click()
        self.settle()
        self.scope.calls.clear()
        w.ch_on[1].click()                                # would leave nothing on
        self.settle()
        self.assertTrue(w.ch_on[1].isChecked())
        self.assertEqual([c for c in self.scope.calls if c[0] == "channel"], [])
        self.assertIn("At least one channel", w.status_msg.text())
        self.assertEqual([c.channel for c in w._last_frame.channels], [1])

    def test_turning_off_the_trigger_channel_moves_the_trigger(self):
        w = self.make_window()
        self.assertEqual(w.trig_src.currentData(), 1)
        self.scope.calls.clear()
        w.ch_on[1].click()
        self.settle()
        kinds = [(c[0], c[1] if c[0] == "channel" else c[1].get("source")) for c in self.scope.calls
                 if c[0] in ("channel", "trigger")]
        self.assertEqual(kinds, [("channel", 1), ("trigger", 2)])                  # channel first, then the trigger
        self.assertEqual(w.trig_src.currentData(), 2)
        self.assertEqual([c.channel for c in w._last_frame.channels], [2])

    def test_changes_made_on_the_scope_are_followed(self):
        w = self.make_window()
        self.scope.enabled[2] = False                     # CH2 switched off with the front panel button
        w._capture_once()
        self.settle()
        self.assertFalse(w.ch_on[2].isChecked())
        self.assertFalse(w.vdiv[2].isEnabled())
        self.scope.enabled[2] = True
        w._capture_once()
        self.settle()
        self.assertTrue(w.ch_on[2].isChecked() and w.vdiv[2].isEnabled())

    def test_an_old_frame_does_not_flip_the_tick_box_back(self):
        w = self.make_window()
        self.scope.latency = 0.3
        w._capture_once()                                 # a frame that still shows CH2 is on its way
        spin(20)
        w.ch_on[2].click()
        for _ in range(6):
            spin(60)
            self.assertFalse(w.ch_on[2].isChecked())
        self.settle()
        self.assertFalse(w.ch_on[2].isChecked())
        self.assertEqual([c.channel for c in w._last_frame.channels], [1])


if __name__ == "__main__":
    unittest.main()
