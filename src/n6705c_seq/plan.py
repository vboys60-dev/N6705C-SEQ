"""A power-up sequencing table: CSV rows -> one trapezoid per channel.

The row order *is* the channel number (row 1 = CH1 ... row 5 = F2:CH1), which
matches the table the sequence is designed in, disabled rows included.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, replace
from itertools import combinations
from pathlib import Path

from n6705c.core.arb import ArbSpec, TrapezoidArb
from n6705c.core.function import ChannelFunction
from n6705c.core.group import CHANNELS_PER_FRAME, GlobalChannel

__all__ = [
    "SeqRow",
    "load_csv",
    "write_csv",
    "to_functions",
    "order_warnings",
    "suggest_assignment",
    "timeline",
]

REQUIRED = ("enabled", "delay_ms", "low_v", "high_v", "rise_ms", "hold_ms", "fall_ms")
TRUE = {"1", "true", "yes", "y"}
FALSE = {"0", "false", "no", "n"}


@dataclass(frozen=True)
class SeqRow:
    """One channel's step, in seconds / volts / amps."""

    channel: int
    enabled: bool
    name: str
    delay: float
    low_v: float
    high_v: float
    rise: float
    hold: float
    fall: float
    current_limit: float | None = None

    @property
    def label(self) -> str:
        return GlobalChannel.from_index(self.channel).label()

    @property
    def duration(self) -> float:
        return self.delay + self.rise + self.hold + self.fall


def _number(row: dict, column: str, line: int) -> float:
    text = (row.get(column) or "").strip()
    if not text:
        raise ValueError(f"line {line}, column {column}: value is missing")
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"line {line}, column {column}: {text!r} is not a number") from None
    if not math.isfinite(value):
        raise ValueError(f"line {line}, column {column}: {text} is not a finite number")
    if value < 0:
        raise ValueError(f"line {line}, column {column}: {text} is negative")
    return value


def _flag(row: dict, line: int) -> bool:
    text = (row.get("enabled") or "").strip().lower()
    if text in TRUE:
        return True
    if text in FALSE:
        return False
    raise ValueError(f"line {line}, column enabled: {text!r} is not 1/0/yes/no")


def load_csv(path: str | Path) -> list[SeqRow]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"missing column(s): {', '.join(missing)}")
        rows: list[SeqRow] = []
        for number, raw in enumerate(reader, start=1):
            line = reader.line_num
            limit = (raw.get("current_limit_a") or "").strip()
            if limit and _number(raw, "current_limit_a", line) == 0:
                raise ValueError(f"line {line}, column current_limit_a: 0 A is not a limit; leave it blank for none")
            rows.append(
                SeqRow(
                    channel=number,
                    enabled=_flag(raw, line),
                    name=(raw.get("name") or f"CH{number}").strip(),
                    delay=_number(raw, "delay_ms", line) / 1000,
                    low_v=_number(raw, "low_v", line),
                    high_v=_number(raw, "high_v", line),
                    rise=_number(raw, "rise_ms", line) / 1000,
                    hold=_number(raw, "hold_ms", line) / 1000,
                    fall=_number(raw, "fall_ms", line) / 1000,
                    current_limit=_number(raw, "current_limit_a", line) if limit else None,
                )
            )
    if not rows:
        raise ValueError("the file has no data rows")
    return rows


def to_functions(rows: list[SeqRow]) -> list[ChannelFunction]:
    """Enabled rows only. The delay becomes the trapezoid's start time (count is 1,
    so it happens once), so ``ChannelFunction.delay`` stays 0 -- never both."""
    return [
        ChannelFunction(
            channel=r.channel,
            voltage=r.low_v,
            current=r.current_limit or 0.0,
            output_on=True,
            arb_enabled=True,
            delay=0.0,
            spec=ArbSpec(
                channel=r.channel,
                waveform=TrapezoidArb(
                    start_level=r.low_v,
                    start_time=r.delay,
                    rise_time=r.rise,
                    top_level=r.high_v,
                    top_time=r.hold,
                    fall_time=r.fall,
                    end_time=0.0,
                ),
                terminate_last=True,
            ),
        )
        for r in rows
        if r.enabled
    ]


def _tied(a: SeqRow, b: SeqRow, skew: float) -> bool:
    """Closer than the skew -- rounded, since 0.031 - 0.029 is 0.00199999... in floats."""
    return round(abs(a.delay - b.delay), 9) < skew


