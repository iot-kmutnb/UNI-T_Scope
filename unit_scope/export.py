"""Waveform file export: VCD (GTKWave, Surfer, PulseView, ...) and CSV.

VCD carries each channel twice, so both viewers have something useful to show:

* ``CH1`` ... ``$var real`` - the analog voltage (GTKWave: right-click the signal ->
  Data Format -> Analog -> Step/Interpolated; Surfer: set the variable's format to *Analog*).
* ``CH1_dig`` ... ``$var wire`` - the same signal thresholded at mid-level with hysteresis, so a
  square wave / logic signal shows as a normal digital trace. Omitted for flat signals.

A ``trigger`` wire rises at t = 0 (the trigger point) so the viewer cursor can be lined up on it.
VCD times cannot be negative, so the file starts at the first sample and the trigger point
sits ``-t[0]`` into the file; the header ``$comment`` records this.
"""
import csv
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from .errors import ScopeError
from .measure import value_at
from .models import Capture, Waveform

FORMATS = {".vcd": "VCD (GTKWave / Surfer)", ".csv": "CSV"}


def default_filename(ext: str) -> str:
    """Current date and time, e.g. ``2026-11-14_12-30-05.vcd``."""
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ext


def export(capture: Capture, path: Union[str, Path], fmt: Optional[str] = None, **options) -> Path:
    """Write ``capture`` to ``path``. ``fmt`` ("vcd"/"csv") defaults to the file extension.
    Options are passed to :func:`write_vcd` / :func:`write_csv`."""
    path = Path(path)
    fmt = (fmt or path.suffix.lstrip(".")).lower()
    if fmt == "vcd":
        return write_vcd(capture, path, **options)
    if fmt == "csv":
        return write_csv(capture, path, **options)
    raise ScopeError(f"Unsupported export format {fmt!r} (use one of: vcd, csv)")


def _need_channels(capture: Capture) -> List[Waveform]:
    if not capture.channels:
        raise ScopeError("Nothing to export: the capture has no channels")
    return capture.channels


# --------------------------------------------------------------------------- CSV
def write_csv(capture: Capture, path: Union[str, Path]) -> Path:
    """``time_s, CH1_V, CH2_V`` on a common time axis (the first channel's; others are
    linearly interpolated if their time axes differ)."""
    chans = _need_channels(capture)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    t = chans[0].t
    cols = []
    for w in chans:
        cols.append(w.volts if w.t == t else [value_at(w.t, w.volts, x) for x in t])
    with open(path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["time_s"] + [f"CH{w.channel}_V" for w in chans])
        for k, x in enumerate(t):
            wr.writerow([f"{x:.9g}"] + ["" if c[k] is None else f"{c[k]:.6g}" for c in cols])
    return path


# --------------------------------------------------------------------------- VCD
def _timescale(dt: float) -> Tuple[str, float]:
    """Pick a legal ``$timescale`` (1/10/100 of fs..s) giving at least ~10 ticks per sample."""
    e = max(-15, min(0, math.floor(math.log10(dt / 10)))) if dt > 0 else -9
    unit_exp = 3 * math.floor(e / 3)
    unit = {-15: "fs", -12: "ps", -9: "ns", -6: "us", -3: "ms", 0: "s"}[unit_exp]
    return f"{10 ** (e - unit_exp):.0f} {unit}", 10.0 ** e


def _ids():
    """VCD identifier codes: printable ASCII 33..126, then two characters."""
    chars = [chr(c) for c in range(33, 127)]
    for a in chars:
        yield a
    for a in chars:
        for b in chars:
            yield a + b


def _digitize(volts: List[float], noise: float) -> Optional[List[int]]:
    """Schmitt-trigger the signal at 35 %/65 % of its range. None if the signal is flat.
    The first sample of a record is often a stray value, so it copies the second one."""
    skip = 1 if len(volts) > 4 else 0
    ref = volts[skip:]
    lo, hi = min(ref), max(ref)
    if hi - lo <= noise:
        return None
    t_lo, t_hi = lo + 0.35 * (hi - lo), lo + 0.65 * (hi - lo)
    state = 1 if ref[0] > (lo + hi) / 2 else 0
    out = [state] * skip
    for x in ref:
        if state and x < t_lo:
            state = 0
        elif not state and x > t_hi:
            state = 1
        out.append(state)
    return out


def write_vcd(capture: Capture, path: Union[str, Path], digital: bool = True,
              trigger_marker: bool = True, module: str = "unit_scope") -> Path:
    """Value-change dump of every channel.

    digital         also write a thresholded ``CHn_dig`` wire for each non-flat channel.
    trigger_marker  add a ``trigger`` wire that goes high at t = 0.
    """
    chans = _need_channels(capture)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    t0 = min(w.t[0] for w in chans)
    dts = [w.sample_interval for w in chans if w.sample_interval > 0]
    dt = min(dts) if dts else 1e-6
    timescale, tick = _timescale(dt)

    ids = _ids()
    signals = []        # (id, kind, name, events[(tick, value)])
    header_notes = []
    for w in chans:
        events, last = [], None
        for x, v in zip(w.t, w.volts):
            tk = round((x - t0) / tick)
            val = f"{v:.6g}"
            if val != last:
                events.append((tk, f"r{val}"))
                last = val
        signals.append((next(ids), "real 64", f"CH{w.channel}", events))
        if digital:
            bits = _digitize(w.volts, 3 * w.scale / 25)
            if bits is not None:
                dev, lastb = [], None
                for x, b in zip(w.t, bits):
                    if b != lastb:
                        dev.append((round((x - t0) / tick), str(b)))
                        lastb = b
                signals.append((next(ids), "wire 1", f"CH{w.channel}_dig", dev))
        header_notes.append(f"CH{w.channel}: {w.scale:g} V/div, offset {w.offset:g} V, {w.coupling}, {w.probe:g}X")
    if trigger_marker and t0 < 0 <= max(w.t[-1] for w in chans):
        t_trig = round((0 - t0) / tick)
        signals.append((next(ids), "wire 1", "trigger", [(0, "0"), (t_trig, "1")] if t_trig else [(0, "1")]))

    # merge into time order
    by_tick: Dict[int, List[str]] = {}
    for sid, kind, _, events in signals:
        real = kind.startswith("real")
        for tk, val in events:
            by_tick.setdefault(tk, []).append(f"{val} {sid}" if real else f"{val}{sid}")

    with open(path, "w", newline="\n") as f:
        f.write(f"$date {capture.stamp:%Y-%m-%d %H:%M:%S} $end\n")
        f.write("$version unit_scope (UNI-T oscilloscope capture) $end\n")
        f.write(f"$timescale {timescale} $end\n")
        f.write(f"$comment time/div {capture.time_scale:g} s; trigger {capture.state or '?'} on CH{capture.trigger.source} "
                f"({capture.trigger.slope}, {capture.trigger.level:g} V); trigger point at "
                f"{-t0:.9g} s into the file; analog values in volts $end\n")
        for note in header_notes:
            f.write(f"$comment {note} $end\n")
        f.write(f"$scope module {module} $end\n")
        for sid, kind, name, _ in signals:
            f.write(f"$var {kind} {sid} {name} $end\n")
        f.write("$upscope $end\n$enddefinitions $end\n")
        f.write("#0\n$dumpvars\n")
        for line in by_tick.get(0, []):
            f.write(line + "\n")
        f.write("$end\n")
        for tk in sorted(by_tick):
            if tk == 0:
                continue
            f.write(f"#{tk}\n")
            for line in by_tick[tk]:
                f.write(line + "\n")
    return path
