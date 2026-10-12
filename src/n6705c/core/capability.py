"""Per-channel module capabilities.

Design rule (PLAN.md section 4): **query the instrument whenever it can tell
us**, and fall back to a model table only for capabilities the instrument does
not report but the manual documents.

Page references are to the N6705 Programmer's Reference Guide.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

__all__ = [
    "BUFFER_SECONDS",
    "ChannelCapability",
    "SMU_MODELS",
    "HISTOGRAM_MODELS",
    "RESISTANCE_MODELS",
    "ELOG_PERIOD_GRANULARITY",
    "ELOG_MIN_PERIOD",
    "ELOG_MAX_PERIOD",
    "DLOG_PERIOD_GRANULARITY",
    "DLOG_MIN_PERIOD",
    "DLOG_MAX_PERIOD",
    "DLOG_INTERLEAVED_MIN_PERIOD",
    "DLOG_MAX_FILE_BYTES",
    "min_elog_period",
    "min_dlog_period",
    "min_sample_interval",
    "max_sweep_points",
    "round_to_granularity",
]

# --- Model families -------------------------------------------------------
# N678xA SMU modules. N6783A is *not* an SMU; the manual lists it separately.
SMU_MODELS = frozenset({"N6781A", "N6782A", "N6784A", "N6785A", "N6786A"})

# Histogram requires the Data Logger option (FETCh:HISTogram:CURRent?, p.98).
HISTOGRAM_MODELS = frozenset({"N6781A", "N6782A", "N6785A", "N6786A"})

# Output resistance programming ([SOURce:]RESistance:STATe, p.452).
RESISTANCE_MODELS = frozenset({"N6781A", "N6785A"})

# --- Timing constants -----------------------------------------------------
# FETCh:ELOG? (p.96): the instrument holds this much data per channel, and
# frees space only when it is read. Overrun it and records are lost silently.
BUFFER_SECONDS = 10.0

# SENSe:ELOG:PERiod (p.192)
ELOG_MIN_PERIOD = 102.4e-6
ELOG_MAX_PERIOD = 60.0
ELOG_PERIOD_GRANULARITY = 20.48e-6

# SENSe:DLOG:PERiod (p.181) -- note the coefficient is 20.48 us, *not* the
# 102.4 us that applies to ELOG. Mixing these up is an easy mistake.
DLOG_MIN_PERIOD = 20.48e-6
DLOG_MAX_PERIOD = 60.0
DLOG_PERIOD_GRANULARITY = 20.48e-6
DLOG_INTERLEAVED_MIN_PERIOD = 75e-3  # when interleaved logging is in effect
DLOG_MAX_FILE_BYTES = 2_000_000_000  # SENSe:DLOG:TIME (p.182)

# SENSe:SWEep:TINTerval (p.211)
ACQ_MIN_TINTERVAL_SMU_1PARAM = 5.12e-6
ACQ_MIN_TINTERVAL_1_2PARAMS = 10.24e-6
ACQ_MIN_TINTERVAL_3_4PARAMS = 20.48e-6

# SENSe:SWEep:POINts (p.210): the point ceiling halves with each extra
# measured parameter. K = 1024.
_SWEEP_POINT_LIMITS = {1: 512 * 1024, 2: 256 * 1024, 3: 128 * 1024, 4: 64 * 1024}


def round_to_granularity(seconds: float, granularity: float) -> float:
    """Round to the nearest instrument increment, as the manual describes."""
    return round(seconds / granularity) * granularity


def min_elog_period(parameters: int) -> float:
    """Minimum ``SENSe:ELOG:PERiod`` for *parameters* logged per interval.

    Per p.192 the absolute floor is 102.4 us, but the achievable minimum is
    ``102.4 us x parameters`` -- so enabling min/max raises the floor
    substantially.  Example from the manual: average voltage, min voltage,
    max voltage and average current on one channel is 4 parameters, giving
    409.6 us.
    """
    if parameters < 1:
        raise ValueError("at least one parameter must be logged")
    return max(ELOG_MIN_PERIOD, ELOG_MIN_PERIOD * parameters)


def min_dlog_period(parameters: int, interleaved: bool = False) -> float:
    """Minimum ``SENSe:DLOG:PERiod`` for *parameters* logged per interval (p.181)."""
    if parameters < 1:
        raise ValueError("at least one parameter must be logged")
    floor = max(DLOG_MIN_PERIOD, DLOG_MIN_PERIOD * parameters)
    if interleaved:
        floor = max(floor, DLOG_INTERLEAVED_MIN_PERIOD)
    return floor


def min_sample_interval(parameters: int, is_smu: bool) -> float:
    """Minimum ``SENSe:SWEep:TINTerval`` for a scope acquisition (p.211).

    5.12 us is reachable only with a single parameter on an N678xA SMU.
    """
    if parameters < 1:
        raise ValueError("at least one parameter must be measured")
    if parameters == 1 and is_smu:
        return ACQ_MIN_TINTERVAL_SMU_1PARAM
    if parameters <= 2:
        return ACQ_MIN_TINTERVAL_1_2PARAMS
    return ACQ_MIN_TINTERVAL_3_4PARAMS


def max_sweep_points(parameters: int) -> int:
    """Maximum ``SENSe:SWEep:POINts`` for *parameters* measured (p.210)."""
    if parameters < 1:
        raise ValueError("at least one parameter must be measured")
    return _SWEEP_POINT_LIMITS.get(parameters, _SWEEP_POINT_LIMITS[4])


@dataclass
class ChannelCapability:
    """What one populated mainframe slot can do.

    Ranges are read back from the instrument rather than taken from a table,
    so an unrecognised module still reports usable limits.
    """

    channel: int
    model: str = ""
    serial: str = ""
    options: str = ""
    voltage_max: float = math.nan
    current_max: float = math.nan
    voltage_ranges: list[float] = field(default_factory=list)
    current_ranges: list[float] = field(default_factory=list)

    @property
    def present(self) -> bool:
        """False for an empty slot (the mainframe reports an empty model)."""
        return bool(self.model.strip())

    @property
    def is_smu(self) -> bool:
        return self.model.upper() in SMU_MODELS

    @property
    def supports_histogram(self) -> bool:
        return self.model.upper() in HISTOGRAM_MODELS

    @property
    def supports_resistance(self) -> bool:
        return self.model.upper() in RESISTANCE_MODELS

    @property
    def supports_bandwidth(self) -> bool:
        """Voltage-loop compensation ([SOURce:]VOLTage:BWIDth, p.457).

        Documented as N678xA SMU only.
        """
        return self.is_smu

    @property
    def supports_priority(self) -> bool:
        """Voltage/current priority ([SOURce:]FUNCtion) is an SMU feature."""
        return self.is_smu

    def min_sample_interval(self, parameters: int) -> float:
        return min_sample_interval(parameters, self.is_smu)

    def describe(self) -> str:
        if not self.present:
            return f"Ch{self.channel}: <empty>"
        bits = [f"Ch{self.channel}: {self.model}"]
        if self.serial:
            bits.append(f"S/N {self.serial}")
        if not math.isnan(self.voltage_max):
            bits.append(f"{self.voltage_max:g} V max")
        if not math.isnan(self.current_max):
            bits.append(f"{self.current_max:g} A max")
        if self.is_smu:
            bits.append("SMU")
        if self.options.strip():
            bits.append(f"opt {self.options.strip()}")
        return "  ".join(bits)
