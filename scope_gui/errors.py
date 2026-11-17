"""Last-resort error handling.

An exception that escapes a Qt slot would normally abort the whole program (PyQt6 calls ``qFatal`` when
``sys.excepthook`` is the default one). ``ErrorReporter`` replaces the hooks: the traceback goes to a log file,
the status bar and a (non-blocking) message box, and the program keeps running. Exceptions from the worker
thread and from plain Python threads are handled the same way.
"""
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QMainWindow, QMessageBox


def default_log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "UNI-T-Scope" / "scope_gui.log"


def write_log(text: str, path: Optional[Path] = None) -> Optional[Path]:
    """Append ``text`` with a time stamp to the log file; returns the path, or None if it cannot be written."""
    path = Path(path) if path else default_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{text.rstrip()}\n")
        return path
    except OSError:
        return None


def log_exception(path: Optional[Path] = None) -> Optional[Path]:
    """Log the exception being handled (call from an ``except`` block)."""
    return write_log(traceback.format_exc(), path)


class ErrorReporter(QObject):
    reported = pyqtSignal(str, str)           # one-line summary, full traceback (queued to the GUI thread)

    def __init__(self, window: Optional[QMainWindow] = None, log_path: Optional[Path] = None,
                 show_dialog: bool = True):
        super().__init__(window)
        self.window = window
        self.log_path = Path(log_path) if log_path else default_log_path()
        self.show_dialog = show_dialog
        self.count = 0
        self.last = None                      # (summary, traceback) of the latest error
        self._box = None
        self._last_dialog = 0.0
        self._old_hook = None
        self._old_thread_hook = None
        self.reported.connect(self._show)

    def install(self) -> "ErrorReporter":
        self._old_hook, self._old_thread_hook = sys.excepthook, threading.excepthook
        sys.excepthook = self._hook
        threading.excepthook = self._thread_hook
        return self

    def uninstall(self) -> None:
        if self._old_hook is not None:
            sys.excepthook, threading.excepthook = self._old_hook, self._old_thread_hook
            self._old_hook = None

    def _hook(self, etype, value, tb):
        if issubclass(etype, KeyboardInterrupt):
            return (self._old_hook or sys.__excepthook__)(etype, value, tb)
        text = "".join(traceback.format_exception(etype, value, tb))
        summary = f"{etype.__name__}: {value}"
        write_log(text, self.log_path)
        self.count += 1
        self.last = (summary, text)
        self.reported.emit(summary, text)

    def _thread_hook(self, args):
        self._hook(args.exc_type, args.exc_value, args.exc_traceback)

    def _show(self, summary: str, text: str) -> None:
        if self.window is not None:
            self.window.statusBar().showMessage(f"Unexpected error: {summary}", 15000)
        now = time.monotonic()
        if not self.show_dialog or self._box is not None or now - self._last_dialog < 3.0:
            return                            # one dialog at a time, none for a burst of errors
        self._last_dialog = now
        box = QMessageBox(QMessageBox.Icon.Warning, "Unexpected error",
                          f"{summary}\n\nThe program keeps running. If something stops working, disconnect and "
                          f"connect again.\nDetails are in {self.log_path}", QMessageBox.StandardButton.Ok, self.window)
        box.setDetailedText(text)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.finished.connect(lambda _=0: setattr(self, "_box", None))
        self._box = box
        box.open()                            # window-modal but does not block the event loop


def install(app, window: Optional[QMainWindow] = None, **kw) -> ErrorReporter:
    return ErrorReporter(window, **kw).install()
