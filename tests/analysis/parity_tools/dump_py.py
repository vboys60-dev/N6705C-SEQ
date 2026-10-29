#!/usr/bin/env python3
"""Python 端 dump：把 analysis.core 的 V/I 與 LOD 寫成與 run_worker.js 相同檔名的 raw float32。

用法：python dump_py.py <dlog路徑> <輸出前綴> <快取目錄>
stdout 最後一行為 meta JSON（欄位與 run_worker.js 的 stdout 相同），供 test_parity 比對結構。
"""
import json
import sys
from pathlib import Path

import numpy as np

from adapter import open_entry

META_KEYS = ("tint", "declaredTime", "nRecords", "channelModels", "activeChannels", "fields", "minmax")


def dump(dlog_path, out_prefix, cache_root) -> dict:
    meta, entry = open_entry(dlog_path, cache_root)

    def write(name, arr):
        np.asarray(arr, dtype=np.float32).tofile("%s.%s.f32" % (out_prefix, name))

    for ch in meta["activeChannels"]:
        write("V%d" % ch, entry["V"][ch])
        write("I%d" % ch, entry["I"][ch])
        for kind in ("V", "I", "P"):
            for level in ("l0", "l1"):
                for which in ("min", "max"):
                    write("%s%d_%s%s" % (kind, ch, level, which), entry["lod"]["%s%d_%s%s" % (kind, ch, level, which)])
    return {k: meta[k] for k in META_KEYS}


if __name__ == "__main__":
    dlog, prefix, cache = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    print(json.dumps(dump(dlog, prefix, cache), indent=2))
