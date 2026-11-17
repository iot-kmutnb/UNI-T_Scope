"""Saved settings: the GUI's own choices (not the scope's, which the scope keeps)."""
from PyQt6.QtCore import QByteArray


def _combo_index(box, data, default=0):
    i = box.findData(data)
    return i if i >= 0 else default


def save(w, s) -> None:
    s.setValue("record", w.record.currentData())
    s.setValue("interval", w.interval.currentData())
    s.setValue("reconnect", w.reconnect_chk.isChecked())
    s.setValue("meas_voltages", w.vm_btn.isChecked())
    s.setValue("meas_timing", w.tm_btn.isChecked())
    s.setValue("vcd_digital", w.vcd_digital.isChecked())
    s.setValue("tab", w.tabs.currentIndex() if w.tabs.currentIndex() != 1 else 0)    # XY needs the scope in XY mode
    s.setValue("fft_source", w.fft_src.currentData())
    s.setValue("fft_window", w.fft_win.currentData())
    s.setValue("fft_scale", w.fft_scale.currentData())
    s.setValue("fft_data", w.fft_data.currentData())
    s.setValue("fft_span", str(w.fft_span.currentData()))
    s.setValue("geometry", w.saveGeometry())
    s.sync()


def load(w, s) -> None:
    """Apply the saved choices to a freshly built window (nothing is sent to the scope)."""
    w.reconnect_chk.setChecked(s.value("reconnect", False, type=bool))
    w.vm_btn.setChecked(s.value("meas_voltages", True, type=bool))
    w.tm_btn.setChecked(s.value("meas_timing", True, type=bool))
    w.vcd_digital.setChecked(s.value("vcd_digital", False, type=bool))
    w.interval.setCurrentIndex(_combo_index(w.interval, s.value("interval", 1, type=int)))
    w.record.setCurrentIndex(_combo_index(w.record, s.value("record", "screen")))
    w.request_record_mode.emit(w.record.currentData())
    opts = {}
    for key, box, attr in (("fft_source", w.fft_src, "source"), ("fft_window", w.fft_win, "window"),
                           ("fft_scale", w.fft_scale, "scale"), ("fft_data", w.fft_data, "data")):
        raw = s.value(key)
        if raw is not None:
            box.setCurrentIndex(_combo_index(box, int(raw) if key == "fft_source" else raw))
        opts[attr] = box.currentData()
    span = s.value("fft_span", "auto")
    value = span
    if span not in ("auto", "full"):
        try:
            value = float(span)
        except ValueError:
            value = "auto"
    i = next((k for k in range(w.fft_span.count()) if w.fft_span.itemData(k) == value), 0)
    w.fft_span.setCurrentIndex(i)
    opts["span_hz"] = w.fft_span.currentData()
    w.fft_view.set_options(**opts)
    tab = s.value("tab", 0, type=int)
    if tab in (0, 2):
        w.tabs.setCurrentIndex(tab)
    geometry = s.value("geometry")
    if isinstance(geometry, (QByteArray, bytes)):
        w.restoreGeometry(geometry)
