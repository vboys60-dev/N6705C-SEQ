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
import time
from typing import Callable

from n6705c.core.scpi import format_channel_list
from n6705c.core.session import Session
from n6705c.core.transport import VisaTransport

from .execute import OutputsNotConfirmedOff, PlanRejected, close_all, fire_together, run_callback
from .plan import SeqRow

#: Wait for the output to reach the low level before the list is armed.
SETTLE_SECONDS = 1.0  # ponytail: fixed wait, poll MEAS:VOLT? if a slow load needs it

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
    for name in ("delay", "rise", "hold", "fall"):
        if getattr(row, name) < 0:
            errors.append(f"{name} must be nonnegative")
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


def model_of(idn: str) -> str:
    """Model field of an ``*IDN?`` reply, upper-cased ('' if malformed)."""
    parts = idn.split(",")
    return parts[1].strip().upper() if len(parts) > 1 else ""


def detect_model(resource: str, *, backend: str = "auto", timeout_ms: int = 10_000) -> str:
    """Open the resource just long enough to read its model."""
    transport = VisaTransport(resource, backend=backend, timeout_ms=timeout_ms)
    transport._inst.encoding = "latin-1"  # noqa: SLF001 - same workaround as connect_group
    try:
        return model_of(transport.query("*IDN?"))
    finally:
        transport.close()


def check_settings(query, channels: list[int]) -> None:
    """Refuse continuous triggering, coupling, tracking or pairing before writes."""
    checks = [(f"INIT:CONT? {format_channel_list(ch)}", ("0", "OFF")) for ch in channels]
    checks += [
        ("OUTP:COUP:CHAN?", ("NONE",)),
        ("OUTP:TRAC?", ("0", "OFF")),
        ("OUTP:PAIR?", ("OFF",)),
    ]
    for command, allowed in checks:
        answer = query(command).strip().strip('"').upper()
        if answer not in allowed:
            raise PlanRejected(f"{command} returned {answer!r}; must be {'/'.join(allowed)}")


def run_rows(sessions: list[Session], rows: list[SeqRow], *, hold_dc: bool = False,
             on_cleanup: Callable[[], None] | None = None) -> None:
    """Run the table on E36441A frames (4 channels each, in frame order).

    Outputs are restored (off) at the end unless ``hold_dc``; any failure restores
    every channel touched.
    """
    active = [r for r in rows if r.enabled]
    if not active:
        raise PlanRejected("no enabled channels in the table")
    if (max(r.channel for r in active) + 3) // 4 > len(sessions):
        raise PlanRejected(f"the table needs more than {len(sessions)} mainframe(s)")
    problems = [f"{r.name}: {e}" for r in active for e in check(r, hold_dc)[0]]
    if problems:
        raise PlanRejected("; ".join(problems))

    def where(r: SeqRow) -> tuple[Session, int]:
        return sessions[(r.channel - 1) // 4], (r.channel - 1) % 4 + 1

    try:
        kind = kind_of([model_of(s.query("*IDN?")) for s in sessions])
    except ValueError as exc:
        raise PlanRejected(str(exc)) from exc
    if kind != "E36441A":
        raise PlanRejected("run_rows requires E36441A instruments")
    frames: dict[int, tuple[Session, list[int]]] = {}
    for r in active:
        session, ch = where(r)
        frames.setdefault(id(session), (session, []))[1].append(ch)
    for session, chans in frames.values():
        check_settings(session.query, chans)

    touched: list[tuple[Session, int]] = []
    try:
        for r in active:
            session, ch = where(r)
            touched.append((session, ch))
            for cmd in prepare_commands(r, ch):
                session.write_checked(cmd)
        time.sleep(SETTLE_SECONDS)
        for r in active:
            session, ch = where(r)
            for cmd in configure_commands(r, ch, hold_dc):
                session.write_checked(cmd)
        for session, chans in frames.values():
            for cmd in arm_commands(chans):
                session.write_checked(cmd)
        fire_together([(lambda s=s: s.write("*TRG")) for s, _ in frames.values()])
        # last edge ends at delay + rise + hold (+ fall); hold_dc stops after the rise
        end = max(r.delay + r.rise + (0 if hold_dc else r.hold + r.fall) for r in active)
        time.sleep(end + 0.5)
        for session, _ in frames.values():
            session.check_errors("E36441A run")
    except BaseException as exc:
        run_callback(on_cleanup, exc)
        try:
            _restore(touched, sessions)
        except OutputsNotConfirmedOff as off:
            raise OutputsNotConfirmedOff(f"{exc}; also {off}") from exc
        raise
    if not hold_dc:
        failed = run_callback(on_cleanup)
        try:
            _restore(touched, sessions)
        except BaseException as off:
            if failed is not None:
                off.add_note(f"cleanup callback failed ({failed!r})")
            raise
        if failed is not None:
            raise failed


def _restore(touched: list[tuple[Session, int]], sessions: list[Session]) -> None:
    indices = {id(session): i for i, session in enumerate(sessions, 1)}
    for session, ch in touched:
        for cmd in restore_commands(ch):
            try:
                session.write(cmd)
            except Exception:  # noqa: BLE001 - keep switching the rest off
                pass
    problems = []
    for session, ch in touched:
        label = f"frame {indices[id(session)]} CH{ch}"
        try:
            answer = session.query(f"OUTP? {format_channel_list(ch)}").strip().upper()
            if answer not in ("0", "OFF"):
                problems.append(f"{label} still reports {answer!r}")
        except Exception as exc:  # noqa: BLE001 - an unreadable output is not confirmed off
            problems.append(f"{label} unreadable ({exc})")
    if problems:
        raise OutputsNotConfirmedOff("could not confirm outputs off: " + "; ".join(problems))


def kind_of(models: list[str]) -> str:
    """'E36441A' or 'N6705C' for a set of models; mixed instruments are refused."""
    kinds = {"E36441A" if m == "E36441A" else "N6705C" for m in models}
    if len(kinds) > 1:
        raise ValueError(f"mixed instrument models are not supported: {models}")
    return kinds.pop() if kinds else "N6705C"


def _sessions(resources: list[str], backend: str, timeout_ms: int):
    transports = []
    try:
        for resource in resources:
            t = VisaTransport(resource, backend=backend, timeout_ms=timeout_ms)
            transports.append(t)
            t._inst.encoding = "latin-1"  # noqa: SLF001
    except BaseException as exc:
        close_all(transports, exc)
        raise
    return transports


def run_resources(resources: list[str], rows: list[SeqRow], *, hold_dc: bool = False,
                  backend: str = "auto", timeout_ms: int = 10_000,
                  on_cleanup: Callable[[], None] | None = None) -> None:
    """Open each resource as an E36441A frame, run the table, close."""
    transports = _sessions(resources, backend, timeout_ms)
    try:
        run_rows([Session(t) for t in transports], rows, hold_dc=hold_dc,
                 on_cleanup=on_cleanup)
    except BaseException as exc:
        run_callback(on_cleanup, exc)
        close_all(transports, exc)
        raise
    failed = run_callback(on_cleanup)
    close_all(transports, failed)
    if failed is not None:
        raise failed


def off_resources(resources: list[str], *, backend: str = "auto", timeout_ms: int = 10_000) -> None:
    """All four outputs of every frame off, then read ``OUTP?`` back."""
    transports = _sessions(resources, backend, timeout_ms)
    try:
        sessions = [Session(t) for t in transports]
        _restore([(s, ch) for s in sessions for ch in range(1, 5)], sessions)
    except BaseException as exc:
        close_all(transports, exc)
        raise
    close_all(transports)
