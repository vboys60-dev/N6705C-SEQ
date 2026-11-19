"""External data logging (ELOG): configuration, state machine, background fetch.

The defining constraint of this subsystem is the instrument's **10-second
per-channel buffer** (``FETCh:ELOG?``, p.96).  Records accumulate at
``SENSe:ELOG:PERiod`` and are only freed when they are read, so a controller
that stops fetching for more than ten seconds loses data -- silently.  Nothing
in the SCPI interface reports the loss, which is why :class:`ElogStats` derives
it from record accounting instead.

Sequence (PLAN.md section 5.2)::

    FORM REAL / FORM:BORD SWAP     multi-channel requires REAL
    SENS:ELOG:FUNC:...             from the user's tick boxes
    SENS:ELOG:PER <p>              then read back -- the value is rounded
    TRIG:ELOG:SOUR BUS
    INIT:ELOG  ->  TRIG:ELOG
    FETC:ELOG? <maxrecords>        repeatedly, faster than the buffer fills
    ABOR:ELOG
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

import numpy as np

from .capability import (
    BUFFER_SECONDS,
    ELOG_MAX_PERIOD,
    ELOG_PERIOD_GRANULARITY,
    min_elog_period,
    round_to_granularity,
)
from .instrument import N6705C
from .parsing import ElogSelection
from .scpi import format_channel_list
from .session import ErrorQueueError, ScpiError

__all__ = [
    "ElogConfig",
    "ElogChunk",
    "ElogStats",
    "ElogState",
    "ElogSession",
    "ElogRecorder",
    "ElogAlignmentError",
    "ElogSinkError",
    "ElogFetchError",
    "MAX_FETCH_FAILURES",
    "BUFFER_SECONDS",
    "MAX_RECORDS_PER_FETCH",
]

log = logging.getLogger(__name__)

#: ``FETCh:ELOG?`` maxrecords ceiling for a 4-channel mainframe (p.96).
MAX_RECORDS_PER_FETCH = 16_384

#: Fetch this often by default: a 5x margin against the 10-second buffer.
DEFAULT_FETCH_INTERVAL = 2.0
MAX_FETCH_FAILURES = 3


class ElogSinkError(RuntimeError):
    """A sink or persistence callback failed; the original error is the cause."""


class ElogAlignmentError(RuntimeError):
    """One channel stopped delivering records while others kept coming (pending cap hit)."""


#: Per-channel records held back waiting for the slowest channel before giving up.
MAX_PENDING_RECORDS = 200_000


class ElogFetchError(RuntimeError):
    """Fetching cannot safely continue; the original error is the cause."""


class _ElogErrorQueueError(RuntimeError):
    """The error queue could not be verified empty."""


def _validate_max_records(max_records: int) -> None:
    if type(max_records) is not int or not 1 <= max_records <= MAX_RECORDS_PER_FETCH:
        raise ValueError(f"max_records must be an integer in 1..{MAX_RECORDS_PER_FETCH}")


VALID_TRIGGER_SOURCES = ("BUS", "EXT", "IMM", "PIN1", "PIN2", "PIN3", "PIN4",
                         "PIN5", "PIN6", "PIN7")


class ElogState(Enum):
    IDLE = "idle"
    CONFIGURED = "configured"
    INITIATED = "initiated"
    RUNNING = "running"
    STOPPED = "stopped"


@dataclass
class ElogConfig:
    """Everything the user chooses before a run starts."""

    channels: list[int]
    selection: ElogSelection = field(default_factory=ElogSelection)
    period: float = 0.1
    #: ``None`` keeps the instrument's present range: ELOG autoranging returns
    #: no records on E.02.09 + N6781A (hardware, 2026-11-18).
    voltage_range: float | None = None
    current_range: float | None = None
    trigger_source: str = "BUS"

    def validate(self) -> None:
        """Reject what the instrument would reject, with a usable reason."""
        if not self.channels:
            raise ValueError("no channels selected for logging")
        if self.selection.is_empty:
            raise ValueError(
                "no quantities selected: tick at least one of voltage/current "
                "average, minimum or maximum"
            )
        source = self.trigger_source.strip().upper()
        if source not in VALID_TRIGGER_SOURCES:
            raise ValueError(
                f"invalid trigger source {self.trigger_source!r}; "
                f"expected one of {', '.join(VALID_TRIGGER_SOURCES)}"
            )
        floor = self.minimum_period
        if self.period < floor - 1e-12:
            forced = self.selection.forced_fields
            because = ""
            if forced:
                because = (
                    f"; {'/'.join(forced)} must also be measured because minimum "
                    "and maximum share one instrument switch"
                )
            per_channel = self.selection.hardware_parameters
            n = max(1, len(self.channels))
            raise ValueError(
                f"period {self.period * 1e6:.1f} us is below the minimum "
                f"{floor * 1e6:.1f} us for {per_channel * n} measured parameters "
                f"({per_channel} per channel x {n} channel"
                f"{'s' if n != 1 else ''}){because}"
            )
        if self.period > ELOG_MAX_PERIOD:
            raise ValueError(
                f"period {self.period} s exceeds the maximum {ELOG_MAX_PERIOD} s"
            )

    @property
    def minimum_period(self) -> float:
        """Floor implied by what the *hardware* measures, not what is kept.

        The count is **per interval across every logged channel**, not per
        channel.  p.192: the minimum "varies as a function of the number of
        channels *and* readings being logged ... (102.4 us x number of
        parameters being logged at each interval)", and the worked example is
        for "a single channel" -- so four channels of the same selection costs
        four times as much.

        Getting this wrong is not caught until the run starts: the period is
        accepted, and then ``INIT:ELOG`` fails with
        ``+323 Meas TINTerval too small on too many channels``.  Reported from
        the GUI against real hardware, where three channels at a period that
        one channel could sustain was enough to trigger it.
        """
        return min_elog_period(
            self.selection.hardware_parameters * max(1, len(self.channels))
        )

    @property
    def expected_rounded_period(self) -> float:
        """What the instrument will most likely round the request to."""
        return round_to_granularity(self.period, ELOG_PERIOD_GRANULARITY)


@dataclass
class ElogChunk:
    """One fetch: aligned records for every channel, plus their time base."""

    start_index: int
    period: float
    data: dict[int, np.ndarray]
    fields: tuple[str, ...]

    @property
    def n_records(self) -> int:
        return next(iter(self.data.values())).shape[0] if self.data else 0

    @property
    def is_empty(self) -> bool:
        return self.n_records == 0

    def times(self) -> np.ndarray:
        """Seconds since the trigger, derived from the index and the period.

        ELOG records carry no timestamp; the time base is reconstructed here
        (PLAN.md section 5.4).
        """
        return (self.start_index + np.arange(self.n_records, dtype=np.float64)) * self.period


@dataclass
class ElogStats:
    """Record accounting, used to detect the silent buffer overflow."""

    period: float = 0.1
    records_received: int = 0
    fetches: int = 0
    elapsed: float = 0.0
    overflow_detected: bool = False
    worst_backlog: int = 0

    @property
    def records_produced(self) -> int:
        """How many records the instrument should have generated by now."""
        if self.period <= 0:
            return 0
        return int(self.elapsed / self.period)

    @property
    def backlog(self) -> int:
        """Records generated but not yet read; these sit in the instrument."""
        return max(0, self.records_produced - self.records_received)

    @property
    def buffer_capacity(self) -> int:
        return max(1, int(BUFFER_SECONDS / self.period)) if self.period > 0 else 1

    @property
    def buffer_fill(self) -> float:
        """Backlog as a fraction of buffer depth. Above 1.0 means data was lost."""
        return self.backlog / self.buffer_capacity

    def note_fetch(self, records: int, elapsed: float) -> None:
        self.fetches += 1
        self.records_received += records
        self.elapsed = elapsed
        self.worst_backlog = max(self.worst_backlog, self.backlog)
        if self.backlog > self.buffer_capacity:
            self.overflow_detected = True

    def summary(self) -> str:
        text = (
            f"{self.records_received} records in {self.fetches} fetches "
            f"over {self.elapsed:.1f} s; peak buffer use "
            f"{self.worst_backlog}/{self.buffer_capacity}"
        )
        if self.overflow_detected:
            text += "  *** BUFFER OVERFLOW: records were lost ***"
        return text


class ElogSession:
    """Synchronous ELOG state machine over a connected instrument."""

    def __init__(self, instrument: N6705C, config: ElogConfig) -> None:
        config.validate()
        self.instrument = instrument
        self.config = config
        self.state = ElogState.IDLE
        self.init_attempted = False
        self.actual_period = config.period
        self.trigger_time_utc: datetime | None = None
        #: Things the instrument would not do, in the user's words.  Populated
        #: by :meth:`configure`; belongs in the run's metadata and on screen,
        #: because a log recorded without the settings that were asked for is
        #: not the log the user thinks they have.
        self.notes: list[str] = []
        self.stats = ElogStats(period=config.period)
        self._t0 = 0.0
        self._last_fetch = 0.0
        self._next_index = 0
        self._pending: dict[int, list[np.ndarray]] = {}

    # -- Configuration ------------------------------------------------------

    @property
    def chanlist(self) -> str:
        return format_channel_list(self.config.channels)

    #: SCPI code for "the command is not supported by this model".  Positive,
    #: unlike the standard negative codes -- do not test errors with ``< 0``.

    def _read_errors(self, context: str) -> list[ScpiError]:
        """Require a valid zero terminator, rather than silently truncating."""
        try:
            errors = self.instrument.session.read_errors()
        except Exception as exc:
            message = f"cannot drain ELOG error queue after {context}"
            if isinstance(exc, ErrorQueueError):
                for error in exc.errors:
                    error.command = context
                    self.notes.append(str(error))
                if exc.response is None:
                    message = f"ELOG error queue did not reach zero in 50 reads after {context}"
            raise _ElogErrorQueueError(message) from exc
        for error in errors:
            error.command = context
            self.notes.append(str(error))
        return errors

    def _check_errors(self, context: str) -> None:
        errors = self._read_errors(context)
        if errors:
            for extra in errors[1:]:
                errors[0].add_note(str(extra))
            raise errors[0]

    def _write_checked(self, command: str) -> None:
        # Even a failed write may have left an instrument error behind.
        try:
            self.instrument.session.write(command)
        finally:
            self._check_errors(command)

    def configure(self) -> None:
        """Apply the configuration and read back what the instrument accepted."""
        self.init_attempted = False
        session = self.instrument.session
        chan = self.chanlist
        config = self.config

        # Multi-channel fetches must be binary: ASCII allows only one channel.
        self._write_checked("FORM REAL")
        self._write_checked("FORM:BORD SWAP")

        for command in config.selection.scpi_commands(chan):
            self._write_checked(command)

        if config.voltage_range is None:
            # E.02.09 + N6781A accepts VOLT:RANG:AUTO ON without error, then
            # FETC:ELOG? returns only empty #10 blocks (hardware, 2026-11-18).
            self._write_checked(f"SENS:ELOG:VOLT:RANG:AUTO OFF,{chan}")
            self._write_checked(f"SENS:ELOG:VOLT:RANG MAX,{chan}")
            self.notes.append(
                "ELOG voltage autoranging is not used (it yields no records on "
                "E.02.09/N6781A); the maximum voltage range was used. "
                "Set voltage_range to control it."
            )
        else:
            self._write_checked(f"SENS:ELOG:VOLT:RANG:AUTO OFF,{chan}")
            self._write_checked(f"SENS:ELOG:VOLT:RANG {config.voltage_range:G},{chan}")

        if config.current_range is None:
            self._write_checked(f"SENS:ELOG:CURR:RANG:AUTO OFF,{chan}")
            self._write_checked(f"SENS:ELOG:CURR:RANG MAX,{chan}")
            self.notes.append(
                "ELOG current autoranging is not used (it yields no records on "
                "E.02.09/N6781A); the maximum current range was used. "
                "Set current_range to control it."
            )
        else:
            self._write_checked(f"SENS:ELOG:CURR:RANG:AUTO OFF,{chan}")
            self._write_checked(f"SENS:ELOG:CURR:RANG {config.current_range:G},{chan}")

        self._write_checked(f"SENS:ELOG:PER {config.period:.9G},{chan}")
        self._write_checked(f"TRIG:ELOG:SOUR {config.trigger_source.upper()},{chan}")

        # The instrument rounds the period to a 20.48 us grid and clamps it to
        # the parameter-count floor. Using the requested value would skew every
        # timestamp, so the accepted value is the one we keep.
        query = f"SENS:ELOG:PER? {chan}"
        try:
            periods = session.query_floats(query)
        finally:
            self._check_errors(query)
        if not periods:
            raise RuntimeError("instrument did not report the ELOG period")
        if len(set(round(p, 12) for p in periods)) > 1:
            raise RuntimeError(
                f"channels disagree on the ELOG period: {periods}; "
                "a common time base is required"
            )
        self.actual_period = periods[0]
        if not math.isclose(self.actual_period, config.period, rel_tol=1e-6):
            log.info(
                "instrument rounded the ELOG period from %.9g s to %.9g s",
                config.period,
                self.actual_period,
            )
        self.stats = ElogStats(period=self.actual_period)
        self._pending = {channel: [] for channel in config.channels}
        self._next_index = 0
        self.state = ElogState.CONFIGURED

    # -- Run control --------------------------------------------------------

    def arm(self) -> None:
        """Initiate without triggering.

        Split from :meth:`start` so an integrated run can arm the logger and
        the transient system and then release both with one ``*TRG`` -- that
        is what puts the recording and the waveform on the same time base.
        """
        if self.state not in (ElogState.CONFIGURED, ElogState.STOPPED):
            raise RuntimeError(f"cannot arm from state {self.state.value}")
        self.init_attempted = True
        self._write_checked(f"INIT:ELOG {self.chanlist}")
        self.state = ElogState.INITIATED

    def mark_started(self) -> None:
        """Record the moment the trigger fired, whoever sent it."""
        self._t0 = time.monotonic()
        self._last_fetch = self._t0
        self.trigger_time_utc = datetime.now(timezone.utc)
        self.state = ElogState.RUNNING

    def fire(self) -> None:
        """Send this logger's own bus trigger."""
        if self.state is not ElogState.INITIATED:
            raise RuntimeError(f"cannot trigger from state {self.state.value}")
        if self.config.trigger_source.upper() == "BUS":
            self._write_checked(f"TRIG:ELOG {self.chanlist}")
            # Retain the legacy trigger-completion hook used by session
            # adapters to measure trigger latency. The strict drain above
            # has already verified the instrument queue through zero.
            self.instrument.session.check_errors("TRIG:ELOG")
        self.mark_started()

    def start(self) -> None:
        """Initiate and trigger. Configure first."""
        self.arm()
        self.fire()

    def stop(self) -> None:
        if self.state in (ElogState.IDLE, ElogState.STOPPED) and not self.init_attempted:
            return
        if self.state is ElogState.RUNNING:
            self.stats.elapsed = self.elapsed
        # Only a confirmed ABOR clears the flags; a failure stays retryable.
        self._write_checked(f"ABOR:ELOG {self.chanlist}")
        self.state = ElogState.STOPPED
        self.init_attempted = False

    # -- Fetching -----------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._t0 if self.state == ElogState.RUNNING else self.stats.elapsed

    def _records_to_request(self) -> int:
        """Ask for the backlog plus headroom, within the instrument's ceiling."""
        wanted = self.stats.records_produced - self.stats.records_received
        wanted = int(wanted * 1.2) + 1
        return max(1, min(wanted, MAX_RECORDS_PER_FETCH))

    def fetch(self, *, max_records: int | None = None) -> ElogChunk:
        """Read one batch and return the records common to every channel."""
        if self.state != ElogState.RUNNING:
            raise RuntimeError(f"cannot fetch in state {self.state.value}")
        return self._fetch(max_records=max_records)

    def fetch_tail(self, *, max_records: int | None = None) -> ElogChunk:
        """Explicit post-ABOR fetch, using the frozen acquisition duration.

        Whether hardware retains records after ABOR must be established by H0.
        The ordinary fetch method continues to reject STOPPED sessions.
        """
        if self.state is not ElogState.STOPPED:
            raise RuntimeError(f"cannot fetch tail in state {self.state.value}")
        return self._fetch(max_records=max_records)

    def _fetch(self, *, max_records: int | None) -> ElogChunk:
        if max_records is not None:
            _validate_max_records(max_records)
        elapsed = self.elapsed
        self.stats.elapsed = elapsed
        # Observe overflow before consuming the buffer. A large successful
        # fetch can otherwise hide the evidence by reducing the backlog.
        self.stats.worst_backlog = max(self.stats.worst_backlog, self.stats.backlog)
        if self.stats.backlog > self.stats.buffer_capacity:
            self.stats.overflow_detected = True
        request = self._records_to_request()
        if max_records is not None:
            request = min(request, max_records)
        command = f"FETC:ELOG? {request},{self.chanlist}"
        try:
            raw = self.instrument.session.query_binary(command)
            per_channel = self.config.selection.parse_response(raw, "SWAP")
            if len(per_channel) != len(self.config.channels):
                raise RuntimeError(
                    f"expected {len(self.config.channels)} data blocks, "
                    f"got {len(per_channel)}"
                )
        except Exception:
            self._check_errors(command)
            raise

        for channel, records in zip(self.config.channels, per_channel):
            if records.shape[0]:
                self._pending[channel].append(records)
        held = {ch: sum(a.shape[0] for a in parts) for ch, parts in self._pending.items()}
        if held and max(held.values()) - min(held.values()) > MAX_PENDING_RECORDS:
            raise ElogAlignmentError(
                f"channels cannot be aligned: pending records per channel {held} differ by more than "
                f"{MAX_PENDING_RECORDS}")

        chunk = self._take_aligned()
        if chunk.is_empty:
            self._check_errors(command)
        self.stats.note_fetch(chunk.n_records, elapsed)
        self._last_fetch = time.monotonic()
        if self.stats.overflow_detected:
            log.warning(
                "ELOG buffer overflowed: backlog %d exceeds capacity %d; "
                "records have been lost",
                self.stats.backlog,
                self.stats.buffer_capacity,
            )
        return chunk

    def _take_aligned(self) -> ElogChunk:
        """Emit only records every channel has, keeping the rest buffered.

        Channels share a trigger and a period, so record *i* is the same
        instant on every channel; a fetch can still return slightly different
        counts, and emitting those unaligned would shear the time base.
        """
        available = {
            channel: sum(a.shape[0] for a in parts)
            for channel, parts in self._pending.items()
        }
        common = min(available.values()) if available else 0
        data: dict[int, np.ndarray] = {}
        if common:
            for channel, parts in self._pending.items():
                joined = np.concatenate(parts, axis=0) if len(parts) > 1 else parts[0]
                data[channel] = joined[:common]
                remainder = joined[common:]
                self._pending[channel] = [remainder] if remainder.shape[0] else []
        chunk = ElogChunk(
            start_index=self._next_index,
            period=self.actual_period,
            data=data,
            fields=self.config.selection.fields,
        )
        self._next_index += common
        return chunk

    def drain(self, max_batches: int = 16) -> list[ElogChunk]:
        """Fetch repeatedly until the instrument has nothing left to give.

        A single ``FETCh:ELOG?`` is capped at 16384 records, so a fast period
        can leave a backlog behind after one call.
        """
        chunks: list[ElogChunk] = []
        for _ in range(max_batches):
            chunk = self.fetch()
            if chunk.is_empty:
                break
            chunks.append(chunk)
            if self.stats.backlog <= 0:
                break
        return chunks


