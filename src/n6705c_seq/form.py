"""Sequence table data and preview, independent of the GUI toolkit."""

from __future__ import annotations

import json
import math
from pathlib import Path

from n6705c.core.group import TRIGGER_SKEW_SECONDS

from .i18n import tr
from .plan import SeqRow, order_warnings

FRAMES = 2
CONFIG: Path = Path.home() / ".n6705c_seq_gui.json"
PALETTE = ["#e05252", "#4a7fe8", "#55b685", "#e8833a", "#a855f0", "#52b2d0", "#e0b43c", "#dc5090"]

# name, delay, low, high, rise, hold, fall, current limit  (ms / V / A)
DEFAULTS = [
    ("CH1", 0, 0, 12, 3, 60, 3), ("CH2", 5, 0, 5, 2, 55, 2), ("CH3", 10, 0, 3.3, 2, 50, 2),
    ("CH4", 15, 0, 1.8, 1, 45, 1), ("CH5", 8, 0, 24, 4, 65, 4), ("CH6", 20, 0, 9, 3, 40, 3),
    ("CH7", 0, 0, 5, 2, 30, 2), ("CH8", 12, 0, 12, 3, 50, 3),
]  # fmt: skip
COLUMNS = [
    ("name", "column_name"), ("delay", "column_delay"), ("low", "column_low"),
    ("high", "column_high"), ("rise", "column_rise"), ("hold", "column_hold"),
    ("fall", "column_fall"), ("limit", "column_limit"),
]  # fmt: skip
TITLES = dict(COLUMNS)
NUMERIC = {"delay": "ms", "low": "V", "high": "V", "rise": "ms", "hold": "ms", "fall": "ms"}
RAW_KEYS = ("enabled", "name", "delay", "low", "high", "rise", "hold", "fall", "limit")


def waveform(row: SeqRow, t_end: float, hold_dc: bool = False) -> list[tuple[float, float]]:
    """Corner points (seconds, volts) of one rail, from t=0 to *t_end*."""
    t1 = row.delay
    t2 = t1 + row.rise
    if hold_dc:
        return [(0, row.low_v), (t1, row.low_v), (t2, row.high_v), (t_end, row.high_v)]
    t3 = t2 + row.hold
    t4 = t3 + row.fall
    return [(0, row.low_v), (t1, row.low_v), (t2, row.high_v), (t3, row.high_v), (t4, row.low_v), (t_end, row.low_v)]


def tick_step(span_ms: float) -> int:
    """1/2/5 x 10^k ms, at most ~10 ticks, never below 1 ms."""
    raw = max(span_ms / 10, 1)
    decade = 10 ** math.floor(math.log10(raw))
    return int(next(m * decade for m in (1, 2, 5, 10) if m * decade >= raw))


def load_config(path: Path) -> dict:
    """Saved addresses; a missing or damaged file just means the defaults."""
    try:
        saved = json.loads(path.read_text())
        resources, demo = saved["resources"], saved["demo"]
        if not (isinstance(resources, list) and all(isinstance(r, str) for r in resources)
                and isinstance(demo, bool)):
            raise TypeError("bad schema")
        return {"resources": resources, "demo": demo,
                "lang": saved.get("lang") if saved.get("lang") in ("zh", "en") else "zh"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"resources": [], "demo": True, "lang": "zh"}


def save_config(path: Path, resources: list[str], demo: bool, lang: str) -> None:
    path.write_text(json.dumps({"resources": resources, "demo": demo, "lang": lang}))


def default_raw(count: int) -> list[dict]:
    rows = []
    for i in range(count):
        d = DEFAULTS[i] if i < len(DEFAULTS) else (f"CH{i + 1}", 0, 0, 1, 1, 10, 1)
        rows.append({"enabled": i < len(DEFAULTS), "name": d[0],
                     **{key: f"{value:g}" for key, value in zip(NUMERIC, d[1:])}, "limit": ""})
    return rows


