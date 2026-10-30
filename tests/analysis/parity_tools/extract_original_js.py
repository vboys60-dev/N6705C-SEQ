#!/usr/bin/env python3
"""
從改造前的原版 HTML 抽出 JS 片段，供對拍工具使用。

對拍的意義是「Python 實作 vs 原版 JS 實作」，因此 JS 端一律從
original_before_python.html（sha1 見 contracts.ORIGINAL_HTML_SHA1）抽，不從新版 Viewer 抽。

產生三個檔案（中間檔，可重跑）：
  worker_line.js  — 內嵌 Worker 原始碼那一行（含 DLOG_WORKER_SRC 字串字面量）
  js_getters.js   — tintOf/getV/getI/getVal 等取值函式
  js_buckets.js   — computeViewBuckets/bucketsFromLodLevel/lodKeyFor

用法：python3 extract_original_js.py [輸出目錄，預設為本檔所在目錄]
測試端（test_parity.py）會指定暫存目錄，並以環境變數 PARITY_SRC 告訴 JS 腳本去那裡讀。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORIGINAL = HERE / "original_before_python.html"


def slice_functions(lines, first_fn, stop_fn, what):
    """
    抽出 [first_fn 起, stop_fn 前) 這段。原檔中相關函式本來就是連續排列的，
    用「到下一個函式為止」比逐一比對收尾大括號穩健得多。
    """
    start = next((i for i, l in enumerate(lines) if l.strip().startswith(first_fn)), None)
    if start is None:
        raise SystemExit("在原版檔中找不到 %s 的起點：%s" % (what, first_fn))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip().startswith(stop_fn)), None)
    if end is None:
        raise SystemExit("在原版檔中找不到 %s 的終點：%s" % (what, stop_fn))
    # 往回收掉尾端空白行與註解，只留程式碼本體
    while end > start and lines[end - 1].strip().startswith("//") or not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def extract(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = ORIGINAL.read_text(encoding="utf-8").split("\n")

    worker = next((l for l in lines if l.lstrip().startswith("const DLOG_WORKER_SRC =")), None)
    if worker is None:
        raise SystemExit("原版檔中找不到 DLOG_WORKER_SRC")
    (out_dir / "worker_line.js").write_text(worker + "\n", encoding="utf-8")

    getters = slice_functions(lines, "function tintOf(trace)", "function initTraces(", "取值函式")
    (out_dir / "js_getters.js").write_text("\n".join(getters) + "\n", encoding="utf-8")

    buckets = slice_functions(lines, "function computeViewBuckets(trace, nBuckets)",
                              "function computeAxisRangeFromBuckets(", "分桶函式")
    (out_dir / "js_buckets.js").write_text("\n".join(buckets) + "\n", encoding="utf-8")

    for name, needles in [
        ("js_getters.js", ["function tintOf", "function getV", "function getI", "function getVal"]),
        ("js_buckets.js", ["function computeViewBuckets", "function lodKeyFor",
                           "function bucketsFromLodLevel"]),
    ]:
        text = (out_dir / name).read_text(encoding="utf-8")
        missing = [n for n in needles if n not in text]
        if missing:
            raise SystemExit("%s 抽取不完整，缺少：%s" % (name, ", ".join(missing)))


if __name__ == "__main__":
    extract(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE)
    print("JS 片段已抽出：%s" % ORIGINAL.name)
