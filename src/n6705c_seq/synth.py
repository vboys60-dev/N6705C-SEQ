"""Compile List segments into shared constant-dwell waveforms."""

from __future__ import annotations

import math
from dataclasses import dataclass

from n6705c.core.arb import ArbSpec, ConstantDwellArb
from n6705c.core.arb.models import (
    CDWELL_COUNT_MAX,
    CDWELL_DWELL_GRANULARITY,
    CDWELL_DWELL_MAX,
    CDWELL_DWELL_MIN,
    CDWELL_MAX_POINTS,
)
from n6705c.core.function import ChannelFunction

from .listplan import FIELDS, TIMES, ChannelList, ListPlan, Segment

WARN_PTS_PER_PERIOD = 20
BLOCK_PTS_PER_PERIOD = 10
WARN_PTS_PER_EDGE = 10
DISCONTINUITY_V = 1e-3


@dataclass
class SegmentInfo:
    """Requested and quantized segment timing."""

    points: int
    requested: float
    actual: float


@dataclass
class Compiled:
    """Shared timing, per-channel samples, and diagnostics."""

    dwell: float
    n_points: int
    levels: dict[int, list[float]]
    delay_points: dict[int, int]
    pad_points: dict[int, int]
    segments: dict[int, list[SegmentInfo]]
    warnings: list[str]
    errors: list[str]


def base_duration(seg: Segment) -> float:
    """Return the duration of one segment repetition."""
    if seg.shape in ("sine", "triangle"):
        return 1 / seg.params["frequency"]
    return sum(seg.params[key] for key in FIELDS[seg.shape] if key in TIMES and key != "tau")


def value(seg: Segment, u: float) -> float:
    """Evaluate an ideal segment at local time u."""
    p = seg.params
    if seg.shape in ("sine", "triangle"):
        s = math.sin(2 * math.pi * p["frequency"] * u + p["phase"] * math.pi / 180)
        return p["offset"] + p["amplitude"] * (s if seg.shape == "sine" else 2 / math.pi * math.asin(s))
    if seg.shape == "trapezoid":
        lo, hi = p["offset"], p["offset"] + p["amplitude"]
        if p["start_time"] > 0 and u <= p["start_time"]:
            return lo
        u -= p["start_time"]
        if p["up_time"] > 0 and u <= p["up_time"]:
            return lo + p["amplitude"] * u / p["up_time"]
        u -= p["up_time"]
        if p["high_time"] > 0 and u <= p["high_time"]:
            return hi
        u -= p["high_time"]
        if p["down_time"] > 0 and u <= p["down_time"]:
            return hi - p["amplitude"] * u / p["down_time"]
        return lo
    if u <= p["delay"]:
        return p["start"]
    u -= p["delay"]
    if seg.shape == "step":
        return p["end"]
    if seg.shape == "ramp":
        if p["ramp_time"] > 0 and u <= p["ramp_time"]:
            return p["start"] + (p["end"] - p["start"]) * u / p["ramp_time"]
        return p["end"]
    if seg.shape == "staircase":
        if p["stair_time"] > 0 and u <= p["stair_time"]:
            x = math.ceil(u / p["stair_time"] * p["n_steps"]) / p["n_steps"]
            return p["start"] + (p["end"] - p["start"]) * x
        return p["end"]
    if seg.shape == "exponential":
        return p["end"] + (p["start"] - p["end"]) * math.exp(-min(u, p["exp_time"]) / p["tau"])
    raise ValueError("shape must be a valid waveform")


def channel_duration(ch: ChannelList) -> float:
    """Return delay plus all expanded segment durations."""
    return ch.delay + sum(base_duration(seg) * seg.repeat for seg in ch.segments)


def auto_dwell(plan: ListPlan) -> float:
    """Choose the smallest grid interval fitting the ideal period."""
    period = max((channel_duration(ch) for ch in plan.channels if ch.enabled and ch.segments), default=0.0)
    dwell = max(CDWELL_DWELL_MIN, period / CDWELL_MAX_POINTS)
    return math.ceil(dwell / CDWELL_DWELL_GRANULARITY) * CDWELL_DWELL_GRANULARITY


def _check_segment(seg: Segment, dwell: float, where: str, c: Compiled) -> None:
    if seg.shape in ("sine", "triangle"):
        points = 1 / (seg.params["frequency"] * dwell)
        if points < BLOCK_PTS_PER_PERIOD:
            c.errors.append(f"{where}: {points:g} points per period, fewer than {BLOCK_PTS_PER_PERIOD}")
        elif points < WARN_PTS_PER_PERIOD:
            c.warnings.append(f"{where}: {points:g} points per period, fewer than {WARN_PTS_PER_PERIOD}")
    for key in ("ramp_time", "up_time", "down_time", "tau"):
        edge = seg.params.get(key, 0)
        if edge > 0 and edge / dwell < WARN_PTS_PER_EDGE:
            c.warnings.append(f"{where}: {key} has fewer than {WARN_PTS_PER_EDGE} points; edge may be too coarse")


