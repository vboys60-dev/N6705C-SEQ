"""What every channel should do: level, limit, output, waveform, delay.

The function page edits one of these per slot and the rest of the app reads
it, so this is the one place that knows what "the channels are set up like
this" means.  Qt-free, so the whole arrangement -- including the delays and
what they cost -- can be worked out and tested without a display.

Two things it deliberately computes rather than leaves to the UI:

* **the delay mechanism per channel**, via :func:`~.channel_delay.resolve_delay`
  -- a channel running a waveform waits from the trigger, a plain DC channel
  waits from the output-on command, and the two are never combined;
* **how long switching everything off will take**, because a programmed fall
  delay defers ``OUTPut OFF`` (p.152) and an operator who set one should not
  discover that by watching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

import numpy as np

from .arb.models import ArbLimits, ArbSpec
from .arb.sequence import ArbSequence
from .capability import ChannelCapability
from .channel_delay import ChannelDelay, DelayMechanism, resolve_delay
from .delay import DEFAULT_MAX_DELAY, OutputDelay, shutdown_seconds
from .scpi import format_channel_list

__all__ = ["ChannelFunction", "ChannelTrace", "FunctionPlan"]

log = logging.getLogger(__name__)


@dataclass
class ChannelTrace:
    """One channel's line on the overlay preview."""

    channel: int
    times: np.ndarray
    values: np.ndarray
    is_current: bool = False

    @property
    def unit(self) -> str:
        return "A" if self.is_current else "V"

    @property
    def duration(self) -> float:
        return float(self.times[-1]) if len(self.times) else 0.0


@dataclass
class ChannelFunction:
    """One channel's whole setup."""

    channel: int
    voltage: float = 0.0
    current: float = 0.0
    #: Switch this output on as part of applying the plan.
    output_on: bool = False
    #: Whether this channel plays a waveform when the trigger arrives.
    arb_enabled: bool = False
    #: Wait this long after the starting event before driving. Which event
    #: that is depends on ``arb_enabled``; see :mod:`.channel_delay`.
    delay: float = 0.0
    #: The waveform to play. ``None`` while one has not been chosen yet.
    spec: ArbSpec | ArbSequence | None = None

    @property
    def runs_waveform(self) -> bool:
        return self.arb_enabled

    @property
    def is_ready(self) -> bool:
        """Whether this channel has everything it needs to run."""
        return not self.arb_enabled or self.spec is not None

    def resolved_delay(
        self,
        limits: ArbLimits | None = None,
        maximum: float = DEFAULT_MAX_DELAY,
    ) -> ChannelDelay:
        spec = self.spec if isinstance(self.spec, ArbSpec) else None
        return resolve_delay(
            self.channel,
            self.delay,
            spec=spec if self.arb_enabled else None,
            limits=limits,
            maximum=maximum,
            runs_waveform=self.arb_enabled,
        )

    def validate(self, capability: ChannelCapability | None = None) -> None:
        if self.voltage < 0:
            raise ValueError(f"channel {self.channel}: voltage cannot be negative")
        if self.current < 0:
            raise ValueError(f"channel {self.channel}: current limit cannot be negative")
        if self.delay < 0:
            raise ValueError(f"channel {self.channel}: delay cannot be negative")
        if capability is None:
            return
        if not capability.present:
            raise ValueError(f"channel {self.channel} is an empty slot")
        for name, value, ceiling in (
            ("voltage", self.voltage, capability.voltage_max),
            ("current limit", self.current, capability.current_max),
        ):
            if ceiling == ceiling and value > ceiling:  # skip NaN
                raise ValueError(
                    f"channel {self.channel}: {name} {value:g} exceeds the "
                    f"{capability.model} maximum {ceiling:g}"
                )

    def level_commands(self) -> list[str]:
        """Just the DC setpoints; the delay and the waveform go elsewhere."""
        chan = format_channel_list(self.channel)
        return [
            f"VOLT {self.voltage:.6G},{chan}",
            f"CURR {self.current:.6G},{chan}",
        ]

    def describe(self) -> str:
        parts = [f"CH{self.channel} {self.voltage:g} V / {self.current:g} A"]
        if self.arb_enabled:
            shape = (
                self.spec.waveform.describe()
                if isinstance(self.spec, ArbSpec)
                else self.spec.describe()
                if self.spec is not None
                else "no waveform yet"
            )
            parts.append(shape)
        if self.delay:
            parts.append(f"delay {self.delay:g} s")
        return ", ".join(parts)


