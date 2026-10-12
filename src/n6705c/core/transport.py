"""Byte-level transports.

Everything above this module talks to an instrument through the `Transport`
protocol, so the same stack drives a real N6705C over VISA or the built-in
simulator in :mod:`n6705c.core.fake`.
"""

from __future__ import annotations

import multiprocessing as mp
import time
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
            "  - install the pure-Python backend (LAN, plus USB when pyusb and "
            "libusb are present):\n"
            "        python3 -m pip install pyvisa-py pyusb libusb-package"
        )
    return ""


def _is_socket_resource(resource: str) -> bool:
    """True for ``TCPIP...::<port>::SOCKET`` style resources.

    Raw sockets carry no END indicator, so a read terminator must be set
    explicitly; VXI-11 / USB / GPIB ``INSTR`` sessions do not need one.
    """
    return resource.strip().upper().endswith("::SOCKET")


def _probe_native_visa(conn) -> None:
    """Child-process target: try opening the native (IVI) VISA backend.

    A vendor VISA that is only partially installed -- the IVI shared router
    DLL present in system32 with no vendor backend actually registered behind
    it -- does not raise a Python exception when opened; it crashes the
    process outright (seen as STATUS_STACK_BUFFER_OVERRUN on Windows). Doing
    the probe in a throwaway child process means that crash takes down the
    probe, not the GUI.
    """
    try:
        import pyvisa

        rm = pyvisa.ResourceManager("")
        rm.close()
        conn.send(True)
    except Exception:
        conn.send(False)
    finally:
        conn.close()


_native_backend_safe_cache: bool | None = None


def _native_backend_safe(timeout_s: float = 8.0) -> bool:
    """Whether the native VISA backend can be opened without crashing.

    Cached for the life of the process: the answer cannot change while this
    program is running, and checking it costs a subprocess spawn. A real
    crash (seen in testing) takes ~30s to actually surface -- rather than
    wait that out, treat "no answer within timeout_s" the same as "crashed",
    since both mean the native backend is not safe to use right now.
    """
    global _native_backend_safe_cache
    if _native_backend_safe_cache is None:
        parent_conn, child_conn = mp.Pipe(duplex=False)
        proc = mp.Process(target=_probe_native_visa, args=(child_conn,), daemon=True)
        ok = False
        deadline = time.monotonic() + timeout_s
        try:
            proc.start()
            child_conn.close()
            if parent_conn.poll(max(0.0, deadline - time.monotonic())):
                ok = parent_conn.recv()
            proc.join(max(0.0, deadline - time.monotonic()))
            if proc.is_alive():
                proc.terminate()
                proc.join()
        except Exception:
            ok = False
        finally:
            parent_conn.close()
        _native_backend_safe_cache = ok
    return _native_backend_safe_cache


def _register_libusb() -> None:
    """Point pyusb at the libusb DLL shipped by ``libusb-package``.

    pyusb only searches system locations, so without this a bundled/venv
    install has no libusb and pyvisa-py silently offers no USB resources.
    Harmless no-op when either package is missing.
    """
    try:
        import libusb_package
        import usb.backend.libusb1 as libusb1

        libusb1.get_backend(find_library=libusb_package.find_library)
        import usb.core

        _orig_find = usb.core.find
        if getattr(_orig_find, "_n6705c_patched", False):
            return

        def find(*args, **kwargs):
            kwargs.setdefault("backend", libusb_package.get_libusb1_backend())
            return _orig_find(*args, **kwargs)

        find._n6705c_patched = True
        usb.core.find = find
    except Exception:  # pragma: no cover - optional dependency
        pass


def _candidates(backend: str) -> list[str]:
    """Backend order for ``backend='auto'``, with the native backend dropped
    if opening it is known to crash the process (see :func:`_native_backend_safe`)."""
    if backend != "auto":
        return [backend]
    return ["", "@py"] if _native_backend_safe() else ["@py"]


class VisaTransport:
    """PyVISA-backed transport.

    ``backend='auto'`` prefers a native VISA implementation (NI-VISA or
    Keysight IO Libraries) and falls back to ``pyvisa-py``.

    Which one is in use decides what interfaces exist, on every platform:
    a native VISA gives LAN, USB and GPIB, while ``pyvisa-py`` gives LAN and,
    with ``pyusb`` plus a libusb backend (and a WinUSB driver on Windows), USB
    (USBTMC); it has no GPIB.
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
        candidates = _candidates(backend)
        last_error: Exception | None = None
        errors: list[str] = []
        for candidate in candidates:
            try:
                if candidate == "@py":
                    _register_libusb()
                rm = pyvisa.ResourceManager(candidate)
                inst = rm.open_resource(resource)
            except Exception as exc:  # pragma: no cover - depends on host setup
                last_error = exc
                errors.append(f"  [{candidate or 'ivi'}] {exc}")
                continue
            self._rm = rm
            self._inst = inst
            self._backend_used = candidate or "ivi"
            break
        else:  # pragma: no cover - depends on host setup
            hint = ""
            if "" not in candidates and resource.strip().upper().startswith(("USB", "GPIB")):
                hint = (
                    "\n\n原生 VISA 未啟用（探測未通過），只嘗試了 pyvisa-py；"
                    "USB/GPIB 需要原生 VISA（Keysight IO Libraries 或 NI-VISA）。"
                )
            raise TransportError(
                f"could not open {resource!r}; tried backends {candidates}:\n"
                + "\n".join(errors)
                + hint
                + _backend_advice(last_error)
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
    VISA installed has no native USB/GPIB just the same.  Ask the loader, not
    the OS.
    """
    return _native_backend_safe()


def list_resources(backend: str = "auto") -> list[str]:
    """Enumerate visible VISA resources; returns [] if no backend is usable."""
    import pyvisa

    for candidate in _candidates(backend):
        try:
            if candidate == "@py":
                _register_libusb()
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
