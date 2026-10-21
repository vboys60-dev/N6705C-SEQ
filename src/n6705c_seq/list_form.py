"""List editor validation and waveform view data."""

from __future__ import annotations

import copy
import json
import math
import tempfile
from dataclasses import asdict
from pathlib import Path

from . import synth
from .i18n import tr
from .listplan import FIELDS, SHAPES, TIMES, ChannelList, ListPlan, Segment, load_json

SHAPE_NAMES = ("Step", "Ramp", "Trapezoid", "Sine", "Triangle", "Staircase", "Exponential")


def format_number(value) -> str:
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def number(text, title, *, minimum=None, positive=False, integer=False):
    try:
        value = float(text)
        if not math.isfinite(value) or (minimum is not None and value < minimum) or (positive and value <= 0):
            raise ValueError
        if integer and not value.is_integer():
            raise ValueError
    except ValueError:
        requirement = tr("positive_integer") if integer and positive else tr("positive_number") if positive else tr("nonnegative_number") if minimum == 0 else tr("finite_number")
        raise ValueError(tr("number_error", title=title, requirement=requirement)) from None
    return int(value) if integer else value


def meta() -> dict:
    defaults = {"end": 1, "amplitude": 1, "frequency": 100, "n_steps": 5, "tau": 0.01}
    return {
        "shapes": [[key, name] for key, name in zip(SHAPES, SHAPE_NAMES)],
        "fields": {shape: [
            {"key": key,
             "unit": "s" if key in TIMES else "Hz" if key == "frequency" else "deg" if key == "phase" else "" if key == "n_steps" else "V",
             "default": format_number(defaults.get(key, 0.01 if key in TIMES else 0))}
            for key in FIELDS[shape]] for shape in SHAPES},
    }


def read_segment(shape: str, params: dict[str, str], repeat: str) -> Segment:
    if not isinstance(shape, str) or shape not in SHAPES:
        raise TypeError("shape must be one of SHAPES")
    if (not isinstance(params, dict) or params.keys() != set(FIELDS[shape])
            or any(not isinstance(value, str) for value in params.values())):
        raise TypeError("params must contain exactly the shape fields with string values")
    if not isinstance(repeat, str):
        raise TypeError("repeat must be a string")
    values = {key: number(params[key], tr(key), minimum=0 if key in TIMES else None,
                          positive=key in ("frequency", "tau", "n_steps"), integer=key == "n_steps")
              for key in FIELDS[shape]}
    return Segment(shape, values, number(repeat, tr("segment_repeat"), positive=True, integer=True))


def parse_settings(raw: dict) -> dict:
    types = {"channel": int, "enabled": bool, "label": str, "delay": str, "limit": str,
             "repeat": str, "hold_last": bool, "dwell_mode": str, "dwell": str}
    if not isinstance(raw, dict) or raw.keys() != types.keys() or any(type(raw[key]) is not kind for key, kind in types.items()):
        raise TypeError("settings must contain the specified fields and types")
    if raw["dwell_mode"] not in ("auto", "manual"):
        raise TypeError("dwell_mode must be auto or manual")
    settings = dict(raw)
    settings["delay"] = number(raw["delay"], tr("channel_delay"), minimum=0)
    settings["limit"] = number(raw["limit"], tr("current_limit"), positive=True) if raw["limit"].strip() else None
    settings["repeat"] = number(raw["repeat"], tr("sequence_repeat"), positive=True, integer=True)
    if settings["repeat"] > 256:
        raise ValueError(tr("repeat_range"))
    settings["dwell"] = number(raw["dwell"], tr("dwell"), positive=True) if raw["dwell_mode"] == "manual" else None
    return settings


def apply_settings(plan: ListPlan, settings: dict) -> ListPlan:
    result = copy.deepcopy(plan)
    channel = settings["channel"]
    ch = next((ch for ch in result.channels if ch.channel == channel), None)
    new = ch is None
    if new:
        ch = ChannelList(channel, enabled=False)
    ch.enabled, ch.label, ch.delay, ch.current_limit = settings["enabled"], settings["label"], settings["delay"], settings["limit"]
    if new and ch != ChannelList(channel, enabled=False):
        result.channels.append(ch)
    result.repeat, result.hold_last, result.dwell = settings["repeat"], settings["hold_last"], settings["dwell"]
    return result


def plan_to_dict(plan: ListPlan) -> dict:
    return asdict(plan)


def plan_from_dict(raw: dict) -> ListPlan:
    try:
        text = json.dumps(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"plan: cannot read JSON: {exc}") from exc
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "plan.json"
        path.write_text(text, encoding="utf-8")
        return load_json(path)


def _time_label(seconds):
    return f"{seconds * 1000:.4g} ms" if seconds < 1 else f"{seconds:.4g} s"


def _samples(segment):
    duration = synth.base_duration(segment)
    return [synth.value(segment, duration * i / 199) for i in range(200)]


