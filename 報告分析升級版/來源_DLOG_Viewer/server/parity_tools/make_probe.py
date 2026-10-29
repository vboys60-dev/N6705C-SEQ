"""
產生一份「注入探針」的 viewer 複本，供瀏覽器自動化驗證用。

viewer 的內部狀態與函式全包在 (function(){...})() 裡，頁面外的 JS 取不到，
自動化就只能靠截圖猜。這裡在 IIFE 收尾前插入一個 window.__probe 物件，
其餘一個字元都不動——因此驗到的就是正式檔的邏輯本身，不是另一份複製品。

    python3 make_probe.py new    # → ../../__probe.html      （目前版本）
    python3 make_probe.py old    # → ../../__probe_old.html  （V1.4 對照組）

接著透過 server 開 http://127.0.0.1:8765/__probe.html，用瀏覽器自動化
呼叫 window.__probe.* 取值、與 /buckets、/stats、/export/csv 逐值比對。

產生的 __probe*.html 是**臨時檔，驗完請刪掉**，別跟正式檔混在一起。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# 新版（Python 資料層）與舊版（V1.4，全載記憶體）內部函式名稱不同，
# 各自只掛得到的東西。舊版是拿來做「同一個瀏覽器環境」的效能對照組——
# 拿 Node 上的數字跟瀏覽器每幀比是不成立的（見交接文件 §4.5）。
PROBE_NEW = """
  // ===== 驗證用探針（make_probe.py 注入，不在正式檔內）=====
  window.__probe = {
    state, markerValues, computeViewBuckets, computeRangeStats, refreshStats,
    ensureExportData, csvRangeIdx, csvExportColumns, sampleAt, getVal,
    rawWindows, pointCache, statsCache, lodKeyFor, tintOf, nRecOfTrace,
    clampIdxTrace, handleOf, currentViewIdxRange, offsetParams, qs,
    exportBaseName, renderMarkerTable, updateStatsPanel, draw, scheduleRedraw,
    get statsFrozenForDrag(){ return statsFrozenForDrag; },
  };
"""

PROBE_OLD = """
  // ===== 對照組探針（舊版 V1.4）=====
  window.__probe = {
    state, computeViewBuckets, computeRangeStats, getVal, tintOf, nRecOfTrace,
    clampIdxTrace, scheduleRedraw, draw,
  };
"""

TARGETS = {
    "new": ("dlog_viewer_mem3_preview.html", "__probe.html", PROBE_NEW),
    # 對照組指向改造前的備份。原本還有一份內容完全相同的 dlog_viewer_mem3_V1.4.html，
    # 已於 2026-08-14 刪除（sha1 兩者皆為 0117e08d3301…）。
    # 2026-10-14 起改指 parity_tools/original_before_python.html（BACKUP 檔已套用修正）
    "old": ("server/parity_tools/original_before_python.html", "__probe_old.html", PROBE_OLD),
}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "new"
    if which not in TARGETS:
        sys.exit("用法：make_probe.py [new|old]")
    src_name, dst_name, probe = TARGETS[which]
    src = (ROOT / src_name).read_text(encoding="utf-8")
    i = src.rindex("})();")   # 最後一個 IIFE 收尾即整支 script 的結尾
    (ROOT / dst_name).write_text(src[:i] + probe + src[i:], encoding="utf-8")
    print("已產生", ROOT / dst_name, "（臨時檔，驗完請刪除）")


if __name__ == "__main__":
    main()
