"""C7 recorder lifecycle and wire faults, injected without changing core.fake."""

from collections import deque
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from n6705c.core import elog
from n6705c.core.instrument import N6705C
from n6705c.core.session import ErrorQueueError, ScpiError


class FaultIO:
    """Wrap the transport; keep the real Session and binary parser in the path."""

    def __init__(self, base):
        self.base = base
        self.actions = deque()
        self.exchanges = []
        self.after_fetch = None
        self.on_write = None
        self.on_query = None

    def __getattr__(self, name):
        return getattr(self.base, name)

    def write(self, command):
        self.exchanges.append((command, threading.get_ident()))
        if self.on_write:
            self.on_write(command)
        return self.base.write(command)

    def query(self, command):
        self.exchanges.append((command, threading.get_ident()))
        if self.on_query:
            response = self.on_query(command)
            if response is not None:
                if isinstance(response, Exception):
                    raise response
                return response
        return self.base.query(command)

    def query_floats(self, command):
        return [float(value) for value in self.query(command).split(",") if value.strip()]

    def query_binary(self, command):
        self.exchanges.append((command, threading.get_ident()))
        action = self.actions.popleft() if self.actions else None
        if isinstance(action, Exception):
            raise action
        if callable(action):
            response = action(command)
        else:
            response = self.base.query_binary(command) if action is None else action
        if self.after_fetch:
            self.after_fetch()
        return response


def block(records):
    payload = np.arange(records * 2, dtype="<f4").tobytes()
    size = str(len(payload)).encode()
    return b"#" + str(len(size)).encode() + size + payload + b"\n"


@pytest.fixture
def rig():
    inst = N6705C.demo(slots=("N6781A",))
    io = FaultIO(inst.session._transport)
    inst.session._transport = io
    session = elog.ElogSession(inst, elog.ElogConfig([1], period=0.01))
    session.configure()
    session.start()
    io.exchanges.clear()
    yield session, io, io.base
    inst.close()


def stopped():
    event = threading.Event()
    event.set()
    return event


def commands(io):
    return [command for command, _ in io.exchanges]


def abor_threads(io):
    return [owner for command, owner in io.exchanges if command.startswith("ABOR:ELOG")]


def record(session, sink=lambda chunk: None, **kwargs):
    return elog.ElogRecorder(session, sink, interval=0.001, **kwargs)


def test_sink_failure_keeps_cause_and_aborts_on_calling_thread_even_if_abort_fails(rig):
    session, io, _ = rig
    io.actions.append(block(1))
    original = OSError("disk full")
    sink_owners = []

    def sink(chunk):
        sink_owners.append(threading.get_ident())
        raise original

    def fail_abort(command):
        if command.startswith("ABOR:ELOG"):
            raise TimeoutError("ABOR timed out")

    io.on_write = fail_abort
    with pytest.raises(elog.ElogSinkError) as caught:
        record(session, sink).run()
    assert caught.value.__cause__ is original
    assert sink_owners == abor_threads(io) == [threading.get_ident()]
    assert len([c for c in commands(io) if c.startswith("FETC:ELOG?")]) == 1
    assert commands(io)[-1] == "SYST:ERR?"
    assert "ABOR timed out" in " ".join(caught.value.__notes__)
    assert session.state is not elog.ElogState.STOPPED  # unconfirmed ABOR stays retryable


def test_three_consecutive_fetch_failures_are_fatal_after_abort(rig, monkeypatch):
    session, io, _ = rig
    # Wall-clock elapsed would change the requested record count on slow runners.
    monkeypatch.setattr(elog.ElogSession, "elapsed", property(lambda self: 0.0))
    failures = [TimeoutError(f"fetch {i}") for i in range(elog.MAX_FETCH_FAILURES)]
    io.actions.extend(failures)
    errors, events = [], []
    with pytest.raises(elog.ElogFetchError) as caught:
        record(session, on_error=errors.append, on_event=lambda *args: events.append(args)).run()
    assert caught.value.__cause__ is failures[-1]
    assert errors == failures[:2]
    assert events == [("fetch_error", 0, 0)] * 2
    assert abor_threads(io) == [threading.get_ident()]
    assert commands(io) == [
        "FETC:ELOG? 1,(@1)", "SYST:ERR?",
        "FETC:ELOG? 1,(@1)", "SYST:ERR?",
        "FETC:ELOG? 1,(@1)", "SYST:ERR?",
        "ABOR:ELOG (@1)", "SYST:ERR?",
    ]


