"""Running a sequencing table across mainframes.

Everything that can be checked is checked before any output is switched on:
the frame count, and (when the runners are built) every level and time against
the module that will play it.  After that point any failure forces all outputs
off, because a rail left up half-sequenced is the case this tool exists to avoid.
"""

from __future__ import annotations

import threading
import time

from n6705c.core.arb import ArbState, GroupArbRunner, MultiArbRunner
from n6705c.core.group import GlobalChannel, InstrumentGroup
from n6705c.core.scpi import format_channel_list

from .plan import SeqRow, to_functions

__all__ = ["DEMO_SLOTS", "fire_together", "run_plan"]

#: The simulator configured like the real frames (four N6762A modules).
DEMO_SLOTS = ("N6762A",) * 4

#: How long past the programmed end a channel may still report running.
GRACE_SECONDS = 2.0


def fire_together(runners: list[MultiArbRunner]) -> None:
    """Send each frame's ``*TRG`` from its own thread, released by one barrier.

    # ponytail: host-side threads shrink the gap between frames but cannot close
    # it (sub-ms to ms of link jitter remains); wiring the digital ports is the fix.
    """
    barrier = threading.Barrier(len(runners))
    errors: list[BaseException] = []

    def fire(runner: MultiArbRunner) -> None:
        barrier.wait()
        try:
            runner.trigger()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=fire, args=(r,)) for r in runners]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]


def _apply_levels(group: InstrumentGroup, functions) -> None:
    """DC setpoint = the low level; the current limit only if the table gave one."""
    for fn in functions:
        instrument, local = group.instrument_for(fn.channel)
        chan = format_channel_list(local)
        instrument.session.write_checked(f"VOLT {fn.voltage:.6G},{chan}")
        if fn.current:
            instrument.session.write_checked(f"CURR {fn.current:.6G},{chan}")
    group.set_output([fn.channel for fn in functions], True)


def _wait(runner: GroupArbRunner) -> None:
    time.sleep(runner.total_duration)
    deadline = time.monotonic() + GRACE_SECONDS
    while runner.running_channels:
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"channels {runner.running_channels} still running {GRACE_SECONDS:g} s "
                "after the sequence should have ended"
            )
        time.sleep(0.05)


def run_plan(group: InstrumentGroup, rows: list[SeqRow], *, wait: bool = True) -> GroupArbRunner:
    """Run the table. With ``wait`` the outputs are switched off again at the end."""
    functions = to_functions(rows)
    if not functions:
        raise ValueError("no enabled channels in the table")
    needed = max(GlobalChannel.from_index(fn.channel).frame for fn in functions)
    if needed > group.frame_count:
        raise ValueError(
            f"the table uses frame {needed} but only {group.frame_count} "
            f"mainframe(s) are connected"
        )

    runner = GroupArbRunner.from_plan(group, functions)  # validates against each module
    try:
        _apply_levels(group, functions)
        runner.load()
        runner.arm()  # every frame, or none
        fire_together(runner.runners)
        runner.state = ArbState.RUNNING
        if wait:
            _wait(runner)
            group.all_outputs_off()
    except BaseException:
        group.all_outputs_off()
        raise
    return runner
