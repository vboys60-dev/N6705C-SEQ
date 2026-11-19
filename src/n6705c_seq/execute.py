"""Running a sequencing table across mainframes.

Everything that can be checked is checked before any output is switched on:
the frame count, and (when the runners are built) every level and time against
the module that will play it.  After that point any failure forces all outputs
off, because a rail left up half-sequenced is the case this tool exists to avoid.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable

from n6705c.core.arb import ArbSpec, ArbState, GroupArbRunner
from n6705c.core.capability import SMU_MODELS
from n6705c.core.group import MAX_FRAMES, GlobalChannel, InstrumentGroup
from n6705c.core.instrument import N6705C
from n6705c.core.parsing import decode_real32, split_block_list
from n6705c.core.scpi import format_channel_list, parse_channel_list
from n6705c.core.session import ScpiError
from n6705c.core.status import OperationStatus
from n6705c.core.transport import VisaTransport

from .i18n import tr
from .plan import SeqRow, to_functions

__all__ = ["DEMO_SLOTS", "connect_group", "OutputsNotConfirmedOff", "PlanRejected", "fire_together", "start_together", "slew_warnings", "run_plan", "run_functions", "outputs_off"]

log = logging.getLogger(__name__)

#: The simulator configured like the real frames (four N6752A modules).
DEMO_SLOTS = ("N6752A",) * 4

#: How long past the programmed end a channel may still report running.
GRACE_SECONDS = 2.0

#: Rates at or above this threshold represent the instrument's INF sentinel (9.9E37).
INF_SENTINEL = 1e37

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
    check_unique_resources(resources)
    if len(resources) > MAX_FRAMES:
        raise PlanRejected(f"{len(resources)} mainframes exceeds maximum {MAX_FRAMES}")
    opened: list = []  # each entry owns one session: a bare transport until N6705C wraps it
    try:
        for resource in resources:
            transport = VisaTransport(resource, backend=backend, timeout_ms=timeout_ms)
            opened.append(transport)
            transport._inst.encoding = "latin-1"  # noqa: SLF001 - no public hook
            opened[-1] = N6705C.from_transport(transport)
        check_unique_identities([instrument.identity for instrument in opened])
        return InstrumentGroup(opened)
    except BaseException as exc:
        close_all(opened, exc)
        raise


def run_callback(callback: Callable[[], None] | None, primary: BaseException | None = None) -> BaseException | None:
    """Call a cleanup callback so its failure cannot skip the steps after it.

    The failure is attached to ``primary`` as a note when one is propagating;
    otherwise it is returned for the caller to raise once cleanup is done.
    """
    if callback is None:
        return None
    try:
        callback()
    except BaseException as exc:  # noqa: BLE001 - re-raised or attached by the caller
        if primary is None:
            return exc
        primary.add_note(f"cleanup callback failed ({exc!r})")
    return None


def close_all(items, primary: BaseException | None = None) -> None:
    """Close every item even if some closes fail; report the failures afterwards.

    With ``primary`` (an exception already propagating) the failures are attached
    to it as a note so the original error stays the one reported.
    """
    problems = []
    for index, item in enumerate(items, start=1):
        try:
            item.close()
        except Exception as exc:  # noqa: BLE001 - keep closing the rest
            problems.append(f"frame {index} ({exc})")
    if not problems:
        return
    message = "could not close: " + "; ".join(problems)
    if primary is not None:
        primary.add_note(message)
        return
    raise RuntimeError(message)


class PlanRejected(ValueError):
    """The plan failed validation; nothing was switched on or off."""


def check_unique_resources(resources) -> None:
    """Reject repeated VISA addresses before opening any connection."""
    seen = set()
    for resource in resources:
        key = resource.strip().upper()
        if key in seen:
            raise PlanRejected(f"duplicate resource: {resource}")
        seen.add(key)


def check_unique_identities(identities) -> None:
    """Reject repeated manufacturer/serial pairs when a serial is available."""
    seen = {}
    for frame, identity in enumerate(identities, 1):
        serial = identity.serial.strip().upper()
        if not serial:
            continue
        key = (identity.manufacturer.strip().upper(), serial)
        if key in seen:
            raise PlanRejected(f"duplicate instrument: frame {seen[key]} and frame {frame}, {key[0]} S/N {serial}")
        seen[key] = frame


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


def _check_coupling(group: InstrumentGroup, functions) -> None:
    """Refuse coupling that could affect channels outside this plan."""
    command = "OUTP:COUP:CHAN?"
    for instrument, channels in _frames(group, functions):
        frame = group.instruments.index(instrument) + 1
        try:
            raw = instrument.session.query(command)
            instrument.session.check_errors(command)
            answer = "".join(raw.split()).replace('"', '').replace("'", '').upper()
            coupled = set() if answer in ("", "0", "NONE") else set(parse_channel_list(answer)) - {0}
        except Exception as exc:  # noqa: BLE001 - every unreadable coupling state is unsafe
            raise PlanRejected(f"frame {frame}: cannot check coupling ({exc})") from exc
        planned = set(channels)
        if coupled & planned and coupled - planned:
            raise PlanRejected(f"frame {frame}: coupled channels {sorted(coupled)} extend beyond plan channels {sorted(planned)}")


def _check_idle(group: InstrumentGroup, functions) -> None:
    """Refuse channels whose transient system is armed/running or in continuous mode.

    A running ARB ignores VOLT/CURR, and continuous mode re-initiates after ABOR, so
    the plan must start from a quiet channel. Unreadable state is also refused.
    """
    busy = int(OperationStatus.WTG_TRAN | OperationStatus.TRAN_ACTIVE)
    for instrument, channels in _frames(group, functions):
        frame = group.instruments.index(instrument) + 1
        chans = format_channel_list(channels)
        try:
            states = instrument.operation_status(channels)
            cont = instrument.session.query(f"INIT:CONT:TRAN? {chans}")
            instrument.session.check_errors("INIT:CONT:TRAN?")
            flags = [int(x) for x in cont.replace('"', "").split(",")]
            if len(flags) != len(channels):
                raise ValueError(f"unexpected reply {cont!r}")
        except Exception as exc:  # noqa: BLE001 - unreadable state is unsafe
            raise PlanRejected(f"frame {frame}: cannot check transient state ({exc})") from exc
        for ch, flag in zip(channels, flags):
            if int(states[ch]) & busy or flag:
                raise PlanRejected(
                    f"frame {frame} CH{ch}: transient/ARB is active or continuous trigger is ON; "
                    "stop it (ABORt:TRANsient, INIT:CONT:TRAN OFF) before running"
                )


def _set_arb_source_imm(instrument: N6705C, channels: list[int]) -> None:
    """TRIG:ARB:SOUR is per-channel on F.1.1.0 but frame-wide on E.02.09 (-108 with a channel list)."""
    try:
        instrument.session.write_checked(f"TRIG:ARB:SOUR IMM,{format_channel_list(channels)}")
    except ScpiError as exc:
        if exc.code != -108:
            raise
        instrument.session.write_checked("TRIG:ARB:SOUR IMM")


def _set_trigger_sources(group: InstrumentGroup, functions) -> None:
    """Check every frame's sources before enabling any output or initiating."""
    for instrument, channels in _frames(group, functions):
        instrument.session.write_checked(f"TRIG:TRAN:SOUR IMM,{format_channel_list(channels)}")
        _set_arb_source_imm(instrument, channels)