def test_single_error_recovers_and_success_including_zero_resets_failure_count(rig, monkeypatch):
    session, io, _ = rig
    monkeypatch.setattr(elog.ElogSession, "elapsed", property(lambda self: 0.0))
    stop = threading.Event()
    failures = [TimeoutError(str(i)) for i in range(4)]
    io.actions.extend([failures[0], failures[1], block(0),
                       failures[2], failures[3], block(1), block(0)])
    successes = []

    def after_fetch():
        successes.append(True)
        if len(successes) == 2:
            stop.set()

    io.after_fetch = after_fetch
    errors, events, chunks = [], [], []
    stats = record(session, chunks.append, on_error=errors.append,
                   on_event=lambda *args: events.append(args)).run(stop)
    assert errors == failures
    assert events == [("fetch_error", 0, 0)] * 4
    assert [chunk.n_records for chunk in chunks] == [1]
    assert stats is session.stats
    assert stats.records_received == 1
    assert abor_threads(io) == [threading.get_ident()]


def test_empty_backlog_and_new_overflow_evidence_do_not_abort_or_repeat_sticky_flag(rig, monkeypatch):
    session, io, _ = rig
    # Freeze only core's clock: synthetic wire replies model the evidence.
    clock = session._t0 + 11
    monkeypatch.setattr(elog, "time", SimpleNamespace(monotonic=lambda: clock))
    io.actions.extend([block(0), block(0), block(0)])
    events = []
    recorder = record(session, on_event=lambda *args: events.append(args))
    recorder._pump()
    recorder._pump()
    assert [kind for kind, _, _ in events].count("overflow") == 1
    assert [kind for kind, _, _ in events].count("empty_backlog") == 1
    assert all(index == 0 and count > 0 for _, index, count in events)
    assert not abor_threads(io)
    recorder.run(stopped())
    assert abor_threads(io) == [threading.get_ident()]


