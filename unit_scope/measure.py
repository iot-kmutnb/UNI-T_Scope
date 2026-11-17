"""Waveform measurements computed from the captured samples."""
from dataclasses import dataclass
from math import sqrt
from typing import Optional


@dataclass
class Measurement:
    vmax: float
    vmin: float
    vpp: float
    freq: Optional[float] = None     # None when the signal is not periodic
    period: Optional[float] = None
    vrms: float = 0.0               # true RMS (incl. DC); whole cycles when periodic


def _rising_edges(t, v, lo, hi):
    """Times of rising crossings of the mid level, using a Schmitt trigger (lo/hi)
    so noise on a flat level does not create false edges. Linear interpolation
    between the two samples that straddle the mid level."""
    mid = (lo + hi) / 2
    edges = []
    state = v[0] > hi          # True when currently 'high'
    last_low = 0 if not state else None
    for k in range(1, len(v)):
        if state:
            if v[k] < lo:
                state = False
                last_low = k
        else:
            if v[k] <= lo:
                last_low = k
            elif v[k] > hi and last_low is not None:
                # find the sample pair around mid between last_low and k
                j = last_low
                while j < k and v[j + 1] < mid:
                    j += 1
                if v[j + 1] != v[j]:
                    frac = (mid - v[j]) / (v[j + 1] - v[j])
                else:
                    frac = 0.0
                edges.append(t[j] + frac * (t[j + 1] - t[j]))
                state = True
    return edges


def measure(t, v, noise_floor=0.0) -> Optional[Measurement]:
    """noise_floor: peak-to-peak (V) below which the signal counts as flat/DC
    (about 2-3 ADC counts)."""
    if len(v) < 5 or len(t) != len(v):
        return None
    t, v = t[1:], v[1:]        # the first sample of a block is often a stray value
    vmax, vmin = max(v), min(v)
    m = Measurement(vmax, vmin, vmax - vmin, vrms=sqrt(sum(x * x for x in v) / len(v)))
    if m.vpp <= max(noise_floor, 1e-12):
        return m
    lo = vmin + 0.35 * m.vpp
    hi = vmin + 0.65 * m.vpp
    edges = _rising_edges(t, v, lo, hi)
    if len(edges) < 2:
        return m
    periods = [b - a for a, b in zip(edges, edges[1:])]
    # Periodic only if most edges sit on a regular grid e0 + k*T (T = median interval).
    # Edges off the grid (glitches) are ignored; T is then the least-squares slope
    # of edge time vs. cycle number, which is far more precise than any one interval.
    med = sorted(periods)[len(periods) // 2]
    e0 = edges[0]
    best = {}
    for e in edges:
        k = round((e - e0) / med)
        res = abs(e - (e0 + k * med))
        if res <= 0.15 * med and (k not in best or res < best[k][0]):
            best[k] = (res, e)
    pts = sorted((k, e) for k, (_, e) in best.items())
    if len(pts) >= 2 and len(pts) >= 0.6 * len(edges):
        n = len(pts)
        mk = sum(k for k, _ in pts) / n
        me = sum(e for _, e in pts) / n
        den = sum((k - mk) ** 2 for k, _ in pts)
        slope = sum((k - mk) * (e - me) for k, e in pts) / den if den else 0
        if slope > 0 and abs(slope - med) <= 0.05 * med:
            m.period = slope
            m.freq = 1.0 / slope
            # RMS over whole cycles only (first to last edge) so a partial cycle doesn't bias it
            span = [x for tt, x in zip(t, v) if pts[0][1] <= tt < pts[-1][1]]
            if len(span) >= 4:
                m.vrms = sqrt(sum(x * x for x in span) / len(span))
    return m


def value_at(t, v, x):
    """Linear interpolation of the waveform at time x; None outside the record."""
    if len(t) < 2 or x < t[0] or x > t[-1]:
        return None
    lo, hi = 0, len(t) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if t[mid] <= x:
            lo = mid
        else:
            hi = mid
    if t[hi] == t[lo]:
        return v[lo]
    return v[lo] + (v[hi] - v[lo]) * (x - t[lo]) / (t[hi] - t[lo])
