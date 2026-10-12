"""Several N6705C mainframes managed as one.

Up to four frames, so up to sixteen channels.  A channel is identified by the
pair ``(frame, channel)``; :class:`GlobalChannel` also carries a flat 1..16
index, which is what everything downstream -- the logger, the Parquet columns,
the plot -- keeps speaking, so none of that had to be rewritten to gain a
second mainframe.

.. warning::
   **A group is not synchronised.**  Four frames are four LAN connections, and
   four ``*TRG`` commands are four round trips of 0.2 to 2 ms each, jittering.
   :attr:`InstrumentGroup.trigger_skew` is that figure, and it is larger than
   most of the offsets the function page is used to set up.

   Software cannot fix this.  The instruments have an official way to do it --
   the digital port's ``TOUTput``/``TINPut`` pins for triggers and
   ``ONCouple``/``OFFCouple`` for output sequencing (p.424) -- but it needs the
   frames' digital ports wired together, which is M11.3.  Until then anything
   that spans frames must *say* that its channels are not aligned rather than
   present them as if they were.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .capability import ChannelCapability
from .instrument import N6705C
from .transport import Transport

__all__ = [
    "CHANNELS_PER_FRAME",
    "MAX_FRAMES",
    "TRIGGER_SKEW_SECONDS",
    "GlobalChannel",
    "InstrumentGroup",
]

log = logging.getLogger(__name__)

#: An N6705C holds four modules.
CHANNELS_PER_FRAME = 4
#: What the app supports; the limit is the operator's patience, not the SCPI.
MAX_FRAMES = 4

#: Worst-case spread between the first and last frame when each is triggered
#: by its own command over LAN. Measured in round trips, not in anything the
#: instruments guarantee -- it is an honest estimate for a warning, not a spec.
TRIGGER_SKEW_SECONDS = 2e-3


@dataclass(frozen=True, order=True)
class GlobalChannel:
    """One channel, said in a way that survives having four mainframes."""

    frame: int  # 1-based
    channel: int  # 1-based within its frame

    @property
    def index(self) -> int:
        """Flat 1-based number, which is what the data layer stores."""
        return (self.frame - 1) * CHANNELS_PER_FRAME + self.channel

    @classmethod
    def from_index(cls, index: int) -> "GlobalChannel":
        if index < 1:
            raise ValueError(f"channel index {index} must be 1 or more")
        frame, channel = divmod(index - 1, CHANNELS_PER_FRAME)
        return cls(frame + 1, channel + 1)

    def label(self, *, multi_frame: bool = True) -> str:
        """How to write it on screen.

        With one frame the frame number is noise; with several, leaving it out
        would make two different channels look like the same one.
        """
        return f"F{self.frame}:CH{self.channel}" if multi_frame else f"CH{self.channel}"

    def __str__(self) -> str:
        return self.label()


class InstrumentGroup:
    """One to four mainframes, addressed by flat channel index."""

    def __init__(self, instruments: list[N6705C]) -> None:
        if not instruments:
            raise ValueError("a group needs at least one instrument")
        if len(instruments) > MAX_FRAMES:
            raise ValueError(
                f"{len(instruments)} mainframes is more than the {MAX_FRAMES} "
                "this app manages"
            )
        self.instruments = instruments
        self.capabilities: dict[int, ChannelCapability] = {}
        self._rebuild_capabilities()

    # -- Construction -------------------------------------------------------

    @classmethod
    def connect(
        cls,
        resources: list[str],
        *,
        backend: str = "auto",
        timeout_ms: int = 10_000,
    ) -> "InstrumentGroup":
        return cls(
            [
                N6705C.connect(resource, backend=backend, timeout_ms=timeout_ms)
                for resource in resources
            ]
        )

    @classmethod
    def demo(cls, frames: int = 2, slots=None) -> "InstrumentGroup":
        """Several independent simulators -- which is exactly what four
        unwired mainframes are."""
        return cls([N6705C.demo(slots) for _ in range(frames)])

    @classmethod
    def from_transports(cls, transports: list[Transport]) -> "InstrumentGroup":
        return cls([N6705C.from_transport(t) for t in transports])

    @classmethod
    def of(cls, instrument: N6705C) -> "InstrumentGroup":
        """Wrap a single mainframe, so one code path serves both cases."""
        return cls([instrument])

    # -- Identity -----------------------------------------------------------

    @property
    def frame_count(self) -> int:
        return len(self.instruments)

    @property
    def is_multi_frame(self) -> bool:
        return self.frame_count > 1

    @property
    def trigger_skew(self) -> float:
        """How far apart the frames can start, with no hardware trigger line.

        Zero for a single frame: one ``*TRG`` releases everything in it at
        once, which is the whole basis of the integrated test.
        """
        return 0.0 if self.frame_count == 1 else TRIGGER_SKEW_SECONDS

    @property
    def synchronised(self) -> bool:
        return self.frame_count == 1

    def summary(self) -> str:
        return "\n".join(
            f"F{index}: {instrument.summary().splitlines()[0]}"
            for index, instrument in enumerate(self.instruments, start=1)
        )

    # -- Channel views ------------------------------------------------------

    def _rebuild_capabilities(self) -> None:
        self.capabilities = {}
        for frame, instrument in enumerate(self.instruments, start=1):
            for channel, capability in instrument.capabilities.items():
                self.capabilities[GlobalChannel(frame, channel).index] = capability

    def probe(self) -> None:
        for instrument in self.instruments:
            instrument.probe()
        self._rebuild_capabilities()

    @property
    def channel_numbers(self) -> list[int]:
        return sorted(self.capabilities)

    @property
    def populated_channels(self) -> list[int]:
        return [n for n in self.channel_numbers if self.capabilities[n].present]

    def label(self, index: int) -> str:
        return GlobalChannel.from_index(index).label(multi_frame=self.is_multi_frame)

    # -- Routing ------------------------------------------------------------

    def instrument_for(self, index: int) -> tuple[N6705C, int]:
        """The mainframe that owns *index*, and the channel number it knows."""
        target = GlobalChannel.from_index(index)
        if not 1 <= target.frame <= self.frame_count:
            raise ValueError(
                f"channel {index} is on frame {target.frame}, but only "
                f"{self.frame_count} are connected"
            )
        return self.instruments[target.frame - 1], target.channel

    def route(self, indices: list[int]) -> list[tuple[N6705C, list[int]]]:
        """Split a flat channel list into one chanlist per mainframe.

        Frames come back in order so that a command log reads the same way
        twice, which matters when the ordering is the thing under suspicion.
        """
        grouped: dict[int, list[int]] = {}
        for index in indices:
            target = GlobalChannel.from_index(index)
            if not 1 <= target.frame <= self.frame_count:
                raise ValueError(
                    f"channel {index} is on frame {target.frame}, but only "
                    f"{self.frame_count} are connected"
                )
            grouped.setdefault(target.frame, []).append(target.channel)
        return [
            (self.instruments[frame - 1], sorted(channels))
            for frame, channels in sorted(grouped.items())
        ]

    def globals_for(self, frame: int, channels: list[int]) -> list[int]:
        return [GlobalChannel(frame, c).index for c in channels]

    # -- Control ------------------------------------------------------------

    def set_output(self, indices: list[int], on: bool) -> None:
        for instrument, channels in self.route(indices):
            instrument.set_output(channels, on)

    def output_state(self, indices: list[int]) -> dict[int, bool]:
        return self._gather(indices, N6705C.output_state)

    def measure_voltage(self, indices: list[int]) -> dict[int, float]:
        return self._gather(indices, N6705C.measure_voltage)

    def measure_current(self, indices: list[int]) -> dict[int, float]:
        return self._gather(indices, N6705C.measure_current)

    def operation_status(self, indices: list[int]) -> dict[int, object]:
        return self._gather(indices, N6705C.operation_status)

    def questionable_status(self, indices: list[int]) -> dict[int, object]:
        return self._gather(indices, N6705C.questionable_status)

    def _gather(self, indices: list[int], method) -> dict:
        """Run a per-frame query and put the answers back on flat indices."""
        result: dict[int, object] = {}
        for instrument, channels in self.route(indices):
            frame = self.instruments.index(instrument) + 1
            for channel, value in method(instrument, channels).items():
                result[GlobalChannel(frame, channel).index] = value
        return result

    def all_outputs_off(self) -> None:
        """Emergency stop on every frame. Never raises.

        Every frame is attempted even if an earlier one fails: a connection
        that has dropped must not leave the others energised.
        """
        for instrument in self.instruments:
            try:
                instrument.all_outputs_off()
            except Exception:
                log.exception("emergency stop failed on one mainframe")

    def close(self) -> None:
        for instrument in self.instruments:
            try:
                instrument.close()
            except Exception:
                log.exception("closing a mainframe failed")

    def __enter__(self) -> "InstrumentGroup":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.all_outputs_off()
        self.close()
