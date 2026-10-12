"""Turning an :class:`ArbSpec` into SCPI, and running it.

Command order matters: the shape must be selected before its parameters are
sent, and ``VOLTage:MODE ARB`` (or ``CURRent:MODE ARB``) must be set before the
transient system is initiated, or the trigger produces nothing.

The new ``ARB:FUNCtion:SHAPe`` + ``ARB:FUNCtion:TYPE`` pair is used throughout;
the older ``ARB:FUNCtion`` command cannot select current arbs, constant-dwell
arbs or sequences (p.303).
"""

from __future__ import annotations

import logging

from ..scpi import format_channel_list
from .models import ArbSpec

__all__ = [
    "configure_commands",
    "arm_commands",
    "trigger_command",
    "abort_command",
    "VALID_TRANSIENT_SOURCES",
]

log = logging.getLogger(__name__)

#: TRIGger:TRANsient:SOURce (p.547)
VALID_TRANSIENT_SOURCES = (
    "BUS",
    "EXT",
    "IMM",
    *(f"PIN{n}" for n in range(1, 8)),
    *(f"TRAN{n}" for n in range(1, 5)),
)


def configure_commands(spec: ArbSpec) -> list[str]:
    """Everything needed to load *spec* into the channel, in order."""
    chan = format_channel_list(spec.channel)
    kind = spec.normalised_type
    waveform = spec.waveform

    commands = [
        f"ARB:FUNC:TYPE {kind},{chan}",
        f"ARB:FUNC:SHAP {waveform.shape},{chan}",
    ]
    commands += waveform.scpi_commands(kind, chan)

    count = "INF" if spec.infinite else f"{int(spec.count)}"
    commands.append(f"ARB:COUN {count},{chan}")
    commands.append(f"ARB:TERM:LAST {'ON' if spec.terminate_last else 'OFF'},{chan}")
    # Selecting ARB mode is what makes the transient system emit the waveform.
    commands.append(f"{kind}:MODE ARB,{chan}")
    return commands


def arm_commands(spec: ArbSpec) -> list[str]:
    """Select the trigger source and initiate the transient system."""
    chan = format_channel_list(spec.channel)
    source = spec.trigger_source.strip().upper()
    if source not in VALID_TRANSIENT_SOURCES:
        raise ValueError(
            f"invalid transient trigger source {spec.trigger_source!r}; expected "
            f"one of {', '.join(VALID_TRANSIENT_SOURCES)}"
        )
    return [
        f"TRIG:TRAN:SOUR {source},{chan}",
        f"INIT:TRAN {chan}",
    ]


def trigger_command(spec: ArbSpec) -> str:
    return f"TRIG:TRAN {format_channel_list(spec.channel)}"


def abort_command(spec: ArbSpec) -> str:
    """Stop the arb.

    On abort the output returns to whatever was set before the arb started,
    regardless of ``ARB:TERMinate:LAST`` (p.323).
    """
    return f"ABOR:TRAN {format_channel_list(spec.channel)}"
