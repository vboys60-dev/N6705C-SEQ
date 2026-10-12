"""A SCPI simulator for the N6705C.

Shipped inside the package rather than in ``tests/`` on purpose: real hardware
is not always available during development, so the app offers this as a Demo
mode and the whole UI and data path can be exercised without an instrument.

Command matching reuses :func:`n6705c.core.scpi.scpi_regex`, so the simulator
accepts the same short and long forms a real instrument does.

What it deliberately reproduces, because these are the behaviours most likely
to bite us (see PLAN.md sections 5.3 and 12):

* an error queue that must be drained with ``SYST:ERR?``
* values that the instrument silently rounds or clamps
* ``FORMat``/``FORMat:BORDer`` affecting the shape of fetched data
"""

from __future__ import annotations

import math
import re
import struct
import time
from collections import deque
from dataclasses import dataclass, field
import logging
from typing import Callable

from .capability import (
    BUFFER_SECONDS,
    DLOG_MAX_FILE_BYTES,
    DLOG_MIN_PERIOD,
    SMU_MODELS,
    ELOG_MAX_PERIOD,
    ELOG_PERIOD_GRANULARITY,
    min_elog_period,
)
from .delay import DEFAULT_MAX_DELAY as MAX_OUTPUT_DELAY, round_to_grid
from .scpi import parse_channel_list, scpi_regex

log = logging.getLogger(__name__)

__all__ = ["FakeN6705C", "FakeChannel", "DEFAULT_SLOTS"]

# A plausible mixed population: two SMUs and two ordinary DC modules, which
# exercises the per-channel capability logic.
DEFAULT_SLOTS: tuple[str, ...] = ("N6781A", "N6785A", "N6743B", "")


@dataclass
class FakeChannel:
    """Simulated state of one mainframe slot."""

    number: int
    model: str
    serial: str = ""
    options: str = ""
    voltage_max: float = 20.0
    current_max: float = 3.0
    voltage: float = 0.0
    current_limit: float = 1.0
    output_on: bool = False

    # Output sequencing delays (OUTP:DEL:RISE / :FALL). Stored as the
    # instrument would store them -- snapped to its stepped resolution grid --
    # so that a read-back shows what the hardware really accepted.
    delay_rise: float = 0.0
    delay_fall: float = 0.0

    # ELOG state
    elog_current: bool = True
    elog_voltage: bool = True
    elog_current_minmax: bool = False
    elog_voltage_minmax: bool = False
    elog_period: float = 0.1
    elog_initiated: bool = False
    elog_running: bool = False
    elog_t0: float = 0.0
    elog_records_emitted: int = 0
    elog_voltage_autorange: bool = False
    elog_current_autorange: bool = False
    elog_voltage_range: float = 0.0
    elog_current_range: float = 0.0
    elog_trigger_source: str = "BUS"

    # ARB / transient state
    arb_type: str = "VOLT"
    arb_shape: str = "NONE"
    arb_params: dict = field(default_factory=dict)
    arb_count: str = "1"
    arb_terminate_last: bool = False
    voltage_mode: str = "FIX"
    current_mode: str = "FIX"
    tran_trigger_source: str = "BUS"
    tran_initiated: bool = False
    tran_running: bool = False
    #: When the transient was released, for deciding it has finished.
    tran_t0: float = 0.0
    # DLOG (internal data logger) per-channel state
    dlog_voltage: bool = True
    dlog_current: bool = True
    dlog_voltage_autorange: bool = False
    dlog_current_autorange: bool = False

    # Sense wiring and loop compensation
    sense_source: str = "INT"
    voltage_bandwidth: str = "LOW"
    source_priority: str = "VOLT"
    questionable: int = 0

    # ACQuire (scope) state
    sense_voltage: bool = False
    sense_current: bool = False
    sweep_tinterval: float = 20.48e-6
    sweep_points: int = 1024
    sweep_offset: int = 0
    acq_trigger_source: str = "BUS"
    acq_trigger_level: float = 0.0
    acq_trigger_slope: str = "POS"
    acq_initiated: bool = False
    acq_triggered: bool = False
    acq_t0: float = 0.0
    seq_steps: dict = field(default_factory=dict)
    seq_count: str = "1"
    seq_terminate_last: bool = False

    def elog_parameters(self) -> int:
        n = 0
        if self.elog_current:
            n += 3 if self.elog_current_minmax else 1
        if self.elog_voltage:
            n += 3 if self.elog_voltage_minmax else 1
        return n


