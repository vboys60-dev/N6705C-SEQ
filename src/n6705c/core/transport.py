"""Byte-level transports.

Everything above this module talks to an instrument through the `Transport`
protocol, so the same stack drives a real N6705C over VISA or the built-in
simulator in :mod:`n6705c.core.fake`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["Transport", "TransportError", "VisaTransport", "list_resources"]


class TransportError(RuntimeError):
    """Communication failure below the SCPI layer."""


@runtime_checkable
class Transport(Protocol):
    """Minimal instrument I/O surface."""

    @property
    def description(self) -> str:
        """Human-readable identification of the link, for logs and the UI."""
        ...

    def write(self, command: str) -> None: ...

    def query(self, command: str) -> str: ...

    def query_binary(self, command: str) -> bytes:
        """Query a command whose response is raw bytes (block responses)."""
        ...

    def close(self) -> None: ...


def _backend_advice(error: object) -> str:
    """Turn "no package named pyvisa_py" into something actionable.

    This fires only when *no* backend could be loaded -- a native VISA is
    tried first and is the better answer when one is available.
    """
    text = str(error).lower()
    if "pyvisa_py" in text or "pyvisa-py" in text:
        return (
            "\n\nNo VISA backend is installed. Either:\n"
            "  - install a vendor VISA (NI-VISA is available for macOS as "
            "well as Windows and Linux, and covers LAN, USB and GPIB), or\n"
            "  - install the pure-Python backend, which is LAN only:\n"
            "        python3 -m pip install pyvisa-py"
        )
    return ""


def _is_socket_resource(resource: str) -> bool:
    """True for ``TCPIP...::<port>::SOCKET`` style resources.

    Raw sockets carry no END indicator, so a read terminator must be set
    explicitly; VXI-11 / USB / GPIB ``INSTR`` sessions do not need one.
    """
    return resource.strip().upper().endswith("::SOCKET")


class VisaTransport:
    """PyVISA-backed transport.

    ``backend='auto'`` prefers a native VISA implementation (NI-VISA or
    Keysight IO Libraries) and falls back to ``pyvisa-py``.

    Which one is in use decides what interfaces exist, on every platform:
    a native VISA gives LAN, USB and GPIB, while ``pyvisa-py`` is LAN only.
    That split is *not* a macOS-versus-Windows split -- NI-VISA ships for
    macOS too, so a Mac with it installed reaches USB and GPIB instruments
    exactly as Windows does.  Prefer reporting :attr:`backend_used` over
    guessing from ``sys.platform``.
    """

    def __init__(
        self,
        resource: str,
        *,
        backend: str = "auto",
        timeout_ms: int = 10_000,
        chunk_size: int = 1 << 20,
    ) -> None:
        import pyvisa

        self._resource_name = resource
        self._backend_used = ""
        candidates = ["", "@py"] if backend == "auto" else [backend]
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                rm = pyvisa.ResourceManager(candidate)
                inst = rm.open_resource(resource)
            except Exception as exc:  # pragma: no cover - depends on host setup
                last_error = exc
                continue
            self._rm = rm
            self._inst = inst
            self._backend_used = candidate or "ivi"
            break
        else:  # pragma: no cover - depends on host setup
            raise TransportError(
                f"could not open {resource!r} with backend {backend!r}: "
                f"{last_error}{_backend_advice(last_error)}"
            ) from last_error

        self._inst.timeout = timeout_ms
        self._inst.chunk_size = chunk_size
        self._inst.write_termination = "\n"
        self._inst.read_termination = "\n" if _is_socket_resource(resource) else None

    @property
    def description(self) -> str:
        return f"{self._resource_name} (backend={self._backend_used})"

    @property
    def timeout_ms(self) -> int:
        return int(self._inst.timeout)

    @timeout_ms.setter
    def timeout_ms(self, value: int) -> None:
        self._inst.timeout = value

    def write(self, command: str) -> None:
        try:
            self._inst.write(command)
        except Exception as exc:
            raise TransportError(f"write failed for {command!r}: {exc}") from exc

    def query(self, command: str) -> str:
        try:
            return self._inst.query(command).strip()
        except Exception as exc:
            raise TransportError(f"query failed for {command!r}: {exc}") from exc

    def query_binary(self, command: str) -> bytes:
        try:
            self._inst.write(command)
            return bytes(self._inst.read_raw())
        except Exception as exc:
            raise TransportError(f"binary query failed for {command!r}: {exc}") from exc

    def close(self) -> None:
        try:
            self._inst.close()
        finally:
            self._rm.close()


def native_visa_available() -> bool:
    """Whether a vendor VISA (``@ivi``) can be loaded here.

    This is the honest way to ask "can we reach USB and GPIB instruments?".
    The old answer was ``sys.platform != "darwin"``, which is wrong in both
    directions: NI-VISA ships for macOS, and a Windows box without any vendor
    VISA installed is LAN-only just the same.  Ask the loader, not the OS.
    """
    try:
        import pyvisa

        rm = pyvisa.ResourceManager("")
    except Exception:
        return False
    rm.close()
    return True


def list_resources(backend: str = "auto") -> list[str]:
    """Enumerate visible VISA resources; returns [] if no backend is usable."""
    import pyvisa

    for candidate in (["", "@py"] if backend == "auto" else [backend]):
        try:
            rm = pyvisa.ResourceManager(candidate)
        except Exception:  # pragma: no cover - depends on host setup
            continue
        try:
            return list(rm.list_resources())
        except Exception:  # pragma: no cover
            continue
        finally:
            rm.close()
    return []
