# -*- mode: python ; coding: utf-8 -*-
"""Build with ``pyinstaller packaging/n6705c_seq.spec`` from the repository root.

onedir, not onefile: onefile unpacks to a temp dir on every launch (slow start,
more antivirus false positives).  Two executables share one folder: the windowed
``N6705C-SEQ.exe`` and the console ``n6705c-seq-cli.exe`` (a windowed exe cannot
print, so the build self-test runs the CLI).

VISA is NOT bundled: USB needs Keysight IO Libraries or NI-VISA on the target PC.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
# pyvisa loads its backends by name, so nothing imports pyvisa_py statically.
hidden = [*collect_submodules("pyvisa_py"), "pyvisa.resources"]
excludes = ["matplotlib", "IPython", "pytest", "PyQt5", "PyQt6", "pyqtgraph", "pyarrow"]


def analysis(script):
    return Analysis([str(ROOT / "packaging" / script)], pathex=[str(ROOT / "src")],
                    hiddenimports=hidden, excludes=excludes)


gui, cli = analysis("gui_entry.py"), analysis("cli_entry.py")
MERGE((gui, "gui_entry", "N6705C-SEQ"), (cli, "cli_entry", "n6705c-seq-cli"))


def exe(a, name, console):
    return EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name=name,
               console=console, upx=False)  # UPX and code signing do not mix


exes = [exe(gui, "N6705C-SEQ", False), exe(cli, "n6705c-seq-cli", True)]
COLLECT(exes[0], gui.binaries, gui.datas, exes[1], cli.binaries, cli.datas,
        upx=False, name="N6705C-SEQ")
