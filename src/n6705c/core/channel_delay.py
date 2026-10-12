"""One delay per channel: wait, then start driving.

From the operator's side there is a single idea -- *whatever starts this
channel, hold off for N seconds before real output begins*.  The instrument
has no such setting, and what it does have depends on which event starts the
channel:

======================  ==================================================
starts on ``OUTP ON``   ``OUTPut:DELay:RISE`` holds the output off for N
                        seconds after the command arrives (:mod:`.delay`).
starts on a trigger     The transient system has no delay at all, so N
(the channel runs an    seconds of the channel's own starting level are
ARB)                    built onto the front of the waveform
                        (:mod:`.arb.delay`).
======================  ==================================================

Both give "N seconds after the event, output begins", so the operator sets one
number.  This module picks the mechanism and says which one it picked --
because the two are not physically identical during the wait:

* with the output delay the output is **not enabled**: the terminals are open,
  and nothing is driven or sunk;
* with the waveform delay the output **is enabled and regulating** at the
  waveform's starting level, so a channel sitting at 0 V will hold 0 V against
  whatever the load does.

For a device with its own supply that difference decides whether the channel
sinks current during the wait, so :attr:`ChannelDelay.holds_output_off` is
part of the model rather than a footnote.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .arb.delay import DelayedWaveform, delay_spec
from .arb.models import ArbLimits, ArbSpec
from .arb.sequence import ArbSequence
from .delay import DEFAULT_MAX_DELAY, OutputDelay, round_to_grid

__all__ = ["DelayMechanism", "ChannelDelay", "resolve_delay"]

log = logging.getLogger(__name__)


class DelayMechanism:
    """Which of the instrument's two facilities carries the delay."""

    NONE = "none"
    #: ``OUTPut:DELay:RISE`` -- counted from the output-on command.
    OUTPUT = "output"
    #: Built into the waveform -- counted from the transient trigger.
    WAVEFORM = "waveform"


@dataclass
class ChannelDelay:
    """A channel's delay, resolved to something the instrument can be told."""

    channel: int
    requested: float
    actual: float
    mechanism: str
    #: Set when the delay rides on ``OUTPut:DELay``.
    output_delay: OutputDelay | None = None
    #: Set when the delay is built into the waveform.
    waveform: ArbSpec | ArbSequence | None = None
    notes: tuple[str, ...] = ()

    @property
    def error(self) -> float:
        return self.actual - self.requested

    @property
    def is_exact(self) -> bool:
        return abs(self.error) < 1e-12

    @property
    def holds_output_off(self) -> bool:
        """Whether the terminals stay open during the wait.

        True for the output delay, false for the waveform delay -- there the
        channel is already regulating at its starting level.
        """
        return self.mechanism == DelayMechanism.OUTPUT

    @property
    def counted_from(self) -> str:
        return {
            DelayMechanism.OUTPUT: "the output-on command",
            DelayMechanism.WAVEFORM: "the trigger",
        }.get(self.mechanism, "nothing -- there is no delay")

    def describe(self) -> str:
        if self.mechanism == DelayMechanism.NONE:
            return f"CH{self.channel} no delay"
        return (
            f"CH{self.channel} {self.actual:g} s after {self.counted_from}"
            + ("" if self.is_exact else f" (asked for {self.requested:g} s)")
        )


def resolve_delay(
    channel: int,
    delay: float,
    *,
    spec: ArbSpec | None = None,
    limits: ArbLimits | None = None,
    maximum: float = DEFAULT_MAX_DELAY,
    runs_waveform: bool | None = None,
) -> ChannelDelay:
    """Work out how to make *channel* wait *delay* seconds before driving.

    Pass *spec* when the channel runs a waveform; the delay then goes into the
    waveform and is counted from the trigger.  Without one the channel is a
    plain DC output and the delay goes into ``OUTPut:DELay:RISE``, counted
    from the output-on command.

    *runs_waveform* covers the in-between state a UI has: the channel is
    marked as running an ARB but none has been chosen yet.  The mechanism is
    then known -- the delay will come from the trigger -- while the exact
    accepted value is not, so it is reported as asked for and flagged.

    The two mechanisms are never combined.  A channel whose output is switched
    on before it is triggered -- which is the order the integrated test uses --
    would otherwise wait twice for a single number the operator entered once.
    """
    if delay < 0:
        raise ValueError(f"channel {channel} delay {delay:g} s is negative")
    if runs_waveform is None:
        runs_waveform = spec is not None

    if delay == 0:
        return ChannelDelay(
            channel=channel,
            requested=0.0,
            actual=0.0,
            mechanism=DelayMechanism.NONE,
            output_delay=OutputDelay(channel=channel),
            waveform=spec,
        )

    if spec is None and runs_waveform:
        # The waveform decides the accepted value -- a constant-dwell arb can
        # only land on a multiple of its dwell -- so promise nothing yet.
        return ChannelDelay(
            channel=channel,
            requested=delay,
            actual=delay,
            mechanism=DelayMechanism.WAVEFORM,
            output_delay=OutputDelay(channel=channel),
            waveform=None,
            notes=("no waveform has been chosen yet, so this is not final",),
        )

    if spec is None:
        held = OutputDelay(channel=channel, rise=delay)
        held.validate(maximum)
        accepted = held.accepted
        notes: tuple[str, ...] = ()
        if held.is_rounded:
            notes = (
                f"the instrument's delay grid moved this to {accepted.rise:g} s",
            )
        return ChannelDelay(
            channel=channel,
            requested=delay,
            actual=accepted.rise,
            mechanism=DelayMechanism.OUTPUT,
            output_delay=held,
            notes=notes,
        )

    moved: DelayedWaveform = delay_spec(spec, delay, limits)
    return ChannelDelay(
        channel=channel,
        requested=moved.requested,
        actual=moved.actual,
        mechanism=DelayMechanism.WAVEFORM,
        # Left at zero on purpose: the wait is already in the waveform, and
        # adding an output delay as well would double it.
        output_delay=OutputDelay(channel=channel),
        waveform=moved.spec,
        notes=moved.notes,
    )