def test_persistent_200_record_gap_emits_once_until_genuine_backlog_growth(rig, monkeypatch):
    session, io, _ = rig
    session._t0 = 0.0
    period = session.actual_period
    clock = [200.5 * period]
    monkeypatch.setattr(elog, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    events = []
    recorder = elog.ElogRecorder(session, lambda chunk: None, interval=5 * period,
                                 on_event=lambda *args: events.append(args))
    # A discarded buffer leaves the same 200-record accounting gap each time.
    io.actions.extend([block(0)] * 8)
    for _ in range(8):
        recorder._pump()
    assert events == [("empty_backlog", 0, 200)]

    # Receiving real data must not erase the already reported gap baseline.
    clock[0] += period
    io.actions.extend([block(1), block(0)])
    recorder._pump()
    assert events == [("empty_backlog", 0, 200)]
    clock[0] += 10 * period  # exactly two intervals is still tolerated
    io.actions.append(block(0))
    recorder._pump()
    assert events == [("empty_backlog", 0, 200)]
    clock[0] += period
    io.actions.append(block(0))
    recorder._pump()
    assert events == [("empty_backlog", 0, 200), ("empty_backlog", 1, 211)]


def test_overflow_resets_empty_backlog_baseline_to_lost_records(rig, monkeypatch):
    session, io, transport = rig
    session._t0 = transport.channels[1].elog_t0 = 0.0
    period = session.actual_period
    capacity = session.stats.buffer_capacity
    clock = [200.5 * period]
    monkeypatch.setattr(elog, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr("n6705c.core.fake.time", SimpleNamespace(monotonic=lambda: clock[0]))
    events = []
    recorder = record(session, on_event=lambda *args: events.append(args))
    io.actions.append(block(0))
    recorder._pump()
    assert events == [("empty_backlog", 0, 200)]
    clock[0] = (capacity + 400.5) * period
    recorder._pump()  # fake loses 400 records and returns its full buffer
    assert events[-1] == ("overflow", capacity, 400)
    for _ in range(8):
        recorder._pump()
    assert events == [("empty_backlog", 0, 200), ("overflow", capacity, 400)]


def test_successful_large_fetch_does_not_hide_overflow_evidence(rig, monkeypatch):
    session, io, transport = rig
    clock = session._t0 + 11
    monkeypatch.setattr(elog, "time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setattr("n6705c.core.fake.time", SimpleNamespace(monotonic=lambda: clock))
    transport.channels[1].elog_t0 = session._t0
    events = []
    record(session, on_event=lambda *args: events.append(args)).run(stopped())
    overflow = [event for event in events if event[0] == "overflow"]
    assert len(overflow) == 1
    assert overflow[0][2] > 0
    assert session.stats.overflow_detected
    assert session.stats.records_received == session.stats.buffer_capacity


@pytest.mark.parametrize("lost", [950, 1500])
def test_overflow_gap_does_not_repeat_with_alternating_backlog(rig, monkeypatch, lost):
    session, io, transport = rig
    session._t0 = transport.channels[1].elog_t0 = 0.0
    period = session.actual_period
    capacity = session.stats.buffer_capacity
    assert capacity == 1000
    clock = [(capacity + lost + 0.5) * period]
    monkeypatch.setattr(elog, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr("n6705c.core.fake.time", SimpleNamespace(monotonic=lambda: clock[0]))
    events, chunks = [], []
    recorder = elog.ElogRecorder(session, chunks.append, interval=5 * period,
                                 on_event=lambda *args: events.append(args))
    # A transient empty reply exposes both kinds of gap evidence. The next
    # pump drains the fake's full buffer, leaving only the permanent loss.
    io.actions.append(block(0))
    recorder._pump()
    expected = [("overflow", 0, lost), ("empty_backlog", 0, capacity + lost)]
    assert events == expected
    recorder._pump()
    assert session.stats.records_received == capacity
    assert session.stats.backlog == lost

    received = capacity
    for backlog in [60, 65] * 3:
        # In the 950-record case loss is below capacity, but loss + the
        # current unread buffer exceeds it. Neither means another loss.
        assert lost + backlog > capacity
        clock[0] += backlog * period
        recorder._pump()  # real records, followed by an empty fetch
        recorder._pump()  # another empty observation of the same gap
        received += backlog
        assert events == expected
        assert session.stats.backlog == lost
        assert session.stats.records_received == sum(c.n_records for c in chunks) == received
    assert recorder._empty_backlog_baseline == capacity + lost


def test_second_overflow_reports_only_new_loss_and_resets_empty_baseline(rig, monkeypatch):
    session, _, transport = rig
    session._t0 = transport.channels[1].elog_t0 = 0.0
    period = session.actual_period
    capacity = session.stats.buffer_capacity
    clock = [(capacity + 400.5) * period]
    monkeypatch.setattr(elog, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr("n6705c.core.fake.time", SimpleNamespace(monotonic=lambda: clock[0]))
    events, chunks = [], []
    recorder = elog.ElogRecorder(session, chunks.append, interval=5 * period,
                                 on_event=lambda *args: events.append(args))
    recorder._pump()
    assert events == [("overflow", capacity, 400)]
    assert recorder._empty_backlog_baseline == 400
    assert session.stats.records_received == sum(c.n_records for c in chunks) == capacity

    # Repeated empty reads must preserve the 400 records already lost.
    for _ in range(6):
        recorder._pump()
    clock[0] += (capacity + 300) * period
    recorder._pump()
    for _ in range(6):
        recorder._pump()
    assert events == [("overflow", capacity, 400), ("overflow", 2 * capacity, 300)]
    assert recorder._empty_backlog_baseline == session.stats.backlog == 700
    assert session.stats.records_received == sum(c.n_records for c in chunks) == 2 * capacity


@pytest.mark.parametrize("tail", ["drain_then_abort", "abort_then_drain"])
def test_tail_policy_order_full_fake_records_and_same_thread(rig, monkeypatch, tail):
    session, io, transport = rig
    clock = session._t0 + 0.2
    monkeypatch.setattr(elog, "time", SimpleNamespace(monotonic=lambda: clock))
    # Make fake acquisition use exactly the same deterministic instant.
    monkeypatch.setattr("n6705c.core.fake.time", SimpleNamespace(monotonic=lambda: clock))
    transport.channels[1].elog_t0 = session._t0
    produced = int(0.2 / session.actual_period)
    if tail == "abort_then_drain":
        # The shipped fake discards its buffer on ABOR. Retention is a local
        # wrapper scenario, not a claim about fake or real hardware semantics.
        def retain(command):
            if command.startswith("ABOR:ELOG"):
                raw = io.base.query_binary(f"FETC:ELOG? 16384,{session.chanlist}")
                io.actions.extend([raw, block(0)])
        io.on_write = retain
    chunks, events = [], []
    io.exchanges.clear()
    stats = record(session, chunks.append, tail=tail,
                   on_event=lambda *args: events.append(args)).run(stopped())
    assert sum(chunk.n_records for chunk in chunks) == stats.records_received == produced
    assert [chunk.start_index for chunk in chunks] == [0]
    wire = commands(io)
    fetch_at = next(i for i, command in enumerate(wire) if command.startswith("FETC:ELOG?"))
    abort_at = wire.index("ABOR:ELOG (@1)")
    assert (fetch_at < abort_at) == (tail == "drain_then_abort")
    assert {owner for _, owner in io.exchanges} == {threading.get_ident()}
    assert session.state is elog.ElogState.STOPPED
    assert events == []
    assert not any("tail incomplete" in note for note in session.notes)
    with pytest.raises(RuntimeError, match="cannot fetch"):
        session.fetch()


@pytest.mark.parametrize("retain", [False, True])
def test_abort_buffer_wrapper_reports_incomplete_tail_only_when_discarded(rig, monkeypatch, retain):
    session, io, transport = rig
    session.stop()
    session.config.period = 0.001
    session.configure()
    session.start()
    session._t0 = transport.channels[1].elog_t0 = 0.0
    period = session.actual_period
    clock = [509.5 * period]
    monkeypatch.setattr(elog, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr("n6705c.core.fake.time", SimpleNamespace(monotonic=lambda: clock[0]))
    chunks, events = [], []
    recorder = elog.ElogRecorder(session, chunks.append, interval=0.1,
                                 tail="abort_then_drain",
                                 on_event=lambda *args: events.append(args))
    recorder._pump()
    assert session.stats.records_received == 509
    clock[0] = 728.5 * period  # 219 unread records, about 0.7 s total

    def abort_buffer(command):
        if command.startswith("ABOR:ELOG"):
            # Capture/discard the same unread buffer through the wire wrapper;
            # only the retaining scenario serves it after the real fake ABOR.
            raw = io.base.query_binary(f"FETC:ELOG? 16384,{session.chanlist}")
            io.actions.extend([raw, block(0)] if retain else [block(0)])

    io.on_write = abort_buffer
    stats = recorder.run(stopped())
    assert stats.records_received == (728 if retain else 509)
    assert sum(chunk.n_records for chunk in chunks) == stats.records_received
    assert events == ([] if retain else [("empty_backlog", 509, 219),
                                         ("tail_incomplete", 509, 219)])
    assert any("tail incomplete" in note and "219" in note
               for note in session.notes) is (not retain)


@pytest.mark.parametrize("tail", ["drain_then_abort", "abort_then_drain"])
@pytest.mark.parametrize("missing", [12, 13])
def test_tail_incomplete_tolerance_includes_abort_delay(rig, monkeypatch, tail, missing):
    session, io, _ = rig
    session._t0 = 0.0
    period = session.actual_period
    clock = [missing * period + period / 2]
    monkeypatch.setattr(elog, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    io.actions.append(block(0))

    def delayed_abort(command):
        if command.startswith("ABOR:ELOG"):
            clock[0] += 2 * period

    io.on_write = delayed_abort
    events = []
    elog.ElogRecorder(session, lambda chunk: None, interval=5 * period, tail=tail,
                      on_event=lambda *args: events.append(args)).run(stopped())
    incomplete = [event for event in events if event[0] == "tail_incomplete"]
    assert incomplete == ([] if missing == 12 else [("tail_incomplete", 0, missing)])
    assert any("tail incomplete" in note for note in session.notes) is (missing == 13)


def test_direct_run_after_stop_keeps_stop_flag_and_only_performs_cleanup(rig):
    session, io, _ = rig
    recorder = record(session)
    assert recorder.stop()
    io.actions.append(block(0))
    cleanup_calls = []

    def cleanup():
        cleanup_calls.append(True)
        assert io.exchanges == []  # no acquisition fetch before cleanup

    recorder.on_cleanup = cleanup
    recorder.run()
    assert cleanup_calls == [True]
    assert recorder._stop.is_set()
    assert session.state is elog.ElogState.STOPPED


def test_stop_timeout_retains_thread_and_rejects_parallel_start_and_run(rig):
    session, io, _ = rig
    entered, release = threading.Event(), threading.Event()

    def blocked(command):
        entered.set()
        assert release.wait(3), "test must release blocked I/O"
        return block(0)

    io.actions.extend([blocked, block(0)])
    recorder = record(session)
    recorder.start()
    try:
        assert entered.wait(2)
        thread = recorder._thread
        assert recorder.stop(timeout=0.001) is False
        assert recorder._thread is thread
        assert recorder.is_running
        assert not abor_threads(io)
        with pytest.raises(RuntimeError, match="already running"):
            recorder.start()
        with pytest.raises(RuntimeError, match="already running"):
            recorder.run()
    finally:
        release.set()
        assert recorder.stop(timeout=2) is True
    assert recorder._thread is None
    assert not recorder.is_running
    assert abor_threads(io) == [thread.ident]
    assert all(owner == thread.ident for _, owner in io.exchanges)


def test_blocking_run_uses_internal_stop_and_caller_manages_worker_lifetime(rig):
    session, io, _ = rig
    entered, release = threading.Event(), threading.Event()
    results = []

    def blocked(command):
        entered.set()
        assert release.wait(3)
        return block(0)

    io.actions.extend([blocked, block(0)])
    recorder = record(session)
    worker = threading.Thread(target=lambda: results.append(recorder.run()))
    worker.start()
    try:
        assert entered.wait(2)
        assert recorder.is_running
        assert recorder._thread is None
        with pytest.raises(RuntimeError, match="already running"):
            recorder.start()
        with pytest.raises(RuntimeError, match="already running"):
            recorder.run()
        assert recorder.stop(0.001) is True  # no recorder-owned thread to join
        assert worker.is_alive()
        assert not abor_threads(io)
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert results == [session.stats]
    assert abor_threads(io) == [worker.ident]


@pytest.mark.parametrize("callback_name", ["on_event", "on_error", "on_cleanup"])
def test_callback_failure_preserves_cause_and_never_skips_abort(rig, callback_name):
    session, io, _ = rig
    original = OSError(f"{callback_name} persistence failed")

    def fail(*args):
        raise original

    io.actions.append(TimeoutError("fetch failed"))
    with pytest.raises(elog.ElogSinkError) as caught:
        record(session, **{callback_name: fail}).run(
            stopped() if callback_name == "on_cleanup" else None)
    assert caught.value.__cause__ is original
    assert abor_threads(io) == [threading.get_ident()]


@pytest.mark.parametrize("tail", ["drain_then_abort", "abort_then_drain"])
def test_cleanup_precedes_all_tail_and_abort_io(rig, tail):
    session, io, _ = rig
    io.actions.append(block(0))
    owners = []

    def cleanup():
        owners.append(threading.get_ident())
        assert io.exchanges == []

    record(session, tail=tail, on_cleanup=cleanup).run(stopped())
    assert owners == abor_threads(io)


@pytest.mark.parametrize("tail", ["drain_then_abort", "abort_then_drain"])
def test_tail_fetches_honor_max_records_and_sixteen_batch_limit(rig, monkeypatch, tail):
    session, io, _ = rig
    clock = session._t0 + 1
    monkeypatch.setattr(elog, "time", SimpleNamespace(monotonic=lambda: clock))
    io.actions.extend([block(1)] * 20)
    events, chunks = [], []
    record(session, chunks.append, tail=tail, max_records=1,
           on_event=lambda *args: events.append(args)).run(stopped())
    fetches = [c for c in commands(io) if c.startswith("FETC:ELOG?")]
    assert len(fetches) == 16
    assert all(c.startswith("FETC:ELOG? 1,") for c in fetches)
    assert len(chunks) == 16
    assert ("tail_limit", 16, session.stats.backlog) in events
    assert events[-1] == ("tail_incomplete", 16, session.stats.backlog)
    assert any("tail limit" in note for note in session.notes)


@pytest.mark.parametrize("tail", ["drain_then_abort", "abort_then_drain"])
def test_tail_checks_monotonic_ten_second_budget_before_each_batch(rig, monkeypatch, tail):
    session, io, _ = rig
    clock = [session._t0 + 1]
    monkeypatch.setattr(elog, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def slow_fetch(command):
        clock[0] += 11
        return block(1)

    io.actions.extend([slow_fetch, block(1)])
    events = []
    record(session, tail=tail, on_event=lambda *args: events.append(args)).run(stopped())
    assert len([c for c in commands(io) if c.startswith("FETC:ELOG?")]) == 1
    assert any(kind == "tail_limit" for kind, _, _ in events)
    assert events[-1][0] == "tail_incomplete"
    assert abor_threads(io) == [threading.get_ident()]


def test_normal_fetch_honors_record_cap_and_does_not_emit_gap(rig):
    session, io, transport = rig
    session._t0 -= 0.2
    transport.channels[1].elog_t0 -= 0.2
    events = []
    recorder = record(session, max_records=3, on_event=lambda *args: events.append(args))
    recorder._pump()
    assert events == []
    assert all(int(c.split()[1].split(",")[0]) <= 3
               for c in commands(io) if c.startswith("FETC:ELOG?"))
    assert session.stats.records_received > 3
    recorder.run(stopped())


def test_setting_each_command_init_abort_and_zero_fetch_drain_errors(rig):
    session, io, transport = rig
    session.stop()
    io.exchanges.clear()
    session.configure()
    session.start()
    io.actions.append(block(0))
    session.fetch()
    session.stop()
    wire = commands(io)
    for index, command in enumerate(wire):
        if command != "SYST:ERR?":
            assert wire[index + 1] == "SYST:ERR?", command
    assert not transport.errors


@pytest.mark.parametrize("context,code", [("FORM REAL", 310), ("INIT:ELOG", 323)])
def test_positive_fatal_scpi_codes_are_drained_and_do_not_start_running(rig, context, code):
    session, io, transport = rig
    session.stop()

    def refuse(command):
        if command.startswith(context):
            transport.errors.extend([(code, "injected positive error"), (-200, "second error")])

    io.on_write = refuse
    with pytest.raises(ScpiError) as caught:
        session.configure()
        session.start()
    assert caught.value.code == code
    assert "injected positive error" in str(caught.value)
    assert session.state is not elog.ElogState.RUNNING
    assert not transport.errors
    assert any(f"+{code}" in note for note in session.notes)
    assert any("-200" in note for note in session.notes)


def test_default_ranges_never_request_autorange(rig):
    # E.02.09 + N6781A: VOLT:RANG:AUTO ON makes FETC:ELOG? return only empty blocks.
    session, io, _ = rig
    session.stop()
    session.configure()
    sent = commands(io)
    assert not any(c.startswith("SENS:ELOG:VOLT:RANG:AUTO ON") for c in sent)
    assert any(c.startswith("SENS:ELOG:VOLT:RANG:AUTO OFF") for c in sent)
    assert not any(c.startswith("SENS:ELOG:CURR:RANG:AUTO ON") for c in sent)
    assert any(c.startswith("SENS:ELOG:VOLT:RANG MAX") for c in sent)
    assert any(c.startswith("SENS:ELOG:CURR:RANG MAX") for c in sent)
    assert any("voltage autoranging is not used" in n for n in session.notes)
    assert any("current autoranging is not used" in n for n in session.notes)


@pytest.mark.parametrize("reply", ["garbage", "0", '0,"unterminated', OSError("error query failed")])
def test_unverifiable_error_queue_is_fatal_without_retrying_as_success(rig, reply):
    session, io, _ = rig
    io.actions.append(block(0))
    io.on_query = lambda command: reply if command == "SYST:ERR?" else None
    with pytest.raises(elog.ElogFetchError) as caught:
        record(session).run()
    assert caught.value.__cause__ is not None
    assert abor_threads(io) == [threading.get_ident()]
    assert len([c for c in commands(io) if c.startswith("FETC:ELOG?")]) == 1


def test_error_queue_limit_requires_zero_and_retains_all_errors(rig):
    session, io, _ = rig
    io.on_query = lambda command: '+310,"stuck"' if command == "SYST:ERR?" else None
    with pytest.raises(RuntimeError, match="50 reads"):
        session.configure()
    assert commands(io).count("SYST:ERR?") == 50
    assert sum("+310" in note for note in session.notes) == 50


@pytest.mark.parametrize("reply", ["garbage", TimeoutError("error query failed")])
def test_arm_tracks_attempt_before_failed_error_check_and_stop_resets_it(rig, reply):
    session, io, _ = rig
    session.stop()
    session.configure()
    assert session.init_attempted is False
    io.on_query = lambda command: reply if command == "SYST:ERR?" else None
    with pytest.raises(elog._ElogErrorQueueError) as caught:
        session.arm()
    assert session.state is elog.ElogState.CONFIGURED
    assert session.init_attempted is True
    assert "INIT:ELOG" in str(caught.value)
    assert isinstance(caught.value.__cause__, ErrorQueueError if isinstance(reply, str) else TimeoutError)
    with pytest.raises(elog._ElogErrorQueueError):
        session.stop()
    assert session.state is elog.ElogState.CONFIGURED  # failed ABOR keeps the retry flag
    assert session.init_attempted is True
    io.on_query = None
    session.stop()
    assert session.state is elog.ElogState.STOPPED
    assert session.init_attempted is False


@pytest.mark.parametrize("max_records", [0, 16385, True, 1.5])
def test_record_cap_validation(rig, max_records):
    session, io, _ = rig
    with pytest.raises(ValueError, match="max_records"):
        record(session, max_records=max_records)
    with pytest.raises(ValueError, match="max_records"):
        session.fetch(max_records=max_records)
    assert io.exchanges == []


def test_background_api_reports_fatal_sink_after_abort(rig):
    session, io, _ = rig
    io.actions.append(block(1))
    reported, notified = [], threading.Event()
    original = OSError("disk full")

    def sink(chunk):
        raise original

    def on_error(error):
        assert abor_threads(io) == [threading.get_ident()]
        reported.append(error)
        notified.set()

    recorder = record(session, sink, on_error=on_error)
    recorder.start()
    assert notified.wait(2)
    assert recorder.stop(2)
    assert isinstance(reported[0], elog.ElogSinkError)
    assert reported[0].__cause__ is original


def test_failed_abor_keeps_session_retryable(rig):
    session, io, _ = rig
    io.on_write = lambda command: (_ for _ in ()).throw(TimeoutError("ABOR timed out")) if command.startswith("ABOR:ELOG") else None
    with pytest.raises(TimeoutError):
        session.stop()
    assert session.state is elog.ElogState.RUNNING and session.init_attempted is True
    io.on_write = None
    session.stop()
    assert session.state is elog.ElogState.STOPPED and session.init_attempted is False
    assert sum(c.startswith("ABOR:ELOG") for c in commands(io)) == 2


def test_pending_cap_stops_unaligned_channels():
    inst = N6705C.demo(slots=("N6781A", "N6781A"))
    try:
        session = elog.ElogSession(inst, elog.ElogConfig([1, 2], period=0.01))
        session.configure()
        session.start()
        session._pending[1] = [np.zeros((elog.MAX_PENDING_RECORDS + 10, 2), dtype="<f4")]
        with pytest.raises(elog.ElogAlignmentError):
            session.fetch()
        # the recorder treats it as fatal at once, not as a transient fetch failure
        session._pending[2] = []
        rec = record(session, lambda chunk: None)
        with pytest.raises(elog.ElogFetchError) as caught:
            rec._fetch_one(after_abort=False)
        assert isinstance(caught.value.__cause__, elog.ElogAlignmentError)
    finally:
        inst.close()
