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

__all__ = [
    "ElogConfig",
    "ElogChunk",
    "ElogStats",
    "ElogState",
    "ElogSession",
    "ElogRecorder",
    "BUFFER_SECONDS",
    "MAX_RECORDS_PER_FETCH",
]

log = logging.getLogger(__name__)

#: ``FETCh:ELOG?`` maxrecords ceiling for a 4-channel mainframe (p.96).
MAX_RECORDS_PER_FETCH = 16_384

#: Fetch this often by default: a 5x margin against the 10-second buffer.
DEFAULT_FETCH_INTERVAL = 2.0

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
    #: ``None`` selects autoranging for that quantity.
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
    UNSUPPORTED = 310

    def _request_autorange(self, quantity: str, command: str) -> None:
        """Ask for measurement autoranging, tolerating a model that lacks it.

        Not every module has ELOG autoranging: an N6762A rejects
        ``SENS:ELOG:VOLT:RANG:AUTO ON`` with +310 while accepting the explicit
        range commands.  Autoranging is the *default* here, so letting that
        abort configure() made ordinary logging impossible on such a module --
        found only by running against real hardware.

        A refusal is therefore recorded and stepped over.  Any other error is
        still fatal: this must not become a general "ignore ELOG errors".
        """
        session = self.instrument.session
        session.write(command)
        for error in session.read_errors():
            if error.code == self.UNSUPPORTED:
                note = (
                    f"this module does not support ELOG {quantity} autoranging; "
                    f"the instrument's existing {quantity} range was used. Set an "
                    f"explicit range to control it."
                )
                log.warning("%s (%s)", note, command)
                self.notes.append(note)
                continue
            error.command = command
            raise error

    def configure(self) -> None:
        """Apply the configuration and read back what the instrument accepted."""
        session = self.instrument.session
        chan = self.chanlist
        config = self.config

        # Multi-channel fetches must be binary: ASCII allows only one channel.
        session.write("FORM REAL")
        session.write("FORM:BORD SWAP")

        for command in config.selection.scpi_commands(chan):
            session.write(command)

        if config.voltage_range is None:
            self._request_autorange("voltage", f"SENS:ELOG:VOLT:RANG:AUTO ON,{chan}")
        else:
            session.write(f"SENS:ELOG:VOLT:RANG:AUTO OFF,{chan}")
            session.write(f"SENS:ELOG:VOLT:RANG {config.voltage_range:G},{chan}")

        if config.current_range is None:
            self._request_autorange("current", f"SENS:ELOG:CURR:RANG:AUTO ON,{chan}")
        else:
            session.write(f"SENS:ELOG:CURR:RANG:AUTO OFF,{chan}")
            session.write(f"SENS:ELOG:CURR:RANG {config.current_range:G},{chan}")

        session.write(f"SENS:ELOG:PER {config.period:.9G},{chan}")
        session.write(f"TRIG:ELOG:SOUR {config.trigger_source.upper()},{chan}")
        session.check_errors("ELOG configure")

        # The instrument rounds the period to a 20.48 us grid and clamps it to
        # the parameter-count floor. Using the requested value would skew every
        # timestamp, so the accepted value is the one we keep.
        periods = session.query_floats(f"SENS:ELOG:PER? {chan}")
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
        session = self.instrument.session
        session.write(f"INIT:ELOG {self.chanlist}")
        session.check_errors("INIT:ELOG")
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
            session = self.instrument.session
            session.write(f"TRIG:ELOG {self.chanlist}")
            session.check_errors("TRIG:ELOG")
        self.mark_started()

    def start(self) -> None:
        """Initiate and trigger. Configure first."""
        self.arm()
        self.fire()

    def stop(self) -> None:
        if self.state in (ElogState.IDLE, ElogState.STOPPED):
            return
        try:
            self.instrument.session.write(f"ABOR:ELOG {self.chanlist}")
        finally:
            self.state = ElogState.STOPPED

    # -- Fetching -----------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._t0 if self.state == ElogState.RUNNING else self.stats.elapsed

    def _records_to_request(self) -> int:
        """Ask for the backlog plus headroom, within the instrument's ceiling."""
        wanted = self.stats.records_produced - self.stats.records_received
        wanted = int(wanted * 1.2) + 1
        return max(1, min(wanted, MAX_RECORDS_PER_FETCH))

    def fetch(self) -> ElogChunk:
        """Read one batch and return the records common to every channel."""
        if self.state != ElogState.RUNNING:
            raise RuntimeError(f"cannot fetch in state {self.state.value}")

        elapsed = time.monotonic() - self._t0
        self.stats.elapsed = elapsed
        request = self._records_to_request()
        raw = self.instrument.session.query_binary(
            f"FETC:ELOG? {request},{self.chanlist}"
        )
        per_channel = self.config.selection.parse_response(raw, "SWAP")
        if len(per_channel) != len(self.config.channels):
            raise RuntimeError(
                f"expected {len(self.config.channels)} data blocks, "
                f"got {len(per_channel)}"
            )

        for channel, records in zip(self.config.channels, per_channel):
            if records.shape[0]:
                self._pending[channel].append(records)

        chunk = self._take_aligned()
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
    """Runs :class:`ElogSession` on a background thread.

    The fetch loop must never be blocked by downstream work, or the 10-second
    buffer overflows.  *sink* is therefore expected to hand off quickly (a
    queue put, not a disk write).  Exceptions from *sink* are logged and the
    loop continues -- losing a chunk is better than losing the whole run.
    """

    def __init__(
        self,
        session: ElogSession,
        sink: Callable[[ElogChunk], None],
        *,
        interval: float = DEFAULT_FETCH_INTERVAL,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if interval >= BUFFER_SECONDS:
            raise ValueError(
                f"fetch interval {interval} s must stay well below the "
                f"{BUFFER_SECONDS} s instrument buffer"
            )
        self.session = session
        self.sink = sink
        self.interval = interval
        self.on_error = on_error
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("recorder is already running")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="elog-fetch", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._stop.wait(self.interval)
                self._pump()
            self._pump()  # final sweep so the tail of the run is not dropped
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("ELOG fetch loop failed")
            if self.on_error is not None:
                self.on_error(exc)

    def _pump(self) -> None:
        if self.session.state != ElogState.RUNNING:
            return
        try:
            chunks = self.session.drain()
        except Exception as exc:
            log.exception("ELOG fetch failed")
            if self.on_error is not None:
                self.on_error(exc)
            return
        for chunk in chunks:
            try:
                self.sink(chunk)
            except Exception:
                log.exception("ELOG sink rejected a chunk; continuing")
