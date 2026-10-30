"""契約 §10：analysis/*.py 只准 import stdlib、numpy、n6705c_seq.analysis.*；
子進程 import core 後不得載入 pyvisa／usb／webview／n6705c（儀器端）。"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
ANALYSIS = SRC / "n6705c_seq" / "analysis"
PKG = "n6705c_seq.analysis"


def _allowed(name: str) -> bool:
    top = name.split(".")[0]
    return top in sys.stdlib_module_names or top == "numpy" or name == PKG or name.startswith(PKG + ".")


def _absolute_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 套件內相對 import
                continue
            yield node.module or "", node.lineno


@pytest.mark.parametrize("path", sorted(ANALYSIS.glob("*.py")), ids=lambda p: p.name)
def test_analysis_imports_are_allowed(path):
    bad = [(name, line) for name, line in _absolute_imports(path) if not _allowed(name)]
    assert not bad, bad


def test_core_import_loads_no_hardware_or_gui():
    code = (
        "import sys; sys.path.insert(0, %r); import n6705c_seq.analysis.core; "
        "bad = [m for m in ('pyvisa', 'usb', 'webview', 'n6705c') if m in sys.modules]; "
        "print(bad); sys.exit(1 if bad else 0)"
    ) % str(SRC)
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
