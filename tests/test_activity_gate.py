"""Activity contract: admission, ownership, cancellation and every legal edge."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from n6705c_seq.analysis import contracts as c
from n6705c_seq.analysis.gate import Gate


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.002)


def make_gate(**kwargs):
    events = []
    return Gate(events.append, **kwargs), events


def enter(gate):
    return gate.enter(lambda: None)[0]


def test_power_tokens_and_event_order():
    gate, events = make_gate()
    assert events == [["state", {"state": "idle", "gen": 0, "seq": 0}]]
    with pytest.raises(ValueError):
        gate.reserve("analysis")
    first = gate.reserve("power")
    assert gate.reserve("power") is None
    assert gate.enter(lambda: pytest.fail("prepared while power")) == (None, "power")
    assert not gate.release(c.Token("power", 0, first.serial + 1))
    assert gate.release(first)
    second = gate.reserve("power")
    assert second.serial > first.serial
    assert not gate.release(first)
    assert gate.state() == (c.State.POWER, 0)
    assert gate.release(second)
    assert events[1:5] == [["state", {"state": "power", "gen": 0, "seq": 1}],
                           ["busy", True], ["state", {"state": "idle", "gen": 0, "seq": 2}],
                           ["busy", False]]
    assert [p[1]["seq"] for p in events if p[0] == "state"] == list(range(5))


def test_analysis_drain_generation_and_old_jobs():
    gate, events = make_gate()
    gen = enter(gate)
    assert gate.enter(lambda: pytest.fail("idempotent")) == (gen, None)
    assert gate.reserve("power") is None
    job = gate.job_begin(gen, "req")
    assert gate.cancel_req(gen, "req") and job.cancel.is_set()
    assert not gate.cancel_req(gen + 1, "req")
    released = threading.Event()
    gate.set_release_resources(lambda: released.is_set())
    assert gate.leave(gen) and gate.leave(gen)
    assert gate.job_begin(gen) is None
    assert gate.reserve("power") is None
    assert gate.enter(lambda: None) == (None, "draining")
    gate.job_end(job)
    assert gate.state()[0] == c.State.DRAINING
    released.set()
    wait_for(lambda: gate.state()[0] == c.State.IDLE)
    new_gen = enter(gate)
    assert new_gen == gen + 1
    current = gate.job_begin(new_gen)
    gate.job_end(job)
    assert gate.job_count() == 1
    assert not gate.leave(gen)
    assert gate.issue_grant(gen, "open", "dlog", "/tmp/a") is None
    gate.job_end(current)
    assert gate.leave(new_gen)
    wait_for(lambda: gate.state()[0] == c.State.IDLE)
    assert all(e != ["busy", False] for e in events[:2])


@pytest.mark.parametrize("pending", [False, True])
def test_drain_timeout_fault_and_close(monkeypatch, pending):
    monkeypatch.setattr(c, "DRAINING_TIMEOUT_S", 0.03)
    faults, closes = [], []
    gate, events = make_gate(fault=lambda: faults.append(True), closing=lambda: closes.append(True))
    gen = enter(gate)
    job = gate.job_begin(gen)
    grant = gate.issue_grant(gen, "open", "dlog", "/tmp/a")
    assert gate.leave(gen)
    assert job.cancel.is_set()
    if pending:
        assert gate.on_closing() == "draining"
        assert gate.close_pending
    wait_for(lambda: gate.state()[0] == (c.State.CLOSING if pending else c.State.FAULT))
    assert faults == [True]
    assert gate.consume_grant(grant.id, gen, "open", "dlog") is None
    gate.job_end(job)
    time.sleep(0.02)
    assert gate.reserve("power") is None and not gate.leave(gen)
    if pending:
        assert closes == [True]
        states = [e[1]["state"] for e in events if e[0] == "state"]
        assert states[-2:] == ["fault", "closing"]
    else:
        assert gate.enter(lambda: None) == (None, "fault")
        assert gate.on_closing() == "start"
    assert gate.state()[0] == c.State.CLOSING


def test_draining_close_success_and_late_resource_return(monkeypatch):
    monkeypatch.setattr(c, "DRAINING_TIMEOUT_S", 0.5)
    started, resume, closed = threading.Event(), threading.Event(), threading.Event()
    gate, _ = make_gate(closing=closed.set)
    def release():
        started.set()
        resume.wait(2)
        return True
    gate.set_release_resources(release)
    gen = enter(gate)
    assert gate.leave(gen)
    assert started.wait(1)
    assert gate.on_closing() == "draining"
    resume.set()
    assert closed.wait(1)
    assert gate.state()[0] == c.State.CLOSING
    gate._drain_expired(gen)
    assert gate.state()[0] == c.State.CLOSING


def test_late_drain_after_timeout_cannot_overwrite_closing(monkeypatch):
    monkeypatch.setattr(c, "DRAINING_TIMEOUT_S", 0.03)
    started, resume, done = threading.Event(), threading.Event(), threading.Event()
    gate, _ = make_gate()
    def release():
        started.set()
        resume.wait(2)
        done.set()
        return True
    gate.set_release_resources(release)
    gen = enter(gate)
    gate.leave(gen)
    assert started.wait(1)
    wait_for(lambda: gate.state()[0] == c.State.FAULT)
    assert gate.on_closing() == "start"
    resume.set()
    assert done.wait(1)
    assert gate.state()[0] == c.State.CLOSING


def test_finishing_rearms_and_fault_never_unlocks(monkeypatch):
    monkeypatch.setattr(c, "POWER_FINISH_TIMEOUT_S", 0.1)
    gate, _ = make_gate()
    token = gate.reserve("power")
    gate.finishing(token)
    time.sleep(0.06)
    gate.finishing(token)
    time.sleep(0.06)
    assert gate.state()[0] == c.State.POWER
    wait_for(lambda: gate.state()[0] == c.State.FAULT)
    assert not gate.release(token) and gate.worker_done
    assert gate.on_closing() == "start"
    gate.finishing(token)
    assert not gate.release(token)
    assert gate.state()[0] == c.State.CLOSING


def test_late_timer_and_old_generation_token(monkeypatch):
    monkeypatch.setattr(c, "POWER_FINISH_TIMEOUT_S", 0.02)
    gate, _ = make_gate()
    old = gate.reserve("power")
    gate.finishing(old)
    gate.release(old)
    gen = enter(gate)
    gate.leave(gen)
    wait_for(lambda: gate.state()[0] == c.State.IDLE)
    current = gate.reserve("power")
    gate.finishing(old)
    time.sleep(0.04)
    assert not gate.release(old)
    assert gate.state()[0] == c.State.POWER
    assert gate.release(current)


@pytest.mark.parametrize("first", ["power", "analysis", "race"])
def test_bidirectional_admission_race(first):
    for _ in range(15):
        gate, _ = make_gate()
        barrier = threading.Barrier(2)
        def power():
            barrier.wait()
            return gate.reserve("power")
        def analysis():
            barrier.wait()
            return enter(gate)
        if first == "power":
            assert gate.reserve("power")
            assert enter(gate) is None
        elif first == "analysis":
            assert enter(gate)
            assert gate.reserve("power") is None
        else:
            with ThreadPoolExecutor(2) as pool:
                a, b = pool.submit(power), pool.submit(analysis)
                assert (a.result() is not None) != (b.result() is not None)


@pytest.mark.parametrize("action", ["leave", "close"])
def test_job_registration_races_with_leaving(action):
    gate, _ = make_gate()
    gen = enter(gate)
    barrier = threading.Barrier(2)
    def job():
        barrier.wait()
        return gate.job_begin(gen)
    def stop():
        barrier.wait()
        return gate.leave(gen) if action == "leave" else gate.on_closing()
    with ThreadPoolExecutor(2) as pool:
        task, stopping = pool.submit(job), pool.submit(stop)
        token = task.result()
        stopping.result()
    if token is not None:
        assert token.cancel.is_set()
        gate.job_end(token)
    assert gate.job_begin(gen) is None


def test_grants_single_use_purpose_generation_ttl_and_capacity(monkeypatch):
    gate, _ = make_gate()
    gen = enter(gate)
    for mode, purpose, requested in [("open", "dlog", ("open", "markers")),
                                     ("save", "png", ("open", "png"))]:
        grant = gate.issue_grant(gen, mode, purpose, "/tmp/private")
        assert gate.consume_grant(grant.id, gen, *requested) is None
        assert gate.consume_grant(grant.id, gen, mode, purpose) is None
    grant = gate.issue_grant(gen, "open", "dlog", "/tmp/private")
    assert gate.consume_grant(grant.id, gen + 1, "open", "dlog") is None
    assert gate.consume_grant(grant.id, gen, "open", "dlog") is None
    oldest = gate.issue_grant(gen, "open", "dlog", "/tmp/old")
    for _ in range(c.MAX_GRANTS_PER_GEN):
        newest = gate.issue_grant(gen, "open", "dlog", "/tmp/new")
    assert gate.consume_grant(oldest.id, gen, "open", "dlog") is None
    assert gate.consume_grant(newest.id, gen, "open", "dlog") == "/tmp/new"
    assert gate.consume_grant(newest.id, gen, "open", "dlog") is None
    monkeypatch.setattr(c, "GRANT_TTL_S", -1)
    expired = gate.issue_grant(gen, "open", "dlog", "/tmp/expired")
    assert gate.consume_grant(expired.id, gen, "open", "dlog") is None
    grant = gate.issue_grant(gen, "save", "csv", "/tmp/a")
    assert gate.on_closing() == "start"
    assert gate.consume_grant(grant.id, gen, "save", "csv") is None


def test_all_contract_edges_are_exercised(monkeypatch):
    monkeypatch.setattr(c, "POWER_FINISH_TIMEOUT_S", 0.01)
    monkeypatch.setattr(c, "DRAINING_TIMEOUT_S", 0.2)
    edges = set()
    def record(events):
        states = [c.State(e[1]["state"]) for e in events if e[0] == "state"]
        edges.update(zip(states, states[1:]))
    gate, events = make_gate()
    token = gate.reserve("power")
    gate.release(token)
    token = gate.reserve("power")
    gate.finishing(token)
    wait_for(lambda: gate.state()[0] == c.State.FAULT)
    gate.on_closing()
    record(events)
    gate, events = make_gate()
    gate.on_closing()
    record(events)
    gate, events = make_gate()
    enter(gate)
    gate.on_closing()
    record(events)
    for pending, stuck in [(False, False), (True, False), (False, True)]:
        gate, events = make_gate()
        gen = enter(gate)
        job = gate.job_begin(gen)
        gate.leave(gen)
        if pending:
            gate.on_closing()
        if not stuck:
            gate.job_end(job)
        wait_for(lambda: gate.state()[0] != c.State.DRAINING)
        record(events)
    assert edges == {(source, target) for source, targets in c.TRANSITIONS.items() for target in targets}


def test_untimed_owner_checks_do_not_mutate_state(monkeypatch):
    gate, events = make_gate()
    timers = []
    monkeypatch.setattr(gate, "_timer", lambda seconds, callback: timers.append(callback))
    old = gate.reserve("power")
    assert gate.release(old)
    gen = enter(gate)
    assert gate.leave(gen)
    wait_for(lambda: gate.state()[0] == c.State.IDLE)
    token = gate.reserve("power")
    gate.finishing(token)
    before = (gate.payload(), gate._finish_epoch, list(events))
    for invalid in (None, old, c.Token("power", token.gen - 1, token.serial),
                    c.Token("power", token.gen, token.serial + 1)):
        assert gate.untimed(invalid) is False
        assert (gate.payload(), gate._finish_epoch, events) == before
    # Invalid calls must not invalidate the owner's timer.
    timers[-1]()
    assert gate.state()[0] == c.State.FAULT
    before = (gate.payload(), gate._finish_epoch, list(events))
    assert gate.untimed(token) is False
    gate.finishing(token)
    assert (gate.payload(), gate._finish_epoch, events) == before
    assert gate.release(token) is False
    gate.on_closing()
    assert gate.untimed(token) is False
    assert gate.state()[0] == c.State.CLOSING


def test_untimed_invalidates_old_timer_and_finishing_rearms(monkeypatch):
    gate, _ = make_gate()
    timers = []
    monkeypatch.setattr(gate, "_timer", lambda seconds, callback: timers.append(callback))
    token = gate.reserve("power")
    gate.finishing(token)
    assert gate.untimed(token) is True
    timers[-1]()
    assert gate.state()[0] == c.State.POWER
    epoch = gate._finish_epoch
    gate.finishing(c.Token("power", token.gen, token.serial + 1))
    assert gate._finish_epoch == epoch and len(timers) == 1
    gate.finishing(token)
    gate.finishing(token)
    timers[-2]()
    assert gate.state()[0] == c.State.POWER
    timers[-1]()
    assert gate.state()[0] == c.State.FAULT
    gate.finishing(token)
    assert len(timers) == 3

    gate, _ = make_gate()
    token = gate.reserve("power")
    assert gate.release(token)
    before = (gate.payload(), gate._finish_epoch)
    assert gate.untimed(token) is False
    assert (gate.payload(), gate._finish_epoch) == before
