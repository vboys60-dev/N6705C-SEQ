"""Running a sequencing table across mainframes.

Everything that can be checked is checked before any output is switched on:
the frame count, and (when the runners are built) every level and time against
the module that will play it.  After that point any failure forces all outputs
off, because a rail left up half-sequenced is the case this tool exists to avoid.
"""

from __future__ import annotations

import threading
import time

from n6705c.core.arb import ArbState, GroupArbRunner
from n6705c.core.group import GlobalChannel, InstrumentGroup
from n6705c.core.instrument import N6705C
from n6705c.core.scpi import format_channel_list
from n6705c.core.transport import VisaTransport

from .plan import SeqRow, to_functions

__all__ = ["DEMO_SLOTS", "connect_group", "OutputsNotConfirmedOff", "PlanRejected", "fire_together", "start_together", "run_plan", "outputs_off"]

#: The simulator configured like the real frames (four N6762A modules).
DEMO_SLOTS = ("N6762A",) * 4

#: How long past the programmed end a channel may still report running.
GRACE_SECONDS = 2.0

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


def start_together(group: InstrumentGroup, functions) -> None:
    """Start every channel with one ``INIT:TRAN`` per frame.

    The tested unit (firmware F.1.1.0) starts an ARB the moment it is initiated,
    whatever ``TRIG:TRAN:SOUR`` says (BUS, EXT and PIN1 all ran without a ``*TRG``).
    Arming channel by channel therefore staggered them by ~13-22 ms each, so the
    start is a single list command per frame and the source is set to IMM to say
    what really happens.  The simulator agrees: IMM runs at INIT.
    """
    frames: dict[int, tuple[N6705C, list[int]]] = {}
    for fn in functions:
        instrument, local = group.instrument_for(fn.channel)
        frames.setdefault(id(instrument), (instrument, []))[1].append(local)
    for instrument, channels in frames.values():
        instrument.session.write_checked(f"TRIG:TRAN:SOUR IMM,{format_channel_list(channels)}")

    def start(instrument: N6705C, channels: list[int]):
        def go() -> None:
            instrument.session.write(f"INIT:TRAN {format_channel_list(channels)}")
            instrument.session.check_errors("INIT:TRAN")
        return go

    fire_together([start(inst, chans) for inst, chans in frames.values()])


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


def run_plan(
    group: InstrumentGroup, rows: list[SeqRow], *, wait: bool = True, hold_dc: bool = False
) -> GroupArbRunner:
    """Run the table. With ``wait`` the outputs are switched off again at the end,
    unless ``hold_dc``: then each rail stops at its high level and stays on."""
    functions = to_functions(rows, hold_dc)
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
        start_together(group, functions)
        for frame in runner.runners:
            frame.mark_triggered()
        runner.state = ArbState.RUNNING
        if wait:
            _wait(runner)
            if not hold_dc:
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
