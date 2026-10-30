# T8 獨立審查：Opus（HEAD 703a744）

VERDICT: NEEDS_CHANGES（唯讀審查，未跑測試；互斥、gate CAS、世代、grant、import 邊界、feature flag、postMessage 皆未找到可繞過的洞）

## MAJOR
- **M1 `POST /prefs` 在新安裝必定失敗**：`server.py:541`、`storage.py:60-67`。prefs 目錄（`%APPDATA%\N6705C-SEQ\` 或 `~/Library/Application Support/N6705C-SEQ/`）從未建立；Viewer 端 `.catch(()=>{})` 吞錯（`viewer.html:3297`），重開後回預設值。測試用已存在的 `tmp_path`（`test_http.py:118`）沒抓到。修法：寫入前 `ensure_dir(prefs_path.parent)`，補測試（不存在的子目錄）。

## MINOR
- m1 `/open` 回應超過 64 MiB 時 handle 已 adopt 而外洩（`server.py:372-376`）：adopt 前先檢查大小，超過就 release 再回 413。
- m2 大回應在記憶體複製 4–5 份（`/samples` `server.py:410-420`；`/open` blob 約 3 倍）：預先配置 bytearray 或分段寫出。
- m3 `/save` 64 MiB 上傳讀成兩份（`server.py:517`）：改 `atomic_write_iter(path, parts, job.cancel)`。
- m4 未驗 tint/declaredTime（`core.py:48-51`）：nan/inf/0/負值；`json.dumps` 改 `allow_nan=False`。
- m5 崩潰或 `os._exit` 後殘留 `.part`（`storage.py:83`）：啟動時也掃 prefs 父目錄，README 註明。
- m6 多開時快取互踩（`storage.py:83-95` `sweep_tmp`、`evict_lru`；`core.py:246` `os.utime`）：`utime` 包 try/except OSError、`sweep_tmp` 跳過存活 pid、`evict_lru` 先 rename 再刪。
- m7 補副檔名繞過覆寫確認（`webgui.py:117`）：補完後若已存在需再確認。
- m8 首次 enter 在 gate 鎖內啟動 server（`gate.py:107-114`、`webgui.py:72`）：契約刻意設計；可預先 import 暖機或把 `sweep_tmp` 移背景。
- m9 DRAINING 30 s 逾時涵蓋「慢但沒卡住」（flush 1GB memmap、網路磁碟 fsync）：符合契約，Windows 驗收清單補「網路磁碟匯出時切頁」。
- m10 原生對話框未序列化（`webgui._analysis_path`、`analysis.js`）：non-blocking Lock，拿不到回 cancelled。
- m11 token 每 server 一個非每世代（加固）：每次 enter 換新 token。
- m12 `pyproject.toml:33` 的 `analysis/web/*` 會打包 `shell_stub.html`（spec 已排除）：改列 `viewer.html`。

## 已確認無問題
互斥（reserve/enter 僅 IDLE、碰儀器 js_api 都先 `_reserve`、底線屬性 JS 碰不到、舊 token/gen 不能 release/leave/job_begin/consume_grant、`_untimed` 與計時器同鎖 CAS）、import 邊界與 flag 關閉行為、分塊（hash 8MiB、parse/LOD 1M 筆、CSV 20k 列串流）、取消與清理（job_end、adopt 失敗 release、drain 後才 release_all/close_all）、Windows 映射關閉順序、安全（Host 精確比對、hmac token、靜態白名單、grant 單次且綁 gen/mode/purpose、postMessage 驗 source/origin/v/gen/精確 targetOrigin、markers 正規化與 escapeHtml）。
