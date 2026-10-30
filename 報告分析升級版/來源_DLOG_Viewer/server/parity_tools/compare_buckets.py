"""
把 Python compute_buckets() 的輸出與 JS 原班 computeViewBuckets() 的輸出逐 float32 比對。
三條路徑（逐筆 / LOD / 逐筆掃描分桶）各自涵蓋。
用法：python3 compare_buckets.py <dlog路徑> <js輸出前綴>
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))  # server/
import dlog_core  # noqa: E402

# 對拍用獨立快取，不動到 server 正式使用的 server/.cache
CACHE = Path(__file__).parent / ".cache_parity"

dlog_path, js_prefix = sys.argv[1], sys.argv[2]

data = Path(dlog_path).read_bytes()
meta, _lod_blob = dlog_core.open_dlog(data, CACHE)
entry = dlog_core.get_handle(meta["handle"], CACHE)

info = json.loads(Path(js_prefix + ".cases.json").read_text())
all_ok = True

for ci, c in enumerate(info["cases"]):
    t_js = np.fromfile("%s.case%d.t.f32" % (js_prefix, ci), dtype=np.float32)
    mn_js = np.fromfile("%s.case%d.min.f32" % (js_prefix, ci), dtype=np.float32)
    mx_js = np.fromfile("%s.case%d.max.f32" % (js_prefix, ci), dtype=np.float32)

    t_py, mn_py, mx_py = dlog_core.compute_buckets(
        entry, c["kind"], c["ch"], c["idxLoActual"], c["idxHiActual"],
        c["nBuckets"], bool(c.get("offset")), c.get("maxLimit", 0.0))
    t_py = t_py.astype(np.float32)
    mn_py = mn_py.astype(np.float32)
    mx_py = mx_py.astype(np.float32)

    label = "case%-2d %s%d idx[%d..%d] n=%d%s" % (
        ci, c["kind"], c["ch"], c["idxLoActual"], c["idxHiActual"], c["nBuckets"],
        " offset" if c.get("offset") else "")

    if t_js.shape != t_py.shape:
        print("SHAPE %-46s js=%d py=%d" % (label, t_js.size, t_py.size))
        all_ok = False
        continue

    if c.get("offset"):
        # 校正開啟時 JS 用 Math.random()、Python 用索引推導的決定性雜湊，
        # 數值本來就不會一樣。這裡只驗結構（桶數、時間軸）與「不得出現負值」。
        ok_t = np.array_equal(t_js, t_py)
        ok_pos = bool((mn_py > 0).all() and (mx_py > 0).all())
        status = "OK  " if (ok_t and ok_pos) else "FAIL"
        if not (ok_t and ok_pos):
            all_ok = False
        print("%s %-46s [offset: 驗結構+非負] t相同=%s 全正=%s" % (status, label, ok_t, ok_pos))
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
sys.exit(0 if all_ok else 1)
