"""Running a sequencing table across mainframes.

Everything that can be checked is checked before any output is switched on:
the frame count, and (when the runners are built) every level and time against
the module that will play it.  After that point any failure forces all outputs
off, because a rail left up half-sequenced is the case this tool exists to avoid.
"""

from __future__ import annotations

import math
import threading
import time

from n6705c.core.arb import ArbState, GroupArbRunner
from n6705c.core.group import GlobalChannel, InstrumentGroup
from n6705c.core.instrument import N6705C
from n6705c.core.parsing import decode_real32, split_block_list
from n6705c.core.scpi import format_channel_list
from n6705c.core.transport import VisaTransport

from .plan import SeqRow, to_functions

__all__ = ["DEMO_SLOTS", "connect_group", "OutputsNotConfirmedOff", "PlanRejected", "fire_together", "start_together", "run_plan", "run_functions", "outputs_off"]

#: The simulator configured like the real frames (four N6752A modules).
DEMO_SLOTS = ("N6752A",) * 4

#: How long past the programmed end a channel may still report running.
GRACE_SECONDS = 2.0

#: Digitizer record: points per channel, and the sample-interval grid (min 20.48 us).
CAPTURE_POINTS = 6000
CAPTURE_STEP = 20.48e-6

#: How long a frame's trigger thread waits for the others before giving up.
BARRIER_SECONDS = 5.0


class OutputsNotConfirmedOff(RuntimeError):
    """The emergency stop ran but the mainframes did not report every output off."""


def connect_group(resources: list[str], *, backend: str = "auto", timeout_ms: int = 10_000) -> InstrumentGroup:
    """``InstrumentGroup.connect`` that tolerates a non-ASCII ``*IDN?``.

    The unit tested here (S/N MY66102013) answers with garbage bytes in the model
    field, which the library's default ASCII decoding rejects.  Latin-1 never fails.
    """
    instruments: list[N6705C] = []
    try:
        for resource in resources:
            transport = VisaTransport(resource, backend=backend, timeout_ms=timeout_ms)
            transport._inst.encoding = "latin-1"  # noqa: SLF001 - no public hook
            instruments.append(N6705C.from_transport(transport))
    except BaseException:
        for inst in instruments:
            inst.close()
        raise
    return InstrumentGroup(instruments)


class PlanRejected(ValueError):
    """The plan failed validation; nothing was switched on or off."""


def outputs_off(group: InstrumentGroup) -> None:
    """``all_outputs_off`` swallows errors by design, so read the state back.

    ``OUTP?`` is the commanded state, not the rail voltage -- it proves the write
    arrived, nothing more.
    """
    group.all_outputs_off()
    problems = []
    for index, inst in enumerate(group.instruments, start=1):
        try:
            expected = inst.populated_channels
            state = inst.output_state(expected)
            # output_state zips query and channel lists, so a short reply is a short dict
            if state.keys() != set(expected):
                problems.append(f"frame {index} answered for {sorted(state)}, expected {expected}")
            elif any(state.values()):
                problems.append(f"frame {index} still reports an output on")
        except Exception as exc:  # noqa: BLE001 - an unreadable frame is not a confirmed-off frame
            problems.append(f"frame {index} unreadable ({exc})")
    if problems:
        raise OutputsNotConfirmedOff("could not confirm outputs off: " + "; ".join(problems))


def fire_together(actions: list) -> None:
    """Run each frame's start action from its own thread, released by one barrier.

    # ponytail: host-side threads shrink the gap between frames but cannot close
    # it (sub-ms to ms of link jitter remains); wiring the digital ports is the fix.
    """
    barrier = threading.Barrier(len(actions))
    errors: list[BaseException] = []

    def fire(action) -> None:
        try:
            barrier.wait(BARRIER_SECONDS)
            action()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=fire, args=(a,)) for a in actions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]


def _frames(group: InstrumentGroup, functions) -> list[tuple[N6705C, list[int]]]:
    frames: dict[int, tuple[N6705C, list[int]]] = {}
    for fn in functions:
        instrument, local = group.instrument_for(fn.channel)
        frames.setdefault(id(instrument), (instrument, []))[1].append(local)
    return list(frames.values())


def start_together(group: InstrumentGroup, functions, trigger_acquisition: bool = False) -> None:
    """Start every channel with one ``INIT:TRAN`` per frame.

    The tested unit (firmware F.1.1.0) starts an ARB the moment it is initiated,
    whatever ``TRIG:TRAN:SOUR`` says (BUS, EXT and PIN1 all ran without a ``*TRG``).
    Arming channel by channel therefore staggered them by ~13-22 ms each, so the
    start is a single list command per frame and the source is set to IMM to say
    what really happens.  The simulator agrees: IMM runs at INIT.  The digitizer
    does wait for ``*TRG``, so a capture is released right after the INIT.
    """
    frames = _frames(group, functions)
    for instrument, channels in frames:
        instrument.session.write_checked(f"TRIG:TRAN:SOUR IMM,{format_channel_list(channels)}")

    def start(instrument: N6705C, channels: list[int]):
        def go() -> None:
            instrument.session.write(f"INIT:TRAN {format_channel_list(channels)}")
            if trigger_acquisition:
                instrument.session.write("*TRG")
            instrument.session.check_errors("INIT:TRAN")
        return go

    fire_together([start(inst, chans) for inst, chans in frames])


