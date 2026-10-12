"""High-speed digitising acquisition (the instrument's scope mode).

Where ELOG streams for hours at 102.4 us and above, ACQuire captures a single
burst as fast as 5.12 us -- and, unlike ELOG, it can capture *before* the
trigger.  That pre-trigger window is the reason this mode exists: an inrush
surge or a protection trip is only interesting together with what led up to it.

Sequence (PLAN.md section 6.3)::

    SENS:FUNC:VOLT / :CURR        which quantities to digitise
    SENS:SWE:TINT <t>             sample interval -- then read back, it rounds
    SENS:SWE:POIN <n>             sample count
    SENS:SWE:OFFS:POIN <o>        negative = samples kept before the trigger
    TRIG:ACQ:SOUR <src>           BUS, a level on a channel, external, ...
    INIT:ACQ  ->  TRIG:ACQ
    poll STAT:OPER:COND? until MEAS-active clears
    FETC:ARR:VOLT? / FETC:ARR:CURR?

Two limits are asked of the instrument rather than assumed: the manual only
documents the point ceiling for one to four measured parameters, and a
four-channel voltage-and-current capture is eight.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

import numpy as np

from .capability import max_sweep_points, min_sample_interval
from .instrument import N6705C
from .parsing import decode_real32, split_block_list
from .scpi import format_channel_list
from .status import OperationStatus

__all__ = [
    "AcquireConfig",
    "AcquireResult",
    "AcquireState",
    "AcquireSession",
    "VALID_ACQUIRE_SOURCES",
    "LEVEL_SOURCES",
    "TINTERVAL_GRANULARITY",
]

log = logging.getLogger(__name__)

#: TRIGger:ACQuire:SOURce (p.526). Note there is no IMMediate here, unlike the
#: ELOG and transient trigger systems.
VALID_ACQUIRE_SOURCES = (
    "BUS",
    "EXT",
    *(f"PIN{n}" for n in range(1, 8)),
    *(f"VOLT{n}" for n in range(1, 5)),
    *(f"CURR{n}" for n in range(1, 5)),
    *(f"TRAN{n}" for n in range(1, 5)),
)

#: Sources that also need a level and a slope.
LEVEL_SOURCES = tuple(
    f"{kind}{n}" for kind in ("VOLT", "CURR") for n in range(1, 5)
)

#: SENSe:SWEep:TINTerval (p.211) rounds above 20.48 us to this grid.
TINTERVAL_GRANULARITY = 20.48e-6

#: SENSe:SWEep:OFFSet:POINts (p.209)
OFFSET_POINTS_MIN = -524_287
OFFSET_POINTS_MAX = 2_000_000_000


class AcquireState(Enum):
    IDLE = "idle"
    CONFIGURED = "configured"
    ARMED = "armed"
    COMPLETE = "complete"


@dataclass
class AcquireConfig:
    """One capture's settings."""

    channels: list[int]
    measure_voltage: bool = True
    measure_current: bool = True
    sample_interval: float = 20.48e-6
    points: int = 4096
    #: Negative keeps samples from *before* the trigger; positive delays the
    #: start of the capture until after it.
    offset_points: int = 0
    trigger_source: str = "BUS"
    trigger_level: float = 0.0
    trigger_slope: str = "POS"
    trigger_channel: int | None = None
    voltage_range: float | None = None
    current_range: float | None = None

    @property
    def functions(self) -> tuple[str, ...]:
        names = []
        if self.measure_voltage:
            names.append("V")
        if self.measure_current:
            names.append("I")
        return tuple(names)

    @property
    def parameters(self) -> int:
        """Total measured parameters: functions times channels."""
        return len(self.functions) * len(self.channels)

    def minimum_interval_estimate(self, is_smu: bool = False) -> float:
        """Local estimate; the instrument is asked for the real value.

        5.12 us needs a single parameter *and* an N678xA SMU, so a mixed
        selection is limited by its slowest module.
        """
        return min_sample_interval(max(1, self.parameters), is_smu=is_smu)

    @property
    def max_points_estimate(self) -> int:
        return max_sweep_points(max(1, self.parameters))

    @property
    def duration(self) -> float:
        return self.points * self.sample_interval

    @property
    def pre_trigger_points(self) -> int:
        return max(0, -self.offset_points)

    @property
    def normalised_source(self) -> str:
        return self.trigger_source.strip().upper()

    @property
    def needs_level(self) -> bool:
        return self.normalised_source in LEVEL_SOURCES

    @property
    def level_channel(self) -> int:
        """Channel whose level is watched, taken from the source mnemonic."""
        source = self.normalised_source
        if self.needs_level:
            return int(source[-1])
        return self.trigger_channel or (self.channels[0] if self.channels else 1)

    def validate(self) -> None:
        if not self.channels:
            raise ValueError("no channels selected for the capture")
        if not self.functions:
            raise ValueError(
                "no quantities selected: enable voltage, current, or both"
            )
        if self.normalised_source not in VALID_ACQUIRE_SOURCES:
            raise ValueError(
                f"invalid acquisition trigger source {self.trigger_source!r}; "
                f"expected one of {', '.join(VALID_ACQUIRE_SOURCES)}"
            )
        if self.trigger_slope.strip().upper()[:3] not in ("POS", "NEG"):
            raise ValueError(
                f"trigger slope must be POS or NEG, not {self.trigger_slope!r}"
            )
        if self.points < 1:
            raise ValueError("the capture needs at least one point")
        if self.points > self.max_points_estimate:
            raise ValueError(
                f"{self.points:,} points exceeds the {self.max_points_estimate:,} "
                f"available for {self.parameters} measured parameter(s)"
            )
        if self.sample_interval <= 0:
            raise ValueError("the sample interval must be greater than zero")
        # The pre-trigger window cannot be deeper than the capture itself.
        if self.offset_points < -(self.points - 1):
            raise ValueError(
                f"a pre-trigger window of {-self.offset_points:,} samples needs "
                f"more than {self.points:,} points; the deepest allowed is "
                f"{self.points - 1:,}"
            )
        if not OFFSET_POINTS_MIN <= self.offset_points <= OFFSET_POINTS_MAX:
            raise ValueError(
                f"offset {self.offset_points:,} is outside "
                f"{OFFSET_POINTS_MIN:,}..{OFFSET_POINTS_MAX:,}"
            )


