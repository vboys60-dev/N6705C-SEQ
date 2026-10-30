# 對拍工具（parity tools）

這些工具用來證明**Python 資料層的輸出，與改造前的原版 JS 邏輯逐位元組相同**。
改動 `server/dlog_core.py` 之後應重跑一次，這是唯一的數值回歸防線。

## 為什麼對拍基準是備份檔

對拍的意義是「Python 實作 vs 原版 JS 實作」。改造後的
`dlog_viewer_mem3_preview.html` 已經沒有 `DLOG_WORKER_SRC`／`parseDlog` 了，
唯一還保有原版邏輯的是 `parity_tools/original_before_python.html`（sha1 0117e08d3301…）。
專案根目錄的 `dlog_viewer_mem3_preview.BACKUP-before-python.html` 已於 2026-10-14 套用
Codex 審查修正，不再是逐位元組的原版，**不要**拿它對拍。

⚠️ **`original_before_python.html` 不能刪**——刪掉就再也無法對拍，也失去效能對照組
（`make_probe.py old` 拿它產生「同一個瀏覽器環境」的比較基準，
拿 Node 的數字跟瀏覽器每幀比是不成立的）。
原本另有一份內容完全相同的 `dlog_viewer_mem3_V1.4.html`，已於 2026-08-14 刪除。

## 使用方式

需要 `node` 與 `python3`（含 numpy）。先產生原版 JS 片段：

```bash
cd server/parity_tools
python3 extract_original_js.py     # 產生 worker_line.js / js_getters.js / js_buckets.js
```

### 1. 解析器對拍（V/I 原始陣列 + LOD 金字塔）

```bash
node run_worker.js ../../default_20260803_092740.dlog ./js_out
python3 dlog_poc.py ../../default_20260803_092740.dlog ./py_out
python3 compare.py ./js_out ./py_out
```

預期輸出：每個陣列一行 `OK`，最後 `=== 全部比對 PASS ===`。

### 2. 分桶對拍（`computeViewBuckets` 三條路徑）

```bash
node run_buckets.js ../../default_20260803_092740.dlog ./jsb_default ./cases_default.json
python3 compare_buckets.py ../../default_20260803_092740.dlog ./jsb_default
```

`cases_*.json` 是測試案例清單，涵蓋逐筆／LOD／逐筆掃描三條路徑與 V/I/P。
校正開啟的案例只驗「結構相同 + 數值全正」——原版用 `Math.random()`、
Python 用索引推導的決定性雜湊，數值本來就不會一樣（見交接文件 §6.2）。

### 3. CSV 數值格式化對拍

```bash
node fmt_check.js ../../74445_PS_SLC_1GB_Z790_2242_1024GB_20260728_10h41m10s.dlog
# 產生 js_fmt.txt，再與 server 匯出的 CSV 逐字元比對
```

驗證 JS `toFixed(8)`/`toFixed(9)` 與 Python `%.8f`/`%.9f` 結果一致。

### 4. 原版效能基準

```bash
node bench_local.js ../../default_20260803_092740.dlog
```

輸出改造前的每幀分桶耗時，作為新版手感的對照基準。
已知數值：81k×4ch 12 曲線 4.37ms／幀、929k×1ch 3 曲線 0.16ms／幀。

### 5. 合成測試檔

```bash
python3 make_synth.py     # 產生帶 pre-trigger trailer 的小檔，驗 trailer 排序
```

## 中間檔

`*.f32`、`*.cases.json`、`js_fmt.txt`、`.cache_parity/`、
以及 `extract_original_js.py` 產生的三個 `.js` 都是**可重新產生的中間檔**，
不需要保存，可隨時刪除。
