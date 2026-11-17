"""Amplitude spectrum of a captured waveform (pure Python, no dependencies).

A capture is a 1600-sample record covering 16 divisions, so it is already decimated by the scope
(100 samples per division): the spectrum spans 0 .. 50/(time/div) and resolves 1/(16 * time/div).
Change the time/div to trade span against resolution, as on the scope's own FFT.
"""
import cmath
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

WINDOWS = ("rect", "hann", "hamming", "blackman", "flattop")


def window(name: str, n: int) -> List[float]:
    """Symmetric window of length ``n`` (``rect``, ``hann``, ``hamming``, ``blackman``, ``flattop``)."""
    if name not in WINDOWS:
        raise ValueError(f"window must be one of {WINDOWS}")
    if n < 2 or name == "rect":
        return [1.0] * n
    out = []
    for i in range(n):
        x = 2.0 * math.pi * i / (n - 1)
        if name == "hann":
            out.append(0.5 - 0.5 * math.cos(x))
        elif name == "hamming":
            out.append(0.54 - 0.46 * math.cos(x))
        elif name == "blackman":
            out.append(0.42 - 0.5 * math.cos(x) + 0.08 * math.cos(2 * x))
        else:                                  # flat top: the best amplitude accuracy, the widest peak
            out.append(0.21557895 - 0.41663158 * math.cos(x) + 0.277263158 * math.cos(2 * x)
                       - 0.083578947 * math.cos(3 * x) + 0.006947368 * math.cos(4 * x))
    return out


_plans: Dict[int, Tuple[List[int], List[complex]]] = {}


def _plan(n: int):
    if n not in _plans:
        bits = n.bit_length() - 1
        rev = [int(format(i, f"0{bits}b")[::-1], 2) if bits else 0 for i in range(n)]
        tw = [cmath.exp(-2j * math.pi * k / n) for k in range(n // 2)]
        _plans[n] = (rev, tw)
    return _plans[n]


def fft(x: Sequence[complex]) -> List[complex]:
    """Radix-2 FFT; the length must be a power of two."""
    n = len(x)
    if n & (n - 1) or n == 0:
        raise ValueError("FFT length must be a power of two")
    rev, tw = _plan(n)
    a = [complex(x[rev[i]]) for i in range(n)]
    size = 2
    while size <= n:
        half, step = size // 2, n // size
        for start in range(0, n, size):
            for k in range(half):
                u = a[start + k]
                v = a[start + k + half] * tw[k * step]
                a[start + k] = u + v
                a[start + k + half] = u - v
        size *= 2
    return a


@dataclass
class Spectrum:
    freqs: List[float]            # Hz, bin centres 0 .. nyquist
    amps: List[float]             # peak volts of a sine at that frequency
    df: float                     # bin spacing (Hz); the zero padding makes this finer than ``rbw``
    rbw: float                    # resolution 1 / record length (Hz)
    nyquist: float
    n: int                        # samples used
    nfft: int
    window: str
    peak_freq: float = 0.0        # highest non-DC peak, interpolated between bins
    peak_amp: float = 0.0         # its amplitude (V peak)
    rms: List[float] = field(default_factory=list)   # amps / sqrt(2)

    @property
    def fs(self) -> float:
        """Sample rate of the analysed record (Hz)."""
        return 2.0 * self.nyquist


def to_dbv(vrms: float, floor: float = 1e-9) -> float:
    return 20.0 * math.log10(max(vrms, floor))


def spectrum(volts: Sequence[float], dt: float, window_name: str = "hann", nfft: Optional[int] = None) -> Optional[Spectrum]:
    """Single-sided amplitude spectrum of ``volts`` sampled every ``dt`` seconds, or None if it is too short.
    The mean is removed, the window applied, the record zero padded to ``nfft`` (default: next power of two)
    and the result scaled so that a sine of amplitude A volts peaks at A."""
    n = len(volts)
    if n < 8 or dt <= 0:
        return None
    mean = sum(volts) / n
    w = window(window_name, n)
    gain = sum(w) / n                                   # coherent gain of the window
    size = nfft or 1 << (n - 1).bit_length()
    if size < n:
        raise ValueError("nfft must be at least the number of samples")
    x = [(v - mean) * wi for v, wi in zip(volts, w)] + [0.0] * (size - n)
    spec = fft(x)
    half = size // 2
    scale = 2.0 / (n * gain)
    amps = [abs(spec[k]) * scale for k in range(half)]
    amps[0] *= 0.5                                      # DC has no mirror image
    df = 1.0 / (size * dt)
    s = Spectrum(freqs=[k * df for k in range(half)], amps=amps, df=df, rbw=1.0 / (n * dt),
                 nyquist=0.5 / dt, n=n, nfft=size, window=window_name, rms=[a / math.sqrt(2) for a in amps])
    s.peak_freq, s.peak_amp = find_peak(s)
    return s


def find_peak(s: "Spectrum", fmax: Optional[float] = None) -> Tuple[float, float]:
    """(frequency, amplitude) of the highest peak above DC and at or below ``fmax`` (default: the whole
    spectrum). The frequency and amplitude are interpolated between bins (a parabola through the log
    magnitudes), except for the rectangular window whose sinc lobe does not suit that fit."""
    half = len(s.amps)
    last = half - 1 if fmax is None else min(half - 1, int(fmax / s.df))
    skip = min(last - 1, max(2, int(round(2.5 * s.rbw / s.df))))   # the window's main lobe around 0 Hz
    if last <= skip:
        return 0.0, 0.0
    amps = s.amps
    k = max(range(skip, last + 1), key=lambda i: amps[i])
    freq, amp = k * s.df, amps[k]
    if s.window != "rect" and 0 < k < half - 1 and amps[k - 1] > 0 and amps[k + 1] > 0:
        a, b, c = (math.log(amps[k - 1]), math.log(amps[k]), math.log(amps[k + 1]))
        denom = a - 2 * b + c
        if denom < 0:
            shift = 0.5 * (a - c) / denom
            freq, amp = (k + shift) * s.df, math.exp(b - 0.25 * (a - c) * shift)
    return freq, amp
