"""Emergency stop aborts acquisition and attempts every command independently."""

import pytest

from n6705c.core.fake import FakeN6705C
from n6705c.core.instrument import N6705C


def test_all_outputs_off_aborts_acquisition():
    fake = FakeN6705C()
    instrument = N6705C.from_transport(fake)
    for channel in fake.channels.values():
        channel.acq_initiated = True
        channel.output_on = True

    instrument.all_outputs_off()

    assert all(not channel.acq_initiated for channel in fake.channels.values())
    assert all(not channel.output_on for channel in fake.channels.values())


@pytest.mark.parametrize("failed_command", ["ABOR:TRAN", "ABOR:ACQ", "ABOR:ELOG", "OUTP OFF", "all"])
def test_all_outputs_off_continues_after_write_failure(monkeypatch, failed_command):
    fake = FakeN6705C()
    instrument = N6705C.from_transport(fake)
    commands = []

    def write(command):
        commands.append(command)
        if failed_command == "all" or command.startswith(failed_command):
            raise OSError("injected stop failure")

    monkeypatch.setattr(fake, "write", write)

    instrument.all_outputs_off()

    assert commands == [
        "ABOR:TRAN (@1,2,3,4)",
        "ABOR:ACQ (@1,2,3,4)",
        "ABOR:ELOG (@1,2,3,4)",
        "OUTP OFF,(@1,2,3,4)",
    ]
