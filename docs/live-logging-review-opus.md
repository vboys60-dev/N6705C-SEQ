# 即時資料紀錄｜Wave 5 R 獨立審查（Opus）

日期：2026-11-18。範圍：`git diff d6b9d2a^..HEAD`（HEAD = 787df21），對照 `docs/live-logging-contract.md`、`docs/live-logging-task-plan.md` §6、`HANDOFF.md` §13。沒有讀 `docs/live-logging-review-codex.md`。只審查，沒有改程式碼。

驗證：`/opt/anaconda3/bin/python -m pytest -q -p no:cacheprovider` → **1445 passed／7 skipped**；`node --test tests/js/*.test.js` → **67 pass**。另外用一次性指令確認了 M3（大寫副檔名）與 m1（零筆留下孤兒檔）的行為。

重點：`web_record.py`、`record/backend.py`、`record/dlogfile.py`、`record/snapshot.py`、`web/record.js`、`web/analysis.js`、`analysis/web/viewer.html`、`core/dlog.py`。`core/elog.py`（B-ELOG 尚未接線）只抽查了匯入邊界，沒有逐行審查。

## 結論先講

- **合併（`RECORD_ENABLED=False`）**：沒有 Blocker。旗標關閉時，`record_start` 第一步就回 `disabled`；`record_stop`／`record_load` 沒有可作用的對象；前端會隱藏模式切換與錄製面板。Python 端多了 mixin 屬性和既有模組的 import，沒有 I/O。JS 端多兩次唯讀的 `record_meta` 呼叫（見 m9）。
- **打開旗標**：除了 H0／HW-P2／Windows 實機驗收這些既有前置條件，**M1、M2、M3 必須先修**（M2 至少要在 Windows 實測排除）。M4 擋的是 B-ELOG 接線。

| 嚴重度 | 件數 | 擋合併 | 擋打開旗標 |
|---|---|---|---|
| Blocker | 0 | — | — |
| Major | 4 | 否 | M1、M2、M3 擋；M4 擋 B-ELOG 接線 |
| Minor | 10 | 否 | 否（建議一併處理 m1、m2、m3） |

## 各審查重點核對

1. **互斥**：所有儀器 I/O 入口（`seq_run`／`list_run`／`test_connection`／`scan_instruments`／`all_off`／`record_start`）都經過 `_reserve("power")`。`analysis_enter` 只在 gate 為 idle 時才接受。錄製中關窗時，`on_closing()` 回 `power`，`_on_closing` 回 False。`record_start` 的各條路徑都會釋放 token：設定錯誤時根本還沒取 token；對話框取消、對話框鎖忙、`_RecordFailure` 與一般例外由 `finally` 釋放；`Thread.start` 失敗時，在鎖內釋放並標記 failed。worker 以 `except BaseException` 加兩層 `finally` 保證 `inst.close()` 後一定 `_release`。鎖的順序固定為 record_lock → gate lock，gate 的回呼只做 `_emit`（`queue.put`），沒有反向取鎖，因此不會死結。在 FAULT 下關窗會留下仍卡住的 worker，這是既有機制，屬可接受。
2. **單一 worker**：`connect`／`detect_model`、`configure`、INIT/TRIG、MEAS、ABOR、`MMEM:DATA?`、`MMEM:DEL` 都在 `_record_work` 的執行緒裡。`record_stop` 只在鎖內 `Event.set()`，不 join、不做 I/O；`record_snapshot`／`record_status` 只讀記憶體。符合 R9 與 C1。
3. **資料遺失**：沒有發現「靜默丟棄」或覆寫自家 `.part` 的路徑。但有一條「資料已完整下載卻不發布、使用者也無法載入」的路徑，見 M1。
4. **`.part`／旁檔**：B-DLOG 先發布 `complete=False` 的旁檔，再換資料檔，最後才標 `complete=True`，順序正確。`xb` 獨占建檔加上 bridge 拒絕既有 `.part`，可以防止覆寫別人的暫存檔。DlogWriter 的發布順序與此不一致，見 M4。孤兒檔見 m1。
5. **import 邊界**：`record/*` 只 import stdlib、numpy、`n6705c.core`、自身子模組；`analysis/*` 沒有 import `record`；`core` 沒有 import `record`。契約 C1／§6 R1 都符合。
6. **Windows**：見 M2、M3、m8。
7. **前端**：旗標關閉時隱藏；錄製中離開分頁會繼續錄，回來時讀 status 復原錄製模式；重新整理頁面也能復原。i18n：record.js／analysis.js／index.html／web_record.py 用到的 key 全都在 `i18n.STRINGS`，`contracts.I18N_KEYS` 也沒有缺。Viewer 的 `open_push` 會驗 source、origin、v、gen、grant、name，沒有待處理請求時也接受。

