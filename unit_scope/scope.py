"""High-level, pyvisa-based driver for UNI-T oscilloscopes (UTD2000CEX / UTD7000B family).

Tested with a UNI-T DSO enumerating as ``USB0::0x5656::0x0832::<serial>::INSTR`` under NI-VISA.
The scope speaks a SCPI-like dialect over USBTMC with a few quirks this class hides:

* setting commands return nothing (reading after one just times out), and need a short
  settling time before the next command;
* only the exact long/short forms listed here are accepted (``TRIGger:SOURce`` works,
  ``TRIG:SOUR`` does not);
* ``WAV:DATA?`` returns a definite-length block of 16-bit words, the first sample of which
  is often a stray value.

Example::

    from unit_scope import UniTScope

    with UniTScope() as scope:                 # first USB instrument found
        print(scope.idn())
        scope.set_trigger(source=1, slope="falling", level=1.2)
        cap = scope.capture()                  # both enabled channels
        print(cap.channels[0].meas)            # Vpp, Vrms, frequency ...
        scope.save_screenshot("shots")         # screenshots/<datetime>.png
"""
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Union

import pyvisa

from .errors import ScopeCommandError, ScopeConnectionError
from .models import Capture, TriggerSettings, Waveform

# NI-VISA says "config not loaded" when no configuration file exists; defaults are fine.
warnings.filterwarnings("ignore", message=".*VI_WARN_CONFIG_NLOADED.*")

SLOPES_WRITE = {"rising": "POS", "falling": "NEG", "both": "ALT"}
SLOPES_READ = {"POSitive": "rising", "NEGative": "falling", "ALTernation": "both"}
SWEEPS_WRITE = {"auto": "AUTO", "normal": "NORMal", "single": "SINGle"}
SWEEPS_READ = {v.upper(): k for k, v in SWEEPS_WRITE.items()}
COUPLINGS = ("DC", "AC", "GND")
SCREEN_POINTS = 1600                      # samples of the screen record (2 per preamble point)
DIVISIONS = 16                           # horizontal divisions of the screen record
NO_DATA = 0x8000                         # sample value for "no data" in WAV:DATA?
SAMPLES_PER_POINT = 2                    # WAV:DATA? holds 2 samples per point of the preamble
SCALE_MIN, SCALE_MAX = 1e-3, 20.0       # accepted CHAN:SCAL range (V/div at the input)
PROBES = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0, 1000.0)

DEFAULT_PATTERN = "USB?*INSTR"


