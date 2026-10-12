"""ARB sequences: several waveforms played back to back.

A sequence holds up to 100 steps (p.316), but the real ceiling is the
**511-point budget the whole sequence shares** (p.310).  As steps are added the
instrument spends fewer points on each curve, so ``ARB:SEQuence:QUALity?``
falls from 100 towards 16.  That number is worth surfacing: a sequence can be
accepted and still render its curves coarsely.

Constant-dwell arbs cannot be sequence steps -- ``ARB:SEQuence:STEP:FUNCtion:
SHAPe`` does not offer CDWell.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ..scpi import format_channel_list
from .models import (
    ArbLimits,
    ArbWaveform,
    ConstantDwellArb,
    RampArb,
)

__all__ = [
    "SequenceStep",
    "ArbSequence",
    "SEQUENCE_MAX_STEPS",
    "SEQUENCE_POINT_BUDGET",
    "SEQUENCE_COUNT_MAX",
    "STEP_COUNT_MAX",
    "QUALITY_MIN",
    "QUALITY_MAX",
]

log = logging.getLogger(__name__)

#: ARB:SEQuence:STEP:FUNCtion:SHAPe (p.316)
SEQUENCE_MAX_STEPS = 100
#: Points shared by every waveform in the sequence (p.310).
SEQUENCE_POINT_BUDGET = 511
#: ARB:SEQuence:COUNt (p.308) -- note this is *not* the 16 million of ARB:COUNt.
SEQUENCE_COUNT_MAX = 4096
#: ARB:SEQuence:STEP:COUNt (p.312)
STEP_COUNT_MAX = 16_777_216
QUALITY_MIN = 16
QUALITY_MAX = 100

VALID_PACING = ("DWEL", "TRIG")


@dataclass
class SequenceStep:
    """One waveform within a sequence.

    ``pacing`` DWELl runs the step for its own duration and moves on; TRIGgered
    holds the step's last value until a trigger arrives.  ``count`` only has an
    effect on trigger-paced steps (p.312).
    """

    waveform: ArbWaveform
    count: int | str = 1
    pacing: str = "DWEL"

    @property
    def infinite(self) -> bool:
        return isinstance(self.count, str) and self.count.upper().startswith("INF")

    @property
    def normalised_pacing(self) -> str:
        return "TRIG" if self.pacing.strip().upper().startswith("TRIG") else "DWEL"

    @property
    def duration(self) -> float:
        """Seconds this step occupies when dwell-paced."""
        if self.infinite:
            return float("inf")
        repeats = int(self.count) if self.normalised_pacing == "TRIG" else 1
        return self.waveform.duration * repeats

    def validate(self, limits: ArbLimits, index: int) -> None:
        if isinstance(self.waveform, ConstantDwellArb):
            raise ValueError(
                f"step {index}: constant-dwell ARBs cannot be used in a sequence"
            )
        if self.pacing.strip().upper()[:4] not in VALID_PACING:
            raise ValueError(
                f"step {index}: pacing must be DWEL or TRIG, not {self.pacing!r}"
            )
        if not self.infinite and not 1 <= int(self.count) <= STEP_COUNT_MAX:
            raise ValueError(
                f"step {index}: repeat count must be between 1 and {STEP_COUNT_MAX:,}"
            )
        try:
            self.waveform.validate(limits)
        except ValueError as exc:
            raise ValueError(f"step {index}: {exc}") from exc


@dataclass
class ArbSequence:
    """A channel's whole sequence."""

    channel: int
    steps: list[SequenceStep] = field(default_factory=list)
    arb_type: str = "VOLT"
    count: int | str = 1
    terminate_last: bool = False
    trigger_source: str = "BUS"

    @property
    def is_current(self) -> bool:
        return self.arb_type.upper().startswith("C")

    @property
    def normalised_type(self) -> str:
        return "CURR" if self.is_current else "VOLT"

    @property
    def infinite(self) -> bool:
        return isinstance(self.count, str) and self.count.upper().startswith("INF")

    @property
    def duration(self) -> float:
        """One pass through every step."""
        return float(sum(step.duration for step in self.steps))

    @property
    def total_duration(self) -> float:
        if self.infinite:
            return float("inf")
        return self.duration * int(self.count)

    @property
    def curved_steps(self) -> int:
        """Steps whose shape is a smooth curve, and so spends points on quality."""
        return sum(1 for s in self.steps if s.waveform.shape in ("RAMP", "TRAP", "EXP", "SIN"))

    def estimated_quality(self) -> int:
        """Rough guess at ``ARB:SEQuence:QUALity?`` before downloading.

        The instrument is the authority -- this only warns the user early that
        a long sequence will render its curves coarsely.
        """
        curved = self.curved_steps
        if curved == 0:
            return QUALITY_MAX
        per_curve = SEQUENCE_POINT_BUDGET // curved
        return max(QUALITY_MIN, min(QUALITY_MAX, per_curve))

    # -- Validation ---------------------------------------------------------

    def validate(self, limits: ArbLimits | None = None) -> None:
        limits = limits or ArbLimits()
        if not self.steps:
            raise ValueError("the sequence has no steps")
        if len(self.steps) > SEQUENCE_MAX_STEPS:
            raise ValueError(
                f"the sequence has {len(self.steps)} steps; the maximum is "
                f"{SEQUENCE_MAX_STEPS}"
            )
        if not self.infinite and not 1 <= int(self.count) <= SEQUENCE_COUNT_MAX:
            raise ValueError(
                f"sequence repeat count must be between 1 and {SEQUENCE_COUNT_MAX:,}"
            )
        for index, step in enumerate(self.steps, start=1):
            step.validate(limits, index)

    # -- SCPI ---------------------------------------------------------------

    def configure_commands(self) -> list[str]:
        """Everything needed to load the sequence, in order."""
        chan = format_channel_list(self.channel)
        kind = self.normalised_type
        commands = [
            f"ARB:FUNC:TYPE {kind},{chan}",
            # Clear whatever steps were left from a previous sequence, or the
            # old tail stays behind the new, shorter one.
            f"ARB:SEQ:RES {chan}",
            f"ARB:FUNC:SHAP SEQ,{chan}",
        ]
        for index, step in enumerate(self.steps, start=1):
            commands.append(
                f"ARB:SEQ:STEP:FUNC:SHAP {step.waveform.shape},{index},{chan}"
            )
            commands += step.waveform.scpi_commands(kind, chan, step=index)
            commands.append(
                f"ARB:SEQ:STEP:PAC {step.normalised_pacing},{index},{chan}"
            )
            if step.normalised_pacing == "TRIG":
                count = "INF" if step.infinite else f"{int(step.count)}"
                commands.append(f"ARB:SEQ:STEP:COUN {count},{index},{chan}")

        count = "INF" if self.infinite else f"{int(self.count)}"
        commands.append(f"ARB:SEQ:COUN {count},{chan}")
        commands.append(
            f"ARB:SEQ:TERM:LAST {'ON' if self.terminate_last else 'OFF'},{chan}"
        )
        commands.append(f"{kind}:MODE ARB,{chan}")
        return commands

    # -- Preview ------------------------------------------------------------

    def points(self, cycles: int = 1) -> tuple[np.ndarray, np.ndarray]:
        """Preview the whole sequence, steps laid end to end."""
        cycles = max(1, min(int(cycles), 20))
        times: list[np.ndarray] = []
        values: list[np.ndarray] = []
        now = 0.0
        for _ in range(cycles):
            for step in self.steps:
                repeats = (
                    int(step.count)
                    if step.normalised_pacing == "TRIG" and not step.infinite
                    else 1
                )
                repeats = max(1, min(repeats, 20))
                base_t, base_y = step.waveform.points()
                period = step.waveform.duration
                for _ in range(repeats):
                    times.append(base_t + now)
                    values.append(base_y)
                    now += period
        if not times:
            return np.empty(0), np.empty(0)
        return np.concatenate(times), np.concatenate(values)

    def describe(self) -> str:
        shapes = " -> ".join(step.waveform.shape for step in self.steps)
        return f"{len(self.steps)} steps: {shapes}"


def default_sequence(channel: int = 1) -> ArbSequence:
    """A two-step starting point for the editor."""
    return ArbSequence(
        channel=channel,
        steps=[
            SequenceStep(RampArb(start_time=0.0, rise_time=0.5, end_level=5.0, end_time=0.2)),
            SequenceStep(RampArb(start_level=5.0, start_time=0.0, rise_time=0.5,
                                 end_level=0.0, end_time=0.2)),
        ],
    )