def _apply_levels(group: InstrumentGroup, functions) -> None:
    """DC setpoint = the low level; the current limit only if the table gave one."""
    for fn in functions:
        instrument, local = group.instrument_for(fn.channel)
        chan = format_channel_list(local)
        instrument.session.write_checked(f"VOLT {fn.voltage:.6G},{chan}")
        if fn.current:  # the table rejects a 0 A limit, so 0.0 here only ever means "none"
            instrument.session.write_checked(f"CURR {fn.current:.6G},{chan}")
    group.set_output([fn.channel for fn in functions], True)


def _running(runner: GroupArbRunner) -> list[int]:
    """Channels still running.  Unlike ``running_channels`` a failed query raises,
    so a dead link is never mistaken for a finished sequence."""
    return [
        GlobalChannel(runner.group.instruments.index(frame.instrument) + 1, arb.spec.channel).index
        for frame in runner.runners
        for arb in frame.runners
        if arb.is_running
    ]


def _wait(runner: GroupArbRunner) -> None:
    time.sleep(runner.total_duration)
    deadline = time.monotonic() + GRACE_SECONDS
    while running := _running(runner):
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"channels {running} still running {GRACE_SECONDS:g} s "
                "after the sequence should have ended"
            )
        time.sleep(0.05)


def _arm_capture(group: InstrumentGroup, functions, window: float) -> float:
    """Set up the built-in digitizer (it must be INITed before the ARB starts) and
    return its sample interval.  It records from the same ``*TRG`` as the ARB."""
    tint = max(1, math.ceil(window / CAPTURE_POINTS / CAPTURE_STEP)) * CAPTURE_STEP
    for instrument, channels in _frames(group, functions):
        chan = format_channel_list(channels)
        session = instrument.session
        session.write_checked("FORM REAL")
        session.write_checked("FORM:BORD SWAP")
        for command in (
            "SENS:FUNC:VOLT ON",
            f"SENS:SWE:POIN {CAPTURE_POINTS}",
            f"SENS:SWE:TINT {tint:.6E}",
            "SENS:SWE:OFFS:POIN 0",
            "TRIG:ACQ:SOUR BUS",
        ):
            session.write_checked(f"{command},{chan}")
        session.write_checked(f"INIT:IMM:ACQ {chan}")
    return tint


def _fetch(group: InstrumentGroup, functions, tint: float, out: dict) -> None:
    """Measured voltage per global channel as ``(sample interval, volts)``.

    A failed fetch is recorded under ``"error"``, not raised: the capture is a
    check on the run, and must not decide whether the outputs stay on.
    """
    for fn in functions:
        instrument, local = group.instrument_for(fn.channel)
        try:
            raw = instrument.session.query_binary(f"FETC:ARR:VOLT? (@{local})")
            out[fn.channel] = (tint, decode_real32(split_block_list(raw)[0], "SWAP").tolist())
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"CH{fn.channel}: {exc}"
            return


def run_plan(
    group: InstrumentGroup,
    rows: list[SeqRow],
    *,
    wait: bool = True,
    hold_dc: bool = False,
    capture: dict | None = None,
) -> GroupArbRunner:
    """Run the table. With ``wait`` the outputs are switched off again at the end,
    unless ``hold_dc``: then each rail stops at its high level and stays on.

    Pass a dict as ``capture`` to record every channel with the digitizer; it is
    filled with ``{channel: (sample interval, volts)}`` (plus ``"error"`` if a fetch
    failed).  The capture has to finish before the outputs go off, so a captured
    run waits for the whole record."""
    functions = to_functions(rows, hold_dc)
    if not functions:
        raise PlanRejected("no enabled channels in the table")
    return run_functions(group, functions, wait=wait, keep_on=hold_dc, capture=capture)


def run_functions(
    group: InstrumentGroup,
    functions,
    *,
    wait: bool = True,
    keep_on: bool = False,
    capture: dict | None = None,
) -> GroupArbRunner:
    """Run the functions. With ``wait`` the outputs are switched off at the end,
    unless ``keep_on``: then each rail stays on at its waveform's final level.

    Pass a dict as ``capture`` to record every channel with the digitizer; it is
    filled with ``{channel: (sample interval, volts)}`` (plus ``"error"`` if a fetch
    failed). The capture has to finish before the outputs go off, so a captured
    run waits for the whole record."""
    if not functions:
        raise PlanRejected("no enabled channels in the table")
    needed = max(GlobalChannel.from_index(fn.channel).frame for fn in functions)
    if needed > group.frame_count:
        raise PlanRejected(
            f"the table uses frame {needed} but only {group.frame_count} "
            f"mainframe(s) are connected"
        )

    try:
        runner = GroupArbRunner.from_plan(group, functions)  # validates against each module
    except Exception as exc:  # noqa: BLE001
        raise PlanRejected(str(exc)) from exc
    try:
        _apply_levels(group, functions)
        runner.load()
        window = tint = 0.0
        if capture is not None:
            window = runner.total_duration * 1.2 + 0.01
            tint = _arm_capture(group, functions, window)
        started = time.monotonic()
        start_together(group, functions, capture is not None)
        for frame in runner.runners:
            frame.mark_triggered()
        runner.state = ArbState.RUNNING
        if wait:
            _wait(runner)
            if capture is not None:
                time.sleep(max(0.0, tint * CAPTURE_POINTS - (time.monotonic() - started)) + 0.05)
                _fetch(group, functions, tint, capture)
            if not keep_on:
                outputs_off(group)
    except OutputsNotConfirmedOff:
        raise
    except BaseException as exc:
        try:
            outputs_off(group)
        except OutputsNotConfirmedOff as off:
            raise OutputsNotConfirmedOff(f"{exc}; also {off}") from exc
        raise
    return runner
