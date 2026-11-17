"""unit_scope - a small, GUI-free pyvisa driver for UNI-T oscilloscopes.

    from unit_scope import UniTScope, export

    with UniTScope() as scope:
        cap = scope.capture()
        export.export(cap, "wave.vcd")      # open in GTKWave or Surfer
"""
from . import export
from .errors import ScopeCommandError, ScopeConnectionError, ScopeError
from .measure import Measurement, measure, value_at
from .models import Capture, TriggerSettings, Waveform
from .scope import UniTScope

__all__ = ["UniTScope", "Capture", "Waveform", "TriggerSettings", "Measurement", "measure", "value_at",
           "export", "ScopeError", "ScopeConnectionError", "ScopeCommandError"]
__version__ = "1.0.0"
