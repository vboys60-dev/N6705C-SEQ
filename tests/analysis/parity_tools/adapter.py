"""唯一接觸 analysis.core.open_dlog 的地方：parity 工具與其他測試都經此取得 (meta, entry)。

T3 路徑 API 與 acquire_handle 的相容入口。
"""
from pathlib import Path

from n6705c_seq.analysis import core


def open_entry(dlog_path, cache_root) -> tuple[dict, dict]:
    """解析（或命中快取）.dlog，回傳 (meta, entry)。meta 含 handle；entry 為 core 的 memmap 句柄。"""
    cache_root = Path(cache_root)
    meta, _lod_blob = core.open_dlog(Path(dlog_path), cache_root)
    return meta, core.acquire_handle(meta["handle"], cache_root)