## Major

### M1 只是預覽或收尾雜訊，就讓已完整下載的 DLOG 不被發布（擋打開旗標）
- 位置：`src/n6705c_seq/record/backend.py:326-339`（MEAS 連續失敗 3 次，或 MEAS 失敗後讀錯誤佇列再失敗）、`:364-367`（`session.abort()` 經 `_CheckedSession.write` 查錯，任何非零 `SYST:ERR?` 都會拋錯）、`:388-389`（`if error is not None: raise error`）。
- 失敗情境：USB 偶發逾時讓 MEAS 預覽連續失敗 3 次；或到期後送 ABOR:DLOG 時，儀器回報「未在記錄」之類的錯誤（真機行為未證實）。這時 worker 照樣下載完整 DLOG 到 `.part`，`_layout` 也驗證通過，最後卻因 `error` 已被記錄而拋出：
  - 狀態變成 failed（fetch）。
  - `last_path=None`，無法載入。
  - 資料只留在 `X.dlog.part`。這是原生格式，`recover_part()` 不認得，Viewer 的開檔也看不到 `.part`。
  - 旁檔標 `complete=False`。
  
  正式落檔資料其實完好，卻被預覽失敗拖垮。長時間錄製最需要保護的正是這種情況。HANDOFF §13.7 第 2 點已列為已知缺口，但沒有排入修補。
- 建議：
  1. 預覽失敗只降級。達到上限後停止 MEAS 預覽，寫 note 加一筆 `preview_meas_error`（count=null），繼續等待到期或 stop，不設 `error`。
  2. ABOR 的錯誤佇列內容改寫入 notes；只有傳輸或 `_layout` 失敗時才視為致命。
  3. 契約補上：「已下載且 `_layout` 驗證通過的原生 DLOG 一律發布，非致命錯誤寫 notes」。
  4. 補兩個測試：MEAS 三連錯仍然 done；ABOR 回非零錯誤時仍然發布。

### M2 Windows 的 `os.replace` 遇到暫時性鎖定沒有重試（擋打開旗標，需 Windows 實測確認）
- 位置：`record/backend.py:394-395`、`record/dlogfile.py:185`（`_write_sidecar`）、`:219-220`（`_publish`）。
- 失敗情境：在 Windows 上，剛寫完、剛關閉的大檔（B-DLOG 最多 256 MiB）常被 Defender、索引服務或雲端同步短暫開啟。`MoveFileEx` 這時會回 `PermissionError`（WinError 5 或 32）。結果是：
  - 旁檔已經換成 `complete=False`，資料檔還是 `.part`。
  - run 變成 failed（sink）。
  - 覆寫情境下，舊的 final 留著，配上新的 incomplete 旁檔。
  
  資料沒有遺失，但使用者拿不到可載入的檔案，而且 `.part` 會擋住同名再錄（m1）。
- 建議：把 `os.replace` 包成小 helper，只在 `os.name == "nt"` 且遇到 `PermissionError` 時做有限次退避重試（例如 10 次、累計約 1–2 s）。兩處發布與 `_write_sidecar` 都改用它。`HANDOFF_WINDOWS.md` §8 的驗收清單要加一項：開著 Defender 即時保護，錄一個大於 100 MB 的檔。

