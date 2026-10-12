"""The instrument's built-in data logger.

DLOG writes to a file inside the instrument instead of streaming over SCPI,
which removes the failure mode that dominates ELOG: there is no 10-second
buffer to overrun, so a controller that stalls -- or a laptop that sleeps --
cannot punch a hole in the data.  It also samples faster (20.48 us x parameters
rather than 102.4 us) and runs for up to 99,999 hours.

The cost is that **nothing can be plotted while it runs**.  ``FETCh:DLOG:*``
returns marker statistics from the front panel's viewer, not the sample
stream; the data only arrives once the run has finished and the file has been
transferred.

Which settings are per channel is not obvious and matters (p.178-185):

===================================  ===========================
``SENS:DLOG:FUNC:VOLTage`` / ``:CURRent``   per channel
``SENS:DLOG:FUNCtion:MINMax``               **global**, and covers
                                            voltage and current together
``SENS:DLOG:PERiod`` / ``:TIME`` / ``:OFFSet``   **global**
``TRIGger:DLOG:SOURce``                     **global**, defaults to IMMediate
===================================  ===========================
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .capability import (
    DLOG_INTERLEAVED_MIN_PERIOD,
    DLOG_MAX_FILE_BYTES,
    DLOG_MAX_PERIOD,
    min_dlog_period,
)
from .instrument import N6705C
from .parsing import parse_definite_block
from .scpi import format_channel_list

__all__ = [
    "DlogConfig",
    "DlogState",
    "DlogSession",
    "VALID_DLOG_SOURCES",
    "DLOG_SAMPLE_INTERVAL",
    "DLOG_MIN_DURATION",
    "DLOG_MAX_DURATION",
]

log = logging.getLogger(__name__)

#: TRIGger:DLOG:SOURce (p.537). ARSKey and OOOKey are front-panel keys and are
#: unique to this trigger system; the default here is IMMediate, not BUS.
VALID_DLOG_SOURCES = (
    "BUS",
    "IMM",
    "EXT",
    *(f"VOLT{n}" for n in range(1, 5)),
    *(f"CURR{n}" for n in range(1, 5)),
    "ARSK",  # Arb Run/Stop key
    "OOOK",  # Output On/Off keys
)

LEVEL_SOURCES = tuple(f"{kind}{n}" for kind in ("VOLT", "CURR") for n in range(1, 5))

#: Regardless of the logging period, the instrument samples this fast and
#: derives min/max/average from every sample in the interval (p.179).
DLOG_SAMPLE_INTERVAL = 20.48e-6

#: SENSe:DLOG:TIME (p.182)
DLOG_MIN_DURATION = 1.0
DLOG_MAX_DURATION = 3_599_964_000.0  # 99,999 hours

#: Bytes per logged value in the .dlog file. The format is not documented, so
#: this is an estimate used only to warn early; the instrument's own read-back
#: of the period is what actually settles it.
ESTIMATED_BYTES_PER_VALUE = 4


class DlogState(Enum):
    IDLE = "idle"
    CONFIGURED = "configured"
    ARMED = "armed"
    RUNNING = "running"
    COMPLETE = "complete"
    ABORTED = "aborted"


@dataclass
class DlogConfig:
    """One internal-datalogger run.

    Only the voltage/current enables and the ranges are per channel; the
    period, duration, pre-trigger offset, min/max and trigger source are
    instrument-wide.
    """

    channels: list[int]
    measure_voltage: bool = True
    measure_current: bool = True
    #: Global. Records min and max alongside the average, tripling the file.
    minmax: bool = False
    period: float = 0.1
    duration: float = 30.0
    #: Global. Pre-trigger data as a percentage of the whole run.
    offset_percent: int = 0
    trigger_source: str = "BUS"
    trigger_level: float = 0.0
    trigger_slope: str = "POS"
    filename: str = "internal:\\session.dlog"
    voltage_range: float | None = None
    current_range: float | None = None
    #: Interleaved logging changes several limits at once; see `validate`.
    interleaved: bool = False

    @property
    def values_per_channel(self) -> int:
        """Values written per channel per logged interval."""
        functions = int(self.measure_voltage) + int(self.measure_current)
        return functions * (3 if self.minmax else 1)

    @property
    def parameters(self) -> int:
        """Parameters the period floor is computed from."""
        return max(1, self.values_per_channel * len(self.channels))

    @property
    def minimum_period(self) -> float:
        return min_dlog_period(self.parameters, interleaved=self.interleaved)

    @property
    def normalised_source(self) -> str:
        return self.trigger_source.strip().upper()

    @property
    def needs_level(self) -> bool:
        return self.normalised_source in LEVEL_SOURCES

    @property
    def level_channel(self) -> int:
        if self.needs_level:
            return int(self.normalised_source[-1])
        return self.channels[0] if self.channels else 1

    @property
    def estimated_records(self) -> int:
        if self.period <= 0:
            return 0
        return int(self.duration / self.period)

    @property
    def estimated_bytes(self) -> int:
        """Rough file size, used to warn before the instrument quietly reacts.

        Exceeding 2 GB does not fail: the instrument lengthens the logging
        interval on its own, which is worse than an error because the run
        silently ends up at a different resolution than the one requested.
        """
        return (
            self.estimated_records
            * self.values_per_channel
            * len(self.channels)
            * ESTIMATED_BYTES_PER_VALUE
        )

    @property
    def exceeds_file_limit(self) -> bool:
        return self.estimated_bytes > DLOG_MAX_FILE_BYTES

    def validate(self) -> None:
        if not self.channels:
            raise ValueError("no channels selected for logging")
        if not (self.measure_voltage or self.measure_current):
            raise ValueError("enable voltage, current, or both")
        if self.normalised_source not in VALID_DLOG_SOURCES:
            raise ValueError(
                f"invalid data-log trigger source {self.trigger_source!r}; expected "
                f"one of {', '.join(VALID_DLOG_SOURCES)}"
            )
        if self.trigger_slope.strip().upper()[:3] not in ("POS", "NEG"):
            raise ValueError(f"trigger slope must be POS or NEG, not {self.trigger_slope!r}")
        if self.interleaved:
            # Checked before the general period floor so the message names the
            # actual constraint rather than the number it produces.
            if self.minmax:
                raise ValueError(
                    "min/max logging cannot be used with interleaved data logging"
                )
            if self.offset_percent:
                raise ValueError(
                    "the pre-trigger offset must be zero with interleaved data logging"
                )
            if self.period < DLOG_INTERLEAVED_MIN_PERIOD:
                raise ValueError(
                    f"interleaved data logging needs a period of at least "
                    f"{DLOG_INTERLEAVED_MIN_PERIOD * 1e3:.0f} ms"
                )
        floor = self.minimum_period
        if self.period < floor - 1e-12:
            raise ValueError(
                f"period {self.period * 1e6:.1f} us is below the minimum "
                f"{floor * 1e6:.1f} us for {self.parameters} logged value(s)"
            )
        if self.period > DLOG_MAX_PERIOD:
            raise ValueError(f"period {self.period} s exceeds the {DLOG_MAX_PERIOD} s maximum")
        if not DLOG_MIN_DURATION <= self.duration <= DLOG_MAX_DURATION:
            raise ValueError(
                f"duration {self.duration} s is outside "
                f"{DLOG_MIN_DURATION:g}..{DLOG_MAX_DURATION:g} s"
            )
        if not 0 <= self.offset_percent <= 100:
            raise ValueError("the pre-trigger offset must be between 0 and 100 percent")
        if not self.filename.lower().endswith(".dlog"):
            raise ValueError(
                f"the data-log filename must end in .dlog, not {self.filename!r}"
            )


class DlogSession:
    """Drives one internal-datalogger run and brings the file back."""

    def __init__(self, instrument: N6705C, config: DlogConfig) -> None:
        config.validate()
        self.instrument = instrument
        self.config = config
        self.state = DlogState.IDLE
        self.actual_period = config.period
        self.actual_duration = config.duration
        self.trigger_time_utc = ""
        self._t0 = 0.0

    @property
    def chanlist(self) -> str:
        return format_channel_list(self.config.channels)

    # -- Configuration ------------------------------------------------------

    def configure(self) -> None:
        session = self.instrument.session
        chan = self.chanlist
        config = self.config

        # Both enables default to ON on *every* channel, so configuring only
        # the selected ones would quietly log the rest as well -- a bigger file
        # than asked for, holding channels nobody wanted.
        others = [
            channel
            for channel in self.instrument.populated_channels
            if channel not in config.channels
        ]
        if others:
            excluded = format_channel_list(others)
            session.write(f"SENS:DLOG:FUNC:VOLT OFF,{excluded}")
            session.write(f"SENS:DLOG:FUNC:CURR OFF,{excluded}")
        session.write(f"SENS:DLOG:FUNC:VOLT {'ON' if config.measure_voltage else 'OFF'},{chan}")
        session.write(f"SENS:DLOG:FUNC:CURR {'ON' if config.measure_current else 'OFF'},{chan}")
        # Global: one switch, covering voltage and current on every channel.
        session.write(f"SENS:DLOG:FUNC:MINM {'ON' if config.minmax else 'OFF'}")

        if config.voltage_range is None:
            session.write(f"SENS:DLOG:VOLT:RANG:AUTO ON,{chan}")
        else:
            session.write(f"SENS:DLOG:VOLT:RANG:AUTO OFF,{chan}")
            session.write(f"SENS:DLOG:VOLT:RANG {config.voltage_range:G},{chan}")
        if config.current_range is None:
            session.write(f"SENS:DLOG:CURR:RANG:AUTO ON,{chan}")
        else:
            session.write(f"SENS:DLOG:CURR:RANG:AUTO OFF,{chan}")
            session.write(f"SENS:DLOG:CURR:RANG {config.current_range:G},{chan}")

        session.write(f"SENS:DLOG:PER {config.period:.9G}")
        session.write(f"SENS:DLOG:TIME {config.duration:.9G}")
        session.write(f"SENS:DLOG:OFFS {int(config.offset_percent)}")
        self._configure_trigger()
        session.check_errors("data logger configure")

        # The instrument lengthens the period on its own if the file would
        # exceed 2 GB, so the requested value cannot be trusted for the time
        # base -- or for telling the user what they actually got.
        self.actual_period = session.query_float("SENS:DLOG:PER?")
        self.actual_duration = session.query_float("SENS:DLOG:TIME?")
        if not math.isclose(self.actual_period, config.period, rel_tol=1e-6):
            log.warning(
                "instrument changed the data-log period from %.9g s to %.9g s "
                "(usually the 2 GB file limit)",
                config.period,
                self.actual_period,
            )
        self.state = DlogState.CONFIGURED

    def _configure_trigger(self) -> None:
        session = self.instrument.session
        config = self.config
        session.write(f"TRIG:DLOG:SOUR {config.normalised_source}")
        if not config.needs_level:
            return
        kind = "VOLT" if config.normalised_source.startswith("VOLT") else "CURR"
        level_chan = format_channel_list(config.level_channel)
        slope = config.trigger_slope.strip().upper()[:3]
        session.write(f"TRIG:DLOG:{kind} {config.trigger_level:.9G},{level_chan}")
        session.write(f"TRIG:DLOG:{kind}:SLOP {slope},{level_chan}")

    @property
    def period_was_changed(self) -> bool:
        return not math.isclose(self.actual_period, self.config.period, rel_tol=1e-6)

    # -- Run control --------------------------------------------------------

    def start(self) -> None:
        """Initiate the logger and, for a bus source, trigger it."""
        if self.state is DlogState.IDLE:
            self.configure()
        session = self.instrument.session
        session.write(f'INIT:DLOG "{self.config.filename}"')
        session.check_errors("INIT:DLOG")
        self.state = DlogState.ARMED

        if self.config.normalised_source == "BUS":
            session.write("TRIG:DLOG")
            session.check_errors("TRIG:DLOG")
        self._t0 = time.monotonic()
        self.trigger_time_utc = datetime.now(timezone.utc).isoformat()
        self.state = DlogState.RUNNING

    def abort(self) -> None:
        try:
            self.instrument.session.write("ABOR:DLOG")
        finally:
            self.state = DlogState.ABORTED

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._t0 if self._t0 else 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.actual_duration - self.elapsed)

    @property
    def progress(self) -> float:
        if self.actual_duration <= 0:
            return 0.0
        return min(1.0, self.elapsed / self.actual_duration)

    @property
    def is_complete(self) -> bool:
        """Derived from the elapsed time.

        The data logger has no documented status bit to poll, so the run
        length is the only thing to go on. Flagged for hardware verification.
        """
        if self.state is DlogState.COMPLETE:
            return True
        if self.state is not DlogState.RUNNING:
            return False
        if self.elapsed >= self.actual_duration:
            self.state = DlogState.COMPLETE
            return True
        return False

    def wait(self, poll: float = 0.5, margin: float = 2.0) -> bool:
        """Block until the run finishes, plus a small settling margin."""
        while not self.is_complete:
            time.sleep(min(poll, max(0.05, self.remaining)))
        time.sleep(margin)
        return True

    # -- Retrieval ----------------------------------------------------------

    def export_csv_on_instrument(self, remote_csv: str | None = None) -> str:
        """Ask the instrument to write a CSV next to the .dlog file.

        .. note::
           ``MMEMory:EXPort:DLOG`` exports "the data currently loaded in the
           instrument's datalog viewer".  Whether a run just finished by
           ``INITiate:DLOG`` is loaded there automatically is not stated in the
           manual; flagged for hardware verification.
        """
        target = remote_csv or self.config.filename[: -len(".dlog")] + ".csv"
        session = self.instrument.session
        session.write(f'MMEM:EXP:DLOG "{target}"')
        session.check_errors("MMEM:EXP:DLOG")
        return target

    def transfer(self, remote_path: str, local_path: Path | str) -> Path:
        """Copy a file off the instrument.

        The response is one definite-length block; a multi-hour log can be
        large, so the caller decides where it lands.
        """
        raw = self.instrument.session.query_binary(f'MMEM:DATA? "{remote_path}"')
        payload, _ = parse_definite_block(raw)
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return destination

    def delete_on_instrument(self, *remote_paths: str) -> None:
        """Remove files from the instrument, ignoring ones already gone."""
        session = self.instrument.session
        for path in remote_paths:
            session.write(f'MMEM:DEL "{path}"')
            for error in session.read_errors():
                log.debug("deleting %s: %s", path, error)

    def retrieve(
        self, local_directory: Path | str, *, cleanup: bool = True
    ) -> dict[str, Path]:
        """Export to CSV, bring both files back, then tidy the instrument.

        The instrument's file system is small and shared; leaving multi-hour
        logs on it is how the next run fails for lack of space.  Deletion only
        happens after the local copies exist.
        """
        directory = Path(local_directory)
        stem = Path(self.config.filename.replace("\\", "/")).stem
        remote_csv = self.export_csv_on_instrument()

        results: dict[str, Path] = {}
        results["csv"] = self.transfer(remote_csv, directory / f"{stem}.csv")
        try:
            results["dlog"] = self.transfer(
                self.config.filename, directory / f"{stem}.dlog"
            )
        except Exception as exc:
            # The CSV is the usable artefact; the raw file is a bonus.
            log.warning("could not transfer the raw .dlog file: %s", exc)
            self.instrument.session.read_errors()

        if cleanup:
            self.delete_on_instrument(remote_csv, self.config.filename)
        return results
