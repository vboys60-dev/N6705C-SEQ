"""DLOG trigger state and per-file retrieval cleanup, without hardware."""

from datetime import datetime
from pathlib import Path

import pytest

from n6705c.core import dlog
from n6705c.core.dlog import DlogConfig, DlogSession, DlogState, VALID_DLOG_SOURCES
from n6705c.core.fake import FakeN6705C
from n6705c.core.instrument import N6705C


class DownloadFaultTransport:
    """Delegate to the shared fake; inject download failures only here."""

    def __init__(self):
        self.fake = FakeN6705C()
        self.fail_suffix = None

    def __getattr__(self, name):
        return getattr(self.fake, name)

    def query_binary(self, command):
        if self.fail_suffix and command.endswith(self.fail_suffix + '"'):
            raise OSError("injected download failure")
        return self.fake.query_binary(command)


@pytest.fixture
def instrument():
    transport = DownloadFaultTransport()
    inst = N6705C.from_transport(transport)
    yield inst, transport
    inst.close()


@pytest.mark.parametrize("source", [s for s in VALID_DLOG_SOURCES if s not in ("BUS", "IMM")])
def test_external_sources_stay_armed_without_trigger_time(instrument, monkeypatch, source):
    inst, transport = instrument
    now = [100.0]
    monkeypatch.setattr(dlog.time, "monotonic", lambda: now[0])
    session = DlogSession(inst, DlogConfig(channels=[1], trigger_source=source, duration=1))

    session.start()

    assert session.state is DlogState.ARMED
    assert session.trigger_time_utc == ""
    assert session.elapsed == 0
    now[0] += 100
    assert session.elapsed == 0
    assert session.progress == 0
    assert not session.is_complete
    assert session.state is DlogState.ARMED
    assert 'INIT:DLOG "internal:\\session.dlog"' in transport.fake.command_log
    assert "TRIG:DLOG" not in transport.fake.command_log


@pytest.mark.parametrize("source", ["BUS", "IMM", " bus ", " imm "])
def test_bus_and_immediate_sources_run_after_successful_trigger(instrument, monkeypatch, source):
    inst, transport = instrument
    now = [100.0]
    monkeypatch.setattr(dlog.time, "monotonic", lambda: now[0])
    session = DlogSession(inst, DlogConfig(channels=[1], trigger_source=source, duration=1))

    session.start()

    assert session.state is DlogState.RUNNING
    assert datetime.fromisoformat(session.trigger_time_utc).utcoffset().total_seconds() == 0
    commands = transport.fake.command_log
    init_index = commands.index('INIT:DLOG "internal:\\session.dlog"')
    assert commands[init_index + 1] == "SYST:ERR?"
    if source.strip().upper() == "BUS":
        assert commands[init_index + 2:init_index + 4] == ["TRIG:DLOG", "SYST:ERR?"]
    else:
        assert "TRIG:DLOG" not in commands
    now[0] += 0.25
    assert session.elapsed == pytest.approx(0.25)
    assert not session.is_complete
    now[0] += 1
    assert session.is_complete
    assert session.state is DlogState.COMPLETE


def test_rearming_external_source_clears_previous_trigger_time(instrument, monkeypatch):
    inst, _ = instrument
    monkeypatch.setattr(dlog.time, "monotonic", lambda: 100.0)
    session = DlogSession(inst, DlogConfig(channels=[1]))
    session.start()
    session.abort()
    session.config.trigger_source = "EXT"
    session.configure()

    session.start()

    assert session.state is DlogState.ARMED
    assert session.trigger_time_utc == ""
    assert session.elapsed == 0
    assert not session.is_complete


