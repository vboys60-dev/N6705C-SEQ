import glob
import os
import sys

import numpy as np

js_prefix, py_prefix = sys.argv[1], sys.argv[2]

js_files = sorted(glob.glob(js_prefix + ".*.f32"))
if not js_files:
    print("找不到 JS dump 檔案，前綴:", js_prefix)
    sys.exit(1)

all_ok = True
for jf in js_files:
    name = jf[len(js_prefix) + 1:-4]  # 去掉前綴與 .f32
    pf = py_prefix + "." + name + ".f32"
    if not os.path.exists(pf):
        print("MISSING py side:", name)
        all_ok = False
        continue
    a = np.fromfile(jf, dtype=np.float32)
    b = np.fromfile(pf, dtype=np.float32)
    if a.shape != b.shape:
        print("SHAPE MISMATCH:", name, a.shape, b.shape)
        all_ok = False
        continue
    if np.array_equal(a, b, equal_nan=True):
        print("OK   %-14s n=%d" % (name, a.size))
    else:
        diff = np.where(~np.isclose(a, b, equal_nan=True) & ~((np.isnan(a)) & (np.isnan(b))))[0]
        print("DIFF %-14s n=%d mismatches=%d  first few: %s" % (
            name, a.size, diff.size, list(zip(diff[:5], a[diff[:5]], b[diff[:5]]))
        ))
        all_ok = False

print()
print("=== 全部比對 %s ===" % ("PASS" if all_ok else "FAIL"))
