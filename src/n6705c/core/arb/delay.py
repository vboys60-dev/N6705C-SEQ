"""Starting a channel's waveform later than the trigger.

The instrument has no such setting.  ``TRIGger:TRANsient:DELay`` does not
exist -- a channel's ARB begins the instant its trigger arrives, and the only
per-channel delay in the hardware is ``OUTPut:DELay``, which staggers when the
*output* switches on (see :mod:`..delay`).  So a staggered *waveform* has to be
built into the waveform: a flat stretch at its own starting level, in front.

Which is easy for six of the nine shapes, and needs care for the rest:

============  ==========================================================
STEP RAMP     ``start_time`` already is a leading flat stretch; add to it.
STA PULS
TRAP EXP
UDEF          Prepend one point at the starting level, dwell = the delay.
CDW           Every point shares one dwell, so the delay can only land on a
              multiple of it.  The error is reported, never hidden.
SIN           No leading segment and no point list.  Becomes a two-step
              sequence: hold, then the sine.
============  ==========================================================

**Repeats are the subtle part.**  Folding a delay into the waveform makes it
part of the repeat unit, so with ``count > 1`` the delay happens *every* cycle
and the channel drifts away from its neighbours instead of running a fixed
offset behind them::

    A: |--wave--|--wave--|--wave--|          period 1.0 s
    B:    0.5 |--wave--|    0.5 |--wave--|   period 1.5 s  <- drifts

A one-time offset simply cannot live inside a repeating unit.  For repeats the
delay therefore becomes its own leading step, played once, followed by the
waveform as many steps as it repeats::

    B: 0.5 |--wave--|--wave--|--wave--|      period 1.0 s, offset 0.5 s

which costs sequence steps (100 of them, p.316) and shares the 511-point
budget.  :func:`delay_spec` does this automatically and says so in
:attr:`DelayedWaveform.notes`; it refuses rather than guessing when the shape
or the repeat count makes it impossible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from .models import (
    ArbLimits,
    ArbSpec,
    ArbWaveform,
    ConstantDwellArb,
    SinusoidArb,
    StepArb,
    UserDefinedArb,
)
from .sequence import SEQUENCE_MAX_STEPS, ArbSequence, SequenceStep

__all__ = [
    "DelayedWaveform",
    "starting_level",
    "hold_step",
    "delay_waveform",
    "delay_spec",
]

log = logging.getLogger(__name__)


@dataclass
class DelayedWaveform:
    """A waveform moved later, plus what it cost to do so.

    ``requested`` and ``actual`` differ only for constant-dwell arbs, where
    the delay has to land on a multiple of the shared dwell.
    """

    #: An :class:`ArbSpec` when a single ARB still suffices, otherwise an
    #: :class:`ArbSequence`.
    spec: ArbSpec | ArbSequence
    requested: float
    actual: float
    #: Plain-language changes the caller should show rather than absorb.
    notes: tuple[str, ...] = ()

    @property
    def error(self) -> float:
        return self.actual - self.requested

    @property
    def is_exact(self) -> bool:
        return abs(self.error) < 1e-12

    @property
    def became_sequence(self) -> bool:
        return isinstance(self.spec, ArbSequence)


def starting_level(waveform: ArbWaveform) -> float:
    """The level the waveform sits at before it does anything.

    Taken from :meth:`ArbWaveform.points` rather than a named field, so it
    works for the point-list shapes and the sine as well.
    """
    _, values = waveform.points()
    return float(values[0])


def hold_step(level: float, seconds: float) -> StepArb:
    """A waveform that holds *level* for *seconds* and then does nothing.

    ``STEP`` with both levels equal: its duration is ``start_time``, and the
    step it takes at the end is to the value it was already at.
    """
    return StepArb(start_level=level, start_time=seconds, end_level=level)


def _delay_constant_dwell(
    waveform: ConstantDwellArb, delay: float
) -> tuple[ConstantDwellArb, float]:
    """Prepend whole dwells; the delay can only land on a multiple of one."""
    if waveform.dwell <= 0:
        raise ValueError("a constant-dwell ARB with no dwell time cannot be delayed")
    count = int(round(delay / waveform.dwell))
    if count == 0 and delay > 0:
        count = 1  # never silently drop a delay the user asked for
    level = waveform.levels[0] if waveform.levels else 0.0
    return (
        replace(waveform, levels=[level] * count + list(waveform.levels)),
        count * waveform.dwell,
    )


def delay_waveform(
    waveform: ArbWaveform,
    delay: float,
    limits: ArbLimits | None = None,
    channel: int = 0,
) -> DelayedWaveform:
    """Move a single cycle of *waveform* later by *delay* seconds.

    The waveform passed in is never modified -- the caller's object stays the
    one they edited, and the delay is applied to a copy on its way to the
    instrument.

    Use :func:`delay_spec` for anything that repeats; this one only knows about
    a single cycle. *channel* labels the spec that comes back.
    """
    if delay < 0:
        raise ValueError("a waveform delay cannot be negative")
    if delay == 0:
        return DelayedWaveform(
            spec=ArbSpec(channel=channel, waveform=waveform), requested=0.0, actual=0.0
        )

    notes: list[str] = []
    actual = delay

    if "start_time" in waveform.time_params:
        shifted: ArbWaveform = replace(
            waveform, start_time=float(waveform.start_time) + delay  # type: ignore[arg-type]
        )
    elif isinstance(waveform, UserDefinedArb):
        level = waveform.levels[0] if waveform.levels else 0.0
        shifted = replace(
            waveform,
            levels=[level] + list(waveform.levels),
            dwells=[delay] + list(waveform.dwells),
            # A prepended point must not fire a trigger that was not asked for.
            bost=[False] + list(waveform.bost) if waveform.bost else [],
        )
        notes.append("a leading point was added to the list")
    elif isinstance(waveform, ConstantDwellArb):
        shifted, actual = _delay_constant_dwell(waveform, delay)
        if abs(actual - delay) > 1e-12:
            notes.append(
                f"every point shares one {waveform.dwell:g} s dwell, so the delay "
                f"became {actual:g} s"
            )
    else:
        # SIN, and anything else with neither a leading segment nor a point
        # list: the hold has to be a separate step.
        sequence = ArbSequence(
            channel=channel,
            steps=[
                SequenceStep(hold_step(starting_level(waveform), delay)),
                SequenceStep(waveform),
            ],
        )
        if limits is not None:
            sequence.validate(limits)
        notes.append(
            f"{waveform.shape} has no leading segment, so it became a two-step "
            "sequence; sequence repeat limits apply instead of the ARB ones"
        )
        return DelayedWaveform(
            spec=sequence, requested=delay, actual=delay, notes=tuple(notes)
        )

    if limits is not None:
        shifted.validate(limits)
    return DelayedWaveform(
        spec=ArbSpec(channel=channel, waveform=shifted),
        requested=delay,
        actual=actual,
        notes=tuple(notes),
    )


def delay_spec(
    spec: ArbSpec, delay: float, limits: ArbLimits | None = None
) -> DelayedWaveform:
    """Move a whole :class:`ArbSpec` later, repeats included.

    With ``count == 1`` the delay is folded into the waveform.  With repeats
    it becomes a leading step of its own, because a delay inside the repeat
    unit would recur every cycle and the channel would drift (see the module
    docstring).
    """
    if delay < 0:
        raise ValueError("a waveform delay cannot be negative")
    if delay == 0:
        return DelayedWaveform(spec=spec, requested=0.0, actual=0.0)

    if spec.infinite:
        raise ValueError(
            "a waveform that repeats forever cannot be delayed: the delay would "
            "have to be a step of its own, and there is no last cycle to end on. "
            "Set a repeat count, or use the output delay instead"
        )

    count = int(spec.count)

    if count == 1:
        moved = delay_waveform(spec.waveform, delay, limits, spec.channel)
        if isinstance(moved.spec, ArbSequence):
            sequence = replace(
                moved.spec,
                channel=spec.channel,
                arb_type=spec.arb_type,
                terminate_last=spec.terminate_last,
                trigger_source=spec.trigger_source,
            )
            return DelayedWaveform(sequence, moved.requested, moved.actual, moved.notes)
        return DelayedWaveform(
            spec=replace(spec, waveform=moved.spec.waveform),
            requested=moved.requested,
            actual=moved.actual,
            notes=moved.notes,
        )

    # Repeats: one hold, then the waveform once per repeat.
    if isinstance(spec.waveform, ConstantDwellArb):
        raise ValueError(
            "a constant-dwell ARB that repeats cannot be delayed: it may not be "
            "a sequence step (p.316), and folding the delay into the point list "
            "would repeat it every cycle. Set the repeat count to 1, or use the "
            "output delay instead"
        )
    if count + 1 > SEQUENCE_MAX_STEPS:
        raise ValueError(
            f"delaying a waveform that repeats {count} times needs {count + 1} "
            f"sequence steps, above the {SEQUENCE_MAX_STEPS} a sequence holds. "
            "Reduce the repeat count, or fold the delay into the waveform itself"
        )

    sequence = ArbSequence(
        channel=spec.channel,
        steps=[SequenceStep(hold_step(starting_level(spec.waveform), delay))]
        + [SequenceStep(spec.waveform) for _ in range(count)],
        arb_type=spec.arb_type,
        terminate_last=spec.terminate_last,
        trigger_source=spec.trigger_source,
    )
    if limits is not None:
        sequence.validate(limits)
    notes = (
        f"the {count} repeats became {count} sequence steps after a leading hold, "
        "so that the delay happens once rather than every cycle",
    )
    return DelayedWaveform(sequence, requested=delay, actual=delay, notes=notes)
