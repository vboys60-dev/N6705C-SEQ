"""Multi-channel rollback using real runners and a local fault transport.

The transport models partial I/O only; it is not ARB trigger hardware evidence.
"""

import pytest

from n6705c.core.arb.models import ArbSpec, RampArb
from n6705c.core.arb.multi import MultiArbRunner
from n6705c.core.arb.runner import ArbState
from n6705c.core.arb.sequence import ArbSequence, SequenceStep
from n6705c.core.capability import ChannelCapability
from n6705c.core.instrument import N6705C
from n6705c.core.session import Session


class ArmFaultTransport:
    description = "local multi-ARB rollback test"

    def __init__(self, *, fail_channel=None, fail_at="init", abort_fail_channel=None):
        self.fail_channel = fail_channel
        self.fail_at = fail_at
        self.abort_fail_channel = abort_fail_channel
        self.failure = RuntimeError("injected arm failure")
        self.commands = []
        self.active = set()
        self.pending_failure = False

    def write(self, command):
        self.commands.append(command)
        if command == "*TRG":
            return
        channel = int(command.split("(@")[1].removesuffix(")"))
        if command.startswith("TRIG:TRAN:SOUR BUS,"):
            return
        if command.startswith("INIT:TRAN "):
            # The device may accept INIT before a write or error check fails.
            self.active.add(channel)
            if channel == self.fail_channel:
                if self.fail_at == "init":
                    raise self.failure
                self.pending_failure = True
            return
        if command.startswith("ABOR:TRAN "):
            if channel == self.abort_fail_channel:
                raise RuntimeError("injected abort failure")
            self.active.discard(channel)
            return
        raise AssertionError(f"unexpected write: {command}")

    def query(self, command):
        assert command == "SYST:ERR?"
        if self.pending_failure:
            self.pending_failure = False
            raise self.failure
        return '0,"No error"'


def make_multi(kind="arb", **faults):
    transport = ArmFaultTransport(**faults)
    instrument = N6705C(Session(transport))
    instrument.capabilities = {
        ch: ChannelCapability(ch, model="N6752A", voltage_max=50.0)
        for ch in (1, 2, 3)
    }
    if kind == "arb":
        specs = [ArbSpec(ch, RampArb()) for ch in (1, 2, 3)]
    else:
        specs = [ArbSequence(ch, [SequenceStep(RampArb())]) for ch in (1, 2, 3)]
    multi = MultiArbRunner.from_specs(instrument, specs)
    # Isolate arm/rollback from waveform downloading.
    multi.state = ArbState.LOADED
    for runner in multi.runners:
        runner.state = ArbState.LOADED
    return multi, transport


@pytest.mark.parametrize("kind", ["arb", "sequence"])
@pytest.mark.parametrize("fail_channel", [1, 2, 3])
@pytest.mark.parametrize("fail_at", ["init", "error_check"])
def test_arm_failure_aborts_every_attempted_channel(kind, fail_channel, fail_at):
    multi, transport = make_multi(kind, fail_channel=fail_channel, fail_at=fail_at)

    with pytest.raises(RuntimeError) as raised:
        multi.arm()

    assert raised.value is transport.failure
    assert multi.state is ArbState.IDLE
    assert not transport.active
    assert [cmd for cmd in transport.commands if cmd.startswith("ABOR:")] == [
        f"ABOR:TRAN (@{ch})" for ch in range(1, fail_channel + 1)
    ]
    assert [runner.state for runner in multi.runners] == [
        ArbState.DONE if ch <= fail_channel else ArbState.LOADED
        for ch in (1, 2, 3)
    ]
    assert "*TRG" not in transport.commands


@pytest.mark.parametrize("kind", ["arb", "sequence"])
@pytest.mark.parametrize("abort_fail_channel", [1, 2, 3])
def test_abort_failure_does_not_stop_rollback_or_hide_arm_failure(kind, abort_fail_channel):
    multi, transport = make_multi(
        kind, fail_channel=3, fail_at="error_check", abort_fail_channel=abort_fail_channel
    )

    with pytest.raises(RuntimeError) as raised:
        multi.arm()

    assert raised.value is transport.failure
    assert multi.state is ArbState.IDLE
    assert [cmd for cmd in transport.commands if cmd.startswith("ABOR:")] == [
        f"ABOR:TRAN (@{ch})" for ch in (1, 2, 3)
    ]
    assert transport.active == {abort_fail_channel}


@pytest.mark.parametrize("kind", ["arb", "sequence"])
def test_successful_arm_preserves_legacy_trigger_and_state_transitions(kind):
    multi, transport = make_multi(kind)

    multi.arm()

    assert multi.state is ArbState.ARMED
    assert all(runner.state is ArbState.ARMED for runner in multi.runners)
    assert transport.commands == [
        command
        for ch in (1, 2, 3)
        for command in (f"TRIG:TRAN:SOUR BUS,(@{ch})", f"INIT:TRAN (@{ch})")
    ]

    multi.trigger()

    assert transport.commands[-1] == "*TRG"
    assert transport.commands.count("*TRG") == 1
    assert multi.state is ArbState.RUNNING
    assert all(runner.state is ArbState.RUNNING for runner in multi.runners)


def test_empty_multi_arm_and_trigger_do_no_io():
    transport = ArmFaultTransport()
    multi = MultiArbRunner(N6705C(Session(transport)))

    multi.arm()
    multi.trigger()

    assert multi.state is ArbState.IDLE
    assert transport.commands == []