### M3 大寫副檔名 `.DLOG`：bridge 接受、backend 拒絕，而且是在設定儀器之後才拒絕（擋打開旗標）
- 位置：`src/n6705c_seq/web_record.py:186`（`Path(path).suffix.lower() != ".dlog"`，因此 `X.DLOG` 不會補上副檔名）、`src/n6705c_seq/record/dlogfile.py:146`（`final.name.endswith(".dlog")` 區分大小寫）、`record/backend.py:258`（在 `session.configure()` 與 `*IDN?` 之後才呼叫 `_paths`）。
- 失敗情境：在 Windows 存檔對話框輸入 `data.DLOG`，或選一個既有的大寫檔名。對話框和覆寫確認都會通過，worker 連上儀器、完成 DLOG 設定後，才拋出 `ValueError("Expected a .dlog basename")`，被 `_failure_code` 映射成 **fetch**，訊息會誤導使用者。已用指令確認：`_paths(Path('X.DLOG'))` 拋 ValueError，`_failure_code` 回 FETCH。
- 建議：在 `_record_save_path` 用同一個 `_paths()` 驗證；對副檔名不分大小寫時，可以統一正規化成小寫，或讓 `_paths` 不分大小寫，但 `.part`／`.json` 的命名要一致。`_paths`／`_safe_paths` 也要挪到 `session.configure()` 之前，避免先動儀器才發現路徑不合法。

### M4 DlogWriter 先發布 `complete=True` 旁檔，才換資料檔（擋 B-ELOG 接線，不擋目前合併）
- 位置：`record/dlogfile.py:216-221`（`_publish`）、`:303-307`、`:319-323`（`finalize` 用 `_metadata(True)` 寫旁檔，再兩次 replace）。
- 失敗情境：覆寫既有 `X.dlog` 時，第二次 `os.replace(part, final)` 失敗（例如 M2 的 Windows 鎖定）。這時 `X.dlog.json` 已經是新 run、`complete=True`，`X.dlog` 卻還是舊錄音，等於旁檔與資料錯配，而且誤標為 complete。`finalize` 的 finally 也不會把旁檔改回 incomplete。B-DLOG 已在自己的發布流程修正這點（`backend.py:390-397`），兩條路徑的語意不一致。契約 C6 的文字允許這種順序（「以兩次 replace 成功作完成標誌」），所以這是契約層面的問題。
- 建議：請 T0 把 C6 改成 B-DLOG 的順序：旁檔 `complete=False` → replace 旁檔 → replace 資料 → 旁檔 `complete=True`。DlogWriter 改用共用的 `_publish` 並補失敗注入測試。B-ELOG 接線前完成即可。

## Minor

