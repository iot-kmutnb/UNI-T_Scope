"""Scope I/O worker: all instrument traffic runs in its own QThread, through unit_scope."""
import functools

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from unit_scope import ScopeError, UniTScope

from . import errors


def guarded(fn):
    """Slot wrapper: an exception that is not a ScopeError (a bug, a malformed reply) is logged and drops the
    connection with a message, instead of reaching Qt and aborting the program."""
    @functools.wraps(fn)
    def wrapper(self, *args):
        try:
            return fn(self, *args)
        except Exception as e:                       # noqa: BLE001 - last line of defence for the worker thread
            errors.log_exception()
            self._drop(f"Unexpected error in {fn.__name__}: {type(e).__name__}: {e}")
    return wrapper


class ScopeWorker(QObject):
    connected = pyqtSignal(str)       # IDN string
    disconnected = pyqtSignal()
    frame = pyqtSignal(object)        # unit_scope.Capture (or None)
    error = pyqtSignal(str)
    run_state = pyqtSignal(str)
    display_synced = pyqtSignal(str)  # display type read on connect; empty = unknown
    display_type = pyqtSignal(str)    # display type after a change; empty = refused/unknown

    def __init__(self):
        super().__init__()
        self.scope = None
        self.record_mode = "screen"       # "screen": the 1600-point screen record, "mem": the full acquisition memory
        self.memory_channel = 0           # channel whose deep memory is read with every capture (0 = none)

    def _drop(self, msg):
        self._close()
        self.error.emit(msg)
        self.disconnected.emit()

    def _close(self):
        if self.scope is not None:
            self.scope.disconnect()
            self.scope = None

    @pyqtSlot()
    @guarded
    def connect_scope(self):
        try:
            self.scope = UniTScope(timeout_ms=4000).connect()
            idn = self.scope.idn()
        except ScopeError as e:
            self._close()
            self.error.emit(f"Connect failed: {e}")
            self.disconnected.emit()
            return
        self.connected.emit(idn)
        self.sync_display_type()

    @pyqtSlot(str)
    @guarded
    def set_record_mode(self, mode):
        self.record_mode = mode

    @pyqtSlot(int)
    @guarded
    def set_memory_channel(self, ch):
        self.memory_channel = ch

    @pyqtSlot()
    @guarded
    def disconnect_scope(self):
        self._close()
        self.disconnected.emit()

    @pyqtSlot()
    @guarded
    def capture(self):
        if self.scope is None:
            self.frame.emit(None)
            return
        try:
            if self.record_mode == "mem":
                cap = self.scope.capture(memory=True)
                cap.memory = {c.channel: c for c in cap.channels}        # the FFT tab uses the same records
            else:
                cap = self.scope.capture()
                mem = self.memory_channel
                if mem and any(c.channel == mem for c in cap.channels):  # FFT tab: memory of one channel
                    cap.memory[mem] = self.scope.read_memory(mem)
            for w in cap.channels:
                w.meas          # computed here (worker thread), cached for the GUI
        except ScopeError as e:
            self._drop(f"Capture failed: {e}")
            return
        self.frame.emit(cap)

    @pyqtSlot(list)
    @guarded
    def apply(self, changes):
        """Apply setting changes, then refresh the display. Each change is a tuple:
        ("trigger", {kwargs}), ("time_scale", s_per_div) or ("channel", ch, {kwargs})."""
        if self.scope is None:
            return
        try:
            for kind, *args in changes:
                if kind == "trigger":
                    self.scope.set_trigger(**args[0])
                elif kind == "time_offset":
                    self.scope.set_time_offset(args[0])
                elif kind == "time_scale":
                    self.scope.set_time_scale(args[0])
                elif kind == "channel":
                    self.scope.set_channel(args[0], **args[1])
        except (ScopeError, ValueError) as e:
            self._drop(f"Setting failed: {e}")
            return
        self.capture()

    @pyqtSlot()
    @guarded
    def sync_display_type(self):
        """Read the scope's display type and report it (empty if unknown) without changing anything."""
        if self.scope is None:
            return
        try:
            self.display_synced.emit(self.scope.get_display_type())
        except ScopeError:
            self.display_synced.emit("")

    @pyqtSlot(str)
    @guarded
    def set_display_type(self, display_type):
        """Switch the scope's display (YT / XY12). Not every model accepts it, so a failure is
        reported without dropping the connection."""
        if self.scope is None:
            return
        try:
            self.scope.set_display_type(display_type)
            actual = self.scope.get_display_type()
        except (ScopeError, ValueError) as e:
            self.error.emit(f"Display type failed: {e}")
            self.display_type.emit("")
            return
        self.display_type.emit(actual)

    @pyqtSlot(bool)
    @guarded
    def set_running(self, run):
        if self.scope is None:
            return
        try:
            self.scope.run() if run else self.scope.stop()
            state = self.scope.state()
        except ScopeError as e:
            self._drop(f"Run/Stop failed: {e}")
            return
        self.run_state.emit(state)
