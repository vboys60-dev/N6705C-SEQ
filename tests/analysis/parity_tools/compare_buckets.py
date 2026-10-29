"""
把 analysis.core.compute_buckets() 的輸出與 JS 原班 computeViewBuckets() 的輸出逐 float32 比對。
三條路徑（逐筆 / LOD / 逐筆掃描分桶）各自涵蓋。

校正開啟的案例只驗結構（契約 §8）：JS 用 Math.random()，數值本來就不同；
這裡只驗長度、t 逐位元組相同、所有值有限，且 I 的 min/max 全 >= 0。

用法：python compare_buckets.py <dlog路徑> <js輸出前綴> <快取目錄>
失敗時退出碼非 0。
"""
import json
import sys
from pathlib import Path

import numpy as np

from adapter import open_entry
from n6705c_seq.analysis import core


def main(dlog_path, js_prefix, cache_root) -> int:
    _meta, entry = open_entry(dlog_path, cache_root)
    info = json.loads(Path(js_prefix + ".cases.json").read_text(encoding="utf-8"))
    all_ok = True

    for ci, c in enumerate(info["cases"]):
        t_js = np.fromfile("%s.case%d.t.f32" % (js_prefix, ci), dtype=np.float32)
        mn_js = np.fromfile("%s.case%d.min.f32" % (js_prefix, ci), dtype=np.float32)
        mx_js = np.fromfile("%s.case%d.max.f32" % (js_prefix, ci), dtype=np.float32)

        t_py, mn_py, mx_py = core.compute_buckets(
            entry, c["kind"], c["ch"], c["idxLoActual"], c["idxHiActual"],
            c["nBuckets"], bool(c.get("offset")), c.get("maxLimit", 0.0))
        t_py = t_py.astype(np.float32)
        mn_py = mn_py.astype(np.float32)
        mx_py = mx_py.astype(np.float32)

        label = "case%-2d %s%d idx[%d..%d] n=%d%s" % (
            ci, c["kind"], c["ch"], c["idxLoActual"], c["idxHiActual"], c["nBuckets"],
            " offset" if c.get("offset") else "")

        if c.get("offset"):
            ok_len = t_js.shape == t_py.shape == mn_py.shape == mx_py.shape
            ok_t = ok_len and np.array_equal(t_js, t_py)
            ok_finite = bool(np.isfinite(mn_py).all() and np.isfinite(mx_py).all())
            ok_sign = True
            if c["kind"] == "I":
                ok_sign = bool((mn_py >= 0).all() and (mx_py >= 0).all())
            ok = ok_t and ok_finite and ok_sign
            all_ok &= ok
            print("%s %-46s [offset: 驗結構] len=%s t相同=%s 有限=%s I>=0=%s" % (
                "OK  " if ok else "FAIL", label, ok_len, ok_t, ok_finite, ok_sign))
            continue

        if t_js.shape != t_py.shape:
            print("SHAPE %-46s js=%d py=%d" % (label, t_js.size, t_py.size))
            all_ok = False
            continue

        same = np.array_equal(t_js, t_py) and np.array_equal(mn_js, mn_py) and np.array_equal(mx_js, mx_py)
        if same:
            print("OK   %-46s n=%d" % (label, t_js.size))
        else:
            bad_t = int((t_js != t_py).sum())
            bad_mn = int((mn_js != mn_py).sum())
            bad_mx = int((mx_js != mx_py).sum())
            print("DIFF %-46s t=%d min=%d max=%d" % (label, bad_t, bad_mn, bad_mx))
            if bad_mn:
                i = int(np.nonzero(mn_js != mn_py)[0][0])
                print("      first min diff @%d: js=%r py=%r" % (i, mn_js[i], mn_py[i]))
            all_ok = False

    print()
    print("=== /buckets 對拍 %s ===" % ("PASS" if all_ok else "FAIL"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
