"""Plain data containers returned by :class:`unit_scope.UniTScope`. No GUI or VISA imports."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .measure import Measurement, measure


@dataclass
class Waveform:
    """One channel's record. ``t`` is seconds relative to the trigger point, ``volts`` are
    signal volts (offset/probe already applied by the scope)."""
    channel: int
    t: List[float]
    volts: List[float]
    scale: float = 1.0            # V/div at capture time
    offset: float = 0.0           # display offset, volts
    coupling: str = "DC"
    probe: float = 1.0
    missing: int = 0              # samples of the 16-division record without data (still acquiring, or off memory)
    _meas: Optional[Measurement] = field(default=None, repr=False, compare=False)

    @property
    def sample_interval(self) -> float:
        return (self.t[-1] - self.t[0]) / (len(self.t) - 1) if len(self.t) > 1 else 0.0

    @property
    def meas(self) -> Optional[Measurement]:
        """Vpp/Vmax/Vmin/Vrms/frequency/period, computed on first access and cached."""
        if self._meas is None:
            self._meas = measure(self.t, self.volts, noise_floor=3 * self.scale / 25)  # ~3 ADC counts
        return self._meas


@dataclass
class TriggerSettings:
    source: int = 1               # 1 or 2
    slope: str = "rising"         # "rising" | "falling" | "both"
    level: float = 0.0            # volts (signal volts of the source channel)
    sweep: str = "auto"           # "auto" | "normal" | "single"


@dataclass
class Capture:
    """Everything fetched in one :meth:`UniTScope.capture` call."""
    channels: List[Waveform] = field(default_factory=list)
    time_scale: float = 5e-4      # s/div
    time_offset: float = 0.0      # trigger position: screen centre is t = time_offset (s)
    points: int = 0               # acquisition memory depth in points (0 = unknown)
    sample_rate: float = 0.0      # Sa/s (0 = unknown)
    memory: Dict[int, 'Waveform'] = field(default_factory=dict)   # channel -> deep-memory record (see read_memory)
    state: str = ""               # run state reported by the scope: TRIGED / AUTO / STOP ...
    trigger: TriggerSettings = field(default_factory=TriggerSettings)
    stamp: datetime = field(default_factory=datetime.now)

    def channel(self, ch: int) -> Optional[Waveform]:
        return next((w for w in self.channels if w.channel == ch), None)
