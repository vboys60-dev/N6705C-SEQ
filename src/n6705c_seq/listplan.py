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
        raise ValueError(f"{location}：{name} 必須是數字")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{location}：{name} 必須是有限數字")


def _integer(value, name: str, location: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{location}：{name} 必須是 >= 1 的整數")


def _object(value, location: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{location}：必須是 JSON 物件")


def _list(value, name: str, location: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{location}：{name} 必須是陣列")


def _flag(value, name: str, location: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{location}：{name} 必須是布林值")


def load_json(path: str | Path) -> ListPlan:
    """Load a plan, reporting format errors with their location."""
    try:
        with open(path, encoding="utf-8-sig") as handle:
            raw = json.load(handle)
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise ValueError(f"計畫：無法讀取 JSON：{exc}") from exc
    _object(raw, "計畫")
    repeat = raw.get("repeat", 1)
    _integer(repeat, "repeat", "計畫")
    if repeat > 256:
        raise ValueError("計畫：repeat 必須在 1..256")
    hold_last = raw.get("hold_last", False)
    _flag(hold_last, "hold_last", "計畫")
    dwell = raw.get("dwell")
    if dwell is not None:
        _number(dwell, "dwell", "計畫")
    raw_channels = raw.get("channels")
    _list(raw_channels, "channels", "計畫")
    channels: list[ChannelList] = []
    seen: set[int] = set()
    for index, raw_ch in enumerate(raw_channels, start=1):
        location = f"計畫第 {index} 個通道"
        _object(raw_ch, location)
        channel = raw_ch.get("channel")
        _integer(channel, "channel", location)
        location = f"CH{channel}"
        if channel in seen:
            raise ValueError(f"{location}：通道不可以重複")
        seen.add(channel)
        enabled = raw_ch.get("enabled", True)
        _flag(enabled, "enabled", location)
        label = raw_ch.get("label", "")
        if not isinstance(label, str):
            raise ValueError(f"{location}：label 必須是字串")
        delay = raw_ch.get("delay", 0.0)
        _number(delay, "delay", location)
        if delay < 0:
            raise ValueError(f"{location}：delay 必須 >= 0")
        current_limit = raw_ch.get("current_limit")
        if current_limit is not None:
            _number(current_limit, "current_limit", location)
            if current_limit <= 0:
                raise ValueError(f"{location}：current_limit 必須 > 0（不限流請留空）")
        raw_segments = raw_ch.get("segments", [])
        _list(raw_segments, "segments", location)
        segments: list[Segment] = []
        for number, raw_seg in enumerate(raw_segments, start=1):
            seg_location = f"{location} 第 {number} 段"
            _object(raw_seg, seg_location)
            shape = raw_seg.get("shape")
            if not isinstance(shape, str) or shape not in SHAPES:
                raise ValueError(f"{seg_location}：shape 必須是合法波形")
            params = raw_seg.get("params")
            _object(params, seg_location)
            missing = [key for key in FIELDS[shape] if key not in params]
            extra = [key for key in params if key not in FIELDS[shape]]
            if missing or extra:
                raise ValueError(
                    f"{seg_location}：params 欄位不符；"
                    f"缺少：{', '.join(missing) or '無'}；多餘：{', '.join(extra) or '無'}"
                )
            for key in FIELDS[shape]:
                value = params[key]
                _number(value, key, seg_location)
                if key in ("frequency", "tau") and value <= 0:
                    raise ValueError(f"{seg_location}：{key} 必須 > 0")
                if key in TIMES and value < 0:
                    raise ValueError(f"{seg_location}：{key} 必須 >= 0")
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
        raise ValueError(f"計畫：無法寫入 JSON：{exc}") from exc