- **m1 零筆或 INIT 後失敗會留下孤兒檔，擋住同名再錄**：`backend.py:412-427` 只在 `attempted=False` 時刪暫存。INIT 後的失敗（包括零筆，見 `tests/record/test_backend_dlog.py:563-572`）會留下 `X.dlog.part`（可能只有 0 bytes）和 `X.dlog.json.part`。之後同一個檔名會被 `web_record.py:198-200` 以 sink 錯誤拒絕，UI 沒有提示怎麼處理。另外，在 starting 階段按停止，仍會 INIT 再立刻 ABOR（`backend.py:301-316`），幾乎一定產生零筆。建議：INIT 前先檢查 `stop.is_set()`，若已停止就走 `attempted=False` 的清理路徑；零筆且 `.part` 沒有資料時，刪除自己擁有的暫存檔；錯誤文案提示使用者換檔名或手動刪除。
- **m2 預覽 MEAS 的缺口被當成資料缺口顯示**：`backend.py:329` 把 `preview_meas_error` 計入 `gaps`，`record.js:207` 據此顯示 `record_gap_warning`（「資料有缺口；未補值…」）。但正式 DLOG 並沒有缺口。建議 B-DLOG 的 `RecordStatus.gaps` 不計預覽事件（旁檔照記），或另外用 preview 專用文案；如需新 key，交 T0 增加。
- **m3 預覽時間軸假設 25 ms**：`backend.py:209` 用 `_PREVIEW_PERIOD` 建 Envelope，但每輪實際是 2 次 MEAS 加 2 次 `SYST:ERR?`，真實速率可能遠低於 40 Hz，`record.js:234,264` 的時間軸終點就會小於 elapsed。建議用實測平均間隔，或把軸標成「樣本」而不是秒。
- **m4 已發布但 gate 已 fault 時，UI 顯示 failed 且細節為 "None"**：`web_record.py:335-336` 在 `released=False`、`error=None` 時呼叫 `_record_detail(None, …)`，得到 `"None"`。這時磁碟上的旁檔已是 `complete=True`，UI 卻說失敗。資料沒問題，但訊息會誤導。建議 stuck 時給固定說明，加一筆 note 說明檔案已發布。
- **m5 錯誤碼映射粗糙**：`web_record.py:85-92` 用正則在任意例外字串裡找 ` 323,` 或 ` 310,`，可能誤判。其他 SCPI 設定錯誤（例如 `-222` 出現在 `SENS:DLOG:*` 之後）因為 phase 已是 fetch，會被報成 fetch。建議 backend 拋帶 code 的例外（沿用 `_RecordFailure` 的模式），不再靠解析字串。
- **m6 檢視→錄製的 idle 等待逾時 10 s，比 draining 的 30 s 短**：`analysis.js:147` 用 `SELFTEST_STEP_TIMEOUT_MS`。draining 若超過 10 s，會進 catch，把 mode 設回 view（`:165`），但 iframe 已經是 `about:blank`、`currentGen=null`，「檢視」按鈕又因 mode 已是 view 而停用，使用者看到空白 Viewer，要切分頁才能恢復。建議等待時間至少用 `DRAINING_TIMEOUT_S`，或失敗時重新 `enterViewer`。
- **m7 覆寫確認可能問兩次**：`web_record.py:201-204` 對 final 與 `.json` 各問一次。建議合併成一個對話框。
- **m8 Windows 目錄 fsync 是 no-op**：`dlogfile.py:202-213`。改名的持久性只靠 NTFS 日誌，`os.replace` 也沒有 write-through。可接受，但要在 HANDOFF_WINDOWS 註明斷電語意。
- **m9 旗標關閉時，analysis 的初始化仍依賴 `record_meta`**：`analysis.js:275`。`app.js:144` 依序 await 每個 tab 的 init，所以這個呼叫如果拋錯，會中斷後續 tab 的初始化和 selftest。實際風險很低（唯讀、無 I/O），但這是旗標關閉時唯一新增的相依。建議用 try/catch 包住，失敗時視為 `enabled=false`。
- **m10 失敗時遠端檔與記憶體**：失敗時保留遠端 `internal:\record_*.dlog`（`backend.py:401` 只在發布後才刪）。這是正確的保守做法，但長期會占滿儀器的檔案系統。建議在 UI note 提示遠端檔名，或提供清理方式。另外，`_transfer`（`backend.py:72-75`）在 256 MiB 上限下，`raw` 加切片 payload 峰值約 512 MiB，Windows 低記憶體機器要留意（串流留 P5）。

## 已確認無問題（摘要）

- `record_load` 只在 `analysis(gen)`、有 `last_path`、沒有活躍 run 時才簽 grant；`last_path` 只在 `released=True` 且沒有錯誤時設定；新 run 一被接受就清除。
- `_record_emit` 用白名單欄位，路徑與 notes 不會送到 JS；terminal 狀態只由 bridge 發出。錯誤細節裡的絕對路徑，包括 `.part`、`.json.part.*.tmp` 與 Windows 的 repr 形式，都會被替換成 basename。
- 快照是有界的（≤2001 bucket 再二合一），在鎖內複製，非有限值轉成 null；JS 依 run_id、seq、epoch 丟棄過期回應。
- Viewer 的 `open_push` 不檢查 id，`*_result` 才對 id；忙碌時顯示自己字典裡的雙語 toast；shell 端用精確 origin 送出，而且送出前確認 frame、gen、src、epoch 都沒變。
- `core/dlog.py`：只在 BUS 或 IMM 時才標 RUNNING；`retrieve` 逐檔刪除；`wait()` 不會再無限迴圈。

VERDICT: CHANGES REQUIRED（在 `RECORD_ENABLED=False` 下可以合併；打開旗標前必須先修 M1、M2、M3，B-ELOG 接線前必須先修 M4）
