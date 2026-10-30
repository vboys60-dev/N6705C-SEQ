#!/usr/bin/env python3
"""
從「改造前的備份 HTML」抽出原版 JS 片段，供對拍工具使用。

為什麼要從備份抽：對拍的意義是「Python 實作 vs 原版 JS 實作」。
改造後的 dlog_viewer_mem3_preview.html 已經沒有 DLOG_WORKER_SRC／parseDlog 了，
唯一還保有原版邏輯的就是備份檔，因此對拍基準一律以備份為準。

產生三個檔案（都是 git-ignorable 的中間檔，隨時可重跑）：
  worker_line.js  — 內嵌 Worker 原始碼那一行（含 DLOG_WORKER_SRC 字串字面量）
  js_getters.js   — tintOf/getV/getI/getVal 等取值函式
  js_buckets.js   — computeViewBuckets/bucketsFromLodLevel/lodKeyFor

用法：python3 extract_original_js.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).parent
PROJECT = HERE.parent.parent
# 首選 original_before_python.html：專案根目錄的 BACKUP 檔已於 2026-10-14 套用 Codex 審查修正，
# 不再是逐位元組的原版；這份是修正前的原樣副本（sha1 0117e08d3301…），對拍一律以它為準。
BACKUP_CANDIDATES = [
    HERE / "original_before_python.html",
    PROJECT / "dlog_viewer_mem3_V1.4.html",
]


def find_backup() -> Path:
    for p in BACKUP_CANDIDATES:
        if p.exists():
            return p
    raise SystemExit(
        "找不到改造前的備份 HTML，對拍無法進行。預期以下其中之一存在：\n  "
        + "\n  ".join(str(p) for p in BACKUP_CANDIDATES)
    )


def slice_functions(lines, first_fn, stop_fn, what):
    """
    抽出 [first_fn 起, stop_fn 前) 這段。原檔中相關函式本來就是連續排列的，
    用「到下一個函式為止」比逐一比對收尾大括號穩健得多。
    """
    start = next((i for i, l in enumerate(lines) if l.strip().startswith(first_fn)), None)
    if start is None:
        raise SystemExit("在備份檔中找不到 %s 的起點：%s" % (what, first_fn))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip().startswith(stop_fn)), None)
    if end is None:
        raise SystemExit("在備份檔中找不到 %s 的終點：%s" % (what, stop_fn))
    # 往回收掉尾端空白行與註解，只留程式碼本體
    while end > start and lines[end - 1].strip().startswith("//") or not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def main():
    backup = find_backup()
    lines = backup.read_text(encoding="utf-8").split("\n")
    print("對拍基準：%s" % backup.name)

    worker = next((l for l in lines if l.lstrip().startswith("const DLOG_WORKER_SRC =")), None)
    if worker is None:
        raise SystemExit("備份檔中找不到 DLOG_WORKER_SRC")
    (HERE / "worker_line.js").write_text(worker + "\n", encoding="utf-8")
    print("  worker_line.js  (%d 字元)" % len(worker))

    # tintOf … getVal，止於 initTraces
    getters = slice_functions(lines, "function tintOf(trace)", "function initTraces(", "取值函式")
    (HERE / "js_getters.js").write_text("\n".join(getters) + "\n", encoding="utf-8")
    print("  js_getters.js   (%d 行)" % len(getters))

    # computeViewBuckets / lodKeyFor / bucketsFromLodLevel 三者連續，止於 computeAxisRangeFromBuckets
    buckets = slice_functions(lines, "function computeViewBuckets(trace, nBuckets)",
                              "function computeAxisRangeFromBuckets(", "分桶函式")
    (HERE / "js_buckets.js").write_text("\n".join(buckets) + "\n", encoding="utf-8")
    print("  js_buckets.js   (%d 行)" % len(buckets))

    for name, needles in [
        ("js_getters.js", ["function tintOf", "function getV", "function getI", "function getVal"]),
        ("js_buckets.js", ["function computeViewBuckets", "function lodKeyFor",
                           "function bucketsFromLodLevel"]),
    ]:
        text = (HERE / name).read_text(encoding="utf-8")
        missing = [n for n in needles if n not in text]
        if missing:
            raise SystemExit("%s 抽取不完整，缺少：%s" % (name, ", ".join(missing)))

    print("\n完成。接著可執行：")
    print("  node run_worker.js <dlog路徑> ./js_out        # 解析器對拍")
    print("  node run_buckets.js <dlog路徑> ./jsb cases_default.json && \\")
    print("    python3 compare_buckets.py <dlog路徑> ./jsb  # 分桶對拍")


if __name__ == "__main__":
    main()