def parse_rows(raw: list[dict]) -> list[SeqRow]:
    if not isinstance(raw, list):
        raise TypeError("rows must be a list")
    for row in raw:
        if not isinstance(row, dict) or set(row) != set(RAW_KEYS):
            raise TypeError("each row must be a dict with exactly RAW_KEYS")
        if not isinstance(row["enabled"], bool) or any(not isinstance(row[key], str) for key in RAW_KEYS[1:]):
            raise TypeError("enabled must be bool and all other row values must be str")
    rows = []
    for i, f in enumerate(raw, start=1):
        on = f["enabled"]
        label = f["name"] or f"CH{i}"
        vals = {}
        for key, unit in NUMERIC.items():
            try:
                v = float(f[key])
                if v < 0 or not math.isfinite(v):
                    raise ValueError
            except ValueError:
                if on:
                    raise ValueError(tr("invalid_nonnegative", label=label, title=tr(TITLES[key]).splitlines()[0])) from None
                v = 0.0
            vals[key] = v / 1000 if unit == "ms" else v
        limit = f["limit"].strip()
        try:
            current = float(limit) if limit else None
            if current is not None and not (math.isfinite(current) and current > 0):
                raise ValueError
        except ValueError:
            if on:
                raise ValueError(tr("invalid_limit", label=label)) from None
            current = None
        rows.append(
            SeqRow(i, on, label, vals["delay"], vals["low"], vals["high"], vals["rise"], vals["hold"], vals["fall"], current)
        )
    return rows


def rows_to_raw(rows: list[SeqRow], current: list[dict]) -> list[dict]:
    if len(rows) > len(current):
        raise ValueError(tr("csv_capacity", rows=len(rows), channels=len(current)))
    raw = [dict(row) for row in current]
    for f, r in zip(raw, rows):
        f["enabled"] = r.enabled
        f["name"] = r.name
        for key, value in (("delay", r.delay * 1000), ("low", r.low_v), ("high", r.high_v),
                           ("rise", r.rise * 1000), ("hold", r.hold * 1000), ("fall", r.fall * 1000)):  # fmt: skip
            f[key] = f"{value:.10g}"
        f["limit"] = "" if r.current_limit is None else f"{r.current_limit:g}"
    for f in raw[len(rows):]:
        f["enabled"] = False
    return raw


def seq_view(rows: list[SeqRow], hold_dc: bool, measured: dict) -> dict:
    view = {"message": "", "t_end": 1.0, "ticks": [], "time_axis": tr("time_axis"),
            "measured_label": tr("measured"), "lanes": []}
    warnings = order_warnings(rows, TRIGGER_SKEW_SECONDS)
    view["message"] = "\n".join(f"⚠ {w}" for w in warnings[:3]) + (
        tr("more_warnings", count=len(warnings) - 3) if len(warnings) > 3 else "")
    live = [r for r in rows if r.enabled]
    if not live:
        return view
    t_end = max((r.delay + r.rise if hold_dc else r.duration) for r in live) * 1.18 * 1000 or 1.0
    view["t_end"] = t_end
    view["ticks"] = list(range(0, int(t_end) + 1, tick_step(t_end)))
    for r in live:
        got = None
        if r.channel in measured:
            dt, volts = measured[r.channel]
            stride = max(1, math.ceil(len(volts) / 2000))
            got = [[i * dt * 1000, volts[i]] for i in range(0, len(volts), stride) if i * dt * 1000 <= t_end]
        view["lanes"].append({
            "channel": r.channel, "name": r.name, "low": r.low_v, "high": max(r.high_v, r.low_v + 1e-9),
            "points": [[t * 1000, v] for t, v in waveform(r, t_end / 1000, hold_dc)],
            "delay": r.delay * 1000, "top0": (r.delay + r.rise) * 1000,
            "top1": (r.delay + r.rise + (0 if hold_dc else r.hold)) * 1000,
            "delay_text": tr("delay_annotation", value=r.delay * 1000),
            "hold_text": None if hold_dc else tr("hold_annotation", value=r.hold * 1000),
            "levels_text": tr("levels_annotation", high=r.high_v, low=r.low_v), "measured": got,
        })
    return view
