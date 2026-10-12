"""Downloading and running an ARB on a connected instrument."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from ..capability import ChannelCapability
from ..scpi import format_channel_list
from ..instrument import N6705C
from ..status import OperationStatus
from .models import ArbLimits, ArbSpec
from .scpi import abort_command, arm_commands, configure_commands, trigger_command
from .sequence import ArbSequence

__all__ = ["ArbState", "ArbRunner", "SequenceRunner"]

log = logging.getLogger(__name__)


class ArbState(Enum):
    IDLE = "idle"
    LOADED = "loaded"
    ARMED = "armed"
    RUNNING = "running"
    DONE = "done"


@dataclass
class ArbRunner:
    """Loads one :class:`ArbSpec` onto a channel and drives it.

    Note that the runner never switches the output on.  A waveform is only
    produced when the output is already enabled, and turning an output on is a
    decision that belongs to the operator, not to a side effect of pressing
    "run" (PLAN.md section 9).
    """

    instrument: N6705C
    spec: ArbSpec
    state: ArbState = ArbState.IDLE

    def __post_init__(self) -> None:
        self.spec.validate(self.limits())

    # -- Capability ---------------------------------------------------------

    def capability(self) -> ChannelCapability:
        capability = self.instrument.capabilities.get(self.spec.channel)
        if capability is None:
            raise ValueError(f"channel {self.spec.channel} is not present")
        if not capability.present:
            raise ValueError(f"channel {self.spec.channel} is an empty slot")
        return capability

    def limits(self) -> ArbLimits:
        return ArbLimits.for_channel(self.capability(), self.spec.normalised_type)

    # -- Run control --------------------------------------------------------

    def load(self) -> None:
        """Send the shape and its parameters, then verify the instrument agreed."""
        self.spec.validate(self.limits())
        session = self.instrument.session
        for command in configure_commands(self.spec):
            session.write(command)
        session.check_errors("ARB configure")
        self.state = ArbState.LOADED

    def arm(self) -> None:
        if self.state is ArbState.IDLE:
            self.load()
        session = self.instrument.session
        for command in arm_commands(self.spec):
            session.write(command)
        session.check_errors("ARB arm")
        self.state = ArbState.ARMED

    def trigger(self) -> None:
        if self.state is not ArbState.ARMED:
            self.arm()
        session = self.instrument.session
        if self.spec.trigger_source.strip().upper() == "BUS":
            session.write(trigger_command(self.spec))
            session.check_errors("ARB trigger")
        self.state = ArbState.RUNNING

    def run(self) -> None:
        """Load, arm and trigger in one call."""
        self.load()
        self.arm()
        self.trigger()

    def abort(self) -> None:
        """Stop immediately; the output returns to its pre-arb setting."""
        try:
            self.instrument.session.write(abort_command(self.spec))
        finally:
            self.state = ArbState.DONE

    # -- Progress -----------------------------------------------------------

    def status(self) -> OperationStatus:
        return self.instrument.operation_status([self.spec.channel])[self.spec.channel]

    @property
    def is_running(self) -> bool:
        """True while the transient system is still initiated for this channel."""
        if self.state not in (ArbState.ARMED, ArbState.RUNNING):
            return False
        active = OperationStatus.TRAN_ACTIVE in self.status()
        if not active and self.state is ArbState.RUNNING:
            self.state = ArbState.DONE
        return active

    def readback(self) -> dict[str, float]:
        """Read the parameters back, to confirm what the instrument accepted."""
        session = self.instrument.session
        kind = self.spec.normalised_type
        chan = f"(@{self.spec.channel})"
        values: dict[str, float] = {}
        for name, suffix in self.spec.waveform.scpi_suffixes.items():
            try:
                values[name] = session.query_float(f"ARB:{kind}:{suffix}? {chan}")
            except Exception as exc:
                log.debug("readback of %s failed: %s", suffix, exc)
                session.read_errors()
        return values


@dataclass
class SequenceRunner:
    """Loads and runs an :class:`ArbSequence`.

    Mirrors :class:`ArbRunner`, but reads ``ARB:SEQuence:QUALity?`` back after
    loading: a sequence can be accepted and still draw its curves coarsely,
    because every waveform in it shares one 511-point budget.
    """

    instrument: N6705C
    sequence: ArbSequence
    state: ArbState = ArbState.IDLE
    quality: int | None = None

    def __post_init__(self) -> None:
        self.sequence.validate(self.limits())

    def capability(self) -> ChannelCapability:
        capability = self.instrument.capabilities.get(self.sequence.channel)
        if capability is None:
            raise ValueError(f"channel {self.sequence.channel} is not present")
        if not capability.present:
            raise ValueError(f"channel {self.sequence.channel} is an empty slot")
        return capability

    def limits(self) -> ArbLimits:
        return ArbLimits.for_channel(self.capability(), self.sequence.normalised_type)

    def load(self) -> None:
        self.sequence.validate(self.limits())
        session = self.instrument.session
        for command in self.sequence.configure_commands():
            session.write(command)
        session.check_errors("ARB sequence configure")
        chan = format_channel_list(self.sequence.channel)
        try:
            self.quality = session.query_int(f"ARB:SEQ:QUAL? {chan}")
        except Exception as exc:
            log.debug("sequence quality query failed: %s", exc)
            session.read_errors()
            self.quality = None
        self.state = ArbState.LOADED

    def _spec_view(self) -> ArbSpec:
        """A stand-in used only for the shared arm/trigger/abort commands."""
        return ArbSpec(
            channel=self.sequence.channel,
            waveform=self.sequence.steps[0].waveform,
            arb_type=self.sequence.arb_type,
            trigger_source=self.sequence.trigger_source,
        )

    def arm(self) -> None:
        if self.state is ArbState.IDLE:
            self.load()
        session = self.instrument.session
        for command in arm_commands(self._spec_view()):
            session.write(command)
        session.check_errors("ARB sequence arm")
        self.state = ArbState.ARMED

    def trigger(self) -> None:
        if self.state is not ArbState.ARMED:
            self.arm()
        session = self.instrument.session
        if self.sequence.trigger_source.strip().upper() == "BUS":
            session.write(trigger_command(self._spec_view()))
            session.check_errors("ARB sequence trigger")
        self.state = ArbState.RUNNING

    def run(self) -> None:
        self.load()
        self.arm()
        self.trigger()

    def abort(self) -> None:
        try:
            self.instrument.session.write(abort_command(self._spec_view()))
        finally:
            self.state = ArbState.DONE

    def status(self) -> OperationStatus:
        channel = self.sequence.channel
        return self.instrument.operation_status([channel])[channel]

    @property
    def is_running(self) -> bool:
        if self.state not in (ArbState.ARMED, ArbState.RUNNING):
            return False
        active = OperationStatus.TRAN_ACTIVE in self.status()
        if not active and self.state is ArbState.RUNNING:
            self.state = ArbState.DONE
        return active

    def length(self) -> int:
        """Steps the instrument reports, which should match what we sent."""
        chan = format_channel_list(self.sequence.channel)
        return self.instrument.session.query_int(f"ARB:SEQ:LEN? {chan}")
