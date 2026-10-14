# -*- mode: python ; coding: utf-8 -*-
"""Build with ``pyinstaller packaging/n6705c_seq.spec`` from the repository root.

onedir, not onefile: onefile unpacks to a temp dir on every launch (slow start,
more antivirus false positives).  Two executables share one folder: the windowed
``N6705C-SEQ.exe`` and the console ``n6705c-seq-cli.exe`` (a windowed exe cannot
print, so the build self-test runs the CLI).

Vendor VISA is NOT bundled.  USB works either through Keysight IO Libraries / NI-VISA
on the target PC, or through pyvisa-py + pyusb + the libusb DLL bundled below (the
instrument's USB driver must then be WinUSB, e.g. via Zadig).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
# pyvisa loads its backends by name, so nothing imports pyvisa_py statically.
# pyvisa_py.testsuite is test-only and drags in pyvisa.testsuite, which crashes
# PyInstaller's isolated-subprocess binary-dependency analysis on Windows; exclude it.
hidden = [m for m in collect_submodules("pyvisa_py") if not m.startswith("pyvisa_py.testsuite")]
hidden.append("pyvisa.resources")
hidden += collect_submodules("usb") + ["libusb_package"]
usb_binaries = collect_dynamic_libs("libusb_package")
excludes = ["matplotlib", "IPython", "pytest", "PyQt5", "PyQt6", "pyqtgraph", "pyarrow",
            "pyvisa.testsuite", "pyvisa_py.testsuite"]


def analysis(script):
    return Analysis([str(ROOT / "packaging" / script)], pathex=[str(ROOT / "src")],
                    binaries=usb_binaries, datas=collect_data_files("sv_ttk"), hiddenimports=hidden, excludes=excludes)


gui, cli = analysis("gui_entry.py"), analysis("cli_entry.py")
MERGE((gui, "gui_entry", "N6705C-SEQ"), (cli, "cli_entry", "n6705c-seq-cli"))


def exe(a, name, console):
    return EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name=name,
               console=console, upx=False)  # UPX and code signing do not mix


exes = [exe(gui, "N6705C-SEQ", False), exe(cli, "n6705c-seq-cli", True)]
COLLECT(exes[0], gui.binaries, gui.datas, exes[1], cli.binaries, cli.datas,
        upx=False, name="N6705C-SEQ")
