"""ARB List parameters and JSON files, in seconds / volts / hertz / degrees."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

SHAPES = ("step", "ramp", "trapezoid", "sine", "triangle", "staircase", "exponential")

FIELDS: dict[str, tuple[str, ...]] = {
    "step": ("start", "end", "delay", "end_time"),
    "ramp": ("start", "end", "delay", "ramp_time", "end_time"),
    "trapezoid": ("offset", "amplitude", "start_time", "up_time", "high_time", "down_time", "end_time"),
    "sine": ("offset", "amplitude", "frequency", "phase"),
    "triangle": ("offset", "amplitude", "frequency", "phase"),
    "staircase": ("start", "end", "delay", "n_steps", "stair_time", "end_time"),
    "exponential": ("start", "end", "delay", "tau", "exp_time", "end_time"),
}

TIMES = {
    "delay", "end_time", "ramp_time", "start_time", "up_time", "high_time",
    "down_time", "stair_time", "exp_time", "tau",
}


@dataclass
class Segment:
    """One waveform and its repeat count."""

    shape: str
    params: dict[str, float]
    repeat: int = 1


@dataclass
class ChannelList:
    """An ordered list of segments for one global channel."""

    channel: int
    enabled: bool = True
    label: str = ""
    delay: float = 0.0
    current_limit: float | None = None
    segments: list[Segment] = field(default_factory=list)


@dataclass
class ListPlan:
    """Channels sharing a dwell and a whole-list repeat count."""

    channels: list[ChannelList]
    repeat: int = 1
    hold_last: bool = False
    dwell: float | None = None


def _number(value, name: str, location: str) -> None:
    if type(value) not in (int, float):
        raise ValueError(f"{location}: {name} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{location}: {name} must be a finite number")


def _integer(value, name: str, location: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{location}: {name} must be an integer >= 1")


def _object(value, location: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{location}: must be a JSON object")


def _list(value, name: str, location: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{location}: {name} must be an array")


def _flag(value, name: str, location: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{location}: {name} must be a boolean")


def load_json(path: str | Path) -> ListPlan:
    """Load a plan, reporting format errors with their location."""
    try:
        with open(path, encoding="utf-8-sig") as handle:
            raw = json.load(handle)
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise ValueError(f"plan: cannot read JSON: {exc}") from exc
    _object(raw, "plan")
    repeat = raw.get("repeat", 1)
    _integer(repeat, "repeat", "plan")
    if repeat > 256:
        raise ValueError("plan: repeat must be in 1..256")
    hold_last = raw.get("hold_last", False)
    _flag(hold_last, "hold_last", "plan")
    dwell = raw.get("dwell")
    if dwell is not None:
        _number(dwell, "dwell", "plan")
    raw_channels = raw.get("channels")
    _list(raw_channels, "channels", "plan")
    channels: list[ChannelList] = []
    seen: set[int] = set()
    for index, raw_ch in enumerate(raw_channels, start=1):
        location = f"plan channel {index}"
        _object(raw_ch, location)
        channel = raw_ch.get("channel")
        _integer(channel, "channel", location)
        location = f"CH{channel}"
        if channel in seen:
            raise ValueError(f"{location}: duplicate channel is not allowed")
        seen.add(channel)
        enabled = raw_ch.get("enabled", True)
        _flag(enabled, "enabled", location)
        label = raw_ch.get("label", "")
        if not isinstance(label, str):
            raise ValueError(f"{location}: label must be a string")
        delay = raw_ch.get("delay", 0.0)
        _number(delay, "delay", location)
        if delay < 0:
            raise ValueError(f"{location}: delay must be >= 0")
        current_limit = raw_ch.get("current_limit")
        if current_limit is not None:
            _number(current_limit, "current_limit", location)
            if current_limit <= 0:
                raise ValueError(f"{location}: current_limit must be > 0 (leave blank for no limit)")
        raw_segments = raw_ch.get("segments", [])
        _list(raw_segments, "segments", location)
        segments: list[Segment] = []
        for number, raw_seg in enumerate(raw_segments, start=1):
            seg_location = f"{location} segment {number}"
            _object(raw_seg, seg_location)
            shape = raw_seg.get("shape")
            if not isinstance(shape, str) or shape not in SHAPES:
                raise ValueError(f"{seg_location}: shape must be a valid waveform")
            params = raw_seg.get("params")
            _object(params, seg_location)
            missing = [key for key in FIELDS[shape] if key not in params]
            extra = [key for key in params if key not in FIELDS[shape]]
            if missing or extra:
                raise ValueError(
                    f"{seg_location}: params fields do not match; "
                    f"missing: {', '.join(missing) or 'none'}; extra: {', '.join(extra) or 'none'}"
                )
            for key in FIELDS[shape]:
                value = params[key]
                _number(value, key, seg_location)
                if key in ("frequency", "tau") and value <= 0:
                    raise ValueError(f"{seg_location}: {key} must be > 0")
                if key in TIMES and value < 0:
                    raise ValueError(f"{seg_location}: {key} must be >= 0")
                if key == "n_steps":
                    _integer(value, key, seg_location)
            seg_repeat = raw_seg.get("repeat", 1)
            _integer(seg_repeat, "repeat", seg_location)
            segments.append(Segment(shape, params, seg_repeat))
        channels.append(ChannelList(channel, enabled, label, delay, current_limit, segments))
    return ListPlan(channels, repeat, hold_last, dwell)


def write_json(plan: ListPlan, path: str | Path) -> None:
    """Write a versioned UTF-8 plan."""
    try:
        text = json.dumps({"version": 1, **asdict(plan)}, ensure_ascii=False, indent=2, allow_nan=False)
        Path(path).write_text(text + "\n", encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"plan: cannot write JSON: {exc}") from exc