class UniTScope:
    """Connection to one UNI-T oscilloscope. Not thread-safe: use one instance per thread
    (or guard it with your own lock)."""

    def __init__(self, resource: Optional[str] = None, timeout_ms: int = 5000,
                 settle_s: float = 0.2, visa_library: Optional[str] = None):
        """
        resource      VISA resource string; ``None`` picks the first USB instrument.
        timeout_ms    I/O timeout.
        settle_s      pause after every setting command (the scope needs it).
        visa_library  passed to ``pyvisa.ResourceManager`` (``None`` = NI-VISA default,
                      ``"@py"`` = pyvisa-py).
        """
        self.resource = resource
        self.timeout_ms = timeout_ms
        self.settle_s = settle_s
        self.memory_settle_s = 0.1       # pause around the RAW-mode setup of read_memory (shorter than settle_s)
        self.visa_library = visa_library
        self._rm = None
        self._inst = None

    # ------------------------------------------------------------------ connection
    @staticmethod
    def list_resources(pattern: str = DEFAULT_PATTERN, visa_library: Optional[str] = None) -> List[str]:
        """VISA resource strings matching ``pattern`` (default: every USB instrument)."""
        try:
            rm = pyvisa.ResourceManager(visa_library) if visa_library else pyvisa.ResourceManager()
            return list(rm.list_resources(pattern))
        except Exception as e:
            raise ScopeConnectionError(f"VISA unavailable: {e}") from e

    @property
    def connected(self) -> bool:
        return self._inst is not None

    def connect(self) -> "UniTScope":
        if self._inst is not None:
            return self
        try:
            self._rm = pyvisa.ResourceManager(self.visa_library) if self.visa_library else pyvisa.ResourceManager()
            res = self.resource
            if res is None:
                found = self._rm.list_resources(DEFAULT_PATTERN)
                if not found:
                    raise ScopeConnectionError("No USB VISA instrument found (power, cable, NI-VISA?)")
                res = found[0]
            self._inst = self._rm.open_resource(res)
            self._inst.timeout = self.timeout_ms
            self._inst.chunk_size = 1 << 20
            self.resource = res
        except ScopeConnectionError:
            self._cleanup()
            raise
        except Exception as e:
            self._cleanup()
            raise ScopeConnectionError(f"Cannot open scope: {e}") from e
        return self

    def disconnect(self) -> None:
        self._cleanup()

    def _cleanup(self):
        for obj in (self._inst, self._rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._inst = self._rm = None

    def __enter__(self) -> "UniTScope":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # ------------------------------------------------------------------ raw I/O
    def _need(self):
        if self._inst is None:
            raise ScopeConnectionError("Not connected")
        return self._inst

    def write(self, cmd: str, settle: bool = False) -> None:
        """Send a command that produces no reply. ``settle`` adds the settling pause."""
        inst = self._need()
        try:
            inst.write(cmd)
        except pyvisa.VisaIOError as e:
            raise ScopeCommandError(f"{cmd!r}: {e}") from e
        if settle and self.settle_s:
            time.sleep(self.settle_s)

    def query(self, cmd: str, default: Optional[str] = None) -> str:
        """Send a query and return the stripped reply. With ``default`` given, a failed query
        (the scope times out on commands it does not know) returns it instead of raising."""
        inst = self._need()
        try:
            return inst.query(cmd).strip()
        except pyvisa.VisaIOError as e:
            if default is not None:
                return default
            raise ScopeCommandError(f"{cmd!r}: {e}") from e

    def query_float(self, cmd: str, default: Optional[float] = None) -> float:
        raw = self.query(cmd, None if default is None else str(default))
        try:
            return float(raw)
        except ValueError as e:
            raise ScopeCommandError(f"{cmd!r}: expected a number, got {raw!r}") from e

    def query_block(self, cmd: str) -> bytes:
        """Query returning an IEEE 488.2 definite-length block (``#<n><len><payload>``)."""
        inst = self._need()
        try:
            inst.write(cmd)
            raw = inst.read_raw()
        except pyvisa.VisaIOError as e:
            raise ScopeCommandError(f"{cmd!r}: {e}") from e
        if raw[:1] != b"#" or len(raw) < 3:
            raise ScopeCommandError(f"{cmd!r}: unexpected reply {raw[:32]!r}")
        n = int(raw[1:2])
        length = int(raw[2:2 + n])
        payload = raw[2 + n:2 + n + length]
        if len(payload) != length:
            raise ScopeCommandError(f"{cmd!r}: block truncated ({len(payload)} of {length} bytes)")
        return payload

    # ------------------------------------------------------------------ identity / run state
    def idn(self) -> str:
        """``*IDN?`` reply, e.g. ``UNI-TREND TECHNOLOGY,DSO,ADO2726190811,1.10.0060``."""
        return self.query("*IDN?")

    def run(self) -> None:
        self.write(":RUN")

    def stop(self) -> None:
        self.write(":STOP")

    def state(self) -> str:
        """Run/trigger state: ``TRIGED``, ``AUTO``, ``STOP``, ..."""
        return self.query("TRIG:STAT?", default="")

    # ------------------------------------------------------------------ display type
    def get_display_type(self) -> str:
        """``YT`` or ``XY12`` (CH1 horizontal, CH2 vertical); empty if the scope does not answer."""
        return self.query(":DISP:TYPE?", default="").upper()

    def set_display_type(self, display_type: str) -> None:
        """Switch the scope's own display between ``"YT"`` and ``"XY12"`` (also accepts ``"XY"``)."""
        t = display_type.upper()
        t = "XY12" if t == "XY" else t
        if t not in ("YT", "XY12"):
            raise ValueError(f"display type must be 'YT' or 'XY12', got {display_type!r}")
        self.write(f":DISP:TYPE {t}", settle=True)

    # ------------------------------------------------------------------ horizontal / vertical
    def get_time_scale(self) -> float:
        """Seconds per division."""
        return self.query_float("TIM:SCAL?", 5e-4)

    def set_time_scale(self, seconds_per_div: float) -> None:
        self.write(f"TIM:SCAL {seconds_per_div:.6g}", settle=True)

    def get_sample_info(self):
        """``(points, sample_rate)``: acquisition memory depth in points and the sample rate in Sa/s, e.g.
        ``(32000, 2e6)`` for 32 kpts at 2 MSa/s (64 kpts, with only CH1 on). The scope has no direct query for them, but in RAW
        waveform mode the preamble's X increment is 1/sample-rate and the read range ends at the depth;
        the mode is put back afterwards. Unknown values come back as 0."""
        mode = self.query("WAV:MODE?", "NORMal")
        try:
            self.write("WAV:MODE RAW")
            pre = self.query("WAV:PRE?", "").split(",")
            points = int(self.query_float("WAV:STOP?", 0))
            xinc0 = float(pre[4]) if len(pre) > 4 else 0.0
            if points <= 0 and xinc0 > 0:               # the register can read -1: use the 16-division window length
                points = int(round(DIVISIONS * self.get_time_scale() / xinc0))
        except (ScopeCommandError, ValueError):
            return 0, 0.0
        finally:
            try:
                self.write(f"WAV:MODE {mode}")
            except ScopeCommandError:
                pass
        try:
            xinc = float(pre[4])
        except (IndexError, ValueError):
            return points, 0.0
        return points, (1.0 / xinc if xinc > 0 else 0.0)

    def get_time_offset(self) -> float:
        """Horizontal trigger position in seconds: the screen centre shows t = offset (trigger at t = 0)."""
        return self.query_float("TIM:OFFS?", 0.0)

    def set_time_offset(self, seconds: float) -> None:
        self.write(f"TIM:OFFS {seconds:.6g}", settle=True)

    def channel_enabled(self, ch: int) -> bool:
        return self.query(f"CHAN{self._ch(ch)}:DISP?", "0") == "1"

    def get_scale(self, ch: int) -> float:
        """Volts per division."""
        return self.query_float(f"CHAN{self._ch(ch)}:SCAL?", 1.0)

    def get_offset(self, ch: int) -> float:
        """Vertical position as a voltage offset (the scope keeps it in volts, so it changes
        meaning when the V/div changes)."""
        return self.query_float(f"CHAN{self._ch(ch)}:OFFS?", 0.0)

    def get_coupling(self, ch: int) -> str:
        return self.query(f"CHAN{self._ch(ch)}:COUP?", "DC")

    def get_probe(self, ch: int) -> float:
        """Probe attenuation factor set on the scope (1.0 for 1X, 10.0 for 10X, ...)."""
        return self.query_float(f"CHAN{self._ch(ch)}:PROB?", 1.0)

    def set_channel(self, ch: int, scale: Optional[float] = None, offset: Optional[float] = None,
                    enabled: Optional[bool] = None, coupling: Optional[str] = None,
                    probe: Optional[float] = None) -> None:
        """Change any of V/div, offset (V), visibility, coupling and probe attenuation (1, 10, ...).
        ``scale`` is the displayed V/div (what ``get_scale`` returns, probe factor included). The probe is set
        before the V/div, because the scope rescales the displayed V/div when it changes; the offset is
        applied last so it is interpreted at the new V/div."""
        ch = self._ch(ch)
        if enabled is not None:
            self.write(f"CHAN{ch}:DISP {int(enabled)}", settle=True)
        if coupling is not None:
            if coupling.upper() not in COUPLINGS:
                raise ValueError(f"coupling must be one of {COUPLINGS}")
            self.write(f"CHAN{ch}:COUP {coupling.upper()}", settle=True)
        if probe is not None:
            if probe not in PROBES:
                raise ValueError(f"probe must be one of {PROBES}")
            self.write(f"CHAN{ch}:PROB {probe:g}X", settle=True)
        if scale is not None:
            # CHAN:SCAL takes V/div at the input, but CHAN:SCAL? reports it times the probe factor
            # (a 10X probe: write 0.5 -> reads 5 V/div). ``scale`` is the displayed V/div, so divide the
            # probe out; the scope silently falls back to 1 mV/div for a value it rejects, so clamp first.
            factor = probe if probe is not None else self.get_probe(ch)
            raw = min(max(scale / factor, SCALE_MIN), SCALE_MAX)
            self.write(f"CHAN{ch}:SCAL {raw:.6g}", settle=True)
        if offset is not None:
            self.write(f"CHAN{ch}:OFFS {offset:.3f}", settle=True)

    # ------------------------------------------------------------------ trigger
    def get_trigger(self) -> TriggerSettings:
        src = self.query("TRIGger:SOURce?", "CHANnel1")
        return TriggerSettings(
            source=2 if src.endswith("2") else 1,
            slope=SLOPES_READ.get(self.query("TRIG:EDGE:SLOPe?", "POSitive"), "rising"),
            level=self.query_float("TRIG:LEVel?", 0.0),
            sweep=SWEEPS_READ.get(self.query("TRIG:SWEep?", "AUTO").upper(), "auto"))

    def set_trigger(self, source: Optional[int] = None, slope: Optional[str] = None,
                    level: Optional[float] = None, sweep: Optional[str] = None) -> None:
        """Any argument left ``None`` is not touched. ``slope``: rising/falling/both,
        ``sweep``: auto/normal/single, ``level`` in volts (the scope rounds it to its ADC step)."""
        if source is not None:
            self.write(f"TRIGger:SOURce CHANnel{self._ch(source)}", settle=True)
        if slope is not None:
            if slope not in SLOPES_WRITE:
                raise ValueError(f"slope must be one of {tuple(SLOPES_WRITE)}")
            self.write(f"TRIG:EDGE:SLOPe {SLOPES_WRITE[slope]}", settle=True)
        if sweep is not None:
            if sweep not in SWEEPS_WRITE:
                raise ValueError(f"sweep must be one of {tuple(SWEEPS_WRITE)}")
            self.write(f"TRIG:SWEep {SWEEPS_WRITE[sweep]}", settle=True)
        if level is not None:
            self.write(f"TRIG:LEVel {level:.3f}", settle=True)

    # ------------------------------------------------------------------ waveform / capture
    def read_waveform(self, ch: int, restore_source: bool = True) -> Waveform:
        """Fetch the displayed record of one channel (about 1600 samples spanning 16 divisions).
        Volts = (raw - yorigin - yref) * yinc from the preamble; time is centred on the trigger."""
        ch = self._ch(ch)
        orig = self.query("WAV:SOUR?", "CHANnel1") if restore_source else None
        try:
            self.write(f"WAV:SOUR CHANnel{ch}")
            pre = self.query("WAV:PRE?").split(",")
            if len(pre) < 10:
                raise ScopeCommandError(f"WAV:PRE? returned {len(pre)} fields, expected 10")
            points, xinc, xorig, xref = int(pre[2]), float(pre[4]), float(pre[5]), float(pre[6])
            yinc, yorig, yref = float(pre[7]), float(pre[8]), float(pre[9])
            # The scope may be set to return only part of the record (WAV:STOP 480 gives 960 of the 1600
            # samples); the time axis below assumes the whole 16-division record, so ask for all of it.
            self.write("WAV:STAR 0")
            self.write(f"WAV:STOP {points}")
            body = self.query_block("WAV:DATA?")
        finally:
            if orig is not None:
                try:
                    self.write(f"WAV:SOUR {orig}")
                except ScopeCommandError:
                    pass
        samples = [int.from_bytes(body[k:k + 2], "little") for k in range(0, len(body) - 1, 2)]
        # 0x8000 marks "no data": the not yet acquired tail of a slow (roll-mode) sweep, or the part before
        # the stored memory when a stopped scope is zoomed out. Keep only the real samples.
        lead = 0
        while lead < len(samples) and samples[lead] == NO_DATA:
            lead += 1
        trail = 0
        while trail < len(samples) - lead and samples[len(samples) - 1 - trail] == NO_DATA:
            trail += 1
        valid = samples[lead:len(samples) - trail]
        full = SAMPLES_PER_POINT * points
        missing = max(0, full - len(valid))
        if not valid:
            # The scope also sends an empty block for a trace clipped at its ADC range; that is not a failure
            # of the link, so hand back an empty record (the GUI flags it) instead of raising.
            return Waveform(channel=ch, t=[], volts=[], scale=self.get_scale(ch), offset=self.get_offset(ch),
                            coupling=self.get_coupling(ch), probe=self.get_probe(ch), missing=full)
        dt = xinc / SAMPLES_PER_POINT           # the block holds 2 samples per preamble point
        base = yorig + yref
        # xorig is the time of the first real sample (also when padding precedes it)
        return Waveform(
            channel=ch,
            t=[xorig + (k - xref) * dt for k in range(len(valid))],
            volts=[(x - base) * yinc for x in valid],
            scale=self.get_scale(ch), offset=self.get_offset(ch),
            coupling=self.get_coupling(ch), probe=self.get_probe(ch), missing=missing)

    def read_memory(self, ch: int, points: Optional[int] = None) -> Waveform:
        """The raw acquisition memory of one channel instead of the 1600-point screen record: 64 kpts with only one
        channel enabled, 32 kpts per channel with both (the datasheet's 64 kpts are shared).

        It spans the same 16 divisions, so the frequency resolution of an FFT is the same, but it holds
        ``points / 1600`` times more samples (2 MSa/s at 1 ms/div, 200 kSa/s at 10 ms/div): a much wider span,
        and none of the scope's own decimation. Works while the scope is running or stopped. ``t`` is relative to
        the trigger, like :meth:`read_waveform`."""
        return self.read_memories([ch], points)[0]

    def read_memories(self, channels: Sequence[int], points: Optional[int] = None) -> List[Waveform]:
        """:meth:`read_memory` for several channels in one RAW-mode session (the mode and range are set once)."""
        chs = [self._ch(c) for c in channels]
        src = self.query("WAV:SOUR?", "CHANnel1")
        mode = self.query("WAV:MODE?", "NORMal")
        poin = self.query("WAV:POIN?", "800")
        raw = []
        try:
            self.write("WAV:MODE RAW")
            time.sleep(self.memory_settle_s)
            pre = self.query("WAV:PRE?").split(",")
            if len(pre) < 10 or float(pre[4]) <= 0:
                raise ScopeCommandError(f"WAV:PRE? returned {pre!r} in RAW mode")
            # samples in the 16-division window = 16 x time/div / sample interval (WAV:STOP? is not reliable)
            window = int(round(DIVISIONS * self.get_time_scale() / float(pre[4])))
            n = min(points, window) if points else window
            if n < 1:
                raise ScopeCommandError("scope reports no acquisition memory")
            for ch in chs:
                # the source must be chosen before the range: changing it afterwards makes WAV:DATA? answer nothing
                self.write(f"WAV:SOUR CHANnel{ch}")
                self.write(f"WAV:POIN {n}")
                self.write("WAV:STAR 0")
                self.write(f"WAV:STOP {n}")
                time.sleep(self.memory_settle_s)           # the scope needs a moment to prepare the long block
                pre = self.query("WAV:PRE?").split(",")
                if len(pre) < 10:
                    raise ScopeCommandError(f"WAV:PRE? returned {len(pre)} fields, expected 10")
                raw.append((pre, self.query_block("WAV:DATA?")))
        finally:
            for cmd in (f"WAV:POIN {poin}", f"WAV:MODE {mode}", f"WAV:SOUR {src}"):
                try:
                    self.write(cmd)
                except ScopeCommandError:
                    pass
        out = []
        for ch, (pre, body) in zip(chs, raw):
            xinc, xorig = float(pre[4]), float(pre[5])
            yinc, yorig, yref = float(pre[7]), float(pre[8]), float(pre[9])
            samples = [int.from_bytes(body[k:k + 2], "little") for k in range(0, len(body) - 1, 2)]
            valid = [x for x in samples if x != NO_DATA]
            base = yorig + yref
            out.append(Waveform(
                channel=ch, t=[xorig + k * xinc for k in range(len(valid))], volts=[(x - base) * yinc for x in valid],
                scale=self.get_scale(ch), offset=self.get_offset(ch), coupling=self.get_coupling(ch),
                probe=self.get_probe(ch), missing=max(0, n - len(valid))))
        return out

    def capture(self, channels: Sequence[int] = (1, 2), only_enabled: bool = True, memory: bool = False) -> Capture:
        """Waveforms of ``channels`` plus the settings needed to interpret them. Channels are read
        one after another, so stop the acquisition first if they must be time-aligned. With ``memory`` the
        waveforms are the raw acquisition memory (32-64 kpts, see :meth:`read_memory`) instead of the
        1600-point screen record."""
        points, rate = self.get_sample_info()
        cap = Capture(time_scale=self.get_time_scale(), time_offset=self.get_time_offset(), state=self.state(),
                      trigger=self.get_trigger(), points=points, sample_rate=rate)
        if memory:
            wanted = [ch for ch in channels if not only_enabled or self.channel_enabled(ch)]
            if wanted:
                for w in self.read_memories(wanted):
                    if len(w.volts) < SCREEN_POINTS:        # e.g. only CH2 enabled: no deep memory, only 800 points
                        orig = self.query("WAV:SOUR?", "CHANnel1")
                        try:
                            w = self.read_waveform(w.channel, restore_source=False)
                        finally:
                            self.write(f"WAV:SOUR {orig}")
                    cap.channels.append(w)
            w0 = cap.channels[0] if cap.channels else None
            if w0 is not None and len(w0.volts) > SCREEN_POINTS and w0.t[1] > w0.t[0]:
                # the record itself is the truth about depth and rate (the scope's own answer can lag a channel change)
                cap.points = len(w0.volts) + w0.missing
                cap.sample_rate = 1.0 / (w0.t[1] - w0.t[0])
            return cap
        orig = self.query("WAV:SOUR?", "CHANnel1")
        try:
            for ch in channels:
                if only_enabled and not self.channel_enabled(ch):
                    continue
                cap.channels.append(self.read_waveform(ch, restore_source=False))
        finally:
            try:
                self.write(f"WAV:SOUR {orig}")
            except ScopeCommandError:
                pass
        return cap

    # ------------------------------------------------------------------ screenshot
    def screenshot_bmp(self) -> bytes:
        """The scope's current screen as a BMP file (800x480, includes menus)."""
        bmp = self.query_block("DISP:DATA?")
        if bmp[:2] != b"BM":
            raise ScopeCommandError("DISP:DATA? did not return a BMP")
        return bmp

    def screenshot_image(self):
        """The screen as a ``PIL.Image`` (needs Pillow)."""
        import io
        from PIL import Image
        return Image.open(io.BytesIO(self.screenshot_bmp())).convert("RGB")

    def save_screenshot(self, directory: Union[str, Path] = "screenshots",
                        filename: Optional[str] = None) -> Path:
        """Save the screen as PNG; the default name is the current date and time."""
        path = Path(directory) / (filename or datetime.now().strftime("%Y-%m-%d_%H-%M-%S.png"))
        path.parent.mkdir(parents=True, exist_ok=True)
        self.screenshot_image().save(path)
        return path

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _ch(ch: int) -> int:
        if ch not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        return ch