def start_together(group: InstrumentGroup, functions, trigger_acquisition: bool = False) -> None:
    """Start every channel with one ``INIT:TRAN`` per frame.

    The tested unit (firmware F.1.1.0) starts an ARB the moment it is initiated,
    whatever ``TRIG:TRAN:SOUR`` says (BUS, EXT and PIN1 all ran without a ``*TRG``).
    Arming channel by channel therefore staggered them by ~13-22 ms each, so the
    start is a single list command per frame and the source is set to IMM to say
    what really happens.  The simulator agrees: IMM runs at INIT.  The digitizer
    does wait for ``*TRG``, so a capture is released right after the INIT.

    Both trigger sources are checked here too, for callers that start directly.
    """
    frames = _frames(group, functions)
    _set_trigger_sources(group, functions)

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
        if fn.sense:
            instrument.session.write_checked(f"VOLT:SENS:SOUR {'EXT' if fn.sense == '4wire' else 'INT'},{chan}")
        if fn.current:  # the table rejects a 0 A limit, so 0.0 here only ever means "none"
            # N678xA SMUs run voltage priority by default and reject CURR (+315); their limit is CURR:LIM.
            smu = instrument.capabilities[local].model.upper() in SMU_MODELS
            instrument.session.write_checked(f"CURR{':LIM' if smu else ''} {fn.current:.6G},{chan}")
        instrument.session.write_checked(f"VOLT {fn.voltage:.6G},{chan}")
    group.set_output([fn.channel for fn in functions], True)


