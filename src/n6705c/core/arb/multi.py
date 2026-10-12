"""Running several channels' waveforms from one trigger.

Arming each channel and triggering it separately leaves an unknown gap between
them -- one network round trip each, 0.2 to 2 ms and jittering.  That is larger
than most of the offsets the function page exists to set up, so the staggering
someone carefully typed in would be swamped by the way it was started.

So every channel is armed first and nothing is triggered until all of them are
waiting.  Then one ``*TRG`` releases the lot: the manual's own See Also for it
lists ACQuire, TRANsient, DLOG and ELOG, so a single command starts every armed
BUS system at the same instant.  This is the same mechanism the one-button test
uses (:mod:`...app.testrun`), which is why the two compose: M10.7 arms the
waveforms *and* the recording, then fires once.

Arming is unwound if it fails partway.  A channel left armed after an aborted
setup would sit waiting, and the next ``*TRG`` -- sent for something else
entirely -- would run a waveform nobody asked for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

from ..group import TRIGGER_SKEW_SECONDS, GlobalChannel, InstrumentGroup
from ..instrument import N6705C
from .delay import delay_spec
from .models import ArbLimits, ArbSpec
from .runner import ArbRunner, ArbState, SequenceRunner
from .sequence import ArbSequence

__all__ = ["MultiArbRunner", "GroupArbRunner"]

log = logging.getLogger(__name__)

Runner = ArbRunner | SequenceRunner


def _make_runner(instrument: N6705C, spec: ArbSpec | ArbSequence) -> Runner:
    """A sequence needs its own runner; a delayed sine becomes one."""
    forced = replace(spec, trigger_source="BUS")
    if isinstance(forced, ArbSequence):
        return SequenceRunner(instrument, forced)
    return ArbRunner(instrument, forced)


@dataclass
class MultiArbRunner:
    """Several channels, armed together and released by one trigger."""

    instrument: N6705C
    runners: list[Runner] = field(default_factory=list)
    state: ArbState = ArbState.IDLE

    # -- Construction -------------------------------------------------------

    @classmethod
    def from_specs(
        cls,
        instrument: N6705C,
        specs: list[ArbSpec | ArbSequence],
    ) -> "MultiArbRunner":
        return cls(instrument, [_make_runner(instrument, s) for s in specs])

    @classmethod
    def from_plan(
        cls,
        instrument: N6705C,
        plan,
        limits: dict[int, ArbLimits] | None = None,
    ) -> "MultiArbRunner":
        """Build from a :class:`~...core.function.FunctionPlan`.

        Each channel's delay is folded into its waveform here rather than
        anywhere else, so what gets downloaded is what the function page's
        overlay drew.
        """
        specs: list[ArbSpec | ArbSequence] = []
        for entry in plan:
            if not entry.arb_enabled or entry.spec is None:
                continue
            spec = entry.spec
            if entry.delay:
                spec = delay_spec(
                    spec, entry.delay, (limits or {}).get(entry.channel)
                ).spec
            specs.append(spec)
        return cls.from_specs(instrument, specs)

    # -- Views --------------------------------------------------------------

    @property
    def channels(self) -> list[int]:
        return [_channel_of(r) for r in self.runners]

    @property
    def is_empty(self) -> bool:
        return not self.runners

    @property
    def total_duration(self) -> float:
        """Until the last channel has finished; the slowest one decides."""
        return max(
            (
                (r.spec if isinstance(r, ArbRunner) else r.sequence).total_duration
                for r in self.runners
            ),
            default=0.0,
        )

    def describe(self) -> str:
        return ", ".join(
            f"CH{_channel_of(r)} "
            + (
                r.spec.waveform.describe()
                if isinstance(r, ArbRunner)
                else r.sequence.describe()
            )
            for r in self.runners
        )

    # -- Run control --------------------------------------------------------

    def load(self) -> None:
        for runner in self.runners:
            runner.load()
        self.state = ArbState.LOADED if self.runners else ArbState.IDLE

    def arm(self) -> None:
        """Arm every channel, or none of them.

        A channel left armed after a failure would fire on the next ``*TRG``,
        which may have been sent for something else entirely.
        """
        armed: list[Runner] = []
        try:
            for runner in self.runners:
                runner.arm()
                armed.append(runner)
        except Exception:
            log.exception("arming failed; standing down the channels already armed")
            for runner in armed:
                try:
                    runner.abort()
                except Exception:
                    log.exception("standing down CH%d failed", _channel_of(runner))
            self.state = ArbState.IDLE
            raise
        self.state = ArbState.ARMED if self.runners else ArbState.IDLE

    def trigger(self) -> None:
        """One ``*TRG`` for every armed channel, so they share a start instant."""
        if self.state is not ArbState.ARMED:
            self.arm()
        if not self.runners:
            return
        session = self.instrument.session
        session.write("*TRG")
        session.check_errors("*TRG")
        self.mark_triggered()

    def mark_triggered(self) -> None:
        """Record that the trigger has happened, without sending one.

        For the integrated test, where the same ``*TRG`` also releases the
        recording and is therefore sent by whoever owns both.
        """
        for runner in self.runners:
            runner.state = ArbState.RUNNING
        self.state = ArbState.RUNNING

    def run(self) -> None:
        self.load()
        self.arm()
        self.trigger()

    def abort(self) -> None:
        """Stop every channel. Never raises: it has to work when things are wrong."""
        for runner in self.runners:
            try:
                runner.abort()
            except Exception:
                log.exception("aborting CH%d failed", _channel_of(runner))
        self.state = ArbState.DONE

    # -- Progress -----------------------------------------------------------

    @property
    def running_channels(self) -> list[int]:
        running = []
        for runner in self.runners:
            try:
                if runner.is_running:
                    running.append(_channel_of(runner))
            except Exception as exc:
                log.debug("status poll for CH%d failed: %s", _channel_of(runner), exc)
        return running

    @property
    def is_running(self) -> bool:
        return bool(self.running_channels)


def _channel_of(runner: Runner) -> int:
    return (
        runner.spec.channel
        if isinstance(runner, ArbRunner)
        else runner.sequence.channel
    )


@dataclass
class GroupArbRunner:
    """The same thing across several mainframes -- with one honest difference.

    Within a frame, one ``*TRG`` releases every armed channel at the same
    instant.  Across frames there is no such command: each frame gets its own,
    which is a separate LAN round trip, so the frames start up to
    :data:`~..group.TRIGGER_SKEW_SECONDS` apart.

    That gap is not hidden behind a tidy interface.  :attr:`skew_seconds` says
    how far apart the frames can be, and it is non-zero exactly when more than
    one frame is involved.  Closing it needs the digital ports wired together
    (M11.3), not more software.
    """

    group: InstrumentGroup
    runners: list[MultiArbRunner] = field(default_factory=list)
    state: ArbState = ArbState.IDLE

    @classmethod
    def from_plan(
        cls,
        group: InstrumentGroup,
        plan,
        limits: dict[int, ArbLimits] | None = None,
    ) -> "GroupArbRunner":
        """Split a function plan by mainframe, renumbering to local channels."""
        per_frame: dict[int, list[ArbSpec | ArbSequence]] = {}
        for entry in plan:
            if not entry.arb_enabled or entry.spec is None:
                continue
            target = GlobalChannel.from_index(entry.channel)
            spec = entry.spec
            if entry.delay:
                spec = delay_spec(
                    spec, entry.delay, (limits or {}).get(entry.channel)
                ).spec
            # The frame knows the channel by its own number, not the flat one.
            per_frame.setdefault(target.frame, []).append(
                replace(spec, channel=target.channel)
            )
        runners = [
            MultiArbRunner.from_specs(group.instruments[frame - 1], specs)
            for frame, specs in sorted(per_frame.items())
        ]
        return cls(group=group, runners=runners)

    @property
    def frames(self) -> int:
        return len(self.runners)

    @property
    def is_empty(self) -> bool:
        return not self.runners or all(r.is_empty for r in self.runners)

    @property
    def skew_seconds(self) -> float:
        """How far apart the frames can start. Zero within a single frame."""
        return 0.0 if self.frames <= 1 else TRIGGER_SKEW_SECONDS

    @property
    def synchronised(self) -> bool:
        return self.frames <= 1

    @property
    def total_duration(self) -> float:
        return max((r.total_duration for r in self.runners), default=0.0)

    def describe(self) -> str:
        return ", ".join(r.describe() for r in self.runners if not r.is_empty)

    def load(self) -> None:
        for runner in self.runners:
            runner.load()
        self.state = ArbState.LOADED

    def arm(self) -> None:
        """Arm every frame, or none of them."""
        armed: list[MultiArbRunner] = []
        try:
            for runner in self.runners:
                runner.arm()
                armed.append(runner)
        except Exception:
            log.exception("arming failed; standing down the frames already armed")
            for runner in armed:
                runner.abort()
            self.state = ArbState.IDLE
            raise
        self.state = ArbState.ARMED

    def trigger(self) -> None:
        """One ``*TRG`` per frame -- which is where the skew comes from."""
        if self.state is not ArbState.ARMED:
            self.arm()
        for runner in self.runners:
            runner.trigger()
        self.state = ArbState.RUNNING

    def run(self) -> None:
        self.load()
        self.arm()
        self.trigger()

    def abort(self) -> None:
        for runner in self.runners:
            runner.abort()
        self.state = ArbState.DONE

    @property
    def running_channels(self) -> list[int]:
        """Flat channel indices, so callers need not know about frames."""
        running: list[int] = []
        for runner in self.runners:
            frame = self.group.instruments.index(runner.instrument) + 1
            running.extend(
                GlobalChannel(frame, channel).index
                for channel in runner.running_channels
            )
        return sorted(running)

    @property
    def is_running(self) -> bool:
        return bool(self.running_channels)
