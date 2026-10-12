"""Parsing of IEEE 488.2 block responses and ELOG records.

References (N6705 Programmer's Reference Guide):

* ``FETCh:ELOG?`` (p.96-97) -- with ``FORMat REAL`` each channel's records
  come back as one definite-length block, and **multiple channels are
  separated by commas**.  With ``FORMat ASCII`` only a single channel may be
  queried, so multi-channel logging must use REAL.
* ``FORMat:BORDer`` (p.101) -- ``NORMal`` is big-endian, ``SWAPped`` is
  little-endian.
* Record layout (p.96): when every function is enabled a record holds
  *current average, current minimum, current maximum, voltage average,
  voltage minimum, voltage maximum*, **in that order**.  Functions that are
  not enabled are simply absent from the record.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "BlockParseError",
    "parse_definite_block",
    "split_block_list",
    "decode_real32",
    "ElogFormat",
    "ElogSelection",
    "FIELD_ORDER",
    "FIELD_LABELS",
]

#: Record field names in the fixed order the instrument returns them (p.96).
FIELD_ORDER: tuple[str, ...] = ("I_avg", "I_min", "I_max", "V_avg", "V_min", "V_max")

#: Display labels, used by the UI and by exported column headers.
FIELD_LABELS: dict[str, str] = {
    "I_avg": "Current avg (A)",
    "I_min": "Current min (A)",
    "I_max": "Current max (A)",
    "V_avg": "Voltage avg (V)",
    "V_min": "Voltage min (V)",
    "V_max": "Voltage max (V)",
}


class BlockParseError(ValueError):
    """Raised when a response is not a well-formed definite-length block."""


def parse_definite_block(data: bytes, offset: int = 0) -> tuple[bytes, int]:
    """Parse one ``#<D><LLL><payload>`` block starting at *offset*.

    Returns the payload and the offset just past it.
    """
    view = memoryview(data)
    i = offset
    while i < len(view) and view[i : i + 1] in (b" ", b"\t", b"\r", b"\n"):
        i += 1
    if i >= len(view) or view[i : i + 1] != b"#":
        raise BlockParseError(
            f"expected '#' at offset {i}, found {bytes(view[i : i + 1])!r}"
        )
    i += 1
    if i >= len(view):
        raise BlockParseError("truncated block: missing length-of-length digit")
    ndigits_raw = bytes(view[i : i + 1])
    if not ndigits_raw.isdigit():
        raise BlockParseError(f"expected length-of-length digit, found {ndigits_raw!r}")
    ndigits = int(ndigits_raw)
    i += 1
    if ndigits == 0:
        # #0 is the indefinite-length form, terminated by NL+END. The N6705
        # does not use it for the queries this app issues.
        raise BlockParseError("indefinite-length blocks (#0) are not supported")
    length_raw = bytes(view[i : i + ndigits])
    if len(length_raw) != ndigits or not length_raw.isdigit():
        raise BlockParseError(f"truncated or non-numeric length field {length_raw!r}")
    length = int(length_raw)
    i += ndigits
    payload = bytes(view[i : i + length])
    if len(payload) != length:
        raise BlockParseError(
            f"truncated payload: header declared {length} bytes, got {len(payload)}"
        )
    return payload, i + length


def split_block_list(data: bytes) -> list[bytes]:
    """Parse a comma-separated list of definite-length blocks.

    This is the multi-channel ``FETCh:ELOG?`` response shape: one block per
    channel, in the order the channels were requested.
    """
    blocks: list[bytes] = []
    offset = 0
    n = len(data)
    while offset < n:
        payload, offset = parse_definite_block(data, offset)
        blocks.append(payload)
        while offset < n and data[offset : offset + 1] in (b" ", b"\t", b"\r", b"\n"):
            offset += 1
        if offset < n and data[offset : offset + 1] == b",":
            offset += 1
            continue
        break
    return blocks


def decode_real32(payload: bytes, byte_order: str = "SWAP") -> np.ndarray:
    """Decode a REAL block payload into float32 values.

    *byte_order* takes the ``FORMat:BORDer`` setting: ``NORM`` (big-endian) or
    ``SWAP`` (little-endian).
    """
    order = byte_order.strip().upper()
    if order.startswith("NORM"):
        dtype = np.dtype(">f4")
    elif order.startswith("SWAP"):
        dtype = np.dtype("<f4")
    else:
        raise ValueError(f"unknown byte order {byte_order!r}; expected NORM or SWAP")
    if len(payload) % 4:
        raise BlockParseError(
            f"REAL payload of {len(payload)} bytes is not a whole number of float32 values"
        )
    return np.frombuffer(payload, dtype=dtype).astype(np.float32, copy=True)


@dataclass(frozen=True)
class ElogFormat:
    """Which ELOG sense functions are enabled, and the record layout they imply.

    The field order is fixed by the manual (p.96) and does not depend on the
    order in which the functions were switched on.

    .. note::
       We assume a min/max pair is only present when its parent function is
       also enabled (e.g. ``I_min``/``I_max`` require current logging to be
       on).  The manual does not state this explicitly; it is flagged in
       ``PLAN.md`` section 10.3 for confirmation against real hardware.
    """

    current: bool = True
    voltage: bool = True
    current_minmax: bool = False
    voltage_minmax: bool = False

    @property
    def fields(self) -> tuple[str, ...]:
        """Record field names, in the order the instrument returns them."""
        names: list[str] = []
        if self.current:
            names.append("I_avg")
            if self.current_minmax:
                names += ["I_min", "I_max"]
        if self.voltage:
            names.append("V_avg")
            if self.voltage_minmax:
                names += ["V_min", "V_max"]
        return tuple(names)

    @property
    def values_per_record(self) -> int:
        return len(self.fields)

    @property
    def parameters_per_channel(self) -> int:
        """Number of logged parameters, as used by the minimum-period formula.

        ``SENSe:ELOG:PERiod`` (p.192): the actual minimum period is
        ``102.4 us x number of parameters being logged at each interval``.
        """
        return self.values_per_record

    def reshape(self, values: np.ndarray) -> np.ndarray:
        """Reshape a flat value array into ``(n_records, values_per_record)``."""
        width = self.values_per_record
        if width == 0:
            raise ValueError("no ELOG sense functions are enabled")
        if values.size % width:
            raise BlockParseError(
                f"{values.size} values do not divide into records of {width} "
                f"({'/'.join(self.fields)})"
            )
        return values.reshape(-1, width)

    def parse_response(
        self, data: bytes, byte_order: str = "SWAP"
    ) -> list[np.ndarray]:
        """Parse a full multi-channel ``FETCh:ELOG?`` REAL response.

        Returns one ``(n_records, values_per_record)`` array per channel, in
        the order the channels were requested.
        """
        return [
            self.reshape(decode_real32(block, byte_order))
            for block in split_block_list(data)
        ]


@dataclass(frozen=True)
class ElogSelection:
    """What the user ticked, per measurement quantity.

    The six quantities are independently selectable here, but the instrument
    exposes only **four** switches (p.188-191):

    ===========================  =========================================
    ``SENS:ELOG:FUNC:CURRent``   current average
    ``SENS:ELOG:FUNC:CURR:MINM`` current minimum **and** maximum, together
    ``SENS:ELOG:FUNC:VOLTage``   voltage average
    ``SENS:ELOG:FUNC:VOLT:MINM`` voltage minimum **and** maximum, together
    ===========================  =========================================

    So min and max cannot be enabled separately, and a min/max pair implies
    its parent function.  This class resolves the user's ticks into the
    switches the instrument actually needs (:attr:`hardware_format`) and then
    drops the columns that were not asked for (:meth:`filter`), so an unticked
    quantity is never written to file and never plotted.

    The gap between the two is not cosmetic: the minimum logging period scales
    with what the *hardware* measures, so ticking ``V_min`` alone still costs
    three parameters' worth of period.  Use :attr:`forced_fields` to explain
    that to the user rather than letting the period limit look arbitrary.
    """

    current_avg: bool = True
    current_min: bool = False
    current_max: bool = False
    voltage_avg: bool = True
    voltage_min: bool = False
    voltage_max: bool = False

    @classmethod
    def from_fields(cls, fields: object) -> "ElogSelection":
        """Build a selection from field names such as ``["V_avg", "I_max"]``."""
        wanted = {str(f) for f in fields}  # type: ignore[union-attr]
        unknown = wanted - set(FIELD_ORDER)
        if unknown:
            raise ValueError(f"unknown ELOG field(s): {sorted(unknown)}")
        return cls(
            current_avg="I_avg" in wanted,
            current_min="I_min" in wanted,
            current_max="I_max" in wanted,
            voltage_avg="V_avg" in wanted,
            voltage_min="V_min" in wanted,
            voltage_max="V_max" in wanted,
        )

    @property
    def fields(self) -> tuple[str, ...]:
        """Ticked quantities, in the instrument's canonical order."""
        ticked = {
            "I_avg": self.current_avg,
            "I_min": self.current_min,
            "I_max": self.current_max,
            "V_avg": self.voltage_avg,
            "V_min": self.voltage_min,
            "V_max": self.voltage_max,
        }
        return tuple(name for name in FIELD_ORDER if ticked[name])

    @property
    def is_empty(self) -> bool:
        return not self.fields

    @property
    def hardware_format(self) -> ElogFormat:
        """The four switches needed to obtain everything that was ticked."""
        return ElogFormat(
            current=self.current_avg or self.current_min or self.current_max,
            voltage=self.voltage_avg or self.voltage_min or self.voltage_max,
            current_minmax=self.current_min or self.current_max,
            voltage_minmax=self.voltage_min or self.voltage_max,
        )

    @property
    def forced_fields(self) -> tuple[str, ...]:
        """Quantities the instrument must measure although they were not ticked.

        These are discarded before recording or display, but they still count
        towards the minimum logging period.
        """
        return tuple(f for f in self.hardware_format.fields if f not in self.fields)

    @property
    def hardware_parameters(self) -> int:
        """Parameter count for the minimum-period formula.

        This is what the instrument measures, **not** what the user kept.
        """
        return self.hardware_format.parameters_per_channel

    @property
    def column_indices(self) -> tuple[int, ...]:
        """Positions of the ticked quantities within a hardware record."""
        hardware_fields = self.hardware_format.fields
        return tuple(hardware_fields.index(name) for name in self.fields)

    def filter(self, records: np.ndarray) -> np.ndarray:
        """Drop the columns that were not ticked.

        *records* is a ``(n_records, hardware_width)`` array as returned by
        :meth:`ElogFormat.reshape`.
        """
        if self.is_empty:
            raise ValueError("no ELOG quantities are selected")
        expected = self.hardware_format.values_per_record
        if records.ndim != 2 or records.shape[1] != expected:
            raise ValueError(
                f"expected records of width {expected} "
                f"({'/'.join(self.hardware_format.fields)}), got shape {records.shape}"
            )
        indices = self.column_indices
        if len(indices) == expected:
            return records
        return records[:, list(indices)]

    def parse_response(
        self, data: bytes, byte_order: str = "SWAP"
    ) -> list[np.ndarray]:
        """Parse a ``FETCh:ELOG?`` response down to the ticked columns only."""
        return [
            self.filter(records)
            for records in self.hardware_format.parse_response(data, byte_order)
        ]

    def scpi_commands(self, chanlist: str) -> list[str]:
        """Commands that put the instrument into this configuration."""
        fmt = self.hardware_format
        return [
            f"SENS:ELOG:FUNC:CURR {'ON' if fmt.current else 'OFF'},{chanlist}",
            f"SENS:ELOG:FUNC:CURR:MINM {'ON' if fmt.current_minmax else 'OFF'},{chanlist}",
            f"SENS:ELOG:FUNC:VOLT {'ON' if fmt.voltage else 'OFF'},{chanlist}",
            f"SENS:ELOG:FUNC:VOLT:MINM {'ON' if fmt.voltage_minmax else 'OFF'},{chanlist}",
        ]
