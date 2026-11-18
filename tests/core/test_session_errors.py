"""Strict, bounded SCPI error-queue verification through the real Session."""

from unittest.mock import Mock

import pytest

from n6705c.core.session import ErrorQueueError, InstrumentError, ScpiError, Session


@pytest.mark.parametrize("response", [
    "garbage", "", "+0", '0,"unterminated', '+0,"No error",extra',
    '+0,""', '+0,"   "', '0.0,"No error"', '1e0,"bad"', '1_0,"bad"',
])
def test_write_checked_rejects_malformed_queue_response(response):
    transport = Mock()
    transport.query.return_value = response
    with pytest.raises(ErrorQueueError) as caught:
        Session(transport).write_checked("INIT:ELOG (@1)")
    assert isinstance(caught.value, InstrumentError)
    assert repr(response) in str(caught.value)
    transport.write.assert_called_once_with("INIT:ELOG (@1)")
    transport.query.assert_called_once_with("SYST:ERR?")


def test_write_checked_accepts_zero_terminator():
    transport = Mock()
    transport.query.return_value = '+0,"No error"'
    Session(transport).write_checked("FORM REAL")
    transport.query.assert_called_once_with("SYST:ERR?")


def test_stuck_queue_raises_after_exactly_fifty_reads():
    transport = Mock()
    transport.query.return_value = '-200,"stuck"'
    with pytest.raises(ErrorQueueError, match="50 reads") as caught:
        Session(transport).write_checked("FORM REAL")
    assert transport.query.call_count == 50
    assert all(call.args == ("SYST:ERR?",) for call in transport.query.call_args_list)
    assert len(caught.value.errors) == 50
    assert repr(transport.query.return_value) in str(caught.value)


def test_zero_on_fiftieth_read_returns_all_prior_errors():
    transport = Mock()
    transport.query.side_effect = ['-200,"bad, ""quoted"" message"'] * 49 + ['+0,"No error"']
    errors = Session(transport).read_errors()
    assert transport.query.call_count == 50
    assert len(errors) == 49
    assert all(error.code == -200 and error.message == 'bad, "quoted" message' for error in errors)


def test_write_checked_preserves_scpi_context():
    transport = Mock()
    transport.query.side_effect = ['+310, "Unsupported"', '+0,"No error"']
    with pytest.raises(ScpiError) as caught:
        Session(transport).write_checked("FORM REAL")
    assert (caught.value.code, caught.value.message, caught.value.command) == (
        310, "Unsupported", "FORM REAL")


def test_transport_exception_propagates_unchanged():
    transport = Mock()
    original = TimeoutError("error query timed out")
    transport.query.side_effect = original
    with pytest.raises(TimeoutError) as caught:
        Session(transport).read_errors()
    assert caught.value is original