def draft_view(shape: str, params: dict[str, str], repeat: str) -> dict:
    try:
        segment = read_segment(shape, params, repeat)
        duration = synth.base_duration(segment)
        values = _samples(segment)
    except ValueError as exc:
        return {"error": str(exc), "segment": None, "preview": None}
    p = segment.params
    periodic = shape in ("sine", "triangle")
    if periodic or shape == "trapezoid":
        levels = [(tr("offset"), p["offset"]), (tr("offset_amp"), p["offset"] + p["amplitude"])]
    else:
        levels = [(tr("start_label"), p["start"]), (tr("end_label"), p["end"])]
    bounds = values + [v for _, v in levels]
    if periodic:
        bounds.append(p["offset"] - p["amplitude"])
    combined = (f"{levels[0][0]} = {levels[1][0]} {levels[0][1]:.4g} V"
                if levels[0][1] == levels[1][1] else
                f"{levels[0][0]} {levels[0][1]:.4g} V / {levels[1][0]} {levels[1][1]:.4g} V")
    times = [(tr("period"), duration)] if periodic else [
        (tr(key), p[key]) for key in FIELDS[shape]
        if key in TIMES and key != "tau" and p[key] > 0]
    extras = []
    if periodic:
        extras.append(tr("frequency_phase", frequency=p["frequency"], phase=p["phase"]))
    elif shape == "staircase":
        extras.append(tr("steps_annotation", value=p["n_steps"]))
    elif shape == "exponential":
        extras.append(tr("tau_annotation", value=_time_label(p["tau"])))
    if segment.repeat > 1:
        extras.append(f"×{segment.repeat}")
    return {"error": None, "segment": asdict(segment), "preview": {
        "samples": values, "duration": duration, "lo": min(bounds), "hi": max(bounds), "periodic": periodic,
        "levels": [{"text": f"{name} {v:.4g} V", "value": v} for name, v in levels[:1 if periodic else 2]],
        "combined": combined,
        "times": [{"text": f"{title} {_time_label(seconds)}", "seconds": seconds} for title, seconds in times],
        "amplitude": {"text": tr("amplitude_annotation", value=p["amplitude"]),
                      "base": p["offset"], "peak": p["offset"] + p["amplitude"]} if periodic else None,
        "extras": "   ".join(extras),
    }}


def compiled_view(plan: ListPlan, settings: dict, segment_index: int | None, measured: dict) -> dict:
    settings_error = ""
    try:
        parsed = parse_settings(settings)
    except ValueError as exc:
        parsed = None
        settings_error = str(exc)
    channel = settings["channel"]
    ch = next((ch for ch in plan.channels if ch.channel == channel), ChannelList(channel, enabled=False))
    result = {"ok": False, "message": settings_error, "statistics": "", "plan": None,
              "n_points": 0, "repeat_text": f"× {plan.repeat}", "axis": [""] * 5,
              "budget": None, "lanes": [], "segments": []}
    for i, segment in enumerate(ch.segments):
        result["segments"].append({"title": f"{i + 1} {SHAPE_NAMES[SHAPES.index(segment.shape)]} ×{segment.repeat}",
                                   "samples": _samples(segment)})
    if parsed is None:
        return result
    try:
        plan = apply_settings(plan, parsed)
        c = synth.compile_plan(plan)
    except ValueError as exc:
        result["message"] = str(exc)
        return result
    result.update(ok=not c.errors, message="\n".join([f"✘ {e}" for e in c.errors] + [f"⚠ {w}" for w in c.warnings]),
                  plan=plan_to_dict(plan), n_points=c.n_points, repeat_text=f"× {plan.repeat}")
    details = ""
    infos = c.segments.get(channel, [])
    if segment_index is not None and 0 <= segment_index < len(infos):
        info = infos[segment_index]
        details = tr("segment_statistics", index=segment_index + 1, points=info.points,
                     requested=info.requested, actual=info.actual, error=info.actual - info.requested)
    result["statistics"] = details + tr("statistics", period=c.n_points * c.dwell, dwell=c.dwell,
                                       segments=len(ch.segments), points=c.n_points,
                                       delay=c.delay_points.get(channel, 0), padding=c.pad_points.get(channel, 0))
    result["budget"] = {"n_points": c.n_points, "delay": c.delay_points.get(channel, 0), "pad": c.pad_points.get(channel, 0)}
    period = c.n_points * c.dwell
    scale, unit = (1000, "ms") if period < 1 else (1, "s")
    result["axis"] = [f"{period * scale * j / 4:.4g} {unit}" for j in range(5)]
    for ch in plan.channels:
        if not ch.enabled:
            continue
        values = c.levels.get(ch.channel, [])
        if not values:
            result["lanes"].append({"channel": ch.channel, "label": ch.label or f"CH{ch.channel}",
                                    "selected": ch.channel == channel, "lo": 0.0, "hi": 0.0,
                                    "steps": [], "measured": None})
            continue
        lo, hi = min(values), max(values)
        capture = measured.get(ch.channel)
        points = None
        if capture and capture[1]:
            dt, volts = capture
            lo, hi = min(lo, min(volts)), max(hi, max(volts))
            if c.dwell > 0:
                points = [[j * dt / c.dwell, v] for j, v in enumerate(volts) if j * dt <= period]
        steps = [[0, values[0]]]
        for k, value in enumerate(values):
            if k and value != values[k - 1]:
                steps.extend([[k, values[k - 1]], [k, value]])
        steps.append([len(values), values[-1]])
        result["lanes"].append({"channel": ch.channel, "label": ch.label or f"CH{ch.channel}",
                                "selected": ch.channel == channel, "lo": lo, "hi": hi,
                                "steps": steps, "measured": points})
    return result
