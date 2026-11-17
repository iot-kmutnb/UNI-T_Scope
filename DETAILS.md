# UNI-T Scope Tools - technical details

The full reference behind the short [README](README.md): library API, scope quirks, how the measurements are made, every GUI control, the FFT and record modes, export formats.

A pyvisa library, a command line tool and a PyQt6 panel for UNI-T UTD2000CEX / UTD7000B-class oscilloscopes.
Tested with a UNI-T DSO (`UNI-TREND TECHNOLOGY,DSO,...,1.10.0060`, USB `5656:0832`), 800x480 screen.

```
unit_scope/       reusable library + CLI (pyvisa only; no GUI imports)
scope_gui/        PyQt6 panel built on unit_scope
scripts/          install.ps1 / cleanup.ps1 (+ .bat): build and remove the Python environment
tests/            hardware-free unit tests (fake VISA instrument)
docs/             UNI-T manuals and datasheet (UNI-T's copyright: not in the repository, see below)
UNI-T_SW/         UNI-T Device Manager v2.9 and SDK v2.3 archives (vendor software)
```

A two-channel test-signal generator for an ESP32 (used to test this project) lives in its own project, `esp32_dual_wave`, next to this folder.

The UNI-T documents the code was written from are not part of the repository (they are UNI-T's copyright). Download them from UNI-T's website and put them in `docs/`: *UTD2000CEX+ Series Digital Oscilloscopes* (datasheet), *UTD2000CEX&UTD7000B Series Oscilloscope Programming Manual* and *UTD2000E Series Programmable Digital Oscilloscope* (programming manual). The SCPI commands used here come from them.

## Requirements
- **Windows** and **Python 3.9+**. The library, the CLI and `scope_gui` were developed and tested on **Windows 11** (Home, build 10.0.26200) with Python 3.14.
- **NI-VISA Runtime 15.5** (`NI-VISA 15.5 Runtime`, installed version 15.5.0). It comes with the **UNI-T Device Manager v2.9** installer (the Device Manager v2.9 download -> `Device Manager Installer_V2.9.exe` bundles the NI-VISA 15.5 Runtime), so installing Device Manager v2.9 is enough; the runtime can also be installed on its own from National Instruments. The scope enumerates as a USBTMC device (`USB Test and Measurement Device (IVI)`); no driver swap is needed.
- **`scope_gui` requires PyQt6** (`pip install PyQt6`, included in `requirements.txt`). The library and CLI need only `pyvisa` (and `Pillow` for PNG screenshots). The GUI has only been run on Windows 11; other platforms are untested.
- Close UNI-T Device Manager / Instrument Manager while using these tools: only one program can hold the scope's USB connection at a time (`VI_ERROR_RSRC_BUSY`).

## Setup
One command creates the virtual environment (`.venv`) and installs everything, including PyQt6 for `scope_gui`:
```
scripts\install.bat                  # or: powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```
It reuses an existing `.venv`, checks that PyQt6 and pyvisa import, and tells you if the NI-VISA runtime is missing. Options: `-Recreate` (rebuild from scratch), `-Upgrade`, `-Python <path to python.exe>`. To start over, `scripts\cleanup.bat` removes the `.venv` (it asks first; `-Force` skips the question and stops any program running from it, `-Caches` also removes `__pycache__` and build folders, `-Settings` also removes the saved GUI settings and error log, `-WhatIf` only shows what would be removed). It never touches your source files. Manual equivalent:
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt     # pyvisa, Pillow, PyQt6
```
For another project, only `pyvisa` is needed (plus `Pillow` for PNG screenshots): copy the `unit_scope/` folder, or `pip install .` from this folder (`pyproject.toml` provides `unit-scope` and, with the `gui` extra, `unit-scope-gui` commands).

## Library: `unit_scope.UniTScope`
```python
from unit_scope import UniTScope, export

with UniTScope() as scope:                  # first USB instrument; UniTScope("USB0::...::INSTR") to pick one
    print(scope.idn())
    scope.set_time_scale(2e-4)              # s/div
    scope.set_channel(1, scale=0.5, offset=-1.0)
    scope.set_trigger(source=1, slope="falling", level=1.2, sweep="auto")

    scope.stop()                            # freeze so channels are time-aligned
    cap = scope.capture()                   # enabled channels -> Capture
    w = cap.channel(1)                      # Waveform: t, volts, scale, offset, coupling, probe
    print(w.meas.vpp, w.meas.vrms, w.meas.freq)

    export.export(cap, "wave.vcd")          # GTKWave / Surfer
    export.export(cap, "wave.csv")
    scope.save_screenshot("shots")          # shots/<datetime>.png
    scope.run()
```
- Every method raises `ScopeConnectionError` / `ScopeCommandError` (both `ScopeError`), or `ValueError` for bad arguments.
- Low level: `write`, `query`, `query_float`, `query_block`. Setting commands return nothing on this scope, so `set_*` methods pause `settle_s` (default 0.2 s) after each write.
- `UniTScope(visa_library="@py")` uses pyvisa-py instead of NI-VISA (not tested).
- One instance is not thread-safe; use one per thread, or a lock. The GUI keeps its instance in a worker thread.

| Group | Methods |
|---|---|
| Connection | `connect`, `disconnect`, context manager, `connected`, `UniTScope.list_resources()` |
| Info / run | `idn`, `state`, `run`, `stop` |
| Horizontal | `get_time_scale`, `set_time_scale` |
| Vertical | `channel_enabled`, `get_scale`, `get_offset`, `get_coupling`, `get_probe`, `set_channel(ch, scale, offset, enabled, coupling)` |
| Trigger | `get_trigger`, `set_trigger(source, slope, level, sweep)` (slope: rising/falling/both; sweep: auto/normal/single) |
| Data | `read_waveform(ch)`, `capture(channels)` |
| Screen | `screenshot_bmp`, `screenshot_image`, `save_screenshot` |

Scope quirks handled inside the class: unknown/set commands just time out instead of replying; only the exact long/short command forms work (`TRIGger:SOURce`, not `TRIG:SOUR`); `PrtScn:bmp;` from the manual (meant for UNI-T's `uci.dll`) times out, `DISP:DATA?` works; the first sample of every waveform block is often a stray value (measurements and exports ignore it).

## Command line
```
.venv\Scripts\python -m unit_scope                        # screenshot -> screenshots\<datetime>.png
.venv\Scripts\python -m unit_scope list                   # USB VISA resources
.venv\Scripts\python -m unit_scope idn
.venv\Scripts\python -m unit_scope waveform               # CSV (both enabled channels)
.venv\Scripts\python -m unit_scope waveform -f vcd -c 1   # VCD, CH1 only
.venv\Scripts\python -m unit_scope waveform -f all        # CSV + VCD
```
Common options: `--resource STR`, `--timeout MS`, `--out DIR`; `waveform` also has `-c/--channels`, `--no-digital`.

## Exporting waveforms (`unit_scope.export`)
| Format | For | Notes |
|---|---|---|
| `.vcd` | GTKWave, Surfer (and most waveform viewers) | analog + digital traces, see below |
| `.csv` | Excel, MATLAB, pandas, PulseView import | `time_s, CH1_V, CH2_V` |

**VCD contents** (`export.write_vcd(capture, path, digital=True, trigger_marker=True)`):
- `CH1`, `CH2`: `$var real` analog voltages (every change; the scope's resolution is about 0.04 V at 1 V/div).
- `CH1_dig`, `CH2_dig`: the same signal thresholded at mid-level with 35 %/65 % hysteresis as a `$var wire`, so square waves and logic show as normal digital traces. Left out for flat (DC) channels, or with `digital=False` / `--no-digital` / the GUI checkbox.
- `trigger`: a wire that goes high at t = 0, to line the viewer cursor up on the trigger point.
- VCD times cannot be negative, so the file starts at the first sample (about -4 ms) and the trigger sits that far into the file; the header `$comment` states it, together with V/div, offset, coupling and the trigger settings. The `$timescale` is chosen from the sample interval (about 50 ticks per sample at 5 us).
- In GTKWave, select a `CH1` row and use *Data Format > Analog > Step* (or *Interpolated*) to see the analog trace; in Surfer, use the variable's analog/format option. GTKWave and Surfer are not installed here, so the files are checked with a strict parser (tests) rather than in the viewers themselves.
- Need `.fst`? GTKWave ships `vcd2fst`: `vcd2fst wave.vcd wave.fst`.

## GUI panel (PyQt6)
```
.venv\Scripts\python -m scope_gui
```
- **Connect/Disconnect** toggle; the scope IDN shows under it.
- **Capture** (manual) or **Auto capture** with an interval of 1 s (default), 2 s, 5 s or 10 s. Capture runs in a worker thread, so the UI never blocks.
- **Run/Stop** button (`:RUN` / `:STOP`).
- **Export:** *Save PNG...* saves the current view (1600x960); *Export waveform...* writes the captured waveforms as VCD or CSV (file dialog; default name `screenshots/<datetime>.vcd`); *VCD: add digital traces* controls the thresholded traces.
- **Measurements** table under the display, per channel: Vpp, Vmax, Vmin, Vrms, Freq and Period. The **Voltages** and **Timing** buttons above it show/hide those column groups (the table disappears when both are off). Vrms is the true RMS including any DC offset; for periodic signals it is taken over whole cycles only.
- The measure buttons and both tables keep the same left edge as the waveform area, also when the window is resized and the display is letterboxed. Both tables use the same label-column width and fixed, padded data columns, and only take the width they need (hiding a column group shrinks the table). Colours (text, grid and header lines, status LED) follow the theme.
- **Cursors** (*Time cursors* / *Voltage cursors*): a vertical pair X1/X2 and a horizontal pair Y1/Y2, drawn on the display (and in saved PNGs) and draggable. A readout table shows X1, X2, ΔT and 1/ΔT, each channel's voltage at X1/X2 and its ΔV, and Y1, Y2, ΔV for the channel chosen in the *on* box. Δ is second minus first. Y cursors are in signal volts (like the trigger level) and start on the signal's max/min, so the first ΔV is Vpp. Cursors are display-only.
- **How the measurements are made** (`unit_scope.measure`): computed from the captured samples (the 8-16 division record), not read from the scope. Freq/Period use rising-edge crossings of the mid level (with hysteresis) and a least-squares fit, and show `---` when the signal is not periodic (DC, noise, a single pulse, or fewer than two edges on screen). Vmax/Vmin/Vpp include noise peaks. The frequency agrees with the scope's own `MEAS:FREQ?` to about 0.1 %.
- **Trigger** controls: source (CH1/CH2), edge (Rising/Falling/Both), level (the scope quantises it to its ADC step, 0.04 V at 1 V/div) and mode (Auto/Normal/Single). The level is drawn as a dashed line with a `T` tag in the source channel's colour.
- **Drag on the display:** drag the dashed trigger line (or its `T` tag) to set the trigger level; drag a channel's ground marker (the numbered arrow at the left edge) or the trace itself to move it vertically. The value follows the cursor and is sent when you release. The status badge reads TRIG / AUTO / STOP as reported by the scope.
- **Scale / coupling** controls: time/div, and per channel V/div (1-2-5 steps) and input coupling **DC / AC / GND** (library: `scope.set_channel(ch, coupling="ac")`). The channel boxes under the display show the active coupling. The controls always show the scope's actual values, so a request the scope clamps (or a knob you turn) shows up on the next capture. Note that the scope keeps the vertical offset in volts, so changing V/div moves the trace on screen.
- **Record: Screen or Memory** (Acquisition group). *Screen (1600 pts)* (default) plots and measures the 1600-sample record that the scope draws (100 samples per division, about 1 s per capture). *Memory (full depth)* captures the scope's raw acquisition memory of every enabled channel (`UniTScope.capture(memory=True)` / `read_memory`). The datasheet's 64 kpts are shared between the channels: with both enabled each gets 32000 samples (2 MSa/s at 1 ms/div, 40 kSa/s at 50 ms/div); with only CH1 enabled it gets all 64000 (4 MSa/s at 1 ms/div). With only CH2 enabled the scope gives no deep memory and the screen record is used, so narrow pulses and edges are resolved (a 10 us pulse is 1 sample on the screen record and about 11 in memory), and Vpp/Vrms/Freq are computed from all samples. It takes about 1.7 s per capture and is drawn as a min/max envelope per pixel column, so the display stays fast. The bottom bar shows `Record <n> <rate>` and `Memory <depth> <rate>` in screen mode, and one line (`32kpts 2MSa/s`) in memory mode. The FFT tab's memory source uses the same records.
- **Tabs: Y-T, XY and FFT.** The XY tab (CH1 horizontal, CH2 vertical) also switches the scope's own display to XY. The **FFT** tab shows the amplitude spectrum of CH1 or CH2 (`unit_scope.spectrum`, pure Python): window (Hann, Hamming, Blackman, Flat top, Rectangular), scale (dBV rms or volts peak), a peak readout and a hover cursor.
  - **Resolution and span.** The spectrum covers the 16-division window, so the resolution is 1 / (16 x time/div) whatever the sample rate (1 ms/div: 62.5 Hz; 10 ms/div: 6.25 Hz; 50 ms/div: 1.25 Hz), and the span is half the sample rate. A fast time base therefore shows a low line such as 1 kHz as a sliver at the left of a wide span, with only a few periods in the window. The default span is **Auto** (0 to 10 x the strongest line, so a 1 kHz tone and its harmonics fill the plot); *Full* shows 0 to Nyquist, which is very wide at a fast time base (1 MHz at 1 ms/div with the memory source). **Choose a span in Hz (e.g. `5kHz`) and the GUI sets the time/div** for you: the longest time base (up to 50 ms/div) whose Nyquist frequency still covers the span. Fit several periods of the signal in the record; the view warns when the peak is within 4 resolution steps of 0 Hz. The time/div box in the side panel still works (and the FFT follows it) when the span is `Full`.
  - **Data source.** *Memory (full depth)* (default) reads the scope's raw acquisition memory with `UniTScope.read_memory()` (`WAV:MODE RAW`; works while running): 32000 samples per channel at 2 MSa/s (1 ms/div) down to 40 kSa/s (50 ms/div) with both channels on, 64000 with only CH1 on, no decimation, accurate amplitudes (1 kHz, 0.95 V reads 0.955 V at every time base). *Screen (1600 pts)* uses the record that is drawn on the scope; at slow time bases the scope decimates it in hardware and the amplitude drops (a 1 kHz line read 0.70 V at 20 ms/div instead of 0.95 V). No downsampling is needed on the PC: a 32768-point FFT takes about 0.1 s in pure Python, and long spectra are drawn with a peak-per-pixel reduction so narrow lines are never lost.
- **Auto-reconnect** (checkbox under the Connect button). If the connection is lost (cable, scope power, a communication error), the GUI tries to connect again every 3 s until it works or you click Disconnect; the status bar says `Connection lost - reconnecting in 3 s...` and then `Reconnected`. A disconnect you asked for is never retried. If the scope was power-cycled it has lost its own settings, which the GUI then simply shows.
- **Saved settings.** The GUI remembers its own choices between runs (Windows registry, `HKCU\Software\UNI-T-Scope\ScopeGUI`): window size and position, theme, Record (Screen/Memory), auto-capture interval, Auto-reconnect, the Voltages/Timing chips, the VCD option, the selected tab (Y-T or FFT; XY is not restored because it changes the scope's display) and the FFT choices (source, window, scale, data, span). The scope's own settings (V/div, trigger, ...) stay in the scope.
- **Errors do not close the program.** An unexpected exception (a bug, a malformed reply) is written to `%LOCALAPPDATA%\UNI-T-Scope\scope_gui.log`, shown in the status bar and in a message box with the traceback under *Show Details*, and the program keeps running; an unexpected error in the scope thread drops the connection with a message (and reconnects if Auto-reconnect is on).
- **Channel on/off tick boxes** (the CH1 / CH2 labels in the Scale / coupling group). Ticking or unticking a channel enables or disables it on the scope; the boxes follow the scope after every capture, so a change made with the front-panel buttons shows up too. A disabled channel disappears from the display and its V/div, coupling, probe and GND controls are greyed out. At least one channel always stays on (the scope would accept turning both off, and then show nothing). If you turn off the trigger channel the trigger moves to the other channel: a trigger on a channel that is off leaves the display free-running (state AUTO). It also sets the memory depth, because the 64 kpts are shared: one channel on gives 64000 samples, both on 32000 each.
- **Dark theme** by default, with a Light toggle (remembered between runs). The light theme uses semi-bold text, darker muted text and deeper channel colours for contrast (at least 4:1 against the white backgrounds); dark keeps the normal weight.
- The display is redrawn from the waveform data in the scope's layout (16 x 8 divisions, trigger marker at t=0, ground markers, V/div and s/div from the scope), not a copy of the scope's bitmap, so the scope's own menus are not shown; use the CLI screenshot for those.

## License
The source code of this project (`unit_scope/`, `scope_gui/`, `scripts/`, `tests/`) is released under the **MIT License**, see
[LICENSE](LICENSE). Copyright (c) 2026 RSP @KMUTNB.

Things the MIT license does not cover:
- **PyQt6** (needed by `scope_gui`) is licensed GPL v3 or commercially by Riverbank Computing. This project only imports it and
  does not include it; you install it yourself (`scripts\install.bat`). If you distribute a *bundle* that contains PyQt6 (for
  example a packaged `.exe`), that bundle has to follow the GPL (or you need a commercial PyQt license). The library
  `unit_scope` and the command line tool do not use PyQt6.
- **UNI-T's documents** (datasheet and programming manuals) and software (Device Manager, SDK) belong to UNI-T. They are not
  in this repository.
- **NI-VISA** is National Instruments' runtime, installed separately (it comes with UNI-T Device Manager v2.9).
- The other Python packages keep their own (permissive) licenses: pyvisa and pyvisa-py (MIT), pyusb (BSD), Pillow (HPND).

## Tests
```
.venv\Scripts\python -m unittest discover -s tests -v
```
Run without a scope. `tests/test_unit_scope.py`: a fake VISA instrument checks the commands sent, reply parsing, capture scaling, the "no data" padding, the deep-memory read, the spectrum code, measurements and the CSV/VCD writers. `tests/test_gui.py` (needs PyQt6, runs offscreen): the real window and worker thread against a fake scope, covering the mouse-wheel time/div (a burst of notches becomes one request, the box does not jump back, bursts while the scope is busy are merged), partial records from a sweep that is still filling (refreshed until complete, a clipped trace is not refreshed forever, a stopped scope shows the stored part), tab switching and the scope's display mode, the FFT span, errors that must not close the program, auto-reconnect and saved settings. The whole suite takes about 10 s.

## Troubleshooting
- **No instrument found / `list` is empty:** check power and the USB cable, and that NI-VISA is installed.
- **`VI_ERROR_TMO` / `ScopeCommandError` on a command:** the scope does not answer unknown commands or setting commands; only queries (`?`) reply.
- **`VI_ERROR_RSRC_BUSY`:** close other software using the scope (e.g. UNI-T Device Manager).
- **`VI_WARN_CONFIG_NLOADED`:** harmless NI-VISA notice (no saved configuration); filtered by the library.
- **Raw pyusb / WinUSB (Zadig) route:** removed. NI-VISA's USBTMC driver works without replacing the Windows driver, and the raw route was never needed.