def slew_warnings(group: InstrumentGroup, functions) -> list[str]:
    """Report limited voltage slew; unsupported probes are diagnostic only.

    Compare the current rate with the plan's maximum positive-duration slope.
    SLEW:MAX? is a boolean override; INF sentinel rates impose no limit.
    Drain each probe's errors even when its reply is empty or malformed.
    """
    required_rates = {}
    for fn in functions:
        required = 0.0
        if fn.arb_enabled and fn.spec is not None and not fn.spec.is_current:
            points = fn.spec.waveform if isinstance(fn.spec, ArbSpec) else fn.spec
            times, values = points.points()
            required = max((abs(float(v1) - float(v0)) / (float(t1) - float(t0))
                            for t0, t1, v0, v1 in zip(times, times[1:], values, values[1:])
                            if t1 > t0), default=0.0)
        required_rates[fn.channel] = max(required_rates.get(fn.channel, 0.0), required)

    warnings = []
    for instrument, channels in _frames(group, functions):
        channels = list(dict.fromkeys(channels))
        chan = format_channel_list(channels)
        probes = []
        for command in (f"VOLT:SLEW:MAX? {chan}", f"VOLT:SLEW? {chan}"):
            raw = None
            errors = []
            try:
                raw = instrument.session.query(command)
            except Exception as exc:  # noqa: BLE001 - still drain the probe's errors
                errors.append(exc)
            finally:
                try:
                    instrument.session.check_errors(command)
                except Exception as exc:  # noqa: BLE001 - retain both query and SCPI failures
                    errors.append(exc)
            if errors:
                for exc in errors:
                    log.warning("slew query %s returned %r: %s", command, raw, exc)
                probes.append(None)
                continue
            try:
                values = [float(part) for part in raw.split(",")]
                if len(values) != len(channels) or not all(math.isfinite(v) for v in values):
                    raise ValueError("invalid slew reply")
                probes.append(values)
            except Exception as exc:  # noqa: BLE001 - optional capability probe
                log.warning("slew query %s returned %r: %s", command, raw, exc)
                probes.append(None)
        if any(values is None for values in probes):
            continue
        overrides, rates = probes
        frame = group.instruments.index(instrument) + 1
        for local, override, rate in zip(channels, overrides, rates):
            required = required_rates[GlobalChannel(frame, local).index]
            if override == 0 and rate < INF_SENTINEL and rate < required:
                warnings.append(tr("slew_warning", channel=GlobalChannel(frame, local).label(),
                                   actual=rate, required=required))
    return warnings


def _running(runner: GroupArbRunner) -> list[int]:
    """Channels still running.  Unlike ``running_channels`` a failed query raises,
    so a dead link is never mistaken for a finished sequence."""
    return [
        GlobalChannel(runner.group.instruments.index(frame.instrument) + 1, arb.spec.channel).index
        for frame in runner.runners
        for arb in frame.runners
        if arb.is_running
    ]


def _wait(runner: GroupArbRunner, stop: threading.Event) -> None:
    if stop.wait(runner.total_duration):
        return
    deadline = time.monotonic() + GRACE_SECONDS
    while running := _running(runner):
        if stop.wait(0.05):
            return
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"channels {running} still running {GRACE_SECONDS:g} s "
                "after the sequence should have ended"
            )


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
    on_cleanup: Callable[[], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
    stop: threading.Event | None = None,
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
    # E36441A has its own runner (e36441a.run_rows); this one is N6705C only
    models = [inst.identity.model.upper() for inst in group.instruments]
    if any("E36441A" in m for m in models):
        raise PlanRejected(f"N6705C runner cannot drive E36441A (or a mix): {models}")
    return run_functions(group, functions, wait=wait, keep_on=hold_dc, capture=capture,
                         on_cleanup=on_cleanup, on_warning=on_warning, stop=stop)


def run_functions(
    group: InstrumentGroup,
    functions,
    *,
    wait: bool = True,
    keep_on: bool = False,
    capture: dict | None = None,
    on_cleanup: Callable[[], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
    stop: threading.Event | None = None,
) -> GroupArbRunner:
    """Run the functions. Setting ``stop`` ends the wait early and always switches off. With ``wait`` the outputs are switched off at the end,
    unless ``keep_on``: then each rail stays on at its waveform's final level.

    Pass a dict as ``capture`` to record every channel with the digitizer; it is
    filled with ``{channel: (sample interval, volts)}`` (plus ``"error"`` if a fetch
    failed). The capture has to finish before the outputs go off, so a captured
    run waits for the whole record. ``on_warning`` receives slew warnings before
    outputs are enabled; when omitted, they are logged."""
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
    for fn in functions:
        capability = group.capabilities.get(fn.channel)
        if capability is not None and capability.is_load:
            raise PlanRejected(
                f"CH{fn.channel} ({capability.model}): "
                "electronic-load modules cannot be used as supply channels; use another channel"
            )
    _check_coupling(group, functions)
    _check_idle(group, functions)
    try:
        for warning in slew_warnings(group, functions):
            if on_warning is not None:
                on_warning(warning)
            else:
                log.warning("%s", warning)
        _set_trigger_sources(group, functions)
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
            stop = stop or threading.Event()
            _wait(runner, stop)
            if capture is not None and not stop.is_set():
                time.sleep(max(0.0, tint * CAPTURE_POINTS - (time.monotonic() - started)) + 0.05)
                _fetch(group, functions, tint, capture)
            if not keep_on or stop.is_set():  # a stop always switches off
                if on_cleanup is not None:
                    on_cleanup()
                outputs_off(group)
    except OutputsNotConfirmedOff:
        raise
    except BaseException as exc:
        run_callback(on_cleanup, exc)
        try:
            outputs_off(group)
        except OutputsNotConfirmedOff as off:
            raise OutputsNotConfirmedOff(f"{exc}; also {off}") from exc
        raise
    return runner