def _validate(plan: ListPlan, live: list[ChannelList], c: Compiled) -> None:
    if type(plan.repeat) is not int or not 1 <= plan.repeat <= CDWELL_COUNT_MAX:
        c.errors.append(f"plan: repeat must be in 1..{CDWELL_COUNT_MAX}")
    seen = set()
    for ch in live:
        where = f"CH{ch.channel}"
        if type(ch.channel) is not int or ch.channel < 1 or ch.channel in seen:
            c.errors.append(f"{where}: channel must be a unique positive integer")
        seen.add(ch.channel)
        if not math.isfinite(ch.delay) or ch.delay < 0:
            c.errors.append(f"{where}: delay must be a finite number >= 0")
        if ch.current_limit is not None and (not math.isfinite(ch.current_limit) or ch.current_limit <= 0):
            c.errors.append(f"{where}: current_limit must be > 0")
        for i, seg in enumerate(ch.segments, start=1):
            where = f"CH{ch.channel} segment {i}"
            if seg.shape not in FIELDS or set(seg.params) != set(FIELDS[seg.shape]):
                c.errors.append(f"{where}: shape or params fields do not match")
                continue
            if type(seg.repeat) is not int or seg.repeat < 1:
                c.errors.append(f"{where}: repeat must be an integer >= 1")
            for key, v in seg.params.items():
                if type(v) not in (int, float) or not math.isfinite(v):
                    c.errors.append(f"{where}: {key} must be a finite number")
                elif key in ("frequency", "tau") and v <= 0:
                    c.errors.append(f"{where}: {key} must be > 0")
                elif key in TIMES and v < 0:
                    c.errors.append(f"{where}: {key} must be >= 0")
                elif key == "n_steps" and (type(v) is not int or v < 1):
                    c.errors.append(f"{where}: n_steps must be an integer >= 1")


def compile_plan(plan: ListPlan) -> Compiled:
    """Compile without raising; errors prevent execution."""
    c = Compiled(0.0, 0, {}, {}, {}, {}, [], [])
    try:
        live = [ch for ch in plan.channels if ch.enabled and ch.segments]
        if not live:
            c.errors.append("no enabled channel with segments")
            return c
        _validate(plan, live, c)
        if c.errors:
            return c
        for ch in live:
            for i, seg in enumerate(ch.segments, start=1):
                if base_duration(seg) == 0:
                    c.errors.append(f"CH{ch.channel} segment {i}: segment duration must be > 0")
        if c.errors:
            return c
        if plan.dwell is None:
            c.dwell = auto_dwell(plan)
        else:
            c.dwell = round(plan.dwell / CDWELL_DWELL_GRANULARITY) * CDWELL_DWELL_GRANULARITY
        if not CDWELL_DWELL_MIN <= c.dwell <= CDWELL_DWELL_MAX:
            c.errors.append(f"dwell must be in {CDWELL_DWELL_MIN:g}..{CDWELL_DWELL_MAX:g} s")
            return c

        for ch in live:
            cum_ideal = ch.delay
            cum_pts = c.delay_points[ch.channel] = round(ch.delay / c.dwell)
            info = c.segments[ch.channel] = []
            for i, seg in enumerate(ch.segments, start=1):
                where = f"CH{ch.channel} segment {i}"
                want = base_duration(seg) * seg.repeat
                cum_ideal += want
                n = round(cum_ideal / c.dwell) - cum_pts
                if n < 1:
                    n = 1
                    c.warnings.append(f"{where}: rounds to 0 points, kept 1 point")
                info.append(SegmentInfo(n, want, n * c.dwell))
                cum_pts += n
                _check_segment(seg, c.dwell, where, c)
                if i > 1:
                    previous = ch.segments[i - 2]
                    jump = abs(value(previous, base_duration(previous)) - value(seg, 0))
                    if jump > DISCONTINUITY_V:
                        c.warnings.append(f"{where}: seam jump {jump:g} V between segments")
                if seg.repeat > 1 and abs(value(seg, base_duration(seg)) - value(seg, 0)) > DISCONTINUITY_V:
                    c.warnings.append(f"{where}: repeat seam jump within segment exceeds {DISCONTINUITY_V:g} V")
            c.n_points = max(c.n_points, cum_pts)
        for ch in live:
            length = c.delay_points[ch.channel] + sum(s.points for s in c.segments[ch.channel])
            c.pad_points[ch.channel] = c.n_points - length
        if c.n_points > CDWELL_MAX_POINTS:
            c.errors.append(f"{c.n_points:,} points exceed {CDWELL_MAX_POINTS:,}; increase dwell")
            return c

        for ch in live:
            pts = [value(ch.segments[0], 0)] * c.delay_points[ch.channel]
            for seg, info in zip(ch.segments, c.segments[ch.channel]):
                T = base_duration(seg)
                for k in range(info.points):
                    if k == info.points - 1:
                        pts.append(value(seg, T))
                        continue
                    # Rounded-up tails hold the endpoint instead of starting another repeat.
                    tau = min((k + 1) * c.dwell, info.requested)
                    u = tau - T * (math.ceil(tau / T) - 1)
                    if tau == info.requested or abs(u) <= 2 * math.ulp(tau):
                        u = T
                    pts.append(value(seg, u))
            pts.extend([pts[-1]] * c.pad_points[ch.channel])
            c.levels[ch.channel] = pts
    except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError) as exc:
        c.errors.append(f"plan: cannot synthesize; check parameters ({exc})")
    return c


def to_functions(plan: ListPlan, c: Compiled) -> list[ChannelFunction]:
    """Convert an executable compilation into core functions."""
    if c.errors:
        raise ValueError("; ".join(c.errors))
    return [
        ChannelFunction(
            channel=ch.channel, voltage=c.levels[ch.channel][0],
            current=ch.current_limit or 0.0, output_on=True, arb_enabled=True, delay=0.0,
            spec=ArbSpec(
                channel=ch.channel,
                waveform=ConstantDwellArb(levels=c.levels[ch.channel], dwell=c.dwell),
                count=plan.repeat, terminate_last=True,
            ),
        )
        for ch in plan.channels if ch.enabled and ch.segments
    ]
