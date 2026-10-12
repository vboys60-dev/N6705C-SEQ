"""High-level N6705C facade.

Deliberate behaviours (PLAN.md section 9):

* **Connecting never issues ``*RST``.**  Overwriting a bench setup that
  someone spent an afternoon dialling in is not an acceptable side effect of
  opening a connection.  ``reset()`` exists and must be called explicitly.
* Settings that the instrument may silently round or clamp are always read
  back, never assumed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .capability import ChannelCapability
from .fake import FakeN6705C
from .scpi import format_channel_list
from .session import Session
from .channels import ChannelSettings, SenseMode, SourcePriority, VoltageBandwidth
from .coupling import CouplingConfig, read_coupling, read_maximum_doffset
from .delay import OutputDelay, read_delay_limit
from .status import OperationStatus, QuestionableStatus
from .transport import Transport, VisaTransport

__all__ = ["Identity", "N6705C"]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    """Parsed ``*IDN?`` response."""

    manufacturer: str = ""
    model: str = ""
    serial: str = ""
    firmware: str = ""

    @classmethod
    def parse(cls, response: str) -> "Identity":
        parts = [p.strip() for p in response.split(",")]
        parts += [""] * (4 - len(parts))
        return cls(*parts[:4])

    def __str__(self) -> str:
        return f"{self.manufacturer} {self.model} (S/N {self.serial}, fw {self.firmware})"


class N6705C:
    """Connected mainframe: identity, channel capabilities, basic control."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.identity = Identity()
        self.capabilities: dict[int, ChannelCapability] = {}
        #: Cached ``OUTP:DEL:*? MAX`` per (channel, edge); see delay_limit.
        self._delay_limits: dict[tuple[int, str], float] = {}

    # -- Construction -------------------------------------------------------

    @classmethod
    def connect(
        cls,
        resource: str,
        *,
        backend: str = "auto",
        timeout_ms: int = 10_000,
    ) -> "N6705C":
        """Open a real instrument over VISA and probe it."""
        return cls.from_transport(
            VisaTransport(resource, backend=backend, timeout_ms=timeout_ms)
        )

    @classmethod
    def demo(cls, slots: tuple[str, ...] | list[str] | None = None) -> "N6705C":
        """Open the built-in simulator (Demo mode)."""
        fake = FakeN6705C(slots) if slots is not None else FakeN6705C()
        return cls.from_transport(fake)

    @classmethod
    def from_transport(cls, transport: Transport) -> "N6705C":
        instrument = cls(Session(transport))
        instrument.probe()
        return instrument

    # -- Probing ------------------------------------------------------------

    def probe(self) -> None:
        """Identify the mainframe and build the per-channel capability table."""
        self.identity = Identity.parse(self.session.query("*IDN?"))
        if "N6705" not in self.identity.model.upper():
            log.warning(
                "connected instrument reports model %r, which is not an N6705 "
                "family mainframe; proceeding but capabilities may be wrong",
                self.identity.model,
            )
        count = self.session.query_int("SYST:CHAN:COUN?")
        self.capabilities = {}
        for channel in range(1, count + 1):
            self.capabilities[channel] = self._probe_channel(channel)
        self.session.check_errors("probe")

    def _probe_channel(self, channel: int) -> ChannelCapability:
        chan = format_channel_list(channel)
        capability = ChannelCapability(channel=channel)
        capability.model = self._safe_query(f"SYST:CHAN:MOD? {chan}")
        if not capability.model:
            return capability  # empty slot; skip the remaining queries
        capability.serial = self._safe_query(f"SYST:CHAN:SER? {chan}")
        capability.options = self._safe_query(f"SYST:CHAN:OPT? {chan}")
        capability.voltage_max = self._safe_query_float(f"VOLT:RANG? MAX,{chan}")
        capability.current_max = self._safe_query_float(f"CURR:RANG? MAX,{chan}")
        return capability

    def _safe_query(self, command: str) -> str:
        """Query, treating a refusal as 'not supported' rather than fatal.

        Slot-dependent queries legitimately fail on empty or unusual slots;
        the error queue is drained so it does not leak into the next check.
        """
        try:
            value = self.session.query(command).strip().strip('"')
        except Exception as exc:
            log.debug("query %r failed: %s", command, exc)
            return ""
        self.session.read_errors()
        return value

    def _safe_query_float(self, command: str) -> float:
        text = self._safe_query(command)
        try:
            return float(text)
        except ValueError:
            return float("nan")

    # -- Channel views ------------------------------------------------------

    @property
    def channel_numbers(self) -> list[int]:
        return sorted(self.capabilities)

    @property
    def populated_channels(self) -> list[int]:
        return [n for n in self.channel_numbers if self.capabilities[n].present]

    # -- Basic control ------------------------------------------------------

    def output_state(self, channels: list[int]) -> dict[int, bool]:
        values = self.session.query_floats(f"OUTP? {format_channel_list(channels)}")
        return dict(zip(channels, (bool(v) for v in values)))

    def set_output(self, channels: list[int], on: bool) -> None:
        state = "ON" if on else "OFF"
        self.session.write_checked(f"OUTP {state},{format_channel_list(channels)}")

    def all_outputs_off(self) -> None:
        """Emergency stop. Never raises -- it must work when things are wrong.

        Not necessarily *instant*: a programmed ``OUTP:DEL:FALL`` defers the
        off transition by up to about a second (p.152). That is left alone on
        purpose -- an out-of-order rail drop can damage a device under test,
        and the hardware exempts protection trips from the delay anyway. Use
        :func:`~.delay.shutdown_seconds` to tell the operator how long it will
        take.
        """
        if not self.capabilities:
            return
        chan = format_channel_list(self.channel_numbers)
        for command in (f"ABOR:TRAN {chan}", f"ABOR:ELOG {chan}", f"OUTP OFF,{chan}"):
            try:
                self.session.write(command)
            except Exception:
                log.exception("emergency stop: %s failed", command)

    def measure_voltage(self, channels: list[int]) -> dict[int, float]:
        values = self.session.query_floats(f"MEAS:VOLT? {format_channel_list(channels)}")
        return dict(zip(channels, values))

    def measure_current(self, channels: list[int]) -> dict[int, float]:
        values = self.session.query_floats(f"MEAS:CURR? {format_channel_list(channels)}")
        return dict(zip(channels, values))

    def operation_status(self, channels: list[int]) -> dict[int, OperationStatus]:
        values = self.session.query_floats(
            f"STAT:OPER:COND? {format_channel_list(channels)}"
        )
        return dict(zip(channels, (OperationStatus(int(v)) for v in values)))

    def questionable_status(self, channels: list[int]) -> dict[int, QuestionableStatus]:
        """Why an output is disabled or limited -- including OSC."""
        values = self.session.query_floats(
            f"STAT:QUES:COND? {format_channel_list(channels)}"
        )
        return dict(zip(channels, (QuestionableStatus(int(v)) for v in values)))

    def clear_protection(self, channels: list[int]) -> None:
        """Clear a latched protection trip.

        For an OSC trip the cause must be removed first -- a different
        bandwidth mode -- or the output simply trips again.
        """
        self.session.write_checked(
            f"OUTP:PROT:CLE {format_channel_list(channels)}"
        )

    # -- Sense wiring and compensation --------------------------------------

    def channel_settings(self, channel: int) -> ChannelSettings:
        """Read the current sense wiring and compensation back."""
        chan = format_channel_list(channel)
        capability = self.capabilities.get(channel)
        settings = ChannelSettings(channel=channel)
        settings.sense = SenseMode.parse(self._safe_query(f"VOLT:SENS:SOUR? {chan}") or "INT")
        if capability is not None and capability.supports_bandwidth:
            text = self._safe_query(f"VOLT:BWID? {chan}")
            if text:
                settings.bandwidth = VoltageBandwidth.parse(text)
        if capability is not None and capability.supports_priority:
            text = self._safe_query(f"FUNC? {chan}")
            if text:
                settings.priority = SourcePriority.parse(text)
        return settings

    def apply_channel_settings(self, settings: ChannelSettings) -> None:
        """Apply sense wiring and compensation, refusing invalid combinations."""
        capability = self.capabilities.get(settings.channel)
        for command in settings.scpi_commands(capability):
            self.session.write(command)
        self.session.check_errors(f"channel {settings.channel} settings")

    # -- Output sequencing delays --------------------------------------------

    def delay_limit(self, channel: int, edge: str = "RISE") -> float:
        """The longest delay this channel accepts, as the instrument reports it.

        Cached: the value cannot change while connected, and the manual's own
        figure is contradictory enough that it is worth asking once rather
        than trusting either printed number.
        """
        key = (channel, edge.strip().upper())
        if key not in self._delay_limits:
            self._delay_limits[key] = read_delay_limit(self.session, channel, edge)
        return self._delay_limits[key]

    def output_delay(self, channel: int) -> OutputDelay:
        """Read both delays back, as the instrument actually holds them."""
        chan = format_channel_list(channel)
        rise = self._safe_query(f"OUTP:DEL:RISE? {chan}")
        fall = self._safe_query(f"OUTP:DEL:FALL? {chan}")
        return OutputDelay(
            channel=channel,
            rise=float(rise) if rise else 0.0,
            fall=float(fall) if fall else 0.0,
        )

    def apply_output_delay(self, delay: OutputDelay) -> OutputDelay:
        """Apply a delay and return what the instrument settled on.

        The return value is not decoration: the resolution is stepped, so a
        request of 7 ms becomes 7 ms but a request of 7.005 s becomes 7.01 s,
        and the caller needs the accepted figure to report the real sequence.
        """
        for command in delay.scpi_commands(self.delay_limit(delay.channel)):
            self.session.write(command)
        self.session.check_errors(f"channel {delay.channel} output delay")
        return self.output_delay(delay.channel)

    def apply_output_delays(self, delays: list[OutputDelay]) -> dict[int, OutputDelay]:
        return {d.channel: self.apply_output_delay(d) for d in delays}

    # -- Output synchronisation ----------------------------------------------

    def coupling(self) -> CouplingConfig:
        """What the instrument is coupled as, read rather than assumed.

        The setting is non-volatile and survives ``*RST`` (p.146), so it can
        have been left by an earlier session or by the front panel.
        """
        return read_coupling(self.session)

    def maximum_doffset(self) -> float:
        return read_maximum_doffset(self.session)

    def apply_coupling(self, config: CouplingConfig) -> CouplingConfig:
        """Apply and read back.

        .. warning::
           **This switches every output in the mainframe off**, coupled or not
           (p.146/148/149). Confirm with the operator before calling it on a
           frame that is powering something.
        """
        config.validate(self.populated_channels)
        for command in config.scpi_commands():
            self.session.write(command)
        self.session.check_errors("output coupling")
        return self.coupling()

    def reset(self) -> None:
        """Explicit ``*RST``; never called implicitly on connect."""
        self.session.write_checked("*RST")

    # -- Lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "N6705C":
        return self

    def __exit__(self, *exc_info: object) -> None:
        # Leaving outputs live after an unhandled error is the failure mode
        # that damages hardware, so shut them down on the way out.
        try:
            self.all_outputs_off()
        finally:
            self.close()

    def summary(self) -> str:
        lines = [str(self.identity), f"Link: {self.session.description}"]
        lines += [self.capabilities[n].describe() for n in self.channel_numbers]
        return "\n".join(lines)