@dataclass
class AcquireResult:
    """A completed capture."""

    channels: list[int]
    sample_interval: float
    offset_points: int
    voltage: dict[int, np.ndarray] = field(default_factory=dict)
    current: dict[int, np.ndarray] = field(default_factory=dict)
    trigger_time_utc: str = ""

    @property
    def n_points(self) -> int:
        for series in (self.voltage, self.current):
            for values in series.values():
                return int(values.size)
        return 0

    def times(self) -> np.ndarray:
        """Seconds relative to the trigger; pre-trigger samples are negative."""
        return (
            np.arange(self.n_points, dtype=np.float64) + self.offset_points
        ) * self.sample_interval

    def power(self, channel: int) -> np.ndarray | None:
        """Instantaneous power, when both quantities were captured."""
        volts = self.voltage.get(channel)
        amps = self.current.get(channel)
        if volts is None or amps is None:
            return None
        return volts * amps


class AcquireSession:
    """Configures, arms and reads back one digitised capture."""

    def __init__(self, instrument: N6705C, config: AcquireConfig) -> None:
        config.validate()
        self.instrument = instrument
        self.config = config
        self.state = AcquireState.IDLE
        self.actual_interval = config.sample_interval
        self.actual_points = config.points
        self.actual_offset = config.offset_points
        self.trigger_time_utc = ""
        #: Filled in by :meth:`configure` from the instrument's own answer.
        self.min_interval = config.minimum_interval_estimate()
        self.max_points = config.max_points_estimate

    @property
    def chanlist(self) -> str:
        return format_channel_list(self.config.channels)

    # -- Configuration ------------------------------------------------------

    def instrument_limits(self) -> tuple[float, int]:
        """Ask the instrument for the real minimum interval and point ceiling.

        The manual only tabulates the ceiling for one to four parameters, and a
        four-channel voltage-and-current capture is eight, so guessing is not
        good enough.

        Both limits depend on which sense functions are currently enabled, so
        this is only meaningful once they have been set -- :meth:`configure`
        enables them before asking.
        """
        session = self.instrument.session
        chan = self.chanlist
        minimum = session.query_floats(f"SENS:SWE:TINT? MIN,{chan}")
        maximum = session.query_floats(f"SENS:SWE:POIN? MAX,{chan}")
        session.read_errors()
        return (
            max(minimum) if minimum else self.config.minimum_interval_estimate(),
            int(min(maximum)) if maximum else self.config.max_points_estimate,
        )

    def configure(self) -> None:
        session = self.instrument.session
        chan = self.chanlist
        config = self.config

        session.write("FORM REAL")
        session.write("FORM:BORD SWAP")
        # The sense functions come first: both the point ceiling and the
        # minimum interval depend on how many parameters are being measured,
        # so asking before enabling them returns the limits for a capture we
        # are not about to make.
        session.write(f"SENS:FUNC:VOLT {'ON' if config.measure_voltage else 'OFF'},{chan}")
        session.write(f"SENS:FUNC:CURR {'ON' if config.measure_current else 'OFF'},{chan}")
        session.check_errors("acquisition sense functions")

        self.min_interval, self.max_points = self.instrument_limits()
        if config.points > self.max_points:
            raise ValueError(
                f"{config.points:,} points exceeds the {self.max_points:,} the "
                f"instrument reports for {config.parameters} measured "
                f"parameter(s) on this configuration"
            )
        if config.sample_interval < self.min_interval - 1e-15:
            raise ValueError(
                f"sample interval {config.sample_interval * 1e6:.2f} us is below "
                f"the {self.min_interval * 1e6:.2f} us the instrument reports for "
                f"{config.parameters} measured parameter(s)"
            )

        if config.voltage_range is not None:
            session.write(f"SENS:VOLT:RANG {config.voltage_range:G},{chan}")
        if config.current_range is not None:
            session.write(f"SENS:CURR:RANG {config.current_range:G},{chan}")

        session.write(f"SENS:SWE:TINT {config.sample_interval:.9G},{chan}")
        session.write(f"SENS:SWE:POIN {config.points},{chan}")
        session.write(f"SENS:SWE:OFFS:POIN {config.offset_points},{chan}")
        session.check_errors("acquisition configure")

        self._configure_trigger()
        session.check_errors("acquisition trigger configure")

        # The interval is rounded to a 20.48 us grid above 20.48 us; using the
        # requested value would put every sample at the wrong time.
        intervals = session.query_floats(f"SENS:SWE:TINT? {chan}")
        if intervals:
            if len({round(v, 12) for v in intervals}) > 1:
                raise RuntimeError(
                    f"channels disagree on the sample interval: {intervals}"
                )
            self.actual_interval = intervals[0]
            if abs(self.actual_interval - config.sample_interval) > 1e-15:
                log.info(
                    "instrument rounded the sample interval from %.9g s to %.9g s",
                    config.sample_interval,
                    self.actual_interval,
                )
        points = session.query_floats(f"SENS:SWE:POIN? {chan}")
        if points:
            self.actual_points = int(min(points))
        offsets = session.query_floats(f"SENS:SWE:OFFS:POIN? {chan}")
        if offsets:
            self.actual_offset = int(offsets[0])
        self.state = AcquireState.CONFIGURED

    def _configure_trigger(self) -> None:
        session = self.instrument.session
        config = self.config
        chan = self.chanlist
        session.write(f"TRIG:ACQ:SOUR {config.normalised_source},{chan}")
        if not config.needs_level:
            return
        # A level source watches one specific channel, named in the mnemonic.
        level_chan = format_channel_list(config.level_channel)
        kind = "VOLT" if config.normalised_source.startswith("VOLT") else "CURR"
        slope = config.trigger_slope.strip().upper()[:3]
        session.write(f"TRIG:ACQ:{kind} {config.trigger_level:.9G},{level_chan}")
        session.write(f"TRIG:ACQ:{kind}:SLOP {slope},{level_chan}")

    # -- Run control --------------------------------------------------------

    def arm(self) -> None:
        if self.state is AcquireState.IDLE:
            self.configure()
        session = self.instrument.session
        session.write(f"INIT:ACQ {self.chanlist}")
        session.check_errors("INIT:ACQ")
        self.state = AcquireState.ARMED

    def trigger(self) -> None:
        """Send a bus trigger. Other sources fire on their own event."""
        if self.state is not AcquireState.ARMED:
            self.arm()
        if self.config.normalised_source == "BUS":
            session = self.instrument.session
            session.write(f"TRIG:ACQ {self.chanlist}")
            session.check_errors("TRIG:ACQ")
        self.trigger_time_utc = datetime.now(timezone.utc).isoformat()

    def abort(self) -> None:
        try:
            self.instrument.session.write(f"ABOR:ACQ {self.chanlist}")
        finally:
            self.state = AcquireState.IDLE

    # -- Progress -----------------------------------------------------------

    def status(self) -> dict[int, OperationStatus]:
        return self.instrument.operation_status(self.config.channels)

    @property
    def is_waiting_for_trigger(self) -> bool:
        return any(
            OperationStatus.WTG_MEAS in value for value in self.status().values()
        )

    @property
    def is_complete(self) -> bool:
        """True once no channel still has its measurement system initiated."""
        if self.state is AcquireState.COMPLETE:
            return True
        if self.state is not AcquireState.ARMED:
            return False
        active = any(
            OperationStatus.MEAS_ACTIVE in value for value in self.status().values()
        )
        if not active:
            self.state = AcquireState.COMPLETE
        return not active

    def wait(self, timeout: float = 30.0, poll: float = 0.05) -> bool:
        """Block until the capture completes. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_complete:
                return True
            time.sleep(poll)
        return self.is_complete

    # -- Results ------------------------------------------------------------

    def fetch(self) -> AcquireResult:
        """Read the captured arrays back.

        The data stays valid until the next MEASure or INITiate, so this may be
        called more than once.
        """
        result = AcquireResult(
            channels=list(self.config.channels),
            sample_interval=self.actual_interval,
            offset_points=self.actual_offset,
            trigger_time_utc=self.trigger_time_utc,
        )
        if self.config.measure_voltage:
            result.voltage = self._fetch_array("VOLT")
        if self.config.measure_current:
            result.current = self._fetch_array("CURR")
        return result

    def _fetch_array(self, kind: str) -> dict[int, np.ndarray]:
        raw = self.instrument.session.query_binary(
            f"FETC:ARR:{kind}? {self.chanlist}"
        )
        blocks = split_block_list(raw)
        if len(blocks) != len(self.config.channels):
            raise RuntimeError(
                f"expected {len(self.config.channels)} {kind} blocks, got {len(blocks)}"
            )
        return {
            channel: decode_real32(block, "SWAP")
            for channel, block in zip(self.config.channels, blocks)
        }

    def capture(self, timeout: float = 30.0) -> AcquireResult:
        """Configure, arm, trigger, wait and fetch in one call."""
        self.configure()
        self.arm()
        self.trigger()
        if not self.wait(timeout):
            raise TimeoutError(
                f"the capture did not complete within {timeout} s; the trigger "
                f"source is {self.config.normalised_source}"
            )
        return self.fetch()
