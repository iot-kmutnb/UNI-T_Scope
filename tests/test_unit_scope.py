"""Hardware-free tests: a fake VISA instrument stands in for the scope.

Run:  python -m unittest discover -s tests -v
"""
import io
import cmath
import math
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pyvisa
from pyvisa.constants import StatusCode

from unit_scope import (Capture, ScopeCommandError, ScopeConnectionError, ScopeError, UniTScope, Waveform,
                        export, measure, value_at)
from unit_scope.export import _timescale
from unit_scope import spectrum as spec

N = 1600
XINC, XORIG = 1e-5, -4e-3          # preamble: 800 points x 10 us, centred on the trigger


def square_block(high=205, low=128, period=200, lead=0, trail=0, total=N):
    """WAV:DATA? payload: ``total`` 16-bit words, a square wave; ``lead``/``trail`` words of 0x8000 ("no data")."""
    words = [0x8000] * lead + [high if (k % period) < period // 2 else low for k in range(total - lead - trail)]         + [0x8000] * trail
    body = struct.pack(f"<{len(words)}H", *words)
    return b"#9" + f"{len(body):09d}".encode() + body


class FakeInst:
    def __init__(self):
        self.sent = []
        self.pending = None
        self.timeout = None
        self.chunk_size = None
        self.source = "CHANnel1"
        self.block = None                                # WAV:DATA? payload override
        self.blocks = []                                 # payloads to return one after another (then ``block``)
        self.replies = {
            "*IDN?": "UNI-TREND TECHNOLOGY,DSO,FAKE0001,1.10.0060\r\n",
            "TRIG:STAT?": "TRIGED\r\n", "TIM:SCAL?": "5.000000e-04\r\n",
            "TRIGger:SOURce?": "CHANnel2\r\n", "TRIG:EDGE:SLOPe?": "NEGative\r\n",
            "TRIG:LEVel?": "1.480000e+00\r\n", "TRIG:SWEep?": "NORMal\r\n",
            "CHAN1:DISP?": "1\r\n", "CHAN2:DISP?": "0\r\n",
            "CHAN1:SCAL?": "1.000000e+00\r\n", "CHAN1:OFFS?": "-3.040000e+00\r\n",
            "CHAN1:COUP?": "DC\r\n", "CHAN1:PROB?": "1.000000e+00\r\n",
            "WAV:MODE?": "NORMal\r\n", "WAV:STOP?": "32000\r\n", "WAV:POIN?": "800\r\n",
            "WAV:PRE?": f"WORD,NORMal,800,1,{XINC:e},{XORIG:e},0,4.000000e-02,0,128\r\n",
        }

    def write(self, cmd):
        self.sent.append(cmd)
        self.pending = cmd
        if cmd.startswith("WAV:SOUR "):
            self.source = cmd.split()[1]

    def query(self, cmd):
        self.sent.append(cmd)
        if cmd == "WAV:SOUR?":
            return self.source + "\r\n"
        if cmd not in self.replies:                      # the real scope just times out
            raise pyvisa.VisaIOError(StatusCode.error_timeout)
        return self.replies[cmd]

    def read_raw(self):
        if self.pending == "WAV:DATA?":
            return self.blocks.pop(0) if self.blocks else (self.block or square_block())
        if self.pending == "DISP:DATA?":
            return b"#8" + b"00000004" + b"BM\x00\x00"
        raise pyvisa.VisaIOError(StatusCode.error_timeout)

    def close(self):
        pass


class FakeRM:
    inst = None

    def __init__(self, *a):
        pass

    def list_resources(self, pattern="?*::INSTR"):
        return ("USB0::0x5656::0x0832::FAKE0001::INSTR",)

    def open_resource(self, res):
        FakeRM.inst = FakeInst()
        return FakeRM.inst

    def close(self):
        pass


def fake_scope():
    with mock.patch("unit_scope.scope.pyvisa.ResourceManager", FakeRM):
        return UniTScope(settle_s=0).connect()


class ScopeClassTests(unittest.TestCase):
    def test_connect_and_idn(self):
        s = fake_scope()
        self.assertTrue(s.connected)
        self.assertIn("FAKE0001", s.idn())
        s.disconnect()
        self.assertFalse(s.connected)

    def test_context_manager_and_errors_when_disconnected(self):
        s = UniTScope()
        with self.assertRaises(ScopeConnectionError):
            s.idn()
        with mock.patch("unit_scope.scope.pyvisa.ResourceManager", FakeRM):
            with UniTScope(settle_s=0) as sc:
                self.assertTrue(sc.connected)
            self.assertFalse(sc.connected)

    def test_set_trigger_sends_expected_commands(self):
        s = fake_scope()
        s.set_trigger(source=2, slope="falling", level=1.234)
        self.assertEqual(FakeRM.inst.sent, ["TRIGger:SOURce CHANnel2", "TRIG:EDGE:SLOPe NEG", "TRIG:LEVel 1.234"])
        FakeRM.inst.sent.clear()
        s.set_trigger(slope="both", sweep="single")
        self.assertEqual(FakeRM.inst.sent, ["TRIG:EDGE:SLOPe ALT", "TRIG:SWEep SINGle"])

    def test_invalid_arguments(self):
        s = fake_scope()
        with self.assertRaises(ValueError):
            s.set_trigger(slope="sideways")
        with self.assertRaises(ValueError):
            s.set_channel(3, scale=1)
        with self.assertRaises(ValueError):
            s.set_channel(1, coupling="XX")

    def test_get_trigger_maps_scope_words(self):
        t = fake_scope().get_trigger()
        self.assertEqual((t.source, t.slope, t.level, t.sweep), (2, "falling", 1.48, "normal"))

    def test_set_channel_and_timebase(self):
        s = fake_scope()
        s.set_channel(1, scale=0.5, offset=-1.5)
        s.set_time_scale(2e-4)
        self.assertEqual(FakeRM.inst.sent, ["CHAN1:PROB?", "CHAN1:SCAL 0.5", "CHAN1:OFFS -1.500", "TIM:SCAL 0.0002"])

    def test_set_coupling(self):
        s = fake_scope()
        for given, sent in (("dc", "DC"), ("AC", "AC"), ("gnd", "GND")):
            FakeRM.inst.sent.clear()
            s.set_channel(2, coupling=given)
            self.assertEqual(FakeRM.inst.sent, [f"CHAN2:COUP {sent}"])

    def test_probe_attenuation(self):
        s = fake_scope()
        self.assertEqual(s.get_probe(1), 1.0)
        FakeRM.inst.sent.clear()
        s.set_channel(1, probe=10, scale=5)
        self.assertEqual(FakeRM.inst.sent, ["CHAN1:PROB 10X", "CHAN1:SCAL 0.5"])   # probe first; V/div is per input
        with self.assertRaises(ValueError):
            s.set_channel(1, probe=3)

    def test_scale_is_displayed_volts_per_div(self):
        s = fake_scope()
        FakeRM.inst.sent.clear()
        s.set_channel(1, scale=2.0)                      # fake reports a 1X probe: sent as is
        s.set_channel(1, scale=50, probe=10)             # 10X: 50 V/div displayed = 5 V/div at the input
        s.set_channel(1, scale=500, probe=1)             # beyond the scope's range: clamped, not rejected
        s.set_channel(1, scale=1e-5, probe=1)
        self.assertEqual([c for c in FakeRM.inst.sent if "SCAL" in c],
                         ["CHAN1:SCAL 2", "CHAN1:SCAL 5", "CHAN1:SCAL 20", "CHAN1:SCAL 0.001"])

    def test_sample_info(self):
        s = fake_scope()
        FakeRM.inst.sent.clear()
        points, rate = s.get_sample_info()
        self.assertEqual(points, 32000)
        self.assertAlmostEqual(rate, 1 / XINC)                     # preamble X increment = 1 / sample rate
        self.assertEqual([c for c in FakeRM.inst.sent if c.startswith("WAV:MODE ")],
                         ["WAV:MODE RAW", "WAV:MODE NORMal"])     # RAW only for the read, then restored
        cap = s.capture()
        self.assertEqual((cap.points, cap.sample_rate), (32000, 1 / XINC))

    def test_sample_info_unknown(self):
        s = fake_scope()
        del FakeRM.inst.replies["WAV:STOP?"], FakeRM.inst.replies["WAV:PRE?"]
        self.assertEqual(s.get_sample_info(), (0, 0.0))

    def test_no_data_padding_is_dropped(self):
        s = fake_scope()
        dt = XINC / 2                                   # 2 samples per preamble point
        FakeRM.inst.block = square_block(trail=600)     # slow sweep still filling: the tail is not acquired yet
        w = s.read_waveform(1)
        self.assertEqual((len(w.volts), w.missing), (1000, 600))
        self.assertAlmostEqual(w.t[0], XORIG)
        self.assertAlmostEqual(w.t[-1], XORIG + 999 * dt)
        self.assertLess(max(w.volts), 10)               # no 0x8000 spikes
        FakeRM.inst.block = square_block(lead=400, total=1200)   # stopped scope zoomed out: padding comes first
        w = s.read_waveform(1)
        self.assertEqual((len(w.volts), w.missing), (800, 800))
        self.assertAlmostEqual(w.t[0], XORIG)           # xorig is the time of the first real sample
        self.assertAlmostEqual(w.t[1] - w.t[0], dt)
        FakeRM.inst.block = square_block(lead=1600)     # nothing at all
        w = s.read_waveform(1)
        self.assertEqual((w.t, w.volts, w.missing), ([], [], 1600))
        FakeRM.inst.block = None
        self.assertEqual(s.read_waveform(1).missing, 0)

    def test_read_memory(self):
        s = fake_scope()
        FakeRM.inst.replies["TIM:SCAL?"] = "1.250000e-03" + chr(13) + chr(10)    # 16 div x 1.25 ms / 10 us = 2000 samples
        FakeRM.inst.block = square_block(total=2000)
        FakeRM.inst.sent.clear()
        w = s.read_memory(1)
        self.assertEqual(len(w.volts), 2000)
        self.assertAlmostEqual(w.t[0], XORIG)
        self.assertAlmostEqual(w.t[1] - w.t[0], XINC)               # RAW mode: one sample per X increment
        cmds = [c for c in FakeRM.inst.sent if c.startswith(("WAV:MODE ", "WAV:POIN ", "WAV:STOP "))]
        self.assertEqual(cmds, ["WAV:MODE RAW", "WAV:POIN 2000", "WAV:STOP 2000", "WAV:POIN 800", "WAV:MODE NORMal"])
        FakeRM.inst.block = square_block(total=1000, lead=100)      # no-data padding is dropped here too
        w = s.read_memory(1, points=1000)                           # fewer points than the window: asks for 1000
        self.assertEqual((len(w.volts), w.missing), (900, 100))
        FakeRM.inst.block = None
        self.assertEqual(len(s.read_memory(1, points=1600).volts), 1600)
        FakeRM.inst.replies["WAV:STOP?"] = "-1" + chr(13) + chr(10)                  # a register that reads -1 is not used
        self.assertEqual(s.get_sample_info(), (2000, 1 / XINC))

    def test_capture_from_memory(self):
        s = fake_scope()
        FakeRM.inst.replies["TIM:SCAL?"] = "1.250000e-03" + chr(13) + chr(10)    # 16 div x 1.25 ms / 10 us = 2000 samples
        FakeRM.inst.block = square_block(total=2000)
        FakeRM.inst.sent.clear()
        cap = s.capture(memory=True)
        self.assertEqual([(w.channel, len(w.volts)) for w in cap.channels], [(1, 2000)])   # only CH1 is enabled
        self.assertAlmostEqual(cap.channels[0].t[1] - cap.channels[0].t[0], XINC)
        self.assertEqual(cap.points, 2000)                           # depth and rate come from the record itself
        self.assertAlmostEqual(cap.sample_rate, 1 / XINC)
        self.assertEqual(FakeRM.inst.sent.count("WAV:MODE RAW"), 2)  # sample-info query + one session for all channels
        self.assertIn("WAV:MODE NORMal", FakeRM.inst.sent)           # normal mode restored afterwards
        FakeRM.inst.block = None
        self.assertEqual(len(s.capture().channels[0].volts), 1600)  # the default is still the screen record

    def test_memory_capture_falls_back_to_the_screen_record_without_deep_memory(self):
        s = fake_scope()
        # only CH2 enabled on the real scope: RAW mode then holds just 800 points; the screen record is better
        FakeRM.inst.blocks = [square_block(total=800), square_block(total=1600)]
        cap = s.capture(memory=True)
        self.assertEqual([len(w.volts) for w in cap.channels], [1600])
        self.assertEqual(FakeRM.inst.blocks, [])

    def test_query_default_and_error(self):
        s = fake_scope()
        self.assertEqual(s.query("NOPE?", default="x"), "x")
        with self.assertRaises(ScopeCommandError):
            s.query("NOPE?")

    def test_capture_skips_disabled_channel_and_restores_source(self):
        s = fake_scope()
        cap = s.capture()
        self.assertEqual([w.channel for w in cap.channels], [1])         # CH2 reports DISP 0
        self.assertEqual(FakeRM.inst.source, "CHANnel1")                 # source restored
        self.assertEqual((cap.state, cap.time_scale), ("TRIGED", 5e-4))
        self.assertEqual(cap.trigger.source, 2)
        w = cap.channel(1)
        self.assertEqual(len(w.t), N)
        self.assertAlmostEqual(w.t[0], XORIG)
        self.assertAlmostEqual(w.t[-1], XORIG + (N - 1) * (XINC * 800 / N))
        self.assertAlmostEqual(max(w.volts), (205 - 128) * 0.04)         # (raw - yorigin - yref) * yinc
        self.assertAlmostEqual(min(w.volts), 0.0)
        self.assertEqual((w.scale, w.offset, w.coupling), (1.0, -3.04, "DC"))

    def test_waveform_measurements(self):
        w = fake_scope().capture().channel(1)
        m = w.meas
        self.assertAlmostEqual(m.vpp, 3.08, places=2)
        self.assertAlmostEqual(m.freq, 1 / (200 * XINC * 800 / N), delta=5)   # 200 samples per period
        self.assertIs(w.meas, m)                                              # cached

    def test_screenshot_bmp(self):
        self.assertEqual(fake_scope().screenshot_bmp()[:2], b"BM")


class MeasureTests(unittest.TestCase):
    t = [-4e-3 + k * 5e-6 for k in range(N)]

    def test_sine_with_dc(self):
        v = [1 + math.sin(2 * math.pi * 1000 * x) for x in self.t]
        m = measure(self.t, v, 0.08)
        self.assertAlmostEqual(m.vpp, 2.0, places=2)
        self.assertAlmostEqual(m.freq, 1000.0, delta=1)
        self.assertAlmostEqual(m.vrms, math.sqrt(1.5), places=2)

    def test_not_periodic(self):
        for v in ([1.5] * N, [3.0 if abs(x) < 5e-4 else 0.0 for x in self.t]):
            m = measure(self.t, v, 0.08)
            self.assertIsNone(m.freq)
            self.assertIsNone(m.period)

    def test_value_at(self):
        self.assertEqual(value_at([0, 1, 2], [0, 10, 20], 0.5), 5.0)
        self.assertIsNone(value_at([0, 1, 2], [0, 10, 20], 2.1))


def parse_vcd(text):
    """Tiny strict VCD reader: returns (timescale, {name: (kind, [(tick, value)])})."""
    lines = text.splitlines()
    ids, scale, i = {}, None, 0
    while i < len(lines):
        parts = lines[i].split()
        i += 1
        if parts[:1] == ["$timescale"]:
            scale = " ".join(parts[1:3])
        elif parts[:1] == ["$var"]:
            ids[parts[3]] = (parts[1], parts[4], [])
        elif parts[:1] == ["$enddefinitions"]:
            break
    t, last = 0, -1
    for line in lines[i:]:
        line = line.strip()
        if not line or line in ("$dumpvars", "$end"):
            continue
        if line[0] == "#":
            t = int(line[1:])
            assert t > last or t == 0, "time must increase"
            last = t
        elif line[0] == "r":
            val, sid = line[1:].split(" ")
            ids[sid][2].append((t, float(val)))
        else:
            ids[line[1:]][2].append((t, int(line[0])))
    return scale, {name: (kind, ev) for kind, name, ev in ids.values()}


def sample_capture(period=200):
    t = [-4e-3 + k * 5e-6 for k in range(N)]
    v = [3.2 if (k % period) < period // 2 else 0.0 for k in range(N)]
    return Capture(channels=[Waveform(1, t, v, scale=1.0, offset=-3.0), Waveform(2, t, [x / 2 for x in v], scale=2.0)],
                   time_scale=5e-4, state="TRIGED")


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_csv(self):
        p = export.export(sample_capture(), Path(self.dir.name) / "a.csv")
        rows = p.read_text().splitlines()
        self.assertEqual(rows[0], "time_s,CH1_V,CH2_V")
        self.assertEqual(len(rows), N + 1)
        self.assertEqual(rows[1].split(",")[1:], ["3.2", "1.6"])

    def test_vcd_structure(self):
        p = export.export(sample_capture(), Path(self.dir.name) / "a.vcd")
        scale, sig = parse_vcd(p.read_text())
        self.assertEqual(scale, "100 ns")
        self.assertEqual(sorted(sig), ["CH1", "CH1_dig", "CH2", "CH2_dig", "trigger"])
        self.assertEqual(sig["CH1"][0], "real")
        self.assertEqual(sig["CH1"][1][0], (0, 3.2))
        # 5 us per sample at 100 ns/tick = 50 ticks; first fall after 100 samples = 5000 ticks
        self.assertEqual(sig["CH1"][1][1], (5000, 0.0))
        self.assertEqual(sig["trigger"][1], [(0, 0), (40000, 1)])       # t = 0 is 4 ms = 40000 ticks into the file
        dig = [v for _, v in sig["CH1_dig"][1]]
        self.assertEqual(dig[:4], [1, 0, 1, 0])

    def test_vcd_options(self):
        p = export.write_vcd(sample_capture(), Path(self.dir.name) / "b.vcd", digital=False, trigger_marker=False)
        _, sig = parse_vcd(p.read_text())
        self.assertEqual(sorted(sig), ["CH1", "CH2"])

    def test_flat_channel_has_no_digital_trace(self):
        cap = sample_capture()
        cap.channels[1].volts = [1.0] * N
        _, sig = parse_vcd(export.export(cap, Path(self.dir.name) / "c.vcd").read_text())
        self.assertIn("CH1_dig", sig)
        self.assertNotIn("CH2_dig", sig)

    def test_timescale_choice(self):
        self.assertEqual(_timescale(5e-6)[0], "100 ns")
        self.assertEqual(_timescale(2e-7)[0], "10 ns")      # the 20 us/div live capture
        self.assertEqual(_timescale(1e-9)[0], "100 ps")
        self.assertEqual(_timescale(1e-3)[0], "100 us")

    def test_errors(self):
        with self.assertRaises(ScopeError):
            export.export(sample_capture(), Path(self.dir.name) / "a.xyz")
        with self.assertRaises(ScopeError):
            export.export(Capture(), Path(self.dir.name) / "a.vcd")


class VersionTests(unittest.TestCase):
    def test_package_and_pyproject_versions_match(self):
        import re
        import unit_scope
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        declared = re.search(r'^version = "([^"]+)"', text, re.M).group(1)
        self.assertEqual(unit_scope.__version__, declared)
        self.assertRegex(declared, r"^\d+\.\d+\.\d+$")                    # major.minor.patch

    def test_command_line_reports_the_version(self):
        import contextlib
        import io
        import unit_scope
        from unit_scope import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(unit_scope.__version__, out.getvalue())


if __name__ == "__main__":
    unittest.main()


class SpectrumTests(unittest.TestCase):
    FS = 100000.0                      # 1 ms/div: 100 samples per division

    def sine(self, f, amp=1.0, n=1600, dc=0.0):
        return [dc + amp * math.sin(2 * math.pi * f * i / self.FS) for i in range(n)]

    def test_fft_matches_direct_dft(self):
        x = [math.sin(0.3 * k) + 0.5 * math.cos(1.1 * k) for k in range(64)]
        got = spec.fft(x)
        for k in (0, 1, 5, 17, 32, 63):
            direct = sum(x[n] * cmath.exp(-2j * math.pi * k * n / 64) for n in range(64))
            self.assertAlmostEqual(abs(got[k] - direct), 0, places=9)
        with self.assertRaises(ValueError):
            spec.fft([0.0] * 100)

    def test_peak_frequency_and_amplitude_for_every_window(self):
        for name in spec.WINDOWS:
            for f in (1000.0, 1234.5, 7777.0):
                s = spec.spectrum(self.sine(f, amp=1.0, dc=1.0), 1 / self.FS, name)
                # rect: nearest bin, no interpolation; flat top: exact amplitude but a wide, flat peak
                tol = {"rect": 0.6 * s.df, "flattop": 0.15 * s.rbw}.get(name, 0.02 * s.rbw + 1.0)
                self.assertLess(abs(s.peak_freq - f), tol, (name, f, s.peak_freq))
                self.assertLess(abs(s.peak_amp - 1.0), 0.06 if name != "rect" else 0.25, (name, f, s.peak_amp))

    def test_axis_and_resolution(self):
        s = spec.spectrum(self.sine(1000), 1 / self.FS)
        self.assertEqual((s.n, s.nfft), (1600, 2048))               # zero padded to a power of two
        self.assertAlmostEqual(s.nyquist, 50000.0)                  # 50 / (time/div)
        self.assertAlmostEqual(s.rbw, self.FS / 1600)               # 1 / (16 * time/div) = 62.5 Hz
        self.assertAlmostEqual(s.freqs[1], self.FS / 2048)
        self.assertEqual(len(s.freqs), len(s.amps))

    def test_dc_is_removed_and_two_tones_resolve(self):
        x = [a + b + 2.5 for a, b in zip(self.sine(2000, 1.0), self.sine(5000, 0.25))]
        s = spec.spectrum(x, 1 / self.FS, "hann")
        self.assertLess(s.amps[0], 0.01)                            # the 2.5 V offset does not show
        near = lambda f: max(s.amps[int(f / s.df) - 2:int(f / s.df) + 3])
        self.assertAlmostEqual(near(2000), 1.0, delta=0.08)
        self.assertAlmostEqual(near(5000), 0.25, delta=0.03)
        self.assertAlmostEqual(spec.to_dbv(1 / math.sqrt(2)), -3.0103, places=3)

    def test_short_or_bad_input(self):
        self.assertIsNone(spec.spectrum([1.0] * 5, 1e-5))
        self.assertIsNone(spec.spectrum([1.0] * 100, 0.0))
        with self.assertRaises(ValueError):
            spec.window("bogus", 10)
