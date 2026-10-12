"""Per-channel output settings: sense wiring, bandwidth, priority, protection.

Two settings here are coupled in a way that can damage a measurement or latch
an output off, so they are modelled together rather than as loose commands.

**Remote sense** (``[SOURce:]VOLTage:SENSe:SOURce``, p.474) switches the sense
relays.  ``INTernal`` is 2-wire: the sense terminals are tied to the output
terminals internally.  ``EXTernal`` is 4-wire: they are not, and *must* be
wired to the load -- leaving them open in 4-wire mode gives a wrong or
unregulated output.

**Voltage bandwidth** (``[SOURce:]VOLTage:BWIDth``, p.457) trades stability for
response speed against the load capacitance, on N678xA SMUs only.  Every HIGH
mode requires remote sensing, and picking a mode whose capacitance range does
not match the actual load makes the output oscillate; the detector then latches
the output off and raises the OSC bit.  That is why this module refuses
combinations the manual forbids instead of passing them through.

.. note::
   The manual is inconsistent about the bandwidth parameter names: its command
   summary lists ``DEFault | FAST1 | FAST2 | FAST3`` while the command's own
   page lists ``LOW | HIGH1 | HIGH2 | HIGH3`` with a full parameter table and a
   LOW default.  The detailed page is taken as authoritative; the other
   spellings are accepted as aliases.  Flagged for hardware verification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from .capability import ChannelCapability
from .scpi import format_channel_list

__all__ = [
    "SenseMode",
    "VoltageBandwidth",
    "SourcePriority",
    "ChannelSettings",
    "bandwidth_for_capacitance",
    "BANDWIDTH_ALIASES",
]

log = logging.getLogger(__name__)


class SenseMode(Enum):
    """Where the instrument measures voltage."""

    LOCAL = "INT"  # 2-wire: sense terminals tied to the output internally
    REMOTE = "EXT"  # 4-wire: sense terminals wired to the load

    @property
    def wires(self) -> int:
        return 2 if self is SenseMode.LOCAL else 4

    @property
    def is_remote(self) -> bool:
        return self is SenseMode.REMOTE

    @classmethod
    def parse(cls, text: str) -> "SenseMode":
        value = text.strip().upper()
        if value.startswith("EXT") or value.startswith("REM") or value.startswith("4"):
            return cls.REMOTE
        if value.startswith("INT") or value.startswith("LOC") or value.startswith("2"):
            return cls.LOCAL
        raise ValueError(f"unknown sense mode {text!r}; expected INTernal or EXTernal")


class VoltageBandwidth(Enum):
    """Voltage-loop compensation, N678xA SMU only (p.457).

    The capacitance range is not advisory: a load outside it oscillates, and
    the oscillation detector latches the output off.
    """

    LOW = "LOW"
    HIGH1 = "HIGH1"
    HIGH2 = "HIGH2"
    HIGH3 = "HIGH3"

    @property
    def capacitance_range_uf(self) -> tuple[float, float]:
        return {
            VoltageBandwidth.LOW: (0.0, 150.0),
            VoltageBandwidth.HIGH1: (0.0, 1.0),
            VoltageBandwidth.HIGH2: (1.0, 7.0),
            VoltageBandwidth.HIGH3: (7.0, 150.0),
        }[self]

    @property
    def requires_remote_sense(self) -> bool:
        """Every HIGH mode is remote-sense only."""
        return self is not VoltageBandwidth.LOW

    def describe_range(self) -> str:
        low, high = self.capacitance_range_uf
        return f"{low:g}–{high:g} µF"

    @classmethod
    def parse(cls, text: str) -> "VoltageBandwidth":
        value = text.strip().upper()
        if value in BANDWIDTH_ALIASES:
            value = BANDWIDTH_ALIASES[value]
        try:
            return cls(value)
        except ValueError:
            raise ValueError(
                f"unknown voltage bandwidth {text!r}; expected one of "
                f"{', '.join(m.value for m in cls)}"
            ) from None


#: The manual's command summary uses these names for the same four modes.
BANDWIDTH_ALIASES = {
    "DEF": "LOW",
    "DEFAULT": "LOW",
    "FAST1": "HIGH1",
    "FAST2": "HIGH2",
    "FAST3": "HIGH3",
    "HIGH": "HIGH1",
}


class SourcePriority(Enum):
    """``[SOURce:]FUNCtion`` on N678xA SMUs: voltage or current priority."""

    VOLTAGE = "VOLT"
    CURRENT = "CURR"

    @classmethod
    def parse(cls, text: str) -> "SourcePriority":
        value = text.strip().upper()
        if value.startswith("V"):
            return cls.VOLTAGE
        if value.startswith("C"):
            return cls.CURRENT
        raise ValueError(f"unknown source priority {text!r}")


def bandwidth_for_capacitance(microfarads: float) -> VoltageBandwidth:
    """Pick the fastest bandwidth mode valid for a given load capacitance.

    Operators know their output capacitor, not the mode names, so this is the
    more useful way round.
    """
    if microfarads < 0:
        raise ValueError("capacitance cannot be negative")
    if microfarads <= 1.0:
        return VoltageBandwidth.HIGH1
    if microfarads <= 7.0:
        return VoltageBandwidth.HIGH2
    if microfarads <= 150.0:
        return VoltageBandwidth.HIGH3
    raise ValueError(
        f"{microfarads:g} µF exceeds the 150 µF the compensation modes cover"
    )


@dataclass
class ChannelSettings:
    """Sense wiring and loop compensation for one channel."""

    channel: int
    sense: SenseMode = SenseMode.LOCAL
    bandwidth: VoltageBandwidth = VoltageBandwidth.LOW
    priority: SourcePriority | None = None

    def validate(self, capability: ChannelCapability | None = None) -> None:
        """Refuse combinations the instrument documents as unsupported."""
        if self.bandwidth.requires_remote_sense and not self.sense.is_remote:
            raise ValueError(
                f"bandwidth {self.bandwidth.value} ({self.bandwidth.describe_range()}) "
                "requires 4-wire remote sensing; switch the channel to remote "
                "sense or use LOW"
            )
        if capability is None:
            return
        if not capability.present:
            raise ValueError(f"channel {self.channel} is an empty slot")
        if self.bandwidth is not VoltageBandwidth.LOW and not capability.supports_bandwidth:
            raise ValueError(
                f"channel {self.channel} ({capability.model}) has no voltage "
                "bandwidth control; that is an N678xA SMU feature"
            )
        if self.priority is not None and not capability.is_smu:
            raise ValueError(
                f"channel {self.channel} ({capability.model}) has no voltage/current "
                "priority setting; that is an N678xA SMU feature"
            )

    def scpi_commands(self, capability: ChannelCapability | None = None) -> list[str]:
        """Commands to apply these settings, in a safe order.

        Sense wiring goes first: a HIGH bandwidth is rejected by the instrument
        while it is still in local sense, so the order is not cosmetic.
        """
        self.validate(capability)
        chan = format_channel_list(self.channel)
        commands = [f"VOLT:SENS:SOUR {self.sense.value},{chan}"]
        if capability is None or capability.is_smu:
            if self.priority is not None:
                commands.append(f"FUNC {self.priority.value},{chan}")
        if capability is None or capability.supports_bandwidth:
            commands.append(f"VOLT:BWID {self.bandwidth.value},{chan}")
        return commands

    @property
    def wires(self) -> int:
        return self.sense.wires
