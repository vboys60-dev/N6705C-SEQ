"""ARB waveform definitions.

One dataclass per shape, each of which knows three things: the SCPI parameters
it maps to, the limits it must respect, and the points it draws.  Keeping those
together is deliberate -- a parameter added to a shape without a matching
preview would otherwise silently draw the wrong curve.

Sampling returns exact breakpoints for the piecewise-linear shapes and a dense
sweep only for the curved ones (sine, exponential), so a preview is exact
rather than an approximation, and cheap.

Page references are to the N6705 Programmer's Reference Guide.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from typing import ClassVar

import numpy as np

from ..capability import ChannelCapability

__all__ = [
    "ArbLimits",
    "ArbWaveform",
    "StepArb",
    "RampArb",
    "StaircaseArb",
    "SinusoidArb",
    "PulseArb",
    "TrapezoidArb",
    "ExponentialArb",
    "ArbSpec",
    "SHAPES",
    "UserDefinedArb",
    "ConstantDwellArb",
    "make_waveform",
    "TIME_MAX_STANDARD",
    "TIME_MAX_SMU",
    "FREQUENCY_MIN",
    "FREQUENCY_MAX",
    "NSTEPS_MAX",
    "ARB_COUNT_MAX",
    "UDEFINED_MAX_POINTS",
    "CDWELL_MAX_POINTS",
    "CDWELL_DWELL_MIN",
    "CDWELL_DWELL_MAX",
    "CDWELL_COUNT_MAX",
]

# Time parameter ceilings (p.297): the N678xA SMUs get a slightly longer range
# because their timebase resolution differs.
TIME_MAX_STANDARD = 262.144
TIME_MAX_SMU = 268.435

# [SOURce:]ARB:VOLTage:SINusoid:FREQuency (p.360)
FREQUENCY_MIN = 3.8147e-5
FREQUENCY_MAX = 1.0e4

# [SOURce:]ARB:VOLTage:STAircase:NSTeps (p.368)
NSTEPS_MAX = 500

# [SOURce:]ARB:COUNt (p.222)
ARB_COUNT_MAX = 16_777_216

# [SOURce:]ARB:VOLTage:UDEFined:LEVel (p.401): the level, dwell and BOST lists
# each hold at most 511 points.
UDEFINED_MAX_POINTS = 511

# [SOURce:]ARB:VOLTage:CDWell[:LEVel] (p.324)
CDWELL_MAX_POINTS = 65_535
# [SOURce:]ARB:VOLTage:CDWell:DWELl (p.326)
CDWELL_DWELL_MIN = 10.24e-6
CDWELL_DWELL_MAX = 0.30
CDWELL_DWELL_GRANULARITY = 10.24e-6
# Constant-dwell arbs cap the repeat count far below the general limit.
CDWELL_COUNT_MAX = 256

#: Points used to draw one cycle of a curved segment in a preview.
CURVE_POINTS = 512


@dataclass(frozen=True)
class ArbLimits:
    """What the target channel will accept."""

    level_max: float = math.inf
    level_min: float = 0.0
    time_max: float = TIME_MAX_STANDARD

    @classmethod
    def for_channel(cls, capability: ChannelCapability, arb_type: str) -> "ArbLimits":
        level_max = (
            capability.voltage_max if arb_type.upper().startswith("V") else capability.current_max
        )
        return cls(
            level_max=math.inf if math.isnan(level_max) else level_max,
            level_min=0.0,
            time_max=TIME_MAX_SMU if capability.is_smu else TIME_MAX_STANDARD,
        )


class ArbWaveform:
    """Base class: shape mnemonic, SCPI mapping, limits, preview points."""

    #: SCPI mnemonic for ``ARB:FUNCtion:SHAPe``.
    shape: ClassVar[str] = ""
    #: Human-readable parameter -> SCPI suffix, in the order they are sent.
    scpi_suffixes: ClassVar[dict[str, str]] = {}
    #: Parameters that are levels (volts or amps) rather than times.
    level_params: ClassVar[tuple[str, ...]] = ()
    #: Parameters that are durations in seconds.
    time_params: ClassVar[tuple[str, ...]] = ()

    def scpi_parameters(self) -> list[tuple[str, float]]:
        """``[(suffix, value), ...]`` in the order they should be sent."""
        return [
            (suffix, float(getattr(self, name)))
            for name, suffix in self.scpi_suffixes.items()
        ]

    def scpi_commands(self, kind: str, chanlist: str, step: int | None = None) -> list[str]:
        """Parameter commands for this waveform.

        *kind* is ``VOLT`` or ``CURR``; *step* makes them sequence-step
        commands instead of standalone ones.  List-valued shapes override this
        because their values do not fit the one-number-per-command shape.
        """
        prefix = f"ARB:SEQ:STEP:{kind}" if step is not None else f"ARB:{kind}"
        tail = f",{step},{chanlist}" if step is not None else f",{chanlist}"
        return [
            f"{prefix}:{suffix} {value:.9G}{tail}"
            for suffix, value in self.scpi_parameters()
        ]

    @property
    def duration(self) -> float:
        raise NotImplementedError

    def points(self) -> tuple[np.ndarray, np.ndarray]:
        """Exact preview points for one cycle."""
        raise NotImplementedError

    def validate(self, limits: ArbLimits) -> None:
        """Raise ValueError describing the first parameter out of range."""
        for name in self.level_params:
            value = float(getattr(self, name))
            if not limits.level_min <= value <= limits.level_max:
                raise ValueError(
                    f"{self.shape} {name}={value:g} is outside the channel range "
                    f"{limits.level_min:g}..{limits.level_max:g}"
                )
        for name in self.time_params:
            value = float(getattr(self, name))
            if value < 0:
                raise ValueError(f"{self.shape} {name}={value:g} cannot be negative")
            if value > limits.time_max:
                raise ValueError(
                    f"{self.shape} {name}={value:g} s exceeds the maximum "
                    f"{limits.time_max:g} s"
                )
        if self.duration <= 0:
            raise ValueError(f"{self.shape} has zero duration; set at least one time")

    def describe(self) -> str:
        parts = [f"{f.name}={getattr(self, f.name):g}" for f in fields(self)]  # type: ignore[arg-type]
        return f"{self.shape}({', '.join(parts)})"


@dataclass
class StepArb(ArbWaveform):
    """A single level change (p.375-379).

    ``start_time`` is the only time the instrument is given, so the arb's own
    length is that dwell at ``start_level``; ``end_level`` is what the output
    holds afterwards.  The preview shows a trailing segment at ``end_level`` to
    make that visible -- its width is presentational only.
    """

    shape: ClassVar[str] = "STEP"
    scpi_suffixes: ClassVar[dict[str, str]] = {
        "start_level": "STEP:STAR",
        "start_time": "STEP:STAR:TIM",
        "end_level": "STEP:END",
    }
    level_params: ClassVar[tuple[str, ...]] = ("start_level", "end_level")
    time_params: ClassVar[tuple[str, ...]] = ("start_time",)

    start_level: float = 0.0
    start_time: float = 1.0
    end_level: float = 5.0

    @property
    def duration(self) -> float:
        return self.start_time

    def points(self) -> tuple[np.ndarray, np.ndarray]:
        tail = max(self.start_time * 0.5, 1e-9)
        t = np.array([0.0, self.start_time, self.start_time, self.start_time + tail])
        y = np.array([self.start_level, self.start_level, self.end_level, self.end_level])
        return t, y


@dataclass
class RampArb(ArbWaveform):
    """Linear transition with flat sections either side (p.348-356)."""

    shape: ClassVar[str] = "RAMP"
    scpi_suffixes: ClassVar[dict[str, str]] = {
        "start_level": "RAMP:STAR",
        "start_time": "RAMP:STAR:TIM",
        "rise_time": "RAMP:RTIM",
        "end_level": "RAMP:END",
        "end_time": "RAMP:END:TIM",
    }
    level_params: ClassVar[tuple[str, ...]] = ("start_level", "end_level")
    time_params: ClassVar[tuple[str, ...]] = ("start_time", "rise_time", "end_time")

    start_level: float = 0.0
    start_time: float = 0.1
    rise_time: float = 1.0
    end_level: float = 5.0
    end_time: float = 0.1

    @property
    def duration(self) -> float:
        return self.start_time + self.rise_time + self.end_time

    def points(self) -> tuple[np.ndarray, np.ndarray]:
        t0 = self.start_time
        t1 = t0 + self.rise_time
        t2 = t1 + self.end_time
        return (
            np.array([0.0, t0, t1, t2]),
            np.array([self.start_level, self.start_level, self.end_level, self.end_level]),
        )


@dataclass
class StaircaseArb(ArbWaveform):
    """Equal steps from start to end (p.364-373).

    ``n_steps`` equal steps span ``time``; the last step sits at ``end_level``.
    """

    shape: ClassVar[str] = "STA"
    scpi_suffixes: ClassVar[dict[str, str]] = {
        "start_level": "STA:STAR",
        "start_time": "STA:STAR:TIM",
        "end_level": "STA:END",
        "end_time": "STA:END:TIM",
        "n_steps": "STA:NST",
        "time": "STA:TIM",
    }
    level_params: ClassVar[tuple[str, ...]] = ("start_level", "end_level")
    time_params: ClassVar[tuple[str, ...]] = ("start_time", "time", "end_time")

    start_level: float = 0.0
    start_time: float = 0.1
    end_level: float = 5.0
    end_time: float = 0.1
    n_steps: int = 10
    time: float = 1.0

    @property
    def duration(self) -> float:
        return self.start_time + self.time + self.end_time

    def validate(self, limits: ArbLimits) -> None:
        super().validate(limits)
        if not 1 <= self.n_steps <= NSTEPS_MAX:
            raise ValueError(
                f"staircase n_steps={self.n_steps} must be between 1 and {NSTEPS_MAX}"
            )

    def points(self) -> tuple[np.ndarray, np.ndarray]:
        n = max(1, int(self.n_steps))
        step_time = self.time / n
        span = self.end_level - self.start_level
        t = [0.0, self.start_time]
        y = [self.start_level, self.start_level]
        for k in range(n):
            level = self.start_level + span * (k + 1) / n
            begin = self.start_time + k * step_time
            t += [begin, begin + step_time]
            y += [level, level]
        t.append(self.duration)
        y.append(self.end_level)
        return np.array(t), np.array(y)


@dataclass
class SinusoidArb(ArbWaveform):
    """Sine wave; one cycle is one arb (p.358-362)."""

    shape: ClassVar[str] = "SIN"
    scpi_suffixes: ClassVar[dict[str, str]] = {
        "amplitude": "SIN:AMPL",
        "frequency": "SIN:FREQ",
        "offset": "SIN:OFFS",
    }
    level_params: ClassVar[tuple[str, ...]] = ()
    time_params: ClassVar[tuple[str, ...]] = ()

    amplitude: float = 1.0
    frequency: float = 1.0
    offset: float = 2.5

    @property
    def duration(self) -> float:
        return 1.0 / self.frequency if self.frequency > 0 else 0.0

    def validate(self, limits: ArbLimits) -> None:
        if not FREQUENCY_MIN <= self.frequency <= FREQUENCY_MAX:
            raise ValueError(
                f"sine frequency {self.frequency:g} Hz is outside "
                f"{FREQUENCY_MIN:g}..{FREQUENCY_MAX:g} Hz"
            )
        # The instrument clips at the module rating, so check the extremes the
        # wave actually reaches rather than the parameters in isolation.
        peak = self.offset + abs(self.amplitude)
        trough = self.offset - abs(self.amplitude)
        if peak > limits.level_max:
            raise ValueError(
                f"sine peak {peak:g} (offset {self.offset:g} + amplitude "
                f"{abs(self.amplitude):g}) exceeds the channel maximum "
                f"{limits.level_max:g}"
            )
        if trough < limits.level_min:
            raise ValueError(
                f"sine trough {trough:g} (offset {self.offset:g} - amplitude "
                f"{abs(self.amplitude):g}) is below the channel minimum "
                f"{limits.level_min:g}"
            )

    def points(self) -> tuple[np.ndarray, np.ndarray]:
        t = np.linspace(0.0, self.duration, CURVE_POINTS)
        y = self.offset + self.amplitude * np.sin(2 * np.pi * self.frequency * t)
        return t, y


@dataclass
class PulseArb(ArbWaveform):
    """Rectangular pulse (p.338-346)."""

    shape: ClassVar[str] = "PULS"
    scpi_suffixes: ClassVar[dict[str, str]] = {
        "start_level": "PULS:STAR",
        "start_time": "PULS:STAR:TIM",
        "top_level": "PULS:TOP",
        "top_time": "PULS:TOP:TIM",
        "end_time": "PULS:END:TIM",
    }
    level_params: ClassVar[tuple[str, ...]] = ("start_level", "top_level")
    time_params: ClassVar[tuple[str, ...]] = ("start_time", "top_time", "end_time")

    start_level: float = 0.0
    start_time: float = 0.1
    top_level: float = 5.0
    top_time: float = 0.5
    end_time: float = 0.4

    @property
    def duration(self) -> float:
        return self.start_time + self.top_time + self.end_time

    def points(self) -> tuple[np.ndarray, np.ndarray]:
        t0 = self.start_time
        t1 = t0 + self.top_time
        t2 = t1 + self.end_time
        return (
            np.array([0.0, t0, t0, t1, t1, t2]),
            np.array(
                [
                    self.start_level,
                    self.start_level,
                    self.top_level,
                    self.top_level,
                    self.start_level,
                    self.start_level,
                ]
            ),
        )


@dataclass
class TrapezoidArb(ArbWaveform):
    """Pulse with controlled rise and fall (p.381-393)."""

    shape: ClassVar[str] = "TRAP"
    scpi_suffixes: ClassVar[dict[str, str]] = {
        "start_level": "TRAP:STAR",
        "start_time": "TRAP:STAR:TIM",
        "rise_time": "TRAP:RTIM",
        "top_level": "TRAP:TOP",
        "top_time": "TRAP:TOP:TIM",
        "fall_time": "TRAP:FTIM",
        "end_time": "TRAP:END:TIM",
    }
    level_params: ClassVar[tuple[str, ...]] = ("start_level", "top_level")
    time_params: ClassVar[tuple[str, ...]] = (
        "start_time",
        "rise_time",
        "top_time",
        "fall_time",
        "end_time",
    )

    start_level: float = 0.0
    start_time: float = 0.1
    rise_time: float = 0.2
    top_level: float = 5.0
    top_time: float = 0.5
    fall_time: float = 0.2
    end_time: float = 0.1

    @property
    def duration(self) -> float:
        return (
            self.start_time
            + self.rise_time
            + self.top_time
            + self.fall_time
            + self.end_time
        )

    def points(self) -> tuple[np.ndarray, np.ndarray]:
        t0 = self.start_time
        t1 = t0 + self.rise_time
        t2 = t1 + self.top_time
        t3 = t2 + self.fall_time
        t4 = t3 + self.end_time
        return (
            np.array([0.0, t0, t1, t2, t3, t4]),
            np.array(
                [
                    self.start_level,
                    self.start_level,
                    self.top_level,
                    self.top_level,
                    self.start_level,
                    self.start_level,
                ]
            ),
        )


@dataclass
class ExponentialArb(ArbWaveform):
    """Exponential approach to a level (p.329-336).

    ``time_constant`` is tau: after one tau the output has covered about 63% of
    the distance from start to end.
    """

    shape: ClassVar[str] = "EXP"
    scpi_suffixes: ClassVar[dict[str, str]] = {
        "start_level": "EXP:STAR",
        "start_time": "EXP:STAR:TIM",
        "end_level": "EXP:END",
        "time_constant": "EXP:TCON",
        "time": "EXP:TIM",
    }
    level_params: ClassVar[tuple[str, ...]] = ("start_level", "end_level")
    time_params: ClassVar[tuple[str, ...]] = ("start_time", "time_constant", "time")

    start_level: float = 0.0
    start_time: float = 0.1
    end_level: float = 5.0
    time_constant: float = 0.3
    time: float = 1.5

    @property
    def duration(self) -> float:
        return self.start_time + self.time

    def validate(self, limits: ArbLimits) -> None:
        super().validate(limits)
        if self.time_constant <= 0:
            raise ValueError("exponential time_constant must be greater than zero")

    def points(self) -> tuple[np.ndarray, np.ndarray]:
        curve_t = np.linspace(0.0, self.time, CURVE_POINTS)
        span = self.end_level - self.start_level
        curve_y = self.start_level + span * (1.0 - np.exp(-curve_t / self.time_constant))
        t = np.concatenate([[0.0], self.start_time + curve_t])
        y = np.concatenate([[self.start_level], curve_y])
        return t, y


def _format_list(values) -> str:
    return ",".join(f"{float(v):.9G}" for v in values)


@dataclass
class UserDefinedArb(ArbWaveform):
    """A point list with a dwell time per point (p.395-403).

    Up to 511 points.  ``bost`` marks points that emit a trigger-out pulse at
    the beginning of their step; leaving it empty sends no BOST list at all.
    """

    shape: ClassVar[str] = "UDEF"
    scpi_suffixes: ClassVar[dict[str, str]] = {}
    level_params: ClassVar[tuple[str, ...]] = ()
    time_params: ClassVar[tuple[str, ...]] = ()

    levels: list[float] = field(default_factory=lambda: [0.0, 1.0])
    dwells: list[float] = field(default_factory=lambda: [0.1, 0.1])
    bost: list[bool] = field(default_factory=list)

    @property
    def n_points(self) -> int:
        return len(self.levels)

    @property
    def duration(self) -> float:
        return float(sum(self.dwells))

    def scpi_commands(self, kind: str, chanlist: str, step: int | None = None) -> list[str]:
        prefix = f"ARB:SEQ:STEP:{kind}:UDEF" if step is not None else f"ARB:{kind}:UDEF"
        tail = f",{step},{chanlist}" if step is not None else f",{chanlist}"
        commands = [
            f"{prefix}:LEV {_format_list(self.levels)}{tail}",
            f"{prefix}:DWEL {_format_list(self.dwells)}{tail}",
        ]
        if self.bost:
            flags = ",".join("1" if b else "0" for b in self.bost)
            commands.append(f"{prefix}:BOST {flags}{tail}")
        return commands

    def validate(self, limits: ArbLimits) -> None:
        if not self.levels:
            raise ValueError("user-defined ARB needs at least one point")
        if len(self.levels) > UDEFINED_MAX_POINTS:
            raise ValueError(
                f"user-defined ARB has {len(self.levels)} points; the maximum is "
                f"{UDEFINED_MAX_POINTS}"
            )
        if len(self.dwells) != len(self.levels):
            raise ValueError(
                f"user-defined ARB has {len(self.levels)} levels but "
                f"{len(self.dwells)} dwell times; they must match"
            )
        if self.bost and len(self.bost) != len(self.levels):
            raise ValueError(
                f"user-defined ARB has {len(self.levels)} levels but "
                f"{len(self.bost)} BOST flags; they must match"
            )
        for index, level in enumerate(self.levels):
            if not limits.level_min <= level <= limits.level_max:
                raise ValueError(
                    f"user-defined ARB point {index + 1} is {level:g}, outside the "
                    f"channel range {limits.level_min:g}..{limits.level_max:g}"
                )
        for index, dwell in enumerate(self.dwells):
            if dwell < 0:
                raise ValueError(f"dwell {index + 1} is negative")
            if dwell > limits.time_max:
                raise ValueError(
                    f"dwell {index + 1} is {dwell:g} s, above the maximum "
                    f"{limits.time_max:g} s"
                )
        if self.duration <= 0:
            raise ValueError("user-defined ARB has zero total duration")

    def points(self) -> tuple[np.ndarray, np.ndarray]:
        """Each level is held for its dwell, so the trace is a staircase."""
        t: list[float] = []
        y: list[float] = []
        now = 0.0
        for level, dwell in zip(self.levels, self.dwells):
            t += [now, now + dwell]
            y += [level, level]
            now += dwell
        return np.array(t), np.array(y)

    def describe(self) -> str:
        return f"UDEF({self.n_points} points, {self.duration:g} s)"


@dataclass
class ConstantDwellArb(ArbWaveform):
    """A point list sharing one dwell time (p.324-327).

    Holds far more points than a user-defined arb (65,535 vs 511), which makes
    it the right target for a captured waveform imported from CSV.

    Two instrument-level quirks are worth knowing:

    * the repeat count is capped at 256, not the usual 16 million;
    * **voltage and current constant-dwell arbs share one set of settings**, so
      programming one resets the other to its default.
    """

    shape: ClassVar[str] = "CDW"
    scpi_suffixes: ClassVar[dict[str, str]] = {}
    level_params: ClassVar[tuple[str, ...]] = ()
    time_params: ClassVar[tuple[str, ...]] = ()

    levels: list[float] = field(default_factory=lambda: [0.0, 1.0])
    dwell: float = 0.001

    @property
    def n_points(self) -> int:
        return len(self.levels)

    @property
    def duration(self) -> float:
        return len(self.levels) * self.dwell

    @property
    def sample_rate(self) -> float:
        return 1.0 / self.dwell if self.dwell > 0 else 0.0

    def scpi_commands(self, kind: str, chanlist: str, step: int | None = None) -> list[str]:
        if step is not None:
            # ARB:SEQuence:STEP:FUNCtion:SHAPe does not offer CDWell (p.316).
            raise ValueError("constant-dwell ARBs cannot be used as a sequence step")
        return [
            f"ARB:{kind}:CDW {_format_list(self.levels)},{chanlist}",
            f"ARB:{kind}:CDW:DWEL {self.dwell:.9G},{chanlist}",
        ]

    def validate(self, limits: ArbLimits) -> None:
        if not self.levels:
            raise ValueError("constant-dwell ARB needs at least one point")
        if len(self.levels) > CDWELL_MAX_POINTS:
            raise ValueError(
                f"constant-dwell ARB has {len(self.levels):,} points; the maximum "
                f"is {CDWELL_MAX_POINTS:,}"
            )
        if not CDWELL_DWELL_MIN <= self.dwell <= CDWELL_DWELL_MAX:
            raise ValueError(
                f"constant-dwell interval {self.dwell:g} s is outside "
                f"{CDWELL_DWELL_MIN:g}..{CDWELL_DWELL_MAX:g} s"
            )
        for index, level in enumerate(self.levels):
            if not limits.level_min <= level <= limits.level_max:
                raise ValueError(
                    f"constant-dwell point {index + 1} is {level:g}, outside the "
                    f"channel range {limits.level_min:g}..{limits.level_max:g}"
                )

    def points(self) -> tuple[np.ndarray, np.ndarray]:
        n = len(self.levels)
        edges = np.arange(n + 1, dtype=np.float64) * self.dwell
        t = np.repeat(edges, 2)[1:-1]
        y = np.repeat(np.asarray(self.levels, dtype=np.float64), 2)
        return t, y

    def describe(self) -> str:
        return f"CDW({self.n_points} points @ {self.dwell:g} s, {self.duration:g} s)"


SHAPES: dict[str, type[ArbWaveform]] = {
    cls.shape: cls
    for cls in (
        StepArb,
        RampArb,
        StaircaseArb,
        SinusoidArb,
        PulseArb,
        TrapezoidArb,
        ExponentialArb,
        UserDefinedArb,
        ConstantDwellArb,
    )
}


def make_waveform(shape: str, **params: float) -> ArbWaveform:
    """Build a waveform from its SCPI mnemonic, e.g. ``make_waveform("RAMP")``."""
    key = shape.strip().upper()
    # Accept the long spellings the manual also uses.
    aliases = {
        "STAIRCASE": "STA",
        "SINUSOID": "SIN",
        "SINE": "SIN",
        "PULSE": "PULS",
        "TRAPEZOID": "TRAP",
        "EXPONENTIAL": "EXP",
        "UDEFINED": "UDEF",
        "USERDEFINED": "UDEF",
        "CDWELL": "CDW",
        "CONSTANTDWELL": "CDW",
    }
    key = aliases.get(key, key)
    if key not in SHAPES:
        raise ValueError(
            f"unknown ARB shape {shape!r}; expected one of {', '.join(SHAPES)}"
        )
    return SHAPES[key](**params)  # type: ignore[arg-type]


@dataclass
class ArbSpec:
    """A waveform bound to a channel, with its repeat and termination settings."""

    channel: int
    waveform: ArbWaveform
    arb_type: str = "VOLT"  # VOLT or CURR
    count: int | str = 1  # or "INF"
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
    def total_duration(self) -> float:
        """Seconds the whole run takes; infinite repeats give ``inf``."""
        if self.infinite:
            return math.inf
        return self.waveform.duration * int(self.count)

    def validate(self, limits: ArbLimits | None = None) -> None:
        if self.arb_type.upper() not in ("VOLT", "VOLTAGE", "CURR", "CURRENT"):
            raise ValueError(
                f"arb_type must be VOLT or CURR, not {self.arb_type!r}"
            )
        self.waveform.validate(limits or ArbLimits())
        if not self.infinite:
            count = int(self.count)
            # Constant-dwell arbs cap far lower than every other shape.
            ceiling = (
                CDWELL_COUNT_MAX
                if isinstance(self.waveform, ConstantDwellArb)
                else ARB_COUNT_MAX
            )
            if not 1 <= count <= ceiling:
                raise ValueError(
                    f"repeat count {count} must be between 1 and {ceiling:,}"
                    + (" for a constant-dwell ARB" if ceiling == CDWELL_COUNT_MAX else "")
                )

    def points(self, cycles: int | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Preview points, repeated *cycles* times (default: the repeat count).

        Infinite repeats are previewed as three cycles -- enough to show that
        it loops without pretending to draw forever.
        """
        if cycles is None:
            cycles = 3 if self.infinite else int(self.count)
        cycles = max(1, min(int(cycles), 50))  # a preview, not a simulation
        base_t, base_y = self.waveform.points()
        period = self.waveform.duration
        if cycles == 1 or period <= 0:
            return base_t, base_y

        # STEP draws a tail past its own duration so the level it settles at is
        # visible. Repeated, that tail would overlap the next cycle and the
        # polyline would run backwards in time -- drawing a diagonal the
        # waveform never produces. Every cycle but the last is cut at the
        # period; the last keeps its tail.
        keep = base_t <= period + 1e-12
        head_t, head_y = base_t[keep], base_y[keep]

        times = [head_t + index * period for index in range(cycles - 1)]
        values = [head_y] * (cycles - 1)
        times.append(base_t + (cycles - 1) * period)
        values.append(base_y)
        return np.concatenate(times), np.concatenate(values)
