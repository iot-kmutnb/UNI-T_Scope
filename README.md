# UNI-T Scope Tools

Version 1.0.0

A control panel, a command line tool and a Python library for **UNI-T
UTD2000CEX+ series digital oscilloscopes**, connected over USB. The
panel (`scope_gui`, PyQt6) shows the scope's waveforms live on the PC
and lets you change the main settings; the library (`unit_scope`)
talks to the scope with SCPI commands through VISA (pyvisa and the
NI-VISA runtime) and can be used on its own in your own scripts.

> **Tested only with the UNI-T UTD2102CEX+ on Windows 11, with the
> NI-VISA runtime installed.** Please read [Caution and
> limitations](#caution-and-limitations) before you use it.

## What it does
- **Display tabs:** Y-T (time domain), XY (CH1 against CH2) and FFT
  (spectrum of CH1 or CH2, with a peak readout).
- **Control:** run / stop / capture / auto capture, trigger (source,
  edge, level, mode), time/div, V/div, coupling, probe attenuation (1X
  / 10X), channel on/off. Drag the trigger level, the trigger position
  and the ground markers on the display; the mouse wheel changes the
  time/div.
- **Measure:** Vpp, Vmax, Vmin, Vrms, frequency and period per
  channel, and time / voltage cursors.
- **Record:** the 1600-point screen record, or the scope's acquisition
  memory (64 kpts, shared by the channels in use).
- **Save:** PNG of the display, and the waveforms as CSV or VCD (for
  GTKWave / Surfer).
- **Convenience:** dark and light theme, auto-reconnect, saved
  settings, errors reported instead of closing the program.

More detail on every control, the library and the scope's quirks is in
[DETAILS.md](DETAILS.md).

## Requirements
- **Windows** (developed and tested on Windows 11) and **Python 3.9 or
  newer**.
- **NI-VISA Runtime 15.5.** It is bundled with the **UNI-T Device
  Manager v2.9** installer (download it from UNI-T), so installing
  Device Manager is enough; the runtime can also be installed on its
  own from National Instruments. The scope shows up as a USB test and
  measurement device; no driver swap is needed.
- **PyQt6** for the panel (installed by the install script). The
  library and the command line tool need only `pyvisa` (and `Pillow`
  for PNG screenshots).

## Install
```
scripts\install.bat
```
This creates the virtual environment `.venv`, installs the packages
(including PyQt6), checks that they import, and tells you if the
NI-VISA runtime is missing. Run it again at any time; it reuses the
environment. Options (give them to `scripts\install.ps1`): `-Recreate`
rebuilds from scratch, `-Upgrade` updates the packages,
`-Python <path>` picks the Python.

To remove the environment again: `scripts\cleanup.bat` (it asks first;
`-Force` skips the question and stops programs that run from it,
`-Caches` also removes caches, `-Settings` also removes the saved
panel settings, `-WhatIf` only shows what it would do). Your source
files are never touched.

Manual alternative:
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Use
**Panel.** Power the scope, connect its USB cable, close UNI-T Device
Manager (only one program can use the scope at a time), then:
```
.venv\Scripts\python -m scope_gui
```
Click **Connect**; the scope type and serial number appear next to the
button. **Capture** takes one picture, **Auto capture** repeats it,
**Run / Stop** starts and stops the scope's own acquisition (green
*Run*, red-orange *Stop*). Use the tabs for Y-T, XY and FFT. Hover
over a control for a tooltip.

**Command line.**
```
# find the scope
.venv\Scripts\python -m unit_scope list
# screenshot -> screenshots\<date time>.png
.venv\Scripts\python -m unit_scope
# save the waveforms (-f csv, vcd or all)
.venv\Scripts\python -m unit_scope waveform -f vcd
```

**Library.**
```python
from unit_scope import UniTScope, export

with UniTScope() as scope:        # first USB scope found
    print(scope.idn())
    scope.set_time_scale(1e-3)    # seconds per division
    scope.set_channel(1, scale=0.5, offset=-1.0)
    scope.stop()
    cap = scope.capture()         # enabled channels
    w = cap.channel(1)            # w.t, w.volts, w.meas.freq, ...
    export.export(cap, "wave.csv")
    scope.run()
```

**Tests** (no scope needed):
`.venv\Scripts\python -m unittest discover -s tests`

**Troubleshooting.**
- *No scope found:* check the power and the USB cable, and that the
  NI-VISA runtime is installed (`scripts\install.bat` checks).
- *`VI_ERROR_RSRC_BUSY` / cannot connect:* another program (for
  example UNI-T Device Manager) has the scope open; close it.
- *The panel closes the connection or shows odd data after a USB
  hiccup:* click Connect again, or tick *Auto-reconnect*.
- *Something unexpected happened:* details are in
  `%LOCALAPPDATA%\UNI-T-Scope\scope_gui.log`.

## Caution and limitations
- **Tested only with one scope and one setup:** a **UNI-T
  UTD2102CEX+** (firmware 1.10.0060), on **Windows 11** with the
  **NI-VISA 15.5 Runtime** installed, Python 3.14. Other models (for
  example the UTD2052CEX+, UTD2202CEX+, UTD2000E or UTD7000B series),
  other firmware versions and other operating systems have **not**
  been tested; commands may behave differently or fail.
- **The software changes the scope's settings** (time base, V/div,
  offset, trigger, channel on/off, probe attenuation, display mode and
  the waveform read mode) and does not restore them when you close it.
  Check the scope's own screen.
- **The probe setting must match the probe.** The scope uses the
  attenuation you set (1X / 10X); a setting that does not match the
  probe's switch makes every reading wrong by that factor.
- **Measurements are indicative.** They are computed on the PC from
  8-bit samples, not read from the scope, and have not been checked
  against a calibrated reference. Do not rely on them for
  safety-critical decisions, calibration, certification or
  high-voltage work. The software does not protect the scope's inputs:
  observe the input ratings and the safety instructions in the scope's
  manual.
- The acquisition memory (64 kpts) is shared, so with both channels on
  each gets 32 kpts, and with only CH2 on there is no deep memory;
  very slow time bases (200 ms/div and slower) fill the record over
  several seconds; a heavily clipped trace can come back truncated or
  empty.
- **Use at your own risk.** The software is provided "as is", without
  warranty of any kind. The author is not responsible or liable for
  any damage to the oscilloscope, to connected equipment or the device
  under test, for loss of data, for wrong measurement results, or for
  any other loss or injury arising from the use of this software.
- This is an independent project, not made, supported or endorsed by
  UNI-T. UNI-T and UNI-TREND are trademarks of their owners. UNI-T's
  manuals and software are not included here.

## License
MIT, see [LICENSE](LICENSE). Copyright (c) 2026 RSP @KMUTNB.

The MIT license covers this project's own code only. **PyQt6** is
licensed GPL v3 or commercially by Riverbank Computing: this project
only imports it, you install it yourself, and a bundle that contains
PyQt6 (for example a packaged `.exe`) has to follow the GPL or use a
commercial PyQt license. The NI-VISA runtime and UNI-T's documents and
software belong to their owners.
