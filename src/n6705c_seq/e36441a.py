"""Turn sequencing rows into E36441A LIST / SLEW / TRIG commands.

Hardware assumption, not yet verified: LIST voltage transitions apply
VOLT:SLEW, and dwell starts at the transition point. If this proves false,
the commands must use a multi-point staircase instead.

Channels are local output numbers 1..4. Callers check rows before generating
commands, wait for low to settle after preparation, then configure and arm.
The caller sends *TRG once per instrument.
"""

from __future__ import annotations

import math

from n6705c.core.scpi import format_channel_list

from .plan import SeqRow

def _slew(dv: float, seconds: float) -> str:
    return "INF" if dv == 0 or seconds == 0 else f"{abs(dv) / seconds:.6G}"


def prepare_commands(row: SeqRow, ch: int) -> list[str]:
    """Set low quickly; the caller must wait for the output to settle."""
    chan = format_channel_list(ch)
    commands = [
        f"ABOR {chan}",
        f"INIT:CONT OFF,{chan}",
        f"VOLT:SLEW:RIS INF,{chan}",
        f"VOLT:SLEW:FALL INF,{chan}",
        f"VOLT {row.low_v:.6G},{chan}",
    ]
    if row.current_limit is not None:
        commands.append(f"CURR {row.current_limit:.6G},{chan}")
    commands += [
        f"CURR:MODE FIX,{chan}",
        f"VOLT:MODE FIX,{chan}",
        f"OUTP ON,{chan}",
    ]
    return commands


def configure_commands(row: SeqRow, ch: int, hold_dc: bool = False) -> list[str]:
    """Load the list and slew rates before selecting LIST mode."""
    chan = format_channel_list(ch)
    dv = row.high_v - row.low_v
    rising = _slew(dv, row.rise)
    falling = "INF" if hold_dc else _slew(dv, row.fall)
    if dv < 0:
        rising, falling = falling, rising
    volts = [row.high_v] if hold_dc else [row.high_v, row.low_v]
    dwells = [row.rise] if hold_dc else [row.rise + row.hold, row.fall]
    voltage = ",".join(f"{v:.6G}" for v in volts)
    dwell = ",".join(f"{max(0.01, t):.3f}" for t in dwells)
    current = row.current_limit if row.current_limit is not None else 10.3
    return [
        f"TRIG:SOUR BUS,{chan}",
        f"TRIG:DEL {row.delay:.3f},{chan}",
        f"VOLT:SLEW:RIS {rising},{chan}",
        f"VOLT:SLEW:FALL {falling},{chan}",
        f"LIST:VOLT {voltage},{chan}",
        f"LIST:DWEL {dwell},{chan}",
        f"LIST:CURR {current:.6G},{chan}",
        f"LIST:TOUT:BOST 0,{chan}",
        f"LIST:TOUT:EOST 0,{chan}",
        f"LIST:COUN 1,{chan}",
        f"LIST:STEP AUTO,{chan}",
        f"LIST:TERM:LAST ON,{chan}",
        f"VOLT:MODE LIST,{chan}",
        f"CURR:MODE FIX,{chan}",
    ]


def arm_commands(channels: list[int]) -> list[str]:
    """Initiate all selected channels in one command; triggering is separate."""
    return [f"INIT {format_channel_list(channels)}"]


def restore_commands(ch: int) -> list[str]:
    chan = format_channel_list(ch)
    return [
        f"ABOR {chan}",
        f"OUTP OFF,{chan}",
        f"VOLT:SLEW:RIS INF,{chan}",
        f"VOLT:SLEW:FALL INF,{chan}",
        f"LIST:TERM:LAST OFF,{chan}",
        f"VOLT:MODE FIX,{chan}",
    ]


def check(row: SeqRow, hold_dc: bool = False) -> tuple[list[str], list[str]]:
    """Return errors and timing warnings for the edges and dwells in use."""
    errors: list[str] = []
    warnings: list[str] = []
    for name, voltage in (("low_v", row.low_v), ("high_v", row.high_v)):
        if not 0 <= voltage <= 32:
            errors.append(f"{name} must be in 0..32 V")
    if row.current_limit is not None:
        if not 0 < row.current_limit <= 10:
            errors.append("current_limit must be in (0,10] A")
        if row.high_v * row.current_limit > 160:
            errors.append("high_v * current_limit exceeds 160 W")
    if row.delay > 3600:
        errors.append("delay exceeds 3600 s")
    if row.delay != round(row.delay, 3):
        warnings.append("delay will be rounded to 1 ms resolution")

    dv = abs(row.high_v - row.low_v)
    edges = [("rise", row.rise, 0.08)]
    dwells = [("rise", row.rise)] if hold_dc else [("rise+hold", row.rise + row.hold)]
    if not hold_dc:
        edges.append(("fall", row.fall, 0.5))
        dwells.append(("fall", row.fall))
    for name, seconds, settling in edges:
        if seconds == 0:
            warnings.append(f"{name} is 0: fastest slew, not zero elapsed time")
        else:
            # Do not reject the exact minimum because of float subtraction.
            rate = dv / seconds
            if dv != 0 and rate < 0.002 and not math.isclose(rate, 0.002, rel_tol=1e-12):
                errors.append(f"{name} slew is below 0.002 V/s")
            if seconds < settling:
                detail = "typical rising settling" if name == "rise" else "no-load falling settling"
                warnings.append(f"{name} is below {settling:g} s ({detail})")
    for name, seconds in dwells:
        if seconds > 3600:
            errors.append(f"{name} dwell exceeds 3600 s")
        if seconds < 0.01:
            warnings.append(f"{name} dwell will be raised to 10 ms")
    return errors, warnings
