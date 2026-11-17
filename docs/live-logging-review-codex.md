# Wave 5 R：即時資料紀錄獨立審查（Codex）

審查日期：2026-11-18。範圍：`git diff d6b9d2a^..787df21`（本次 HEAD）。已先讀 `docs/live-logging-contract.md`、`docs/live-logging-task-plan.md` §6 與 `HANDOFF.md` §13。`HANDOFF*.md`、`README.md` 僅作背景對照，不將其文字變更列為發現；未讀取其他人的審查報告。未修改程式碼、未 commit。

共發現 **3 項 Major、1 項 Minor；無 Blocker**。目前旗標仍為 False，未發現可直接繞過停用檢查而啟動儀器的路徑；這不消除啟用後的發布與關窗風險。

## Major

### M1．writer／recover 在資料改名成功前，已發布 `complete=True`

- **位置**：`src/n6705c_seq/record/dlogfile.py:319`、`:216`、`:469`。
- **失敗情境**：`finalize()` 先產生 `complete=True` 的 metadata，`_publish()` 先將旁檔 replace 到正式位置，再 replace 資料檔。第二次 replace 若因 Windows 分享鎖、權限或其他 I/O 錯誤失敗，正式旁檔已聲稱新 run 完成，正式資料卻仍是舊 run（或根本不存在），新資料留在 `.part`。`recover_part()` 走同一流程，也有相同問題。`finalize()` 的 finally 又將 writer 關閉，之後 `abort()` 直接返回，不能降回 incomplete。
- **證據**：以記憶體替身模擬第二次 replace 拋 `PermissionError`，結果同時存在舊 `x.dlog`、新 `x.dlog.part`，以及 `{run_id:"new", complete:true}` 的正式 `x.dlog.json`。既有 `test_publication_failure_keeps_recoverable_source` 有測第二次改名失敗，但未驗旁檔的 complete。資料仍可恢復，不代表完成標記正確。
- **建議修法**：比照目前 B-DLOG 的發布方式，先持久化並發布 `complete=False` 的旁檔；資料 replace 成功後，才以暫存＋replace 發布 `complete=True`。writer 與 recover 共用此流程，並測新檔、覆寫、Windows sharing violation、最後一次旁檔更新失敗及中途重啟。重啟判讀仍須核對 run_id。
- **阻擋判定**：**擋住合併此版 writer／recover 的完成語意；使用此 writer 的分支啟用前必修**。目前 B-DLOG 使用自己的發布流程，已有先 false 後 true 的處理，本項不是指它也提前標 true。

### M2．覆寫確認只在開始時做，長錄結束會覆蓋其後出現或被更換的正式檔

- **位置**：`src/n6705c_seq/web_record.py:201`、`src/n6705c_seq/record/backend.py:394`、`src/n6705c_seq/record/dlogfile.py:219`。
- **失敗情境**：使用者開始錄製時選定尚不存在的 `X.dlog`，不需要覆寫確認。錄製數小時期間，另一個程式或應用程式實例把另一份紀錄存到同一正式檔名。收尾時，backend 對正式資料與旁檔直接 `os.replace`，無再次檢查或確認，後來出現的紀錄被靜默覆蓋。開始時已確認的舊檔若在期間被替換，也會遇到相同問題。exclusive create 只保護 `.part`；`_claim()` 也只是本進程記憶體登記，無法保護正式目標。
- **證據**：發布函式沒有接收開始時的目標存在狀態／檔案身分，也未在發布前比較。以記憶體替身提供「錄製期間由他人存入」的正式資料，發布會直接取代它。`recover_part()` 亦未對既有正式目標取得新的覆寫授權。
- **建議修法**：保存使用者確認時的目標身分與存在狀態；發布時若目標新增或改變，保留本次 `.part` 並回報衝突，讓使用者另存或重新確認。以避免無條件 clobber 的發布／保留備份策略處理最後競態；恢復流程也不得把有效旁檔視為覆寫任意現有 final 的授權。補「開始時不存在、結束前出現」及跨實例測試。
- **阻擋判定**：**擋住打開 RECORD_ENABLED**；旗標維持 False 時可隔離此執行路徑，但不可將現況認定為已滿足既有檔保護。

### M3．錄製收尾逾時進 fault 後，可關窗並終止仍存活的 worker

- **位置**：`src/n6705c_seq/web_record.py:154`、`:305`；既有整合路徑為 `src/n6705c_seq/analysis/gate.py:169`、`src/n6705c_seq/webgui.py:165`、`:219` 及 main 的 `os._exit(code)`。
- **失敗情境**：worker 在 ABOR、fsync、發布或 close 卡超過 60 秒，gate 進入 fault，但 worker 尚未返回。此時關窗不再命中 POWER 的 veto：`Gate.on_closing()` 將 fault 轉 closing；沒有分析 server/job 時 `_on_closing()` 立即回 True，有 server 時 cleanup 也只等待 analysis jobs/resources。新的錄製 worker 是 daemon，GUI 結束後會走 `os._exit`，其 finally、ABOR、關檔與 token release 都不保證執行。若尚未下載，會留下本機空 `.part` 與遠端錄製檔；若正在發布，則留下中間狀態。使用者只是在 fault 後關窗，沒有明確接受放棄仍在收尾的紀錄。
- **證據**：以真 Gate 手動觸發 finishing timeout，`worker_done=False` 時呼叫真 `_Core._on_closing()`（server=None）仍回 True。現有 gate 測試涵蓋 POWER 關窗及 fault 遲到 release，但沒有交叉覆蓋「fault＋worker 仍活著＋關窗」。這是新錄製功能沿用既有關窗機制的整合缺口，並非宣稱本次修改了 gate。
- **建議修法**：關窗判斷需追蹤錄製 worker 是否確實結束，fault 不應等同 worker 已停止。預設保留視窗與故障鎖；若產品需要允許強制退出，須明確提示未完成錄製與恢復位置，並在進入可能卡住的工作前持久化可追查的 run／遠端檔資訊。不要讓 GUI 執行緒補送 VISA 指令，也不要逾時釋鎖成 idle。此項涉及凍結的 gate／關窗界面，應回 T0 協調；補 timeout 後關窗與 worker 遲到返回的整合測試。
- **阻擋判定**：**擋住打開 RECORD_ENABLED**。維持 False 可隔離新增錄製風險。