class FakeN6705C:
    """In-process N6705C stand-in implementing the `Transport` protocol."""

    IDN = "Keysight Technologies,N6705C,MY00000000,D.02.05"

    def __init__(self, slots: tuple[str, ...] | list[str] = DEFAULT_SLOTS) -> None:
        self.channels: dict[int, FakeChannel] = {}
        for i, model in enumerate(slots, start=1):
            volt_max, curr_max = _module_ratings(model)
            self.channels[i] = FakeChannel(
                number=i,
                model=model,
                serial=f"MY5000{i:04d}" if model else "",
                voltage_max=volt_max,
                current_max=curr_max,
            )
        self.errors: deque[tuple[int, str]] = deque()
        # A small in-memory file system, so MMEM commands behave like the
        # real thing: write, export, transfer, delete.
        self.files: dict[str, bytes] = {}
        # DLOG settings are instrument-wide, unlike the per-channel enables.
        self.dlog_minmax = False
        self.dlog_period = 0.1
        self.dlog_duration = 30.0
        self.dlog_offset = 0
        self.dlog_trigger_source = "IMM"
        self.dlog_file = ""
        self.dlog_t0 = 0.0
        self.dlog_running = False
        # Output synchronisation. Non-volatile on the real thing, so it is not
        # cleared by *RST here either.
        self.coupled_channels: list[int] = []
        self.coupling_mode = "AUTO"
        self.coupling_doffset = 0.0
        self.data_format = "ASC"
        self.byte_order = "NORM"
        self.command_log: list[str] = []
        self._handlers: list[tuple[re.Pattern[str], Callable[..., str | bytes | None]]] = []
        self._raw_handlers: list[tuple[re.Pattern[str], Callable[..., object]]] = []
        self._pending_binary: bytes | None = None
        self._register_handlers()

    # -- Transport protocol -------------------------------------------------

    @property
    def description(self) -> str:
        populated = ", ".join(c.model or "-" for c in self.channels.values())
        return f"Demo mode (simulated N6705C: {populated})"

    def write(self, command: str) -> None:
        self._dispatch(command)

    def query(self, command: str) -> str:
        result = self._dispatch(command)
        if isinstance(result, bytes):
            return result.decode("latin-1")
        return "" if result is None else result

    def query_binary(self, command: str) -> bytes:
        result = self._dispatch(command)
        if isinstance(result, bytes):
            return result
        return ("" if result is None else result).encode("latin-1") + b"\n"

    def close(self) -> None:
        return None

    # -- Dispatch -----------------------------------------------------------

    def _dispatch(self, command: str) -> str | bytes | None:
        self.command_log.append(command)
        # A message may carry several units separated by ';'. Only the last
        # response is returned, which is enough for how this app talks.
        result: str | bytes | None = None
        for unit in command.split(";"):
            unit = unit.strip()
            if unit:
                result = self._dispatch_one(unit)
        return result

    def _dispatch_one(self, unit: str) -> str | bytes | None:
        header, _, args = unit.partition(" ")
        raw = {id(h) for _, h in self._raw_handlers}
        for pattern, handler in self._handlers:
            match = pattern.match(header)
            if match:
                if id(handler) in raw:
                    return handler(args.strip(), match)  # type: ignore[call-arg]
                return handler(args.strip())
        self.errors.append((-113, "Undefined header"))
        return None

    def _on(self, spelling: str) -> Callable[[Callable[..., object]], Callable[..., object]]:
        def decorate(fn: Callable[..., object]) -> Callable[..., object]:
            self._handlers.append((scpi_regex(spelling), fn))  # type: ignore[arg-type]
            return fn

        return decorate

    def _on_raw(self, pattern: str) -> Callable[[Callable[..., object]], Callable[..., object]]:
        """Register a handler for a hand-written regex.

        Used for the ARB parameter tree, where accepting any well-formed path
        keeps the simulator from simply mirroring our own spelling: the exact
        command strings are pinned by tests against the manual instead.
        """
        compiled = re.compile(pattern + r"\s*$", re.IGNORECASE)

        def decorate(fn: Callable[..., object]) -> Callable[..., object]:
            self._handlers.append((compiled, fn))  # type: ignore[arg-type]
            self._raw_handlers.append((compiled, fn))  # type: ignore[arg-type]
            return fn

        return decorate

    # -- Helpers ------------------------------------------------------------

    #: Modules **measured** to reject ``SENS:ELOG:...:RANGe:AUTO ON`` with +310
    #: while still accepting the explicit range commands.
    #:
    #: This list is empirical, not from the manual -- the manual does not
    #: mention the restriction at all.  It is therefore certainly incomplete:
    #: only models actually tried against hardware appear here, and a model's
    #: absence means "not tested", not "supported".  Add to it when a real
    #: instrument says so, never by guessing from a datasheet.
    NO_ELOG_AUTORANGE: frozenset[str] = frozenset({"N6762A"})

    def _supports_elog_autorange(self, channel: FakeChannel) -> bool:
        return channel.model.upper() not in self.NO_ELOG_AUTORANGE

    def _channels_from(self, args: str) -> list[FakeChannel]:
        match = re.search(r"\(?@[^)]*\)?", args)
        if not match:
            self.errors.append((-109, "Missing parameter"))
            return []
        result = []
        for number in parse_channel_list(match.group()):
            channel = self.channels.get(number)
            if channel is None:
                self.errors.append((-222, "Data out of range;channel"))
                continue
            result.append(channel)
        return result

    @staticmethod
    def _value_from(args: str) -> str:
        """First comma-separated argument, i.e. everything before the chanlist."""
        return args.split(",")[0].strip()

    @staticmethod
    def _as_bool(text: str) -> bool:
        return text.strip().upper() in {"1", "ON", "TRUE"}

    def _join(self, values: list[str]) -> str:
        return ",".join(values)

    # -- Handler registration ----------------------------------------------

    def _register_handlers(self) -> None:  # noqa: C901 - a flat command table
        on = self._on

        # --- IEEE 488.2 common commands ---
        @on("*IDN?")
        def _idn(_: str) -> str:
            return self.IDN

        @on("*RST")
        def _rst(_: str) -> None:
            for channel in self.channels.values():
                channel.output_on = False
                channel.voltage = 0.0
                channel.elog_initiated = channel.elog_running = False
            self.data_format, self.byte_order = "ASC", "NORM"
            return None

        @on("*CLS")
        def _cls(_: str) -> None:
            self.errors.clear()
            return None

        @on("*OPC?")
        def _opc(_: str) -> str:
            return "1"

        @on("*TRG")
        def _trg(_: str) -> None:
            """Release every armed system whose source is BUS, at once.

            This is what lets a recording and a waveform share a start
            instant: arm both, then send one trigger.
            """
            now = time.monotonic()
            fired = False
            for channel in self.channels.values():
                if channel.elog_initiated and channel.elog_trigger_source == "BUS":
                    channel.elog_running = True
                    channel.elog_t0 = now
                    channel.elog_records_emitted = 0
                    fired = True
                if channel.tran_initiated and channel.tran_trigger_source.startswith("BUS"):
                    channel.tran_running = True
                    channel.tran_t0 = now
                    fired = True
                if channel.acq_initiated and channel.acq_trigger_source == "BUS":
                    channel.acq_triggered = True
                    channel.acq_t0 = now
                    fired = True
            if self.dlog_file and self.dlog_trigger_source == "BUS":
                self.dlog_running = True
                self.dlog_t0 = now
                fired = True
            if not fired:
                # Harmless on real hardware too: a trigger with nothing armed
                # is simply ignored.
                log.debug("*TRG with nothing armed on BUS")
            return None

        # --- SYSTem ---
        @on("SYSTem:ERRor?")
        def _syst_err(_: str) -> str:
            if not self.errors:
                return '+0,"No error"'
            code, text = self.errors.popleft()
            return f'{code:+d},"{text}"'

        @on("SYSTem:CHANnel[:COUNt]?")
        def _chan_count(_: str) -> str:
            return str(len(self.channels))

        @on("SYSTem:CHANnel:MODel?")
        def _chan_model(args: str) -> str:
            return self._join([c.model for c in self._channels_from(args)])

        @on("SYSTem:CHANnel:SERial?")
        def _chan_serial(args: str) -> str:
            return self._join([c.serial for c in self._channels_from(args)])

        @on("SYSTem:CHANnel:OPTion?")
        def _chan_option(args: str) -> str:
            return self._join([c.options for c in self._channels_from(args)])

        # --- FORMat ---
        @on("FORMat[:DATA]?")
        def _format_query(_: str) -> str:
            return self.data_format

        @on("FORMat[:DATA]")
        def _format_set(args: str) -> None:
            value = self._value_from(args).upper()
            if value.startswith("ASC"):
                self.data_format = "ASC"
            elif value.startswith("REAL"):
                self.data_format = "REAL"
            else:
                self.errors.append((-224, "Illegal parameter value"))
            return None

        @on("FORMat:BORDer?")
        def _border_query(_: str) -> str:
            return self.byte_order

        @on("FORMat:BORDer")
        def _border_set(args: str) -> None:
            value = self._value_from(args).upper()
            if value.startswith("NORM"):
                self.byte_order = "NORM"
            elif value.startswith("SWAP"):
                self.byte_order = "SWAP"
            else:
                self.errors.append((-224, "Illegal parameter value"))
            return None

        # --- OUTPut ---
        @on("OUTPut[:STATe]?")
        def _outp_query(args: str) -> str:
            return self._join(
                ["1" if c.output_on else "0" for c in self._channels_from(args)]
            )

        @on("OUTPut[:STATe]")
        def _outp_set(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            asked = self._channels_from(args)
            for channel in asked:
                channel.output_on = state
            # Coupled channels follow whenever any one of them is switched.
            if self.coupled_channels and any(
                c.number in self.coupled_channels for c in asked
            ):
                for number in self.coupled_channels:
                    other = self.channels.get(number)
                    if other is not None:
                        other.output_on = state
            return None

        # --- OUTPut:COUPle (output synchronisation) ---
        def _all_outputs_off() -> None:
            """Every coupling command drops every output (p.146/148/149)."""
            for channel in self.channels.values():
                channel.output_on = False

        @on("OUTPut[:STATe]:COUPle:CHANnel?")
        def _coup_chan_q(_: str) -> str:
            return ",".join(str(c) for c in self.coupled_channels) or "0"

        @on("OUTPut[:STATe]:COUPle:CHANnel")
        def _coup_chan(args: str) -> None:
            text = args.strip()
            channels: list[int] = []
            for part in text.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    number = int(float(part))
                except ValueError:
                    self.errors.append((-224, "Illegal parameter value"))
                    return None
                if number not in self.channels:
                    self.errors.append((-222, "Data out of range;channel"))
                    return None
                channels.append(number)
            # One coupled set per mainframe: a new one replaces the old.
            self.coupled_channels = channels
            _all_outputs_off()
            return None

        @on("OUTPut[:STATe]:COUPle:DOFFset:MODE?")
        def _coup_mode_q(_: str) -> str:
            return self.coupling_mode

        @on("OUTPut[:STATe]:COUPle:DOFFset:MODE")
        def _coup_mode(args: str) -> None:
            value = self._value_from(args).upper()
            if value.startswith("MAN"):
                self.coupling_mode = "MAN"
            elif value.startswith("AUTO"):
                self.coupling_mode = "AUTO"
            else:
                self.errors.append((-224, "Illegal parameter value"))
                return None
            _all_outputs_off()
            return None

        @on("OUTPut[:STATe]:COUPle:DOFFset?")
        def _coup_doff_q(_: str) -> str:
            return f"{self.coupling_doffset:+.6E}"

        @on("OUTPut[:STATe]:COUPle:DOFFset")
        def _coup_doff(args: str) -> None:
            try:
                value = float(self._value_from(args))
            except ValueError:
                self.errors.append((-224, "Illegal parameter value"))
                return None
            if not 0.0 <= value <= MAX_OUTPUT_DELAY:
                self.errors.append((-222, "Data out of range;delay offset"))
                return None
            self.coupling_doffset = value
            _all_outputs_off()
            return None

        @on("OUTPut[:STATe]:COUPle:MAX:DOFFset?")
        def _coup_max_doff(_: str) -> str:
            """The largest offset the installed modules need (p.151)."""
            return f"{0.03 if len(self.channels) > 1 else 0.0:+.6E}"

        # --- OUTPut:DELay (output sequencing) ---
        def _delay_query(args: str, attribute: str) -> str:
            value = self._value_from(args).upper()
            channels = self._channels_from(args)
            if value in ("MIN", "MINIMUM"):
                return self._join(["+0.000000E+00" for _ in channels])
            if value in ("MAX", "MAXIMUM"):
                return self._join([f"{MAX_OUTPUT_DELAY:+.6E}" for _ in channels])
            return self._join(
                [f"{getattr(c, attribute):+.6E}" for c in channels]
            )

        def _delay_set(args: str, attribute: str) -> None:
            try:
                requested = float(self._value_from(args))
            except ValueError:
                self.errors.append((-224, "Illegal parameter value"))
                return None
            if requested < 0 or requested > MAX_OUTPUT_DELAY:
                # Out of range is refused outright, unlike the periods which
                # are clamped -- flagged for hardware verification.
                self.errors.append((-222, "Data out of range;delay"))
                return None
            for channel in self._channels_from(args):
                setattr(channel, attribute, round_to_grid(requested))
            return None

        @on("OUTPut[:STATe]:DELay:RISE?")
        def _outp_del_rise_query(args: str) -> str:
            return _delay_query(args, "delay_rise")

        @on("OUTPut[:STATe]:DELay:RISE")
        def _outp_del_rise_set(args: str) -> None:
            return _delay_set(args, "delay_rise")

        @on("OUTPut[:STATe]:DELay:FALL?")
        def _outp_del_fall_query(args: str) -> str:
            return _delay_query(args, "delay_fall")

        @on("OUTPut[:STATe]:DELay:FALL")
        def _outp_del_fall_set(args: str) -> None:
            return _delay_set(args, "delay_fall")

        # --- SOURce levels and ranges ---
        @on("[SOURce:]VOLTage[:LEVel][:IMMediate][:AMPLitude]?")
        def _volt_query(args: str) -> str:
            return self._join(
                [f"{c.voltage:+.6E}" for c in self._channels_from(args)]
            )

        @on("[SOURce:]VOLTage[:LEVel][:IMMediate][:AMPLitude]")
        def _volt_set(args: str) -> None:
            value = self._value_from(args)
            for channel in self._channels_from(args):
                channel.voltage = self._clamp(value, 0.0, channel.voltage_max)
            return None

        @on("[SOURce:]VOLTage:RANGe?")
        def _volt_range(args: str) -> str:
            return self._join(
                [f"{c.voltage_max:+.6E}" for c in self._channels_from(args)]
            )

        @on("[SOURce:]CURRent[:LEVel][:IMMediate][:AMPLitude]?")
        def _curr_query(args: str) -> str:
            return self._join(
                [f"{c.current_limit:+.6E}" for c in self._channels_from(args)]
            )

        @on("[SOURce:]CURRent[:LEVel][:IMMediate][:AMPLitude]")
        def _curr_set(args: str) -> None:
            value = self._value_from(args)
            for channel in self._channels_from(args):
                channel.current_limit = self._clamp(value, 0.0, channel.current_max)
            return None

        @on("[SOURce:]CURRent:RANGe?")
        def _curr_range(args: str) -> str:
            return self._join(
                [f"{c.current_max:+.6E}" for c in self._channels_from(args)]
            )

        # --- MEASure ---
        @on("MEASure[:SCALar]:VOLTage[:DC]?")
        def _meas_volt(args: str) -> str:
            return self._join(
                [f"{self._simulate_voltage(c):+.6E}" for c in self._channels_from(args)]
            )

        @on("MEASure[:SCALar]:CURRent[:DC]?")
        def _meas_curr(args: str) -> str:
            return self._join(
                [f"{self._simulate_current(c):+.6E}" for c in self._channels_from(args)]
            )

        # --- STATus ---
        @on("STATus:OPERation:CONDition?")
        def _stat_cond(args: str) -> str:
            values = []
            for channel in self._channels_from(args):
                bits = 0
                if not channel.output_on:
                    bits |= 4  # OFF
                else:
                    bits |= 1  # CV
                if channel.elog_initiated:
                    bits |= 32  # MEAS-active
                    if not channel.elog_running:
                        bits |= 8  # WTG-meas
                if channel.tran_initiated:
                    if self._tran_complete(channel):
                        channel.tran_initiated = channel.tran_running = False
                    else:
                        bits |= 64  # TRAN-active
                        if not channel.tran_running:
                            bits |= 16  # WTG-tran
                if channel.acq_initiated:
                    if self._acq_complete(channel):
                        channel.acq_initiated = False
                    else:
                        bits |= 32  # MEAS-active
                        if not channel.acq_triggered:
                            bits |= 8  # WTG-meas
                values.append(str(bits))
            return self._join(values)

        # --- SENSe:ELOG ---
        @on("SENSe:ELOG:FUNCtion:VOLTage:MINMax?")
        def _elog_v_mm_q(args: str) -> str:
            return self._join(
                ["1" if c.elog_voltage_minmax else "0" for c in self._channels_from(args)]
            )

        @on("SENSe:ELOG:FUNCtion:VOLTage:MINMax")
        def _elog_v_mm(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                channel.elog_voltage_minmax = state
            return None

        @on("SENSe:ELOG:FUNCtion:CURRent:MINMax?")
        def _elog_i_mm_q(args: str) -> str:
            return self._join(
                ["1" if c.elog_current_minmax else "0" for c in self._channels_from(args)]
            )

        @on("SENSe:ELOG:FUNCtion:CURRent:MINMax")
        def _elog_i_mm(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                channel.elog_current_minmax = state
            return None

        @on("SENSe:ELOG:FUNCtion:VOLTage?")
        def _elog_v_q(args: str) -> str:
            return self._join(
                ["1" if c.elog_voltage else "0" for c in self._channels_from(args)]
            )

        @on("SENSe:ELOG:FUNCtion:VOLTage")
        def _elog_v(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                channel.elog_voltage = state
            return None

        @on("SENSe:ELOG:FUNCtion:CURRent?")
        def _elog_i_q(args: str) -> str:
            return self._join(
                ["1" if c.elog_current else "0" for c in self._channels_from(args)]
            )

        @on("SENSe:ELOG:FUNCtion:CURRent")
        def _elog_i(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                channel.elog_current = state
            return None

        @on("SENSe:ELOG:PERiod?")
        def _elog_period_q(args: str) -> str:
            return self._join(
                [f"{c.elog_period:+.6E}" for c in self._channels_from(args)]
            )

        @on("SENSe:ELOG:PERiod")
        def _elog_period(args: str) -> None:
            raw = self._value_from(args)
            for channel in self._channels_from(args):
                floor = min_elog_period(channel.elog_parameters())
                try:
                    wanted = float(raw)
                except ValueError:
                    upper = raw.upper()
                    wanted = floor if upper.startswith("MIN") else ELOG_MAX_PERIOD
                # Real hardware silently rounds and clamps -- reproduce that so
                # callers are forced to read the value back.
                clamped = min(max(wanted, floor), ELOG_MAX_PERIOD)
                channel.elog_period = (
                    round(clamped / ELOG_PERIOD_GRANULARITY) * ELOG_PERIOD_GRANULARITY
                )
            return None

        # --- ELOG ranging ---
        @on("SENSe:ELOG:VOLTage[:DC]:RANGe:AUTO")
        def _elog_v_auto(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                if state and not self._supports_elog_autorange(channel):
                    self.errors.append(
                        (310, "The command is not supported by this model; chan "
                         f"{channel.number}")
                    )
                    continue
                channel.elog_voltage_autorange = state
            return None

        @on("SENSe:ELOG:CURRent[:DC]:RANGe:AUTO")
        def _elog_i_auto(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                if state and not self._supports_elog_autorange(channel):
                    self.errors.append(
                        (310, "The command is not supported by this model")
                    )
                    continue
                channel.elog_current_autorange = state
            return None

        @on("SENSe:ELOG:VOLTage[:DC]:RANGe[:UPPer]")
        def _elog_v_range(args: str) -> None:
            value = self._value_from(args)
            for channel in self._channels_from(args):
                channel.elog_voltage_range = self._clamp(value, 0.0, channel.voltage_max)
            return None

        @on("SENSe:ELOG:CURRent[:DC]:RANGe[:UPPer]")
        def _elog_i_range(args: str) -> None:
            value = self._value_from(args)
            for channel in self._channels_from(args):
                channel.elog_current_range = self._clamp(value, 0.0, channel.current_max)
            return None

        # --- ELOG trigger / acquisition control ---
        @on("TRIGger:ELOG:SOURce?")
        def _trig_elog_source_q(args: str) -> str:
            return self._join([c.elog_trigger_source for c in self._channels_from(args)])

        @on("TRIGger:ELOG:SOURce")
        def _trig_elog_source(args: str) -> None:
            source = self._value_from(args).upper()
            known = ("BUS", "EXT", "IMM", "PIN")
            if not source.startswith(known):
                self.errors.append((-224, "Illegal parameter value"))
                return None
            for channel in self._channels_from(args):
                channel.elog_trigger_source = source[:3] if source[:3] != "PIN" else source
            return None

        @on("INITiate[:IMMediate]:ELOG")
        def _init_elog(args: str) -> None:
            now = time.monotonic()
            channels = self._channels_from(args)
            if not channels:
                return None

            # The period floor counts parameters *per interval across every
            # logged channel*, so a period that one channel sustains can be too
            # fast for three.  The instrument accepts the period anyway and only
            # objects here, at INIT -- reproduce that timing, because code that
            # validates at set-time would otherwise look correct against the
            # simulator and still fail on hardware.
            total = sum(c.elog_parameters() for c in channels)
            floor = min_elog_period(total)
            period = min((c.elog_period for c in channels), default=floor)
            if period < floor - 1e-12:
                self.errors.append(
                    (323, "Meas TINTerval too small on too many channels")
                )
                return None

            for channel in channels:
                channel.elog_initiated = True
                channel.elog_records_emitted = 0
                # An IMMediate source fires as soon as the log is initiated.
                channel.elog_running = channel.elog_trigger_source == "IMM"
                channel.elog_t0 = now
            return None

        @on("TRIGger:ELOG[:IMMediate]")
        def _trig_elog(args: str) -> None:
            now = time.monotonic()
            for channel in self._channels_from(args):
                if not channel.elog_initiated:
                    self.errors.append((-213, "Init ignored"))
                    continue
                channel.elog_running = True
                channel.elog_t0 = now
                channel.elog_records_emitted = 0
            return None

        @on("ABORt:ELOG")
        def _abort_elog(args: str) -> None:
            for channel in self._channels_from(args):
                channel.elog_initiated = channel.elog_running = False
            return None

        # --- DLOG (internal data logger) ---
        @on("SENSe:DLOG:FUNCtion:VOLTage")
        def _dlog_volt(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                channel.dlog_voltage = state
            return None

        @on("SENSe:DLOG:FUNCtion:CURRent")
        def _dlog_curr(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                channel.dlog_current = state
            return None

        @on("SENSe:DLOG:FUNCtion:MINMax?")
        def _dlog_minmax_q(_: str) -> str:
            return "1" if self.dlog_minmax else "0"

        @on("SENSe:DLOG:FUNCtion:MINMax")
        def _dlog_minmax(args: str) -> None:
            # Global, and it covers voltage and current on every channel.
            self.dlog_minmax = self._as_bool(self._value_from(args))
            return None

        @on("SENSe:DLOG:VOLTage[:DC]:RANGe:AUTO")
        def _dlog_v_auto(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                channel.dlog_voltage_autorange = state
            return None

        @on("SENSe:DLOG:CURRent[:DC]:RANGe:AUTO")
        def _dlog_i_auto(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                channel.dlog_current_autorange = state
            return None

        @on("SENSe:DLOG:VOLTage[:DC]:RANGe[:UPPer]")
        def _dlog_v_range(args: str) -> None:
            return None

        @on("SENSe:DLOG:CURRent[:DC]:RANGe[:UPPer]")
        def _dlog_i_range(args: str) -> None:
            return None

        @on("SENSe:DLOG:PERiod?")
        def _dlog_period_q(_: str) -> str:
            return f"{self.dlog_period:+.6E}"

        @on("SENSe:DLOG:PERiod")
        def _dlog_period(args: str) -> None:
            try:
                wanted = float(self._value_from(args))
            except ValueError:
                self.errors.append((-224, "Illegal parameter value"))
                return None
            self.dlog_period = max(
                DLOG_MIN_PERIOD * self._dlog_parameters(), min(wanted, 60.0)
            )
            self._enforce_dlog_file_limit()
            return None

        @on("SENSe:DLOG:TIME?")
        def _dlog_time_q(_: str) -> str:
            return f"{self.dlog_duration:+.6E}"

        @on("SENSe:DLOG:TIME")
        def _dlog_time(args: str) -> None:
            try:
                self.dlog_duration = max(1.0, float(self._value_from(args)))
            except ValueError:
                self.errors.append((-224, "Illegal parameter value"))
                return None
            self._enforce_dlog_file_limit()
            return None

        @on("SENSe:DLOG:OFFSet?")
        def _dlog_offset_q(_: str) -> str:
            return str(self.dlog_offset)

        @on("SENSe:DLOG:OFFSet")
        def _dlog_offset(args: str) -> None:
            try:
                value = int(float(self._value_from(args)))
            except ValueError:
                self.errors.append((-224, "Illegal parameter value"))
                return None
            if not 0 <= value <= 100:
                self.errors.append((-222, "Data out of range"))
                return None
            self.dlog_offset = value
            return None

        @on("TRIGger:DLOG:SOURce?")
        def _dlog_source_q(_: str) -> str:
            return self.dlog_trigger_source

        @on("TRIGger:DLOG:SOURce")
        def _dlog_source(args: str) -> None:
            source = self._value_from(args).upper()
            if not source.startswith(("BUS", "IMM", "EXT", "VOLT", "CURR", "ARSK", "OOOK")):
                self.errors.append((-224, "Illegal parameter value"))
                return None
            self.dlog_trigger_source = source
            return None

        @on("TRIGger:DLOG:VOLTage[:LEVel]")
        def _dlog_vlevel(args: str) -> None:
            return None

        @on("TRIGger:DLOG:CURRent[:LEVel]")
        def _dlog_ilevel(args: str) -> None:
            return None

        @on("TRIGger:DLOG:VOLTage:SLOPe")
        def _dlog_vslope(args: str) -> None:
            return None

        @on("TRIGger:DLOG:CURRent:SLOPe")
        def _dlog_islope(args: str) -> None:
            return None

        @on("INITiate[:IMMediate]:DLOG")
        def _init_dlog(args: str) -> None:
            name = args.strip().strip('"').strip("'")
            if not name.lower().endswith(".dlog"):
                self.errors.append((-224, "Illegal parameter value;expected .dlog"))
                return None
            self.dlog_file = name
            self.dlog_t0 = time.monotonic()
            self.dlog_running = self.dlog_trigger_source == "IMM"
            return None

        @on("TRIGger:DLOG[:IMMediate]")
        def _trig_dlog(_: str) -> None:
            if not self.dlog_file:
                self.errors.append((-213, "Init ignored"))
                return None
            self.dlog_running = True
            self.dlog_t0 = time.monotonic()
            return None

        @on("ABORt:DLOG")
        def _abort_dlog(_: str) -> None:
            self.dlog_running = False
            self._write_dlog_file()
            return None

        @on("MMEMory:EXPort:DLOG")
        def _export_dlog(args: str) -> None:
            target = args.strip().strip('"').strip("'")
            self._write_dlog_file()
            if not self.dlog_file:
                self.errors.append((-221, "Settings conflict;no data log"))
                return None
            self.files[target] = self._dlog_csv()
            return None

        @on("MMEMory:DATA[:DEFinite]?")
        def _mmem_data_q(args: str) -> bytes:
            name = args.strip().strip('"').strip("'")
            payload = self.files.get(name)
            if payload is None:
                self.errors.append((-256, "File name not found"))
                return b"#10\n"
            header = str(len(payload)).encode()
            return b"#" + str(len(header)).encode() + header + payload + b"\n"

        @on("MMEMory:DELete")
        def _mmem_delete(args: str) -> None:
            name = args.strip().strip('"').strip("'")
            if self.files.pop(name, None) is None:
                self.errors.append((-256, "File name not found"))
            return None

        # --- Sense wiring / bandwidth ---
        @on("[SOURce:]VOLTage:SENSe:SOURce?")
        def _sense_source_q(args: str) -> str:
            return self._join([c.sense_source for c in self._channels_from(args)])

        @on("[SOURce:]VOLTage:SENSe:SOURce")
        def _sense_source(args: str) -> None:
            value = self._value_from(args).upper()[:3]
            if value not in ("INT", "EXT"):
                self.errors.append((-224, "Illegal parameter value"))
                return None
            for channel in self._channels_from(args):
                channel.sense_source = value
                # Dropping back to local sense while a HIGH bandwidth is
                # selected is a settings conflict, exactly as on the hardware.
                if value == "INT" and channel.voltage_bandwidth != "LOW":
                    self.errors.append(
                        (-221, "Settings conflict;HIGH bandwidth needs remote sense")
                    )
            return None

        @on("[SOURce:]VOLTage:BWIDth?")
        def _bandwidth_q(args: str) -> str:
            return self._join([c.voltage_bandwidth for c in self._channels_from(args)])

        @on("[SOURce:]VOLTage:BWIDth")
        def _bandwidth(args: str) -> None:
            raw = self._value_from(args).upper()
            aliases = {"DEF": "LOW", "DEFAULT": "LOW", "FAST1": "HIGH1",
                       "FAST2": "HIGH2", "FAST3": "HIGH3"}
            value = aliases.get(raw, raw)
            if value not in ("LOW", "HIGH1", "HIGH2", "HIGH3"):
                self.errors.append((-224, "Illegal parameter value"))
                return None
            for channel in self._channels_from(args):
                if channel.model.upper() not in SMU_MODELS:
                    self.errors.append((-241, "Hardware missing;not an N678xA SMU"))
                    continue
                if value != "LOW" and channel.sense_source != "EXT":
                    self.errors.append(
                        (-221, "Settings conflict;HIGH bandwidth needs remote sense")
                    )
                    continue
                channel.voltage_bandwidth = value
            return None

        @on("[SOURce:]FUNCtion?")
        def _priority_q(args: str) -> str:
            return self._join([c.source_priority for c in self._channels_from(args)])

        @on("[SOURce:]FUNCtion")
        def _priority(args: str) -> None:
            value = self._value_from(args).upper()[:4]
            if value not in ("VOLT", "CURR"):
                self.errors.append((-224, "Illegal parameter value"))
                return None
            for channel in self._channels_from(args):
                if channel.model.upper() not in SMU_MODELS:
                    self.errors.append((-241, "Hardware missing;not an N678xA SMU"))
                    continue
                channel.source_priority = value
            return None

        @on("STATus:QUEStionable:CONDition?")
        def _quest_cond(args: str) -> str:
            return self._join([str(c.questionable) for c in self._channels_from(args)])

        @on("OUTPut:PROTection:CLEar")
        def _prot_clear(args: str) -> None:
            for channel in self._channels_from(args):
                channel.questionable = 0
            return None

        # --- ACQuire (scope) ---
        @on("SENSe:FUNCtion:VOLTage")
        def _sense_volt(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                channel.sense_voltage = state
            return None

        @on("SENSe:FUNCtion:CURRent")
        def _sense_curr(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                channel.sense_current = state
            return None

        @on("SENSe:SWEep:TINTerval?")
        def _tint_q(args: str) -> str:
            if "MIN" in args.upper():
                out = []
                for channel in self._channels_from(args):
                    out.append(f"{self._min_tinterval(channel):+.6E}")
                return self._join(out)
            return self._join(
                [f"{c.sweep_tinterval:+.6E}" for c in self._channels_from(args)]
            )

        @on("SENSe:SWEep:TINTerval")
        def _tint(args: str) -> None:
            raw = self._value_from(args)
            for channel in self._channels_from(args):
                floor = self._min_tinterval(channel)
                try:
                    wanted = float(raw)
                except ValueError:
                    wanted = floor if raw.upper().startswith("MIN") else 40_000.0
                wanted = max(wanted, floor)
                # Above 20.48 us the instrument snaps to a 20.48 us grid.
                if wanted > 20.48e-6:
                    wanted = round(wanted / 20.48e-6) * 20.48e-6
                channel.sweep_tinterval = wanted
            return None

        @on("SENSe:SWEep:POINts?")
        def _points_q(args: str) -> str:
            if "MAX" in args.upper():
                return self._join(
                    [str(self._max_points(c)) for c in self._channels_from(args)]
                )
            return self._join([str(c.sweep_points) for c in self._channels_from(args)])

        @on("SENSe:SWEep:POINts")
        def _points(args: str) -> None:
            raw = self._value_from(args)
            for channel in self._channels_from(args):
                ceiling = self._max_points(channel)
                try:
                    wanted = int(float(raw))
                except ValueError:
                    wanted = ceiling if raw.upper().startswith("MAX") else 1
                if wanted > ceiling:
                    self.errors.append((-222, "Data out of range"))
                    wanted = ceiling
                channel.sweep_points = max(1, wanted)
            return None

        @on("SENSe:SWEep:OFFSet:POINts?")
        def _offset_q(args: str) -> str:
            return self._join([str(c.sweep_offset) for c in self._channels_from(args)])

        @on("SENSe:SWEep:OFFSet:POINts")
        def _offset(args: str) -> None:
            try:
                value = int(float(self._value_from(args)))
            except ValueError:
                self.errors.append((-224, "Illegal parameter value"))
                return None
            for channel in self._channels_from(args):
                if value < -(channel.sweep_points - 1):
                    self.errors.append((-222, "Data out of range;offset"))
                    value = -(channel.sweep_points - 1)
                channel.sweep_offset = value
            return None

        @on("TRIGger:ACQuire:SOURce?")
        def _acq_source_q(args: str) -> str:
            return self._join([c.acq_trigger_source for c in self._channels_from(args)])

        @on("TRIGger:ACQuire:SOURce")
        def _acq_source(args: str) -> None:
            source = self._value_from(args).upper()
            # Note: no IMMediate here, unlike the ELOG and transient systems.
            if not source.startswith(("BUS", "EXT", "PIN", "VOLT", "CURR", "TRAN")):
                self.errors.append((-224, "Illegal parameter value"))
                return None
            for channel in self._channels_from(args):
                channel.acq_trigger_source = source
            return None

        @on("TRIGger:ACQuire:VOLTage[:LEVel]")
        def _acq_vlevel(args: str) -> None:
            value = self._value_from(args)
            for channel in self._channels_from(args):
                channel.acq_trigger_level = self._clamp(value, 0.0, channel.voltage_max)
            return None

        @on("TRIGger:ACQuire:CURRent[:LEVel]")
        def _acq_ilevel(args: str) -> None:
            value = self._value_from(args)
            for channel in self._channels_from(args):
                channel.acq_trigger_level = self._clamp(value, 0.0, channel.current_max)
            return None

        @on("TRIGger:ACQuire:VOLTage:SLOPe")
        def _acq_vslope(args: str) -> None:
            return self._set_slope(args)

        @on("TRIGger:ACQuire:CURRent:SLOPe")
        def _acq_islope(args: str) -> None:
            return self._set_slope(args)

        @on("INITiate[:IMMediate]:ACQuire")
        def _init_acq(args: str) -> None:
            now = time.monotonic()
            for channel in self._channels_from(args):
                if not (channel.sense_voltage or channel.sense_current):
                    self.errors.append((-221, "Settings conflict;no sense function"))
                    continue
                channel.acq_initiated = True
                channel.acq_triggered = False
                channel.acq_t0 = now
            return None

        @on("TRIGger:ACQuire[:IMMediate]")
        def _trig_acq(args: str) -> None:
            now = time.monotonic()
            for channel in self._channels_from(args):
                if not channel.acq_initiated:
                    self.errors.append((-213, "Init ignored"))
                    continue
                channel.acq_triggered = True
                channel.acq_t0 = now
            return None

        @on("ABORt:ACQuire")
        def _abort_acq(args: str) -> None:
            for channel in self._channels_from(args):
                channel.acq_initiated = channel.acq_triggered = False
            return None

        @on("FETCh:ARRay:VOLTage[:DC]?")
        def _fetch_arr_volt(args: str) -> bytes:
            return self._fetch_array(args, "VOLT")

        @on("FETCh:ARRay:CURRent[:DC]?")
        def _fetch_arr_curr(args: str) -> bytes:
            return self._fetch_array(args, "CURR")

        # --- ARB sequences ---
        @on("[SOURce:]ARB:SEQuence:RESet")
        def _seq_reset(args: str) -> None:
            for channel in self._channels_from(args):
                channel.seq_steps.clear()
            return None

        @on("[SOURce:]ARB:SEQuence:LENgth?")
        def _seq_length(args: str) -> str:
            return self._join([str(len(c.seq_steps)) for c in self._channels_from(args)])

        @on("[SOURce:]ARB:SEQuence:QUALity?")
        def _seq_quality(args: str) -> str:
            out = []
            for channel in self._channels_from(args):
                curved = sum(
                    1
                    for step in channel.seq_steps.values()
                    if step.get("shape") in ("RAMP", "TRAP", "EXP", "SIN")
                )
                quality = 100 if curved == 0 else max(16, min(100, 511 // curved))
                out.append(str(quality))
            return self._join(out)

        @on("[SOURce:]ARB:SEQuence:COUNt?")
        def _seq_count_q(args: str) -> str:
            return self._join([c.seq_count for c in self._channels_from(args)])

        @on("[SOURce:]ARB:SEQuence:COUNt")
        def _seq_count(args: str) -> None:
            value = self._value_from(args).upper()
            for channel in self._channels_from(args):
                channel.seq_count = "INF" if value.startswith("INF") else value
            return None

        @on("[SOURce:]ARB:SEQuence:TERMinate:LAST")
        def _seq_term(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                channel.seq_terminate_last = state
            return None

        @on("[SOURce:]ARB:SEQuence:STEP:FUNCtion:SHAPe")
        def _seq_step_shape(args: str) -> None:
            head = args.split("(@")[0].rstrip(", ").split(",")
            if len(head) < 2:
                self.errors.append((-109, "Missing parameter"))
                return None
            shape = head[0].strip().upper()
            try:
                index = int(float(head[1]))
            except ValueError:
                self.errors.append((-224, "Illegal parameter value"))
                return None
            if shape.startswith("CDW"):
                self.errors.append((-224, "Illegal parameter value;CDW not a sequence step"))
                return None
            for channel in self._channels_from(args):
                if len(channel.seq_steps) >= 100 and index not in channel.seq_steps:
                    self.errors.append((-222, "Data out of range;too many steps"))
                    continue
                channel.seq_steps.setdefault(index, {})["shape"] = shape
            return None

        @on("[SOURce:]ARB:SEQuence:STEP:PACing")
        def _seq_step_pacing(args: str) -> None:
            head = args.split("(@")[0].rstrip(", ").split(",")
            pacing = head[0].strip().upper()[:4]
            index = int(float(head[1])) if len(head) > 1 else 1
            if pacing not in ("DWEL", "TRIG"):
                self.errors.append((-224, "Illegal parameter value"))
                return None
            for channel in self._channels_from(args):
                channel.seq_steps.setdefault(index, {})["pacing"] = pacing
            return None

        @on("[SOURce:]ARB:SEQuence:STEP:COUNt")
        def _seq_step_count(args: str) -> None:
            head = args.split("(@")[0].rstrip(", ").split(",")
            value = head[0].strip().upper()
            index = int(float(head[1])) if len(head) > 1 else 1
            for channel in self._channels_from(args):
                channel.seq_steps.setdefault(index, {})["count"] = value
            return None

        @self._on_raw(
            r"\s*:?(?:SOUR(?:CE)?:)?ARB:SEQ(?:UENCE)?:STEP:(VOLT(?:AGE)?|CURR(?:ENT)?):([A-Z:]+)"
        )
        def _seq_step_param(args: str, match) -> None:
            kind = "CURR" if match.group(1).upper().startswith("C") else "VOLT"
            path = match.group(2).upper().rstrip(":")
            head = args.split("(@")[0].rstrip(", ").split(",")
            if len(head) < 2:
                self.errors.append((-109, "Missing parameter"))
                return None
            try:
                index = int(float(head[-1]))
                values = [float(v) for v in head[:-1] if v.strip()]
            except ValueError:
                self.errors.append((-224, "Illegal parameter value"))
                return None
            for channel in self._channels_from(args):
                step = channel.seq_steps.setdefault(index, {})
                step.setdefault("params", {})[(kind, path)] = (
                    values if len(values) > 1 else values[0]
                )
            return None

        # --- ARB / transient ---
        @on("[SOURce:]ARB:FUNCtion:TYPE?")
        def _arb_type_q(args: str) -> str:
            return self._join([c.arb_type for c in self._channels_from(args)])

        @on("[SOURce:]ARB:FUNCtion:TYPE")
        def _arb_type(args: str) -> None:
            value = self._value_from(args).upper()[:4]
            if value not in ("VOLT", "CURR"):
                self.errors.append((-224, "Illegal parameter value"))
                return None
            for channel in self._channels_from(args):
                channel.arb_type = value
            return None

        @on("[SOURce:]ARB:FUNCtion:SHAPe?")
        def _arb_shape_q(args: str) -> str:
            return self._join([c.arb_shape for c in self._channels_from(args)])

        @on("[SOURce:]ARB:FUNCtion:SHAPe")
        def _arb_shape(args: str) -> None:
            value = self._value_from(args).upper()
            known = ("STEP", "RAMP", "STA", "SIN", "PULS", "TRAP", "EXP",
                     "UDEF", "CDW", "SEQ", "NONE")
            if not value.startswith(known):
                self.errors.append((-224, "Illegal parameter value"))
                return None
            for channel in self._channels_from(args):
                channel.arb_shape = value[:4] if value[:4] in known else value
                channel.arb_shape = next((k for k in known if value.startswith(k)), value)
            return None

        @on("[SOURce:]ARB:COUNt?")
        def _arb_count_q(args: str) -> str:
            return self._join([c.arb_count for c in self._channels_from(args)])

        @on("[SOURce:]ARB:COUNt")
        def _arb_count(args: str) -> None:
            value = self._value_from(args).upper()
            for channel in self._channels_from(args):
                if value.startswith("INF"):
                    channel.arb_count = "INF"
                else:
                    try:
                        channel.arb_count = str(int(float(value)))
                    except ValueError:
                        self.errors.append((-224, "Illegal parameter value"))
            return None

        @on("[SOURce:]ARB:TERMinate:LAST?")
        def _arb_term_q(args: str) -> str:
            return self._join(
                ["1" if c.arb_terminate_last else "0" for c in self._channels_from(args)]
            )

        @on("[SOURce:]ARB:TERMinate:LAST")
        def _arb_term(args: str) -> None:
            state = self._as_bool(self._value_from(args))
            for channel in self._channels_from(args):
                channel.arb_terminate_last = state
            return None

        # Any ARB:VOLT:... / ARB:CURR:... parameter path is stored verbatim.
        @self._on_raw(r"\s*:?(?:SOUR(?:CE)?:)?ARB:(VOLT(?:AGE)?|CURR(?:ENT)?):([A-Z:]+)\?")
        def _arb_param_q(args: str, match) -> str:
            kind = "CURR" if match.group(1).upper().startswith("C") else "VOLT"
            path = match.group(2).upper().rstrip(":")
            if path.endswith("POIN"):
                base = path[: -len(":POIN")] if path.endswith(":POIN") else path
                out = []
                for channel in self._channels_from(args):
                    stored = channel.arb_params.get((kind, base), [])
                    out.append(str(len(stored) if isinstance(stored, list) else 1))
                return self._join(out)
            out = []
            for channel in self._channels_from(args):
                stored = channel.arb_params.get((kind, path), 0.0)
                if isinstance(stored, list):
                    out.append(",".join(f"{v:+.6E}" for v in stored))
                else:
                    out.append(f"{stored:+.6E}")
            return self._join(out)

        @self._on_raw(r"\s*:?(?:SOUR(?:CE)?:)?ARB:(VOLT(?:AGE)?|CURR(?:ENT)?):([A-Z:]+)")
        def _arb_param(args: str, match) -> None:
            kind = "CURR" if match.group(1).upper().startswith("C") else "VOLT"
            path = match.group(2).upper().rstrip(":")
            head = args.split("(@")[0].rstrip(", ")
            try:
                values = [float(part) for part in head.split(",") if part.strip()]
            except ValueError:
                self.errors.append((-224, "Illegal parameter value"))
                return None
            if not values:
                self.errors.append((-109, "Missing parameter"))
                return None

            is_time = path.endswith(("TIM", "TIME", "TCON", "NST", "FREQ", "DWEL"))
            for channel in self._channels_from(args):
                ceiling = channel.voltage_max if kind == "VOLT" else channel.current_max
                stored = []
                for value in values:
                    if not is_time and value > ceiling:
                        self.errors.append((-222, "Data out of range"))
                        value = ceiling
                    stored.append(value)
                channel.arb_params[(kind, path)] = stored if len(stored) > 1 else stored[0]
                # Voltage and current constant-dwell ARBs share one set of
                # settings, so programming one resets the other (p.324).
                if path.startswith("CDW"):
                    other = "CURR" if kind == "VOLT" else "VOLT"
                    channel.arb_params.pop((other, path), None)
            return None

        @on("[SOURce:]VOLTage:MODE")
        def _volt_mode(args: str) -> None:
            for channel in self._channels_from(args):
                channel.voltage_mode = self._value_from(args).upper()[:4]
            return None

        @on("[SOURce:]VOLTage:MODE?")
        def _volt_mode_q(args: str) -> str:
            return self._join([c.voltage_mode for c in self._channels_from(args)])

        @on("[SOURce:]CURRent:MODE")
        def _curr_mode(args: str) -> None:
            for channel in self._channels_from(args):
                channel.current_mode = self._value_from(args).upper()[:4]
            return None

        @on("[SOURce:]CURRent:MODE?")
        def _curr_mode_q(args: str) -> str:
            return self._join([c.current_mode for c in self._channels_from(args)])

        @on("TRIGger:TRANsient:SOURce?")
        def _tran_source_q(args: str) -> str:
            return self._join([c.tran_trigger_source for c in self._channels_from(args)])

        @on("TRIGger:TRANsient:SOURce")
        def _tran_source(args: str) -> None:
            source = self._value_from(args).upper()
            if not source.startswith(("BUS", "EXT", "IMM", "PIN", "TRAN")):
                self.errors.append((-224, "Illegal parameter value"))
                return None
            for channel in self._channels_from(args):
                channel.tran_trigger_source = source
            return None

        @on("INITiate[:IMMediate]:TRANsient")
        def _init_tran(args: str) -> None:
            for channel in self._channels_from(args):
                if channel.arb_shape == "NONE":
                    self.errors.append((-221, "Settings conflict;no ARB selected"))
                    continue
                channel.tran_initiated = True
                channel.tran_running = channel.tran_trigger_source.startswith("IMM")
                channel.tran_t0 = time.monotonic()
            return None

        @on("TRIGger:TRANsient[:IMMediate]")
        def _trig_tran(args: str) -> None:
            for channel in self._channels_from(args):
                if not channel.tran_initiated:
                    self.errors.append((-213, "Init ignored"))
                    continue
                channel.tran_running = True
                channel.tran_t0 = time.monotonic()
            return None

        @on("ABORt:TRANsient")
        def _abort_tran(args: str) -> None:
            for channel in self._channels_from(args):
                channel.tran_initiated = channel.tran_running = False
            return None

        @on("FETCh:ELOG?")
        def _fetch_elog(args: str) -> str | bytes:
            return self._fetch_elog(args)

    # -- Simulation ---------------------------------------------------------

    # -- DLOG helpers -------------------------------------------------------

    def _dlog_parameters(self) -> int:
        """Values written per interval across every logging channel."""
        per_channel = 3 if self.dlog_minmax else 1
        total = 0
        for channel in self.channels.values():
            functions = int(channel.dlog_voltage) + int(channel.dlog_current)
            total += functions * per_channel
        return max(1, total)

    def _enforce_dlog_file_limit(self) -> None:
        """Lengthen the interval rather than exceed 2 GB, as the manual says."""
        values = self._dlog_parameters()
        records = self.dlog_duration / self.dlog_period if self.dlog_period else 0
        size = records * values * 4
        if size > DLOG_MAX_FILE_BYTES and records > 0:
            factor = size / DLOG_MAX_FILE_BYTES
            self.dlog_period *= factor

    def _write_dlog_file(self) -> None:
        if self.dlog_file:
            self.files.setdefault(self.dlog_file, b"\x00DLOG-BINARY-PLACEHOLDER")

    def _dlog_csv(self) -> bytes:
        """A CSV shaped like the instrument's export, for the demo path."""
        channels = [
            c for c in self.channels.values() if c.model and (c.dlog_voltage or c.dlog_current)
        ]
        header = ["Time (s)"]
        for channel in channels:
            for name, enabled in (("V", channel.dlog_voltage), ("I", channel.dlog_current)):
                if not enabled:
                    continue
                if self.dlog_minmax:
                    header += [
                        f"CH{channel.number} {name} avg",
                        f"CH{channel.number} {name} min",
                        f"CH{channel.number} {name} max",
                    ]
                else:
                    header.append(f"CH{channel.number} {name} avg")

        elapsed = min(self.dlog_duration, max(0.0, time.monotonic() - self.dlog_t0))
        count = max(1, min(int(elapsed / self.dlog_period) if self.dlog_period else 1, 20_000))
        lines = [",".join(header)]
        for index in range(count):
            t = index * self.dlog_period
            row = [f"{t:.6f}"]
            for channel in channels:
                for name, enabled in (
                    ("V", channel.dlog_voltage),
                    ("I", channel.dlog_current),
                ):
                    if not enabled:
                        continue
                    value = (
                        self._elog_voltage_at(channel, t)
                        if name == "V"
                        else self._elog_current_at(channel, t)
                    )
                    if self.dlog_minmax:
                        row += [f"{value:.6f}", f"{value * 0.98:.6f}", f"{value * 1.02:.6f}"]
                    else:
                        row.append(f"{value:.6f}")
            lines.append(",".join(row))
        return ("\n".join(lines) + "\n").encode()

    def _set_slope(self, args: str) -> None:
        slope = self._value_from(args).upper()[:3]
        if slope not in ("POS", "NEG"):
            self.errors.append((-224, "Illegal parameter value"))
            return None
        for channel in self._channels_from(args):
            channel.acq_trigger_slope = slope
        return None

    def _sense_parameters(self, channel: FakeChannel) -> int:
        """Parameters this channel contributes to the sweep limits."""
        return int(channel.sense_voltage) + int(channel.sense_current)

    def _active_parameters(self) -> int:
        return max(1, sum(self._sense_parameters(c) for c in self.channels.values()))

    def _min_tinterval(self, channel: FakeChannel) -> float:
        parameters = self._active_parameters()
        if parameters == 1 and channel.model.upper() in SMU_MODELS:
            return 5.12e-6
        return 10.24e-6 if parameters <= 2 else 20.48e-6

    def _max_points(self, channel: FakeChannel) -> int:
        """512K halving with each extra measured parameter (p.210)."""
        parameters = self._active_parameters()
        return max(1, (512 * 1024) >> max(0, parameters - 1))

    def _tran_complete(self, channel: FakeChannel) -> bool:
        """Whether a finite waveform has played out.

        Without this the TRAN-active bit would stay set forever and nothing
        could ever notice a run finishing -- which is most of what the UI
        polls for. The duration is worked out by rebuilding the waveform from
        the parameters that were sent, using the same model classes that
        generated them, so the simulator cannot disagree with the app about
        how long a shape lasts.
        """
        if not channel.tran_running or not channel.tran_t0:
            return False
        # A sequence carries its repeats in ARB:SEQ:COUNt and folds them into
        # its own duration; a single waveform uses ARB:COUNt.
        is_sequence = channel.arb_shape == "SEQ"
        count_text = channel.seq_count if is_sequence else channel.arb_count
        if str(count_text).upper().startswith("INF"):
            return False
        duration = self._arb_duration(channel)
        if duration <= 0:
            return False
        if is_sequence:
            return (time.monotonic() - channel.tran_t0) >= duration
        try:
            count = int(float(channel.arb_count))
        except (TypeError, ValueError):
            count = 1
        return (time.monotonic() - channel.tran_t0) >= duration * max(1, count)

    def _arb_duration(self, channel: FakeChannel) -> float:
        """Seconds one cycle of the loaded waveform lasts, or 0 if unknown."""
        from .arb.models import SHAPES

        # ARB:FUNC:SHAP SEQ is what distinguishes a sequence from a single
        # waveform; the steps live in their own table.
        if channel.arb_shape == "SEQ":
            return self._sequence_duration(channel)
        shape = SHAPES.get(channel.arb_shape)
        if shape is None:
            return 0.0
        kind = channel.arb_type
        params: dict[str, object] = {}
        for name, suffix in getattr(shape, "scpi_suffixes", {}).items():
            value = channel.arb_params.get((kind, suffix.upper()))
            if value is not None:
                params[name] = value
        if channel.arb_shape == "UDEF":
            dwells = channel.arb_params.get((kind, "UDEF:DWEL"))
            if dwells is None:
                return 0.0
            return float(sum(dwells)) if isinstance(dwells, list) else float(dwells)
        if channel.arb_shape == "CDW":
            levels = channel.arb_params.get((kind, "CDW"), [])
            dwell = channel.arb_params.get((kind, "CDW:DWEL"), 0.0)
            n = len(levels) if isinstance(levels, list) else 1
            return n * float(dwell)
        try:
            return float(shape(**params).duration)  # type: ignore[arg-type]
        except Exception as exc:
            log.debug("could not work out the ARB duration: %s", exc)
            return 0.0

    def _sequence_duration(self, channel: FakeChannel) -> float:
        """One pass through every step of a loaded sequence."""
        from .arb.models import SHAPES

        total = 0.0
        for step in channel.seq_steps.values():
            shape = SHAPES.get(str(step.get("shape", "")))
            if shape is None:
                continue
            params: dict[str, object] = {}
            for name, suffix in getattr(shape, "scpi_suffixes", {}).items():
                value = step.get("params", {}).get((channel.arb_type, suffix.upper()))
                if value is not None:
                    params[name] = value
            try:
                total += float(shape(**params).duration)  # type: ignore[arg-type]
            except Exception as exc:
                log.debug("could not work out a sequence step's duration: %s", exc)
        try:
            repeats = max(1, int(float(channel.seq_count)))
        except (TypeError, ValueError):
            repeats = 1
        return total * repeats

    def _acq_complete(self, channel: FakeChannel) -> bool:
        if not channel.acq_triggered:
            return False
        duration = channel.sweep_points * channel.sweep_tinterval
        return (time.monotonic() - channel.acq_t0) >= duration

    def _fetch_array(self, args: str, kind: str) -> bytes:
        """Return one definite-length block per channel, as the real one does."""
        fmt = ">f" if self.byte_order == "NORM" else "<f"
        blocks = []
        for channel in self._channels_from(args):
            n = channel.sweep_points
            values = []
            for index in range(n):
                t = (index + channel.sweep_offset) * channel.sweep_tinterval
                if kind == "VOLT":
                    values.append(self._acq_voltage_at(channel, t))
                else:
                    values.append(self._acq_current_at(channel, t))
            payload = b"".join(struct.pack(fmt, v) for v in values)
            header = str(len(payload)).encode()
            blocks.append(b"#" + str(len(header)).encode() + header + payload)
        return b",".join(blocks) + b"\n"

    def _acq_voltage_at(self, channel: FakeChannel, t: float) -> float:
        """A settling step at t=0, so pre-trigger samples look different."""
        if not channel.output_on:
            return 0.0
        if t < 0:
            return channel.voltage * 0.2
        return channel.voltage * (1.0 - 0.8 * math.exp(-t / 2e-4))

    def _acq_current_at(self, channel: FakeChannel, t: float) -> float:
        if not channel.output_on:
            return 0.0
        base = min(channel.current_limit, 0.25 * (channel.number + 1))
        if t < 0:
            return base * 0.1
        # An inrush spike decaying to the steady value.
        return base * (1.0 + 3.0 * math.exp(-t / 1e-4))

    def _clamp(self, raw: str, low: float, high: float) -> float:
        try:
            value = float(raw)
        except ValueError:
            upper = raw.strip().upper()
            if upper.startswith("MAX"):
                return high
            if upper.startswith("MIN"):
                return low
            self.errors.append((-224, "Illegal parameter value"))
            return low
        if not low <= value <= high:
            self.errors.append((-222, "Data out of range"))
        return min(max(value, low), high)

    def _simulate_voltage(self, channel: FakeChannel) -> float:
        if not channel.output_on:
            return 0.0
        # A little ripple so plots are not flat lines.
        return channel.voltage + 0.002 * math.sin(time.monotonic() * 3.0 + channel.number)

    def _simulate_current(self, channel: FakeChannel) -> float:
        if not channel.output_on:
            return 0.0
        base = min(channel.current_limit, 0.25 * (channel.number + 1))
        return base * (1.0 + 0.05 * math.sin(time.monotonic() * 7.0 + channel.number))

    def _fetch_elog(self, args: str) -> str | bytes:
        parts = args.split(",", 1)
        try:
            max_records = int(float(parts[0]))
        except (ValueError, IndexError):
            self.errors.append((-109, "Missing parameter"))
            return ""
        channels = self._channels_from(args)
        if not channels:
            return ""

        now = time.monotonic()
        per_channel: list[list[float]] = []
        for channel in channels:
            if not channel.elog_running:
                per_channel.append([])
                continue
            elapsed = now - channel.elog_t0
            available = int(elapsed / channel.elog_period) - channel.elog_records_emitted
            # The instrument holds 10 seconds per channel; anything older is
            # overwritten and gone. Reproducing that here is what makes the
            # overflow detection in ElogStats testable.
            capacity = max(1, int(BUFFER_SECONDS / channel.elog_period))
            if available > capacity:
                channel.elog_records_emitted += available - capacity
                available = capacity
            count = max(0, min(available, max_records))
            values: list[float] = []
            for k in range(count):
                index = channel.elog_records_emitted + k
                t = index * channel.elog_period
                current = self._elog_current_at(channel, t)
                voltage = self._elog_voltage_at(channel, t)
                if channel.elog_current:
                    values.append(current)
                    if channel.elog_current_minmax:
                        values += [current * 0.98, current * 1.02]
                if channel.elog_voltage:
                    values.append(voltage)
                    if channel.elog_voltage_minmax:
                        values += [voltage - 0.005, voltage + 0.005]
            channel.elog_records_emitted += count
            per_channel.append(values)

        if self.data_format == "ASC":
            if len(channels) > 1:
                # Matches the manual: only one channel may be queried in ASCII.
                self.errors.append((-221, "Settings conflict;ASCII allows one channel"))
                return ""
            return ",".join(f"{v:+.6E}" for v in per_channel[0])

        fmt = ">f" if self.byte_order == "NORM" else "<f"
        blocks = []
        for values in per_channel:
            payload = b"".join(struct.pack(fmt, v) for v in values)
            header = str(len(payload)).encode()
            blocks.append(b"#" + str(len(header)).encode() + header + payload)
        return b",".join(blocks) + b"\n"

    def _elog_voltage_at(self, channel: FakeChannel, t: float) -> float:
        if not channel.output_on:
            return 0.0
        return channel.voltage + 0.01 * math.sin(2 * math.pi * 0.5 * t + channel.number)

    def _elog_current_at(self, channel: FakeChannel, t: float) -> float:
        if not channel.output_on:
            return 0.0
        base = min(channel.current_limit, 0.25 * (channel.number + 1))
        return base * (1.0 + 0.1 * math.sin(2 * math.pi * 1.7 * t + channel.number))


def _module_ratings(model: str) -> tuple[float, float]:
    """Nominal max voltage/current for the simulated slot population."""
    table = {
        "N6781A": (20.0, 3.0),
        "N6782A": (20.0, 3.0),
        "N6784A": (20.0, 3.0),
        "N6785A": (20.0, 8.0),
        "N6786A": (20.0, 8.0),
        "N6743B": (20.0, 5.0),
    }
    return table.get(model.upper(), (0.0, 0.0) if not model else (50.0, 1.0))