@dataclass
class FunctionPlan:
    """Every channel's setup, and what it adds up to."""

    channels: list[ChannelFunction] = field(default_factory=list)
    #: Fall delays, which are about switching off rather than starting.
    fall_delays: dict[int, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.channels.sort(key=lambda c: c.channel)

    def __iter__(self):
        return iter(self.channels)

    def __len__(self) -> int:
        return len(self.channels)

    def get(self, channel: int) -> ChannelFunction | None:
        return next((c for c in self.channels if c.channel == channel), None)

    @property
    def outputs_on(self) -> list[int]:
        return [c.channel for c in self.channels if c.output_on]

    @property
    def waveform_channels(self) -> list[int]:
        return [c.channel for c in self.channels if c.arb_enabled]

    @property
    def unready_channels(self) -> list[int]:
        """Channels marked as running a waveform that have not been given one."""
        return [c.channel for c in self.channels if not c.is_ready]

    def validate(
        self, capabilities: dict[int, ChannelCapability] | None = None
    ) -> None:
        seen: set[int] = set()
        for entry in self.channels:
            if entry.channel in seen:
                raise ValueError(f"channel {entry.channel} appears twice")
            seen.add(entry.channel)
            entry.validate((capabilities or {}).get(entry.channel))

    def resolved_delays(
        self,
        limits: dict[int, ArbLimits] | None = None,
        maximum: float = DEFAULT_MAX_DELAY,
    ) -> dict[int, ChannelDelay]:
        return {
            entry.channel: entry.resolved_delay(
                (limits or {}).get(entry.channel), maximum
            )
            for entry in self.channels
        }

    def output_delays(self, **kwargs) -> list[OutputDelay]:
        """The ``OUTPut:DELay`` settings this plan implies, both edges.

        Rise comes from whichever channels use the output mechanism; fall is
        independent of all that -- it is about the order things switch off.
        """
        resolved = self.resolved_delays(**kwargs)
        delays = []
        for entry in self.channels:
            rise = resolved[entry.channel]
            delays.append(
                replace(
                    rise.output_delay or OutputDelay(entry.channel),
                    fall=self.fall_delays.get(entry.channel, 0.0),
                )
            )
        return delays

    def shutdown_seconds(self, **kwargs) -> float:
        """How long ``all outputs off`` will now take, worst channel first."""
        return shutdown_seconds(self.output_delays(**kwargs))

    def startup_seconds(self, **kwargs) -> float:
        """How long until the last channel is driving, after its own event."""
        return max(
            (d.actual for d in self.resolved_delays(**kwargs).values()), default=0.0
        )

    # -- Preview ------------------------------------------------------------

    @property
    def timeline_base(self) -> str:
        """Which event ``t = 0`` means for the overlay.

        The two kinds of delay start from different events, so putting both on
        one axis would draw a picture that is simply not true. Whichever kind
        is in play wins, and waveforms win when both are: they are the reason
        anyone is looking at a shared time axis.
        """
        return (
            DelayMechanism.WAVEFORM if self.waveform_channels else DelayMechanism.OUTPUT
        )

    def timeline(
        self, limits: dict[int, ArbLimits] | None = None, **kwargs
    ) -> list["ChannelTrace"]:
        """Every channel that shares a time base, drawn against it.

        Channels on the other base are left out rather than drawn misleadingly;
        :attr:`excluded_from_timeline` names them so a caller can say why.
        """
        base = self.timeline_base
        resolved = self.resolved_delays(limits, **kwargs)
        traces: list[ChannelTrace] = []
        for entry in self.channels:
            if base == DelayMechanism.WAVEFORM:
                if not entry.arb_enabled:
                    continue
                spec = resolved[entry.channel].waveform or entry.spec
                if spec is None:
                    continue
                times, values = spec.points()
                traces.append(
                    ChannelTrace(
                        channel=entry.channel,
                        times=np.asarray(times, dtype=float),
                        values=np.asarray(values, dtype=float),
                        is_current=getattr(spec, "is_current", False),
                    )
                )
            else:
                if entry.arb_enabled:
                    continue
                # Off until its delay expires, then at the programmed level.
                start = resolved[entry.channel].actual
                traces.append(
                    ChannelTrace(
                        channel=entry.channel,
                        times=np.array([0.0, start, start, max(start * 1.5, start + 1.0)]),
                        values=np.array([0.0, 0.0, entry.voltage, entry.voltage]),
                        is_current=False,
                    )
                )
        return traces

    @property
    def excluded_from_timeline(self) -> list[int]:
        """Channels left out of the overlay because they run off the other event."""
        base = self.timeline_base
        return [
            entry.channel
            for entry in self.channels
            if (entry.arb_enabled) != (base == DelayMechanism.WAVEFORM)
        ]

    def summary(self, **kwargs) -> str:
        lines = [entry.describe() for entry in self.channels]
        shutdown = self.shutdown_seconds(**kwargs)
        if shutdown:
            lines.append(f"switching every output off will take {shutdown:g} s")
        return "\n".join(lines)