## Minor

### m1．另存對話框接受 `.DLOG`，backend 卻拒絕同一檔名

- **位置**：`src/n6705c_seq/web_record.py:186`、`src/n6705c_seq/record/dlogfile.py:146`（由 `record/backend.py:258` 呼叫）。
- **失敗情境**：使用者輸入 `CAPTURE.DLOG` 或混合大小寫副檔名。bridge 用 `suffix.lower()` 判定有效，不附加 `.dlog`；worker 已連線、設定儀器後，`_paths()` 的大小寫敏感 `endswith(".dlog")` 才拋錯，整次錄製失敗，並因 phase 已為 fetch 而顯示 fetch 錯誤。Windows 使用者尤其容易遇到。
- **證據**：唯讀呼叫確認 `Path("CAPTURE.DLOG").suffix.lower() == ".dlog"` 為 True，而 `_paths()` 對同名拋 `ValueError("Expected a .dlog basename")`。
- **建議修法**：在對話框回傳後統一正規化副檔名，或讓下游路徑驗證一致接受大小寫；在啟動 worker 前完成驗證。補大寫／混合大小寫檔名測試。
- **阻擋判定**：**不擋合併；不單獨阻擋打開旗標**。

## 其餘核對與驗證限制

- **互斥與釋放**：record、SEQ、List、連線測試、掃描及 all-off 均經 `_reserve("power")`；analysis enter 受同一 gate 限制。取消對話框、啟動失敗及 worker 捕捉到的例外有 release 路徑；fault 後的遲到 release 不會將 gate 解回 idle。正常 POWER 錄製／收尾可阻止關窗，例外為 M3。契約已明列「stop 尚未被阻塞中的 worker 觀察時不會啟動 60 秒 timer」，不重複列為新缺陷。
- **單 worker I/O**：B-DLOG 的偵測、連線、設定、MEAS、ABOR、下載及 close 均由 `_record_work` 呼叫鏈執行；未見錄製期間額外啟動 VISA thread。`record_stop()` 只 set Event；status／snapshot 只讀記憶體。ELOG 的阻塞 `run()`、尾段與 ABOR 同 thread，背景相容 wrapper 的 stop 仍保留 join 語意，符合 C7。
- **資料與恢復**：B-DLOG 不用預覽筆數冒充正式 records；下載／layout 失敗保留遠端檔，不發布 done。連續 MEAS 失敗會使 run failed，完整下載檔留在 `.part`；原生 part 不支援 `recover_part()`，屬 HANDOFF 已列限制，本次未當作靜默丟資料。自家 writer 的恢復有 producer/schema/run_id、標頭、完整筆數、symlink／hardlink 與進程內活躍檔檢查；仍有 M1、M2 的發布問題。
- **Windows**：writer 在 replace 前關閉資料 handle；backend 的下載及 fsync handle 也已離開 with。目錄 fsync 在 Windows 明確略過，其他平台為 best effort，未將它當作 Windows 保證。外部程式持有分享鎖仍可讓 replace 失敗，相關風險見 M1；本次未在 Windows 執行。
- **匯入與停用**：未見 record 匯入 analysis／webgui／webview／web_*，也未見 core 反向匯入 record。writer 僅用 stdlib／numpy；contracts 僅 stdlib。`RECORD_ENABLED=False` 在 start 的 config、reserve、對話框與 worker 前就返回 disabled；以會在 reserve 被碰觸時拋錯的替身驗證此早退。停用時仍會載入輕量契約、初始化記憶體狀態與註冊前端 listener，未見因此連儀器、建立錄製檔或啟動錄製 worker。
- **前端**：停用時模式按鈕與錄製面板保持隱藏；錄製中禁止切到檢視模式，離開 Viewer 分頁會停止快照輪詢並保留錄製，返回讀 status／完整快照。`open_push` 有精確 source/origin/gen、grant 與 basename 驗證，忙碌時顯示 Viewer 自己的雙語提示；未見新增翻譯 key 缺漏。

本次執行 `node --test tests/js/*.test.js`：**67 passed**。執行契約／翻譯與 analysis AST 邊界檢查：**62 passed、1 deselected**；指令為 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /opt/anaconda3/bin/python -m pytest -q -p no:cacheprovider tests/record/test_record_contracts.py tests/analysis/test_boundary.py -k 'not core_import_loads'`。另以不落地的記憶體替身重現 M1、M2，並用真 Gate／關窗方法重現 M3；m1 以純路徑函式確認。未新增測試檔，未重跑完整 Python 套件、HTTP 整合或真機測試；未將既有交接中的測試數字當成本次結果。H0、HW-P2、Windows 實機驗收仍須完成，測試通過不能替代這些啟用條件。

**VERDICT: CHANGES REQUIRED**
