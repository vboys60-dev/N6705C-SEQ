"""Operation Status register decoding.

Bit definitions from the N6705 Programmer's Reference Guide, ``STATus:OPERation``
(p.482).  The same bits appear in the event register (latched, cleared on read)
and the condition register (live, not cleared on read).
"""

from __future__ import annotations

from enum import IntFlag

__all__ = [
    "OperationStatus",
    "QuestionableStatus",
    "describe_operation_status",
    "describe_questionable_status",
]


class OperationStatus(IntFlag):
    """Bits of the Operation Status registers."""

    CV = 1  # output is in constant voltage
    CC = 2  # output is in constant current
    OFF = 4  # output is programmed off
    WTG_MEAS = 8  # measurement system is waiting for a trigger
    WTG_TRAN = 16  # transient system is waiting for a trigger
    MEAS_ACTIVE = 32  # measurement system is initiated
    TRAN_ACTIVE = 64  # transient system is initiated


_LABELS: dict[OperationStatus, str] = {
    OperationStatus.CV: "CV",
    OperationStatus.CC: "CC",
    OperationStatus.OFF: "OFF",
    OperationStatus.WTG_MEAS: "WTG-meas",
    OperationStatus.WTG_TRAN: "WTG-tran",
    OperationStatus.MEAS_ACTIVE: "MEAS-active",
    OperationStatus.TRAN_ACTIVE: "TRAN-active",
}


def describe_operation_status(value: int | OperationStatus) -> list[str]:
    """Return the manual's labels for every bit set in *value*."""
    status = OperationStatus(int(value))
    return [label for bit, label in _LABELS.items() if bit in status]


class QuestionableStatus(IntFlag):
    """Bits of the Questionable Status registers (p.493).

    These are the conditions that disable or limit an output.  ``OSC`` is the
    one to watch when experimenting with voltage bandwidth: an output
    capacitance outside the selected mode's range oscillates, and the detector
    latches the output off.
    """

    OV = 1  # disabled by over-voltage protection
    OC = 2  # disabled by over-current protection
    PF = 4  # disabled by power-fail (low line or brownout)
    CP_POS = 8  # limited or disabled by the positive power limit
    OT = 16  # over-temperature protection tripped
    CP_NEG = 32  # limited by the negative power limit
    OV_NEG = 64  # negative over-voltage protection tripped
    LIM_POS = 128  # in positive voltage or current limit
    LIM_NEG = 256  # in negative voltage or current limit
    INH = 512  # inhibited by an external signal
    UNR = 1024  # output is unregulated
    PROT = 2048  # disabled by a protection coupled from another channel
    OSC = 4096  # the oscillation detector tripped


_QUESTIONABLE_LABELS: dict[QuestionableStatus, str] = {
    QuestionableStatus.OV: "OV",
    QuestionableStatus.OC: "OC",
    QuestionableStatus.PF: "PF",
    QuestionableStatus.CP_POS: "CP+",
    QuestionableStatus.OT: "OT",
    QuestionableStatus.CP_NEG: "CP-",
    QuestionableStatus.OV_NEG: "OV-",
    QuestionableStatus.LIM_POS: "LIM+",
    QuestionableStatus.LIM_NEG: "LIM-",
    QuestionableStatus.INH: "INH",
    QuestionableStatus.UNR: "UNR",
    QuestionableStatus.PROT: "PROT",
    QuestionableStatus.OSC: "OSC",
}


def describe_questionable_status(value: int | QuestionableStatus) -> list[str]:
    """Return the manual's labels for every bit set in *value*."""
    status = QuestionableStatus(int(value))
    return [label for bit, label in _QUESTIONABLE_LABELS.items() if bit in status]
