"""Per-channel output turn-on / turn-off delay (``OUTPut:DELay``, p.152/154).

This is the delay the *hardware* has.  It staggers when each output switches
on or off, which is how a supply sequence for a device under test is built:
3.3 V first, then 1.8 V 50 ms later, then the core rail.

It is **not** a delay on the waveform.  There is no ``TRIGger:TRANsient:DELay``
anywhere in the manual; a channel's ARB always starts the instant the trigger
arrives.  Staggering waveforms is a separate mechanism (see :mod:`.arb.delay`),
and confusing the two produces a test that looks right and measures the wrong
thing.

Two properties of the hardware drive the design here:

**The resolution is stepped, not uniform.**  The manual's table (p.154) gives
eight bands, from 100 ns near zero to 1 s above 102.3 s.  A request of 7 ms
lands on a 10 us grid; a request of 7 s lands on a 10 ms grid.  The instrument
rounds silently, so the accepted value is computed here and shown, rather than
discovered later by looking at a scope.

**The maximum is 1023 s, and the manual is mostly wrong about it.**  p.154's
prose says "0 to 1023 seconds", while the parameter table on the same page says
``0 - 1.023`` and the p.155 example ``OUTP:DEL:RISE? MAX,(@1)`` answers
``+1.023000E+00``.  Two of the three say 1.023, so 1.023 looked like the safe
reading.  It is wrong.

A real N6705C (fw E.02.09.3271, four N6762A) answers ``+1.023000E+03`` on all
four channels, for both edges.  **Only the prose is right**; the parameter
table and the worked example are both wrong.  This is also the reading the
resolution table implies -- eight ranges of 1023 counts ending at 1 s per
count is exactly 1023 s -- so the table above and this ceiling agree.

That is why :func:`read_delay_limit` asks the instrument and believes the
answer, and why the constant below is only a pre-connection placeholder.

.. warning::
   A programmed **fall** delay defers ``OUTPut OFF`` too: p.152 says it
   "affects on-to-off transitions including changes in the ``OUTPut[:STATe]``".
   So :meth:`N6705C.all_outputs_off` can take as long as the longest fall
   delay -- up to about a second -- rather than acting at once.

   This is deliberate and is not worked around here.  A down-sequence exists
   precisely because dropping a device's rails in the wrong order can damage
   it, and an orderly shutdown is the safer behaviour.  What the *hardware*
   guarantees is the case that actually matters: p.152 says the fall delay
   "does NOT affect transitions to off caused by protection functions", so an
   over-current or over-voltage trip is immediate whatever is programmed.

   A UI that lets someone set fall delays should say how long "all outputs
   off" will now take.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .scpi import format_channel_list

__all__ = [
    "RESOLUTION_BANDS",
    "DEFAULT_MAX_DELAY",
    "resolution_for",
    "round_to_grid",
    "quantisation_error",
    "OutputDelay",
    "delay_commands",
    "read_delay_limit",
    "shutdown_seconds",
    "startup_seconds",
]

log = logging.getLogger(__name__)

#: The eight resolutions from the table on p.154, in seconds.
#:
#: The table's bands are not arbitrary: each one spans exactly **1023 counts**
#: of its resolution (1023 x 1 ms = 1.023 s, then the next band picks up at
#: 103 x 10 ms = 1.03 s).  So the hardware is a count plus a range, and the
#: achievable settings are ``count x resolution`` for count in 0..1023 -- which
#: is why the printed bands leave gaps, such as 1.023 s to 1.03 s.
#:
#: Reading it as "which band is this value in" gets those gaps wrong: 1.0234 s
#: falls in the 10 ms band and would snap to 1.02 s, when 1.023 s is reachable
#: on the 1 ms range and is eight times closer.  :func:`round_to_grid`
#: therefore picks the nearest achievable value across all ranges.
RESOLUTIONS: tuple[float, ...] = (
    100e-9, 1e-6, 10e-6, 100e-6, 1e-3, 10e-3, 100e-3, 1.0,
)

#: Counts available at each resolution, from the same table.
MAX_COUNT = 1023

#: ``(upper bound, resolution)`` per range, for display purposes.
RESOLUTION_BANDS: tuple[tuple[float, float], ...] = tuple(
    (MAX_COUNT * r, r) for r in RESOLUTIONS
)

#: What a real N6705C answers to ``OUTP:DEL:RISE? MAX`` -- measured, not taken
#: from the manual, whose parameter table and worked example both say 1.023 and
#: are both wrong (see the module docstring).  It is the top of the resolution
#: table: 1023 counts x 1 s.  Overridden by :func:`read_delay_limit` whenever an
#: instrument is present, so this only governs Demo mode and the pre-connection
#: UI -- which is exactly why it must not be a thousand times too small.
DEFAULT_MAX_DELAY = 1023.0


def _candidates(delay: float) -> list[float]:
    """Every value reachable at some resolution, nearest first per range."""
    values = []
    for resolution in RESOLUTIONS:
        # Half-up rather than Python's banker's rounding: an exact half landing
        # on an even multiple is not a case anyone should have to reason about.
        count = min(MAX_COUNT, math.floor(delay / resolution + 0.5))
        values.append(round(count * resolution, 12))
    return values


def round_to_grid(delay: float) -> float:
    """The value the instrument will actually use for *delay*.

    Rounded to nearest rather than up: unlike a sampling period, a delay is a
    setting and not a limit, so there is no direction that is safe to err in --
    the honest thing is to land where the hardware lands and say so.

    Ties go to the finer resolution, which is the range the instrument would
    have no reason to leave.
    """
    if delay < 0:
        raise ValueError("a delay cannot be negative")
    if delay == 0:
        return 0.0
    # RESOLUTIONS is ordered finest first, and min() keeps the first of equal
    # keys, so the tie-break falls out of the ordering.
    return min(_candidates(delay), key=lambda v: abs(v - delay))


def resolution_for(delay: float) -> float:
    """The resolution the instrument will end up using for *delay*."""
    if delay < 0:
        raise ValueError("a delay cannot be negative")
    snapped = round_to_grid(delay)
    for resolution in RESOLUTIONS:
        count = round(snapped / resolution)
        if count <= MAX_COUNT and abs(count * resolution - snapped) < 1e-12:
            return resolution
    return RESOLUTIONS[-1]


def quantisation_error(delay: float) -> float:
    """How far the accepted value sits from the requested one, in seconds."""
    return round_to_grid(delay) - delay


@dataclass(frozen=True)
class OutputDelay:
    """The turn-on and turn-off delays for one channel.

    Both default to zero, which is what the instrument powers up with (p.560's
    reset table lists ``OUTPut:DELay:RISE 0`` and ``:FALL 0``).
    """

    channel: int
    rise: float = 0.0
    fall: float = 0.0

    def validate(self, maximum: float = DEFAULT_MAX_DELAY) -> None:
        for name, value in (("rise", self.rise), ("fall", self.fall)):
            if value < 0:
                raise ValueError(
                    f"channel {self.channel} {name} delay {value:g} s is negative"
                )
            if value > maximum + 1e-12:
                raise ValueError(
                    f"channel {self.channel} {name} delay {value:g} s exceeds the "
                    f"{maximum:g} s the instrument accepts"
                )

    @property
    def accepted(self) -> "OutputDelay":
        """The same delays as the instrument will actually apply them."""
        return OutputDelay(
            channel=self.channel,
            rise=round_to_grid(self.rise),
            fall=round_to_grid(self.fall),
        )

    @property
    def is_rounded(self) -> bool:
        """True when the hardware grid cannot represent what was asked for."""
        return self.accepted != self

    @property
    def is_zero(self) -> bool:
        return self.rise == 0.0 and self.fall == 0.0

    def scpi_commands(self, maximum: float = DEFAULT_MAX_DELAY) -> list[str]:
        self.validate(maximum)
        chan = format_channel_list(self.channel)
        return [
            f"OUTP:DEL:RISE {self.rise:.9G},{chan}",
            f"OUTP:DEL:FALL {self.fall:.9G},{chan}",
        ]

    def describe(self) -> str:
        return f"CH{self.channel} rise {self.rise:g} s, fall {self.fall:g} s"


def shutdown_seconds(delays: list[OutputDelay]) -> float:
    """How long switching every output off will now take.

    ``OUTPut OFF`` is deferred by each channel's fall delay (p.152), so with a
    down-sequence programmed the emergency stop is no longer instant. Callers
    show this rather than letting an operator find out by watching.
    """
    return max((round_to_grid(d.fall) for d in delays), default=0.0)


def startup_seconds(delays: list[OutputDelay]) -> float:
    """How long the full turn-on sequence takes, last rail included."""
    return max((round_to_grid(d.rise) for d in delays), default=0.0)


def delay_commands(
    delays: list[OutputDelay], maximum: float = DEFAULT_MAX_DELAY
) -> list[str]:
    """Commands for several channels, in channel order.

    Ordered so that a sequence reads the way it runs when the log is replayed.
    """
    commands: list[str] = []
    for delay in sorted(delays, key=lambda d: d.channel):
        commands.extend(delay.scpi_commands(maximum))
    return commands


def read_delay_limit(session, channel: int, edge: str = "RISE") -> float:
    """Ask the instrument for its own maximum, falling back to the manual's.

    The manual contradicts itself about whether the ceiling is 1.023 s or
    1023 s (see the module docstring), so the instrument gets the last word.
    A channel that will not answer is not worth failing a connection over --
    the documented value is used and the disagreement logged.
    """
    edge = edge.strip().upper()
    if edge not in ("RISE", "FALL"):
        raise ValueError(f"edge must be RISE or FALL, not {edge!r}")
    query = f"OUTP:DEL:{edge}? MAX,{format_channel_list(channel)}"
    try:
        return float(session.query(query).strip())
    except Exception:
        log.debug("channel %d did not report a delay limit; using %g s",
                  channel, DEFAULT_MAX_DELAY, exc_info=True)
        return DEFAULT_MAX_DELAY
