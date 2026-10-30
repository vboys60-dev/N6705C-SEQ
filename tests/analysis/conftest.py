"""tests/analysis 共用設定：樣本路徑、暫存快取目錄、每個測試清空 core 的 handle 快取。"""
import sys
from pathlib import Path

import pytest

from n6705c_seq.analysis import core

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"

# 讓 `from fixtures.make_big import make_big`、`from adapter import open_entry` 在任何執行方式下都成立
for _p in (HERE, HERE / "parity_tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture
def default_dlog() -> Path:
    return FIXTURES / "default_20260803_092740.dlog"


@pytest.fixture
def cache_root(tmp_path) -> Path:
    d = tmp_path / "cache"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _fresh_handles():
    def cleanup():
        # Tests may acquire multiple compatibility handles. Release them before
        # removing temp directories, including on Windows.
        for entry in list(core._open_cache.values()):
            while entry["_refs"]:
                core.release_handle(entry)
        assert core.close_all()
    cleanup()
    yield
    cleanup()