class ElogRecorder:
    """Fetch on the calling worker, with an optional background-thread wrapper.

    Configure and start the session before calling run(). All fetching, sinks,
    callbacks and final ABOR execute on that same worker. Sinks must be fast
    enough to keep up with the instrument's ten-second buffer; failure is fatal.
    """

    def __init__(
        self,
        session: ElogSession,
        sink: Callable[[ElogChunk], None],
        *,
        interval: float = DEFAULT_FETCH_INTERVAL,
        on_error: Callable[[Exception], None] | None = None,
        tail: str = "drain_then_abort",
        on_event: Callable[[str, int, int], None] | None = None,
        max_records: int = MAX_RECORDS_PER_FETCH,
        on_cleanup: Callable[[], None] | None = None,
    ) -> None:
        if not math.isfinite(interval) or not 0 < interval < BUFFER_SECONDS:
            raise ValueError(
                f"fetch interval {interval} s must stay well below the "
                f"{BUFFER_SECONDS} s instrument buffer"
            )
        if tail not in ("drain_then_abort", "abort_then_drain"):
            raise ValueError(f"invalid ELOG tail strategy: {tail!r}")
        _validate_max_records(max_records)
        self.session = session
        self.sink = sink
        self.interval = interval
        self.on_error = on_error
        self.tail = tail
        self.on_event = on_event
        self.max_records = max_records
        self.on_cleanup = on_cleanup
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state_lock = threading.RLock()
        self._run_owner: int | None = None
        self._fetch_failures = 0
        self._overflow_excess = 0
        self._empty_backlog_baseline = 0
        self._abort_delay = 0.0

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return self._run_owner is not None or (
                self._thread is not None and self._thread.is_alive()
            )

    def start(self) -> None:
        with self._state_lock:
            if self.is_running:
                raise RuntimeError("recorder is already running")
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="elog-fetch", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        """Signal stop; join only our background thread, without instrument I/O."""
        with self._state_lock:
            self._stop.set()
            thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout)
        with self._state_lock:
            if thread.is_alive():
                return False
            if self._thread is thread:
                self._thread = None
        return True

    def run(self, stop: threading.Event | None = None) -> ElogStats:
        """Block until either stop event is set, then finish on this thread.

        After stop(), the internal stop flag stays set: a direct run() cannot
        resume acquisition. Use a new recorder for another blocking run, or
        start() after restarting the session to clear the flag explicitly.

        Fatal errors skip further sink calls. Cleanup failures are attached to
        the primary error so an ABOR failure cannot hide a rejected chunk.
        """
        with self._state_lock:
            if self.is_running and self._thread is not threading.current_thread():
                raise RuntimeError("recorder is already running")
            if self._run_owner is not None:
                raise RuntimeError("recorder is already running")
            self._run_owner = threading.get_ident()
        self._fetch_failures = 0
        self._overflow_excess = 0
        self._empty_backlog_baseline = 0
        self._abort_delay = 0.0
        failure: BaseException | None = None

        def remember(exc: BaseException) -> None:
            nonlocal failure
            if failure is None:
                failure = exc
            else:
                detail = f"ELOG cleanup also failed: {exc!r}"
                if exc.__cause__ is not None:
                    detail += f"; caused by {exc.__cause__!r}"
                failure.add_note(detail)

        try:
            try:
                if self.session.state is not ElogState.RUNNING:
                    raise RuntimeError("ELOG session must be running before recording")
                while not self._stopping(stop):
                    # An external Event cannot wake our Event.wait(). Keep its
                    # observation delay bounded without busy polling VISA.
                    deadline = time.monotonic() + self.interval
                    while not self._stopping(stop):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._stop.wait(min(remaining, 0.05) if stop is not None else remaining)
                    if not self._stopping(stop):
                        self._pump(stop=stop)
            except BaseException as exc:
                remember(exc)
            finally:
                try:
                    self._callback(self.on_cleanup)
                except BaseException as exc:
                    remember(exc)
                try:
                    if failure is None and self.tail == "drain_then_abort":
                        self._drain_tail(after_abort=False)
                except BaseException as exc:
                    remember(exc)
                finally:
                    abort_started = time.monotonic()
                    try:
                        self.session.stop()
                    except BaseException as exc:
                        remember(exc)
                    finally:
                        self._abort_delay = max(0.0, time.monotonic() - abort_started)
                if failure is None and self.tail == "abort_then_drain":
                    try:
                        self._drain_tail(after_abort=True)
                    except BaseException as exc:
                        remember(exc)
                if failure is None:
                    try:
                        self._note_tail_incomplete()
                    except BaseException as exc:
                        remember(exc)
        finally:
            with self._state_lock:
                self._run_owner = None
        if failure is not None:
            raise failure
        return self.session.stats

    def _stopping(self, stop: threading.Event | None) -> bool:
        return self._stop.is_set() or (stop is not None and stop.is_set())

    @staticmethod
    def _callback(callback: Callable | None, *args: object) -> None:
        if callback is not None:
            try:
                callback(*args)
            except Exception as exc:
                raise ElogSinkError("ELOG sink/callback failed") from exc

    def _event(self, kind: str, count: int) -> None:
        self._callback(self.on_event, kind, self.session._next_index, count)

    def _run(self) -> None:
        try:
            self.run()
        except Exception as exc:
            log.exception("ELOG fetch loop failed")
            # The background API has no caller to receive the exception.
            # run() has already completed ABOR before reporting it here.
            self._callback(self.on_error, exc)

    def _pump(self, *, stop: threading.Event | None = None) -> None:
        for _ in range(16):
            if self._stopping(stop):
                break
            chunk = self._fetch_one(after_abort=False)
            if chunk is None or chunk.is_empty or self.session.stats.backlog <= 0:
                break

    def _fetch_one(self, *, after_abort: bool) -> ElogChunk | None:
        stats = self.session.stats
        backlog_before = max(0, int(self.session.elapsed / stats.period) - stats.records_received)
        excess = max(0, backlog_before - stats.buffer_capacity)
        try:
            fetch = self.session.fetch_tail if after_abort else self.session.fetch
            chunk = fetch(max_records=self.max_records)
        except Exception as exc:
            self._fetch_failures += 1
            if isinstance(exc, ScpiError) and exc.code in (310, 323):
                # These describe unsupported functionality / invalid setup,
                # rather than a transient transport failure.
                raise
            if isinstance(exc, (_ElogErrorQueueError, ElogAlignmentError)) or self._fetch_failures >= MAX_FETCH_FAILURES:
                raise ElogFetchError("ELOG fetching failed") from exc
            self._callback(self.on_error, exc)
            self._event("fetch_error", 0)
            return None
        self._fetch_failures = 0
        if excess > self._overflow_excess:
            self._event("overflow", excess - self._overflow_excess)
            # Lost records remain in host backlog accounting forever. Only
            # growth beyond that known gap can establish a new empty backlog.
            self._empty_backlog_baseline = excess
        # Draining the buffer lowers excess, but cannot recover lost records.
        self._overflow_excess = max(self._overflow_excess, excess)
        if (chunk.is_empty and stats.backlog >
                self._empty_backlog_baseline + 2 * self.interval / stats.period):
            self._event("empty_backlog", stats.backlog)
            self._empty_backlog_baseline = stats.backlog
        if not chunk.is_empty:
            try:
                self.sink(chunk)
            except Exception as exc:
                raise ElogSinkError("ELOG sink rejected a chunk") from exc
        return chunk

    def _note_tail_incomplete(self) -> None:
        stats = self.session.stats
        tolerance = (2 * self.interval + self._abort_delay) / stats.period
        if stats.backlog > tolerance:
            count = stats.backlog
            self.session.notes.append(
                f"ELOG tail incomplete; estimated missing records {count} "
                f"exceed tolerance {tolerance:.1f} "
                f"(two fetch intervals + ABOR delay {self._abort_delay:.6g} s)"
            )
            self._event("tail_incomplete", count)

    def _drain_tail(self, *, after_abort: bool) -> None:
        deadline = time.monotonic() + 10.0
        for _ in range(16):
            if time.monotonic() >= deadline:
                break
            chunk = self._fetch_one(after_abort=after_abort)
            if chunk is not None and (chunk.is_empty or self.session.stats.backlog <= 0):
                return
        if self.session.stats.backlog > 0:
            count = self.session.stats.backlog
            self.session.notes.append(
                f"ELOG tail limit reached (16 batches / 10 s); estimated backlog {count}"
            )
            self._event("tail_limit", count)
