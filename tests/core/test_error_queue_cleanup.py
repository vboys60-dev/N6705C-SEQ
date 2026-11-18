"""Best-effort ARB queries tolerate failures while draining the error queue."""

import pytest

from n6705c.core.arb.models import ArbSpec, RampArb
from n6705c.core.arb.runner import ArbRunner, ArbState, SequenceRunner
from n6705c.core.arb.sequence import ArbSequence, SequenceStep
from n6705c.core.fake import FakeN6705C
from n6705c.core.instrument import N6705C
from n6705c.core.session import ErrorQueueError


@pytest.fixture
def instrument():
    inst = N6705C.from_transport(FakeN6705C())
    yield inst
    inst.close()


def test_readback_continues_when_error_queue_fails(instrument, monkeypatch, caplog):
    runner = ArbRunner(instrument, ArbSpec(1, RampArb()))
    suffixes = list(runner.spec.waveform.scpi_suffixes.items())
    failed_command = f"ARB:VOLT:{suffixes[0][1]}? (@1)"
    queries = []
    drains = []

    def query_float(command):
        queries.append(command)
        if command == failed_command:
            raise ValueError("readback failed")
        return 1.25

    def read_errors():
        drains.append(True)
        raise ErrorQueueError("malformed error queue", errors=[], response="garbled")

    monkeypatch.setattr(instrument.session, "query_float", query_float)
    monkeypatch.setattr(instrument.session, "read_errors", read_errors)
    caplog.set_level("DEBUG", logger="n6705c.core.arb.runner")

    assert runner.readback() == {name: 1.25 for name, _ in suffixes[1:]}
    assert queries == [f"ARB:VOLT:{suffix}? (@1)" for _, suffix in suffixes]
    assert drains == [True]
    assert "readback failed" in caplog.text
    assert "malformed error queue" in caplog.text


def test_sequence_load_completes_when_quality_error_queue_fails(instrument, monkeypatch, caplog):
    runner = SequenceRunner(instrument, ArbSequence(1, [SequenceStep(RampArb())]))
    runner.quality = 100
    drains = []

    def query_int(command):
        assert command == "ARB:SEQ:QUAL? (@1)"

        def read_errors():
            drains.append(True)
            raise ErrorQueueError("malformed error queue", errors=[], response="garbled")

        monkeypatch.setattr(instrument.session, "read_errors", read_errors)
        raise ValueError("quality query failed")

    monkeypatch.setattr(instrument.session, "query_int", query_int)
    caplog.set_level("DEBUG", logger="n6705c.core.arb.runner")

    runner.load()

    assert runner.state is ArbState.LOADED
    assert runner.quality is None
    assert drains == [True]
    assert "quality query failed" in caplog.text
    assert "malformed error queue" in caplog.text