def order_warnings(rows: list[SeqRow], skew: float) -> list[str]:
    """Pairs on different mainframes whose start order the trigger skew can flip."""
    live = [r for r in rows if r.enabled]
    warnings = []
    for a, b in combinations(live, 2):
        if GlobalChannel.from_index(a.channel).frame == GlobalChannel.from_index(b.channel).frame:
            continue
        if _tied(a, b, skew):
            gap = abs(a.delay - b.delay)
            warnings.append(
                f"{a.label} ({a.name}) and {b.label} ({b.name}) start {gap * 1000:g} ms "
                f"apart on different mainframes; their order is not guaranteed "
                f"(trigger skew up to {skew * 1000:g} ms)"
            )
    return warnings


def timeline(rows: list[SeqRow]) -> str:
    return "\n".join(
        f"{r.label:7} {r.name:8} delay {r.delay * 1000:g} ms, "
        f"{r.low_v:g} -> {r.high_v:g} V in {r.rise * 1000:g} ms, "
        f"hold {r.hold * 1000:g} ms, fall {r.fall * 1000:g} ms "
        f"(done at {r.duration * 1000:g} ms)"
        + (f", limit {r.current_limit:g} A" if r.current_limit is not None else "")
        for r in rows
        if r.enabled
    )


def _pack(clusters: list[list[SeqRow]], frames: int, per_frame: int) -> list[list[SeqRow]] | None:
    """Fit tie-groups (which cannot be split) into frames, or ``None``."""
    # Contiguous in delay order first: early rails together on frame 1 reads naturally.
    bins: list[list[SeqRow]] = [[]]
    for cluster in clusters:
        if len(cluster) > per_frame:
            return None
        if len(bins[-1]) + len(cluster) > per_frame:
            bins.append([])
        bins[-1] += cluster
    if len(bins) <= frames:
        return bins
    # ponytail: first-fit decreasing, not optimal bin packing; fine for a handful of groups
    bins = [[] for _ in range(frames)]
    for cluster in sorted(clusters, key=len, reverse=True):
        target = next((b for b in bins if len(b) + len(cluster) <= per_frame), None)
        if target is None:
            return None
        target += cluster
    return bins


def suggest_assignment(
    rows: list[SeqRow], skew: float, frames: int, per_frame: int = CHANNELS_PER_FRAME
) -> tuple[list[SeqRow], list[tuple[SeqRow, int]]] | None:
    """Re-place channels so no pair on different frames starts within *skew*.

    Rows closer than the skew (chained: A~B and B~C puts all three together) form
    a group that has to share a frame -- one ``*TRG`` starts a frame's channels at
    the same instant.  Returns the new table (position = channel, unused slots
    filled with disabled spares so frame 2 still starts on row 5) and the moves
    as ``(old row, new channel)``; ``None`` if no placement exists.
    """
    live = sorted((r for r in rows if r.enabled), key=lambda r: (r.delay, r.channel))
    clusters: list[list[SeqRow]] = []
    for row in live:
        if clusters and _tied(clusters[-1][-1], row, skew):
            clusters[-1].append(row)
        else:
            clusters.append([row])
    bins = _pack(clusters, frames, per_frame)
    if bins is None:
        return None
    while bins and not bins[-1]:
        bins.pop()

    spare = [r for r in rows if not r.enabled]
    filler = SeqRow(0, False, "(unused)", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    placed: list[SeqRow] = []
    for index, members in enumerate(bins):
        placed += sorted(members, key=lambda r: (r.delay, r.channel))
        if index < len(bins) - 1:
            placed += [spare.pop(0) if spare else filler for _ in range(per_frame - len(members))]
    placed += spare

    moves = [(r, n) for n, r in enumerate(placed, start=1) if r.enabled and r.channel != n]
    return [replace(r, channel=n) for n, r in enumerate(placed, start=1)], moves


def write_csv(rows: list[SeqRow], path: str | Path) -> None:
    def ms(seconds: float) -> str:
        return f"{seconds * 1000:.10g}"

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([*REQUIRED[:1], "name", *REQUIRED[1:], "current_limit_a"])
        for r in rows:
            writer.writerow(
                [
                    int(r.enabled),
                    r.name,
                    ms(r.delay),
                    f"{r.low_v:g}",
                    f"{r.high_v:g}",
                    ms(r.rise),
                    ms(r.hold),
                    ms(r.fall),
                    "" if r.current_limit is None else f"{r.current_limit:g}",
                ]
            )
