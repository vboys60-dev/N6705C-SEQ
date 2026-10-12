"""Output synchronisation: switching several channels on and off as one.

Coupled channels "turn on and off together when any one of them is turned on
or off" (p.146), each still honouring its own ``OUTPut:DELay:RISE``.  So the
coupling decides *which* channels take part in a sequence and the per-channel
delays decide the *order*.

Three things about this subsystem are easy to get wrong:

**Applying any of it switches every output off.**  p.146, p.148 and p.149 all
carry the same remark -- "when this command is sent, all output channels in the
mainframe will go to the output Off state".  Not just the coupled ones: every
channel in the frame.  Changing a coupling setting while something is powered
therefore drops it, which is why :attr:`CouplingConfig.drops_every_output` is
part of the model and every caller is expected to say so first.

**It is non-volatile and survives ``*RST``.**  A coupled set left by an earlier
session, or by someone at the front panel, is still in force at the next
connection.  So it is read, never assumed -- the same discipline the turn-off
delays follow.

**There is one coupled set per mainframe.**  Programming another deletes the
first (p.146), and sending no channels at all uncouples everything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

__all__ = [
    "DOFFSET_MAX",
    "CouplingMode",
    "CouplingConfig",
    "read_coupling",
]

log = logging.getLogger(__name__)

#: ``OUTPut:COUPle:DOFFset`` range (p.148), the same span as the delays.
DOFFSET_MAX = 1.023


class CouplingMode:
    """Where the delay offset comes from (p.149)."""

    #: The instrument works out the offset the installed modules need.
    AUTO = "AUTO"
    #: The value set by ``OUTPut:COUPle:DOFFset`` is used instead.
    MANUAL = "MAN"

    @staticmethod
    def parse(text: str) -> str:
        value = text.strip().upper()
        if value.startswith("MAN"):
            return CouplingMode.MANUAL
        if value.startswith("AUTO"):
            return CouplingMode.AUTO
        raise ValueError(f"unknown coupling mode {text!r}; expected AUTO or MANual")


@dataclass
class CouplingConfig:
    """Which channels switch together, and how they are aligned."""

    #: Empty means uncoupled -- sending no channel list undoes the coupling.
    channels: list[int] = field(default_factory=list)
    mode: str = CouplingMode.AUTO
    #: Only used in MANual mode; ignored by the instrument otherwise.
    doffset: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.channels)

    @property
    def drops_every_output(self) -> bool:
        """Whether applying this will switch every output in the frame off.

        Always true: p.146, p.148 and p.149 each say so. Kept as a property so
        callers read the reason rather than a bare constant.
        """
        return True

    def validate(self, populated: list[int] | None = None) -> None:
        seen: set[int] = set()
        for channel in self.channels:
            if channel in seen:
                raise ValueError(f"channel {channel} is listed twice")
            seen.add(channel)
            if not 1 <= channel <= 4:
                raise ValueError(f"channel {channel} is outside 1..4")
            if populated is not None and channel not in populated:
                raise ValueError(f"channel {channel} is an empty slot")
        if self.mode == CouplingMode.MANUAL and not 0 <= self.doffset <= DOFFSET_MAX:
            raise ValueError(
                f"delay offset {self.doffset:g} s is outside 0..{DOFFSET_MAX:g} s"
            )
        if len(self.channels) == 1:
            raise ValueError(
                "coupling one channel does nothing; list two or more, or none "
                "at all to uncouple"
            )

    def scpi_commands(self) -> list[str]:
        """Commands to apply this, in an order that leaves nothing half-set.

        The mode and its value go first so that the channel list -- the command
        that actually starts the synchronisation -- lands on settings that are
        already correct.
        """
        self.validate()
        commands = [f"OUTP:COUP:DOFF:MODE {self.mode}"]
        if self.mode == CouplingMode.MANUAL:
            commands.append(f"OUTP:COUP:DOFF {self.doffset:.9G}")
        # No channel list at all is what uncouples everything (p.146).
        joined = ",".join(str(c) for c in sorted(self.channels))
        commands.append(f"OUTP:COUP:CHAN {joined}" if joined else "OUTP:COUP:CHAN")
        return commands

    def describe(self) -> str:
        if not self.enabled:
            return "not coupled"
        channels = ", ".join(f"CH{c}" for c in sorted(self.channels))
        if self.mode == CouplingMode.MANUAL:
            return f"{channels} switch together, offset {self.doffset:g} s"
        return f"{channels} switch together, offset chosen by the instrument"


def read_coupling(session) -> CouplingConfig:
    """Read what the instrument is actually set to.

    Non-volatile and unaffected by ``*RST`` (p.146), so a set left by an
    earlier session or by the front panel is still in force. A refusal is
    treated as "not coupled" rather than as a fatal error: not every frame
    supports the subsystem, and it is not worth failing a connection over.
    """
    config = CouplingConfig()
    try:
        answer = session.query("OUTP:COUP:CHAN?").strip()
    except Exception:
        log.debug("the instrument did not report its coupling", exc_info=True)
        return config
    config.channels = [
        int(float(part)) for part in answer.split(",") if part.strip() and part.strip() != "0"
    ]
    try:
        config.mode = CouplingMode.parse(session.query("OUTP:COUP:DOFF:MODE?"))
        config.doffset = float(session.query("OUTP:COUP:DOFF?"))
    except Exception:
        log.debug("the instrument did not report its delay offset", exc_info=True)
    return config


def read_maximum_doffset(session) -> float:
    """``OUTP:COUP:MAX:DOFF?`` -- the offset the installed modules need (p.151).

    The value to put in MANual mode if the automatic one is not wanted.
    """
    try:
        return float(session.query("OUTP:COUP:MAX:DOFF?").strip())
    except Exception:
        log.debug("the instrument did not report a maximum delay offset", exc_info=True)
        return 0.0
