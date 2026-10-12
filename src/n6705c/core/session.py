"""SCPI session: serialisation, error checking and command logging.

A VISA session is not thread-safe, so every exchange goes through one lock
here and all instrument I/O is expected to happen on a single worker thread
(PLAN.md section 3.3).  The lock is a safety net, not a licence to call from
many threads.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass

from .transport import Transport

__all__ = ["ScpiError", "InstrumentError", "Session", "LoggedCommand"]

log = logging.getLogger(__name__)


class InstrumentError(RuntimeError):
    """Base class for instrument-reported failures."""


@dataclass
class ScpiError(InstrumentError):
    """One entry drained from the instrument's error queue."""

    code: int
    message: str
    command: str = ""

    def __str__(self) -> str:
        where = f" (after {self.command!r})" if self.command else ""
        return f"SCPI error {self.code:+d}: {self.message}{where}"


@dataclass(frozen=True)
class LoggedCommand:
    """One exchange, kept in a ring buffer for the UI's command log."""

    direction: str  # "->" write, "<-" query response
    text: str


class Session:
    """Serialised SCPI access with automatic error-queue draining."""

    def __init__(self, transport: Transport, *, log_size: int = 500) -> None:
        self._transport = transport
        self._lock = threading.RLock()
        self.history: deque[LoggedCommand] = deque(maxlen=log_size)

    @property
    def description(self) -> str:
        return self._transport.description

    # -- Raw exchanges ------------------------------------------------------

    def write(self, command: str) -> None:
        with self._lock:
            self.history.append(LoggedCommand("->", command))
            self._transport.write(command)

    def query(self, command: str) -> str:
        with self._lock:
            self.history.append(LoggedCommand("->", command))
            response = self._transport.query(command)
            self.history.append(LoggedCommand("<-", response))
            return response

    def query_binary(self, command: str) -> bytes:
        with self._lock:
            self.history.append(LoggedCommand("->", command))
            payload = self._transport.query_binary(command)
            self.history.append(LoggedCommand("<-", f"<{len(payload)} bytes>"))
            return payload

    # -- Typed queries ------------------------------------------------------

    def query_float(self, command: str) -> float:
        return float(self.query(command))

    def query_int(self, command: str) -> int:
        # Some queries answer in NR3 even where the manual says NR1.
        return int(float(self.query(command)))

    def query_floats(self, command: str) -> list[float]:
        response = self.query(command)
        return [float(p) for p in response.split(",") if p.strip()]

    def query_strings(self, command: str) -> list[str]:
        return [p.strip().strip('"') for p in self.query(command).split(",")]

    # -- Error handling -----------------------------------------------------

    def read_errors(self) -> list[ScpiError]:
        """Drain ``SYSTem:ERRor?`` until the queue reports no error."""
        errors: list[ScpiError] = []
        with self._lock:
            for _ in range(50):  # bounded, so a stuck queue cannot hang us
                response = self._transport.query("SYST:ERR?")
                code, _, message = response.partition(",")
                try:
                    code_value = int(float(code))
                except ValueError:
                    log.warning("unparsable error-queue response: %r", response)
                    break
                if code_value == 0:
                    break
                errors.append(ScpiError(code_value, message.strip().strip('"')))
        return errors

    def check_errors(self, context: str = "") -> None:
        """Raise if the instrument queued any error.

        Errors are never swallowed: the original SCPI code and text reach the
        user unchanged (PLAN.md section 9).
        """
        errors = self.read_errors()
        if not errors:
            return
        for error in errors:
            error.command = context
        if len(errors) == 1:
            raise errors[0]
        joined = "; ".join(str(e) for e in errors)
        raise InstrumentError(f"{len(errors)} SCPI errors: {joined}")

    def write_checked(self, command: str) -> None:
        """Write, then verify the instrument accepted it."""
        self.write(command)
        self.check_errors(command)

    # -- Lifecycle ----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._transport.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