@pytest.mark.parametrize("context", ["INIT:DLOG", "TRIG:DLOG"])
def test_failed_init_or_bus_trigger_does_not_mark_running(instrument, monkeypatch, context):
    inst, transport = instrument
    check_errors = inst.session.check_errors

    def failing_check(current=""):
        check_errors(current)
        if current == context:
            raise RuntimeError("injected trigger error")

    monkeypatch.setattr(inst.session, "check_errors", failing_check)
    session = DlogSession(inst, DlogConfig(channels=[1]))

    with pytest.raises(RuntimeError, match="injected trigger error"):
        session.start()

    assert session.state is (DlogState.CONFIGURED if context == "INIT:DLOG" else DlogState.ARMED)
    assert session.trigger_time_utc == ""
    assert session.elapsed == 0
    assert not session.is_complete
    if context == "INIT:DLOG":
        assert "TRIG:DLOG" not in transport.fake.command_log


@pytest.fixture
def recorded(instrument):
    inst, transport = instrument
    session = DlogSession(inst, DlogConfig(channels=[1]))
    session.start()
    session.abort()
    return session, transport


@pytest.mark.parametrize("cleanup", [True, False])
def test_retrieve_both_files_successfully(recorded, tmp_path, cleanup):
    session, transport = recorded

    results = session.retrieve(tmp_path, cleanup=cleanup)

    remote_dlog = session.config.filename
    remote_csv = remote_dlog[:-5] + ".csv"
    assert set(results) == {"csv", "dlog"}
    assert results == {"csv": tmp_path / "session.csv", "dlog": tmp_path / "session.dlog"}
    assert all(path.read_bytes() for path in results.values())
    deletes = [cmd for cmd in transport.fake.command_log if cmd.startswith("MMEM:DEL ")]
    assert deletes == ([f'MMEM:DEL "{remote_csv}"', f'MMEM:DEL "{remote_dlog}"'] if cleanup else [])
    assert (remote_dlog in transport.fake.files) is (not cleanup)
    assert (remote_csv in transport.fake.files) is (not cleanup)


@pytest.mark.parametrize("cleanup", [True, False])
def test_failed_dlog_download_preserves_remote_dlog(recorded, tmp_path, caplog, cleanup):
    session, transport = recorded
    transport.fail_suffix = ".dlog"

    results = session.retrieve(tmp_path, cleanup=cleanup)

    assert results == {"csv": tmp_path / "session.csv"}
    assert results["csv"].read_bytes()
    assert session.config.filename in transport.fake.files
    assert (session.config.filename[:-5] + ".csv" in transport.fake.files) is (not cleanup)
    assert "injected download failure" in caplog.text


def test_failed_csv_download_does_not_delete_either_file(recorded, tmp_path):
    session, transport = recorded
    transport.fail_suffix = ".csv"

    with pytest.raises(OSError, match="injected download failure"):
        session.retrieve(tmp_path)

    assert session.config.filename in transport.fake.files
    assert session.config.filename[:-5] + ".csv" in transport.fake.files
    assert not any(cmd.startswith("MMEM:DEL ") for cmd in transport.fake.command_log)


@pytest.mark.parametrize("suffix", [".csv", ".dlog"])
@pytest.mark.parametrize("failure", ["write", "close"])
def test_local_copy_failure_preserves_remote_file(recorded, tmp_path, monkeypatch, suffix, failure):
    session, transport = recorded
    original_open = Path.open

    class FailingFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def write(self, data):
            if failure == "write":
                raise OSError("injected local write failure")
            return self.handle.write(data)

        def __exit__(self, *exc_info):
            self.handle.__exit__(*exc_info)
            if failure == "close":
                raise OSError("injected local close failure")

    def failing_open(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if path == tmp_path / ("session" + suffix) and mode == "wb":
            return FailingFile(handle)
        return handle

    monkeypatch.setattr(Path, "open", failing_open)
    if suffix == ".csv":
        with pytest.raises(OSError, match=f"injected local {failure} failure"):
            session.retrieve(tmp_path)
    else:
        assert session.retrieve(tmp_path) == {"csv": tmp_path / "session.csv"}

    assert session.config.filename in transport.fake.files
    assert (session.config.filename[:-5] + ".csv" in transport.fake.files) is (suffix == ".csv")
