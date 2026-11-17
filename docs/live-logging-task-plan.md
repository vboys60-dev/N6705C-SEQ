# 即時資料紀錄（DLOG Viewer 分頁）＋ ARB 修補｜施工任務表（規劃，未開工）

日期：2026-11-17　作者：Opus（唯讀規劃，未改任何專案檔、未 commit）
依據：`HANDOFF.md` §11、§13（**以 §13 為準**），以及 `報告分析升級版/升級報告.md` §7 任務表格式。
狀態：**使用者尚未說「開工」**。本檔只描述將來的切法。各軌要等使用者點名「開工」才動（HANDOFF.md:146）。

標記說明：**〔推論〕**＝我讀程式推出來的，沒有實機或執行證據；`檔案:行號`＝我讀過的現況程式。

---

## 0. 已定決策（照抄，不重新辯論）

1. 即時紀錄放進 **DLOG Viewer 分頁**，分頁內有「檢視」和「錄製」兩種模式，兩者互斥；錄製時持有 power token。`analysis/*` 不得 import `n6705c`，錄製程式放在 analysis 之外，經 bridge 呼叫（HANDOFF.md:232）。
2. H0 失敗的退路：改用 DLOG，錄完再下載，同時用約 40 Hz 的 `MEAS` 做預覽，畫面標示「預覽」（HANDOFF.md:233）。
3. 「邊上電邊錄」延到 P4；v1 只做獨立錄製（HANDOFF.md:234）。
4. ARB 修補另立一軌，**排在即時紀錄 P2 之前**（HANDOFF.md:235）。本計畫把它解讀成：P2 任務等 A1 合併後才開始（見 §4 Wave 2）。

---

## 1. 讀程式得到、會影響切法的事實

| # | 事實 | 出處 | 對計畫的影響 |
|---|---|---|---|
| F1 | `Gate.enter` 只接受從 `IDLE` 進入；`reserve` 只收 `"power"` | `analysis/gate.py:59-68,107-117` | 錄製直接沿用 `POWER`。從檢視切到錄製走 ANALYSIS→DRAINING→IDLE→POWER，切回去走 POWER→IDLE→ANALYSIS。**〔推論〕gate.py 不需要新增狀態或轉移**，只補測試（見 T0 §C3） |
| F2 | 長工作用 `_untimed` 停表，用 `_finishing` 重新起算 60 s；契約註解寫明「只有 run_resources/run_plan/run_functions 不計時」 | `analysis/contracts.py:72-81`、`gate.py:86-105` | 錄製迴圈和 DLOG 下載都要列為不計時的主工作，這是契約變更，由 T0 處理 |
| F3 | `power` 狀態下拒絕關窗 | `gate.py:169-179`、`webgui.py:164-168` | 錄製中不能關窗，必須先停止。沿用現況，不改 |
| F4 | 路徑授權只能在 ANALYSIS 狀態、世代相符時發出；Viewer 只在自己送出 `open_request` 後才開檔 | `gate.py:207-216`、`viewer.html:915-938,1798-1803` | 「一鍵載入」需要兩樣新東西：(a) 新訊息 `open_push`（外殼→iframe）；(b) bridge `record_load(gen)`，由 Python 端對「最後一次錄完的檔」發 grant，JS 不能傳路徑 |
| F5 | `analysis/contracts.py` 是凍結契約，改它就是契約變更 | `analysis/contracts.py:1-6` | `Msg.OPEN_PUSH` 和計時註解由 T0 改。selftest 分類 tuple 交給 G 在 Wave 2 改（見 §6 R7 例外） |
| F6 | 每個公開 `Api` 方法都必須列在 selftest 允許清單或禁止清單；公開介面的完整集合有測試釘死 | `tests/test_selftest_api_classes.py:7-12`、`tests/test_webgui.py:119-127` | 新增 `record_*` 方法時，這兩處要在**同一個任務（G）**裡一起改，否則中間狀態測試會紅 |
| F7 | 不准出現中文的檔案：`webgui.py`、`web_*.py`、`web/*`、`form.py`、`list_form.py`、`synth.py`、`listplan.py`、`packaging/gui_entry.py`；用到的 `tr()` key 都必須已經在 `i18n.STRINGS` | `tests/test_webgui.py:146-176` | 新檔 `web_record.py`、`web/record.js` 都在掃描範圍內。**所有新 i18n key 由 T0 一次加齊** |
| F8 | analysis 只准 import stdlib、numpy、`n6705c_seq.analysis.*` | `tests/analysis/test_boundary.py:1-2,40-41` | 錄製程式放在新套件 `n6705c_seq/record/`。只有測試會同時 import `record` 和 `analysis.core`（做往返驗證） |
| F9 | `ElogRecorder.stop()` 的 join 逾時後仍把 `_thread` 清成 None；sink 的錯誤被吞掉；`ABOR:ELOG` 由呼叫端在另一條執行緒送出（`session.stop`） | `core/elog.py:518-522,545-549,377-383` | P1 的 C1 要改：把 `run()` 公開成阻塞式、把 ABOR 移進同一個 worker、定義錯誤語意 |
| F10 | DLOG 沒等到外部觸發就標成 RUNNING；`.dlog` 下載失敗時仍刪掉儀器上的原檔；`transfer` 一次把整個檔讀進記憶體 | `core/dlog.py:323-328,430-436,390-401` | C2 修前兩項。第三項只在 H0 失敗分支有影響：先加檔案大小上限，串流下載排到 P5 |
| F11 | ELOG 每筆記錄的欄位順序是 I 在前、V 在後；Viewer 讀的順序是每通道先 V 再 I | `core/parsing.py:35`、`analysis/core.py:63-71` | 寫檔器（W）必須重排欄位 |
| F12 | Viewer 用 `>Q` 讀筆數；寫入索引之後的資料會被當成「尾段」排到最前面 | `analysis/core.py:93,106-108,152-155` | 自寫檔用 trigger=0、write=N。〔推論〕`.part` 檔裡 write=0 時，全部資料都算尾段，順序仍然正確，所以中斷的檔只要截到完整筆數就能開 |
| F13 | fake 不認識 `TRIG:ARB:SOUR` 和 `VOLT:SLEW`，未知指令會產生 -113；DLOG 寫進去的是佔位位元組，不是真正的 .dlog | `core/fake.py:216-225,1527-1528` | A1 要在 fake 補指令；H0 失敗分支（B-DLOG）要讓 fake 產生真格式的 .dlog |
| F14 | 現在 `start_together` 只設定 `TRIG:TRAN:SOUR IMM` | `n6705c_seq/execute.py:174` | A1 在 `INIT:TRAN` 之前補送 `TRIG:ARB:SOUR IMM` 並查錯 |
| F15 | 打包時 hidden imports 只收集 `n6705c_seq.analysis`；package-data 是 `web/*` | `packaging/n6705c_seq.spec:25-26`、`pyproject.toml:34` | 如果 `record.backend` 用延遲 import，spec 要加 `collect_submodules("n6705c_seq.record")`，由 D 負責。`web/record.js` 已經被 `web/*` 涵蓋 |
| F16 | 每個分頁由 JS 把內容畫進空的 `<section>`；`poll` 收到未知型別的事件會原樣 `emit` | `web/index.html:44-49`、`web/app.js:90-110` | 錄製狀態用新事件 `["record", {...}]` 傳，`app.js` 不用改〔推論〕 |
| F17 | 本 repo 沒有 core 層（elog/dlog/arb）的測試 | `tests/` 檔案清單 | P1 和 A2 自己新建 `tests/core/`，檔名要全域唯一（pytest 用預設 rootdir 匯入） |

---

## 2. 架構（T0 定死，其他任務都依此實作）

```
web/record.js ──js_api──► web_record.py (RecordApi mixin) ──► record/backend.py ──► n6705c.core.{elog,dlog}
   │                         │  _reserve/_untimed/_finishing/_release            │ 單一 worker 做全部 VISA I/O
   │                         │  record_load → gate.issue_grant                   ├─► record/dlogfile.py（.part 寫檔器）
web/analysis.js ◄─open_push─┘                                                   └─► record/snapshot.py（min/max 包絡）
   └─postMessage open_push──► analysis/web/viewer.html ──/open(grant)──► analysis/server.py（不改）
```

- 新套件 `src/n6705c_seq/record/`：`contracts.py`（純常數、型別、Protocol）、`dlogfile.py`、`snapshot.py`、`backend.py`。這個套件**不得 import** `webgui`、`webview`、`web_*`；`analysis/*` **不得 import** `record`。
- 分頁模式與 gate 狀態的對照（T0 寫進契約）：

| 分頁模式 | gate 狀態 | 怎麼進入 | 怎麼離開 |
|---|---|---|---|
| 檢視 | `analysis(gen)` | `analysis_enter`（現況） | `analysis_leave` → `draining` → `idle` |
| 錄製：表單（還沒開始錄） | `idle` | 切到錄製模式：先 leave，等到 `App.state==="idle"` | — |
| 錄製：錄製中 | `power`（`_reserve("power")`） | `record_start` | `record_stop` 只設旗標 → worker 自己送 ABOR、補尾段、寫完檔 → `_release` → `idle` |
| 錄製：收尾 | `power`（`_finishing` 計時 60 s） | 停止旗標被 worker 看到 | `_release`；卡住就進 `fault`（現況機制） |

- 錄製中可以切到別的分頁，錄製會繼續（因為還在 `power`，別的分頁的按鈕都是灰的）；切回來時，`record_status().running` 為真就直接顯示錄製模式。錄製中拒絕關窗（F3）。

---

## 3. 任務表

欄位：ID｜目標｜**獨占輸出檔**｜唯讀依賴｜輸入契約｜輸出契約｜驗收｜大小｜實機｜執行者｜適用界線規則（§6）

### 契約任務

#### T0 契約（Wave 0）
- **目標**：一次定死所有跨任務介面，之後其他任務只看契約就能並行。
- **獨占輸出**：
  - `docs/live-logging-contract.md`（新）
  - `src/n6705c_seq/record/__init__.py`（新，空檔）
  - `src/n6705c_seq/record/contracts.py`（新，只 import stdlib）
  - `src/n6705c_seq/i18n.py`（只新增 key，涵蓋 ARB 軌和錄製軌）
  - `src/n6705c_seq/analysis/contracts.py`（Wave 0 這段時間：`Msg.OPEN_PUSH`、`MSG_FIELDS`、`MSG_TO_IFRAME`、`OpenPush` TypedDict、計時註解 :72-81）
  - `docs/analysis-contract.md`（§1.4 呼叫點表加錄製 worker 一列；§4 加 `open_push`）
  - `tests/record/test_record_contracts.py`（新：`record/contracts.py` 只 import stdlib；`I18N` key 都已存在）
- **唯讀依賴**：§1 列出的所有檔案、`tests/analysis/fixtures/default_20260803_092740.dlog`（用來取得標頭範本）
- **必須定死的內容**：
  - **C1. bridge 方法（白名單）**：簽章與回傳都要寫明。
    - `record_meta() -> {enabled, snapshot_poll_ms, snapshot_max_points, max_channels, period_floor_table…}`
    - `record_start(config: dict, resources: list[str], demo: bool) -> {} | {cancelled} | {error, code}`：取 token 後開原生另存對話框，選 `.dlog` 路徑
    - `record_stop() -> {} | {error, code}`
    - `record_status() -> RecordStatus`
    - `record_snapshot(since_seq: int) -> Snapshot | {unchanged: True, seq}`
    - `record_load(gen: int) -> PathResult`
    - selftest 分類：`record_meta`、`record_status`、`record_snapshot` 放允許清單（唯讀）；`record_start`、`record_stop`、`record_load` 放禁止清單。這段 tuple 的修改交給 G 執行。
  - **C2. 事件與快照型別**：
    - `["record", RecordStatus]` 事件
    - `RecordStatus{run_id, state: idle|starting|running|stopping|done|failed, elapsed_s, records, gaps, preview, name?, error?, code?}`
    - `Snapshot{run_id, seq, preview, period_s, keys: {"V1": {t0, dt, min[], max[]}, …}}`，每個 key 最多 `SNAPSHOT_MAX_POINTS = 2000` 點
    - 輪詢間隔 `SNAPSHOT_POLL_MS = 500`（下限 250）
    - 前端丟掉 `run_id` 不同或 `seq` 較舊的快照
  - **C3. gate 對照**：§2 的表格。明寫「gate.py 不改」〔推論〕，並列出 G 必須補的 6 個轉移測試（§3 G 驗收）。如果 G 實作時發現需要原子交接，回報 T0 改契約，不可以自己加。
  - **C4. 計時**：錄製主迴圈和 DLOG 下載都是主工作，固定寫法是 `_untimed` → `try/finally: _finishing`。停止後的尾段補讀、寫完檔、關連線都在 60 s 計時內。DLOG 下載前可以再呼叫一次 `_untimed`。
  - **C5. Backend Protocol**（`record/contracts.py`）：
    - `make_backend(inst, cfg, final_path, *, untimed, finishing) -> Backend`
    - `Backend.run(stop: Event, emit) -> RunResult`：阻塞執行，所有 VISA I/O 都在呼叫端執行緒
    - `Backend.snapshot(since_seq)`：執行緒安全，不做 I/O
    - `Backend.preview: bool`
    - 模組名稱固定為 `record/backend.py`；H0 成功或失敗，兩個分支都實作同一個名稱
  - **C6. .dlog 寫檔器介面**（`dlogfile.py`）：
    - `DlogWriter(final_path, *, channels, sense_volt, sense_curr, tint, models, sidecar: dict)` 建立 `X.dlog.part` 和 `X.dlog.json.part`
    - `.append(block: float32[n, n_fields])`，欄位順序是每通道先 V 再 I
    - `.finalize() -> Path`：寫入兩個 `>I`（0, N），fsync，`os.replace`
    - `.abort(keep_part=True)`
    - `recover_part(part) -> Path | None`：只處理有對應旁檔的自家 `.part`
    - `elog_block(data, fields, channels) -> ndarray`：把 ELOG 的 I…V 重排成 dlog 的 V,I
    - 標頭範本取自真實檔的結構（上面的 fixture），`sense_minmax=0`
    - `<time>` 省略或固定寬度回填，由 T0 擇一寫死
    - 原始值照寫，不做負電流替換
    - 缺口**不補值**，只記在旁檔的 `gaps[]`，UI 要警告（〔推論〕Viewer 遇到 NaN 的行為沒驗過，所以不補 NaN）
  - **C7. core 錯誤語意**（C1 實作在 `elog.py`，這裡只定名字和行為）：
    - `ElogRecorder.run(stop=None) -> ElogStats`，阻塞執行；最後的 `ABOR:ELOG` 也在同一執行緒送
    - `tail="drain_then_abort" | "abort_then_drain"`：預設前者，H0 的結果決定最後用哪個
    - sink 拋錯 → 送 ABOR 後拋 `ElogSinkError`
    - 連續 `MAX_FETCH_FAILURES=3` 次讀取失敗 → 送 ABOR 後拋 `ElogFetchError`；單次失敗只發事件
    - 積壓量大於門檻卻讀到 0 筆，或發生溢位 → `on_event(kind, index, count)`，不中止
    - `stop(timeout) -> bool`：逾時回 False，**且不清掉 `_thread`**
    - 設定、INIT、ABOR、讀到 0 筆、讀取失敗之後，都把 `SYST:ERR?` 讀到 0（`+310` 寫進 notes）
  - **C8. DLOG 兩個 bug 的修正語意**（C2）：
    - BUS/IMM 以外的觸發來源，`start()` 後保持 `ARMED`
    - `retrieve` 只刪除已經成功下載的檔案
  - **C9. fake 要補的指令**（A1 實作）：
    - `TRIG:ARB:SOUR[?]`：儲存並回讀，**不改變 INIT 的行為**〔推論：真機的語意還沒實測，H-ARB 之後再決定 fake 怎麼模擬〕
    - `[SOUR:]VOLT:SLEW[?]`、`VOLT:SLEW:MAX?`：依通道；`*RST` 預設值等於 MAX
  - **C10. 錯誤碼**：`RecordCode`，取值 `disabled|power|analysis|draining|fault|closing|bad_config|connect|elog_unsupported|fetch|sink|stuck|no_recording|stale`。每個錯誤碼對應一個 i18n key。
  - **C11. H0 判定準則**：照抄本檔 §5。另外列出 H0 結果要回填的契約常數：`tail`、每次讀取的筆數、讀取間隔。
  - **C12. 功能旗標**：`RECORD_ENABLED = False`，定義在 `record/contracts.py`，由 `record_meta().enabled` 下發。不動 `CONFIG_KEYS`（`analysis/contracts.py:24`）。實機驗收後由使用者決定是否打開。
  - **C13. DLOG 分支上限**（只在 H0 失敗時生效）：`DLOG_FALLBACK_MAX_BYTES`，預設 256 MB。超過時拒絕開始錄製，串流下載留到 P5。
- **輸入契約**：本檔與 HANDOFF §13。**輸出契約**：上面 C1–C13。
- **驗收**：
  - `python3 -m pytest -q` 全綠（838 過／7 跳過不能退步）
  - `node --test tests/js` 全綠
  - Codex 唯讀挑錯意見已處理
  - 確認「其他任務只看契約就能動工」
- **大小**：L｜**實機**：否｜**執行者**：Opus 寫；Codex（astra）唯讀挑錯（**雙審**）｜**規則**：R1 R3 R4 R5 R7 R12 R13

### 硬體驗證腳本軌（只寫腳本，實測由使用者做）

#### H0s ELOG 硬體閘門腳本（Wave 0，不依賴 T0）
- **獨占輸出**：`scripts/hw_elog_h0.py`、`tests/test_hw_elog_h0.py`
- **唯讀依賴**：`core/elog.py`（用**現況**的 `ElogSession.fetch`，自己控制變數）、`execute.connect_group`、`scripts/hw_list_check.py`（參考寫法：`--resource/--demo/--out/--yes`）
- **子項**：照 HANDOFF.md:261，每個子項只改一個變數。
  - `firstread`：觸發後立刻讀，對照延後 5 s 再讀
  - `format`：ASCII 單通道，對照 REAL
  - `maxrec`：要求筆數小於 16384，對照等於 16384
  - `stall`：停止讀取 5／12／25 s 後再讀，看緩衝深度，以及溢位時丟掉的是新資料還是舊資料
  - `tail`：送 `ABOR:ELOG` 之後尾段還在不在
  - `long`：連續 ≥ 1 h，USB 和 LAN 各跑一次
  - `elog_arb`：ELOG 和 ARB 同時跑。會開輸出，需要 `--yes`；`finally` 一律 ABOR 並關輸出
- **輸出**：CSV 寫到 `hw_elog_results/`，每個子項印 PASS/FAIL/INCONCLUSIVE，判定依 §5
- **驗收**：`python3 -m pytest -q tests/test_hw_elog_h0.py`，用 `--demo` 跑 fake，所有子項都能走完並產生 CSV。同時檢查只送允許的指令：除了 `elog_arb` 子項，不送 `*RST` 也不開輸出
- **大小**：M｜**實機**：腳本本身不需要；**實測需要使用者接 N6705C**｜**執行者**：Codex（硬體腳本沿用 §9.3 的做法）｜**規則**：R6 R9 R10 R12 R13

#### HAs ARB 觸發實測腳本（Wave 0，不依賴 T0）
- **獨占輸出**：`scripts/hw_arb_trig_check.py`、`tests/test_hw_arb_trig_check.py`
- **唯讀依賴**：`execute.py`（`_arm_capture`、`_fetch` 的數位化器寫法）、`scripts/hw_list_check.py`
- **子項**：
  - `arbsrc`：讀 `TRIG:ARB:SOUR?` 的預設值，試 IMM 和 BUS 是否被接受
  - `bus_delay`：設 ARB 和 TRAN 都是 BUS → `INIT:TRAN` → 延後 2 s 再送 `*TRG`，用數位化器看起點；這項驗證「ARB source 就是 §4.10 現象的原因」
  - `residual_bus`：讓 ARB source 殘留在 BUS，走現況的 `start_together`，記錄會逾時、沒有波形，還是被 `*TRG` 放出來
  - `seqnum`：`ARB:SEQ:RES` 後分別寫 step 0 和 step 1，再查 `LEN?`；判斷步驟編號是 0..99 還是 1..100（〔推論〕對應 `arb/sequence.py:185`）
  - `slew`：讀 `VOLT:SLEW?` 和 `VOLT:SLEW:MAX?`，`*RST` 前後各一次
  - EXT 共用 BNC 只能接示波器看，腳本印操作說明
- **驗收**：`python3 -m pytest -q tests/test_hw_arb_trig_check.py`。測試在檔案內自備假的 transport stub，**不依賴 A1 的 fake 修改**，所以 Wave 0 就能做
- **大小**：M｜**實機**：實測需要｜**執行者**：Codex｜**規則**：R6 R9 R10 R12 R13

### ARB 修補軌

#### A1 ARB 必要修補（Wave 1）
- **目標**：
  - `start_together` 在 `INIT:TRAN` 前補送 `TRIG:ARB:SOUR IMM,(@…)`，用 `write_checked` 查錯
  - 新增 `slew_warnings(group, functions) -> list[str]`，對 `VOLT:SLEW?` 與 `VOLT:SLEW:MAX?` 不相等的通道產生警告
  - `run_functions(..., on_warning=None)` 只能新增關鍵字參數；`None` 表示寫 log。警告在開輸出**之前**發出，執行照常進行
  - SEQ、List、CLI 三條路徑都把 `on_warning` 接上
- **獨占輸出**：
  - `src/n6705c_seq/execute.py`
  - `src/n6705c/core/fake.py`（Wave 1 這段時間，依 C9）
  - `src/n6705c_seq/web_seq.py`
  - `src/n6705c_seq/web_list.py`
  - `src/n6705c_seq/cli.py`
  - `tests/test_execute.py`、`tests/test_web_seq.py`、`tests/test_web_list.py`、`tests/test_cli.py`
- **唯讀依賴**：`i18n.py`（key `slew_warning` 已由 T0 加入）、`core/group.py`、`core/arb/*`
- **驗收**：
  - `python3 -m pytest -q tests/test_execute.py tests/test_web_seq.py tests/test_web_list.py tests/test_cli.py` 通過，且包含這些新測試：送出順序是 `TRIG:ARB:SOUR IMM` → `INIT:TRAN`；ARB source 被拒時在開輸出前中止並關輸出；slew 不是最大值時有警告、等於最大值時沒有
  - 全套 pytest 綠；`node --test tests/js` 綠
  - `n6705c-seq run examples/powerup.csv --demo --yes` 結束碼為 0
- **大小**：M｜**實機**：否（實機效果由 HAs 驗）｜**執行者**：Codex 寫；Opus 審｜**規則**：R3 R4 R6 R8 R9 R10 R16

#### A2 multi.py 回滾與文件（Wave 1，可選，不在 GUI 路徑上）
- **目標**：
  - `MultiArbRunner.arm` 失敗時，也要把正在 arm 的那個通道 abort 掉（`multi.py:137-144`）
  - 改正 docstring 裡「`*TRG` 釋放 ARB」的說法（`multi.py:1-18,151-160`），讓它和 §4.10 一致
- **獨占輸出**：`src/n6705c/core/arb/multi.py`、`tests/core/test_arb_multi.py`
- **驗收**：`python3 -m pytest -q tests/core/test_arb_multi.py`；全套綠
- **大小**：S｜**實機**：否｜**執行者**：Sonnet；Opus 審｜**規則**：R6 R12

#### A3 sequence 步驟編號（等 HAs 的 `seqnum` 實測結果，可選）
- **獨占輸出**：`src/n6705c/core/arb/sequence.py`、`tests/core/test_arb_sequence.py`
- **目標**：依實測結果把 `enumerate(start=1)`（`sequence.py:185`）改成 0 起算，或者確認不用改
- **大小**：S｜**實機**：需要 HAs 的結果｜**執行者**：Sonnet｜**規則**：R6

### 即時紀錄 core 軌（P1）

#### C1 ELOG recorder 錯誤語意與尾段策略（Wave 1）
- **目標**：依 C7 實作 `run()`、`tail` 參數、錯誤型別、`on_event`、`stop()` 回傳 bool、讀錯誤佇列的規則。`start()` 改成「開一條執行緒跑 `run()`」，舊的呼叫方式繼續相容。
- **獨占輸出**：`src/n6705c/core/elog.py`、`tests/core/test_elog_recorder.py`
- **唯讀依賴**：`core/fake.py`（**不改**；要模擬回 4 bytes 空區塊、讀取失敗、sink 拋錯時，用測試檔內包住 session 的 wrapper 注入）
- **驗收**：`python3 -m pytest -q tests/core/test_elog_recorder.py` 通過，涵蓋以下情境：
  - sink 拋錯時中止，且 ABOR 由同一執行緒送出（用 `threading.get_ident` 斷言）
  - 連續 3 次讀取失敗時中止；單次失敗只發事件
  - 積壓中讀到 0 筆時發事件
  - 兩種 `tail` 策略在 fake 上都拿到全部筆數（誤差不超過 1 個讀取間隔）
  - join 逾時時 `stop()` 回 False，且 `_thread` 沒被清掉
  - 全套測試綠
- **大小**：M｜**實機**：否；`tail` 的預設值等 H0｜**執行者**：Codex；**雙審**（Opus＋Codex，因為牽涉資料遺失語意）｜**規則**：R6 R9 R10 R12

#### C2 DLOG 兩個 bug（Wave 1）
- **目標**：依 C8 修正。
- **獨占輸出**：`src/n6705c/core/dlog.py`、`tests/core/test_dlog_session.py`
- **驗收**：`python3 -m pytest -q tests/core/test_dlog_session.py` 通過，涵蓋：EXT 觸發時 `start()` 後狀態是 `ARMED`；`.dlog` 下載失敗時儀器上的 `.dlog` 沒被刪；CSV 和 `.dlog` 都成功下載時兩個都刪。全套綠。
- **大小**：S｜**實機**：否｜**執行者**：Sonnet；Opus 審｜**規則**：R6 R12

#### W .dlog `.part` 寫檔器（Wave 1）
- **目標**：依 C6 實作。純 stdlib＋numpy，不 import `n6705c`、`webgui`、`analysis`。
- **獨占輸出**：`src/n6705c_seq/record/dlogfile.py`、`tests/record/test_dlogfile.py`
- **唯讀依賴**：`analysis/core.py`（只在測試裡用 `parse_dlog` 做往返）、fixture `.dlog`
- **驗收**：`python3 -m pytest -q tests/record/test_dlogfile.py` 通過，涵蓋：
  - 往返：`parse_dlog` 讀回的 V、I 和寫入的值逐點相等，`tint` 正確，`fields` 順序是每通道先 V 再 I
  - `elog_block` 的重排正確
  - finalize 後只剩最終檔和旁檔，沒有殘留 `.part`
  - 寫到一半（含半筆）中斷：`recover_part` 截到完整筆數後可以 parse
  - 沒有對應旁檔的 `.part` 一律不碰
  - write=0 的 `.part` 也能 parse（驗證 F12 的推論）
  - 全套綠
- **大小**：M｜**實機**：否；14585A 能不能開等 HW-P2｜**執行者**：Codex；**雙審**（檔案格式）｜**規則**：R1 R12 R17 R18

#### X1 Viewer 讀兩個 `>I`（可選，不擋任何任務）
- **獨占輸出**：`src/n6705c_seq/analysis/core.py`、`tests/analysis/test_core.py`
- **目標**：把讀取改成兩個 `>I` 並依索引重排，或先直接拒絕 trigger≠0 的檔（HANDOFF.md:240）。
- **大小**：S｜**執行者**：Codex｜**規則**：R1 R7

### 即時紀錄 GUI 軌（P2，等 A1 合併後開始）

#### S 快照包絡（Wave 2）
- **獨占輸出**：`src/n6705c_seq/record/snapshot.py`、`tests/record/test_snapshot.py`
- **目標**：
  - `Envelope(keys, period)`：`append(block)` 在超過 2000 點時，相鄰的 bucket 兩兩合併
  - `snapshot(since_seq)` 執行緒安全，回傳副本，符合 C2 的格式
  - 純 numpy
- **驗收**：pytest 該檔通過，涵蓋：1e6 筆之後每個 key 仍不超過 2000 點；min/max 等於整段的真實極值；20 條執行緒同時 append 和 snapshot 不出錯；seq 不變時回 `unchanged`
- **大小**：S｜**執行者**：Sonnet｜**規則**：R1 R12

#### G Bridge 與 gate 測試（Wave 2）
- **目標**：
  - 在 `web_record.py` 實作 `RecordApi` mixin，提供 C1 的 6 個方法，worker 用 C4 的固定寫法
  - `record_load` 用 `self._gate.issue_grant(gen, "open", "dlog", last_path)`
  - 執行時才 import `record.backend`；測試時 monkeypatch 成假的 backend
  - `webgui.py` 只改 import 和 `class Api(SeqApi, ListApi, RecordApi, _Core)`（`webgui.py:272`）
- **獨占輸出**：
  - `src/n6705c_seq/web_record.py`（新）
  - `src/n6705c_seq/webgui.py`
  - `src/n6705c_seq/analysis/contracts.py`（Wave 2 這段時間，**只改** `SELFTEST_JS_API_*` 兩個 tuple，:33-43）
  - `tests/test_web_record.py`（新）、`tests/test_record_gate.py`（新）
  - `tests/test_webgui.py`（公開介面集合）
  - `src/n6705c_seq/analysis/gate.py`：歸 G 擁有，**預設不改**
- **唯讀依賴**：`record/contracts.py`、`gate.py`、`web_seq.py`（參考 `_seq_start` 的寫法）
- **驗收**：`python3 -m pytest -q tests/test_web_record.py tests/test_record_gate.py tests/test_webgui.py tests/test_selftest_api_classes.py` 通過，其中 `test_record_gate.py` 涵蓋這些轉移：
  1. 從 analysis 經 draining、idle 到 `record_start` 成功取得 power
  2. 在 analysis 或 draining 狀態呼叫 `record_start`，被拒並回對應錯誤碼
  3. 錄製中呼叫 `analysis_enter` 被拒
  4. 錄製中關窗被拒
  5. 停止 → idle → enter → `record_load` 拿到的 grant 能被 `consume_grant` 接受；舊世代或沒有錄完的檔時被拒
  6. 停止卡住超過 60 s 進 `fault`（用短 timeout monkeypatch）

  另外還要符合：`RECORD_ENABLED=False` 時 `record_start` 被拒；`record_stop` 只設旗標（另一條執行緒立刻返回）；全套綠
- **大小**：M–L｜**實機**：否｜**執行者**：Codex；**雙審**（並行與互斥）｜**規則**：R2 R3 R4 R5 R7 R8 R9 R16

#### K 分頁外殼：兩種模式＋一鍵載入（Wave 2）
- **目標**：
  - `analysis.js` 加「檢視／錄製」模式切換。檢視模式維持現況；錄製模式先呼叫 `analysis_leave`、卸下 iframe、顯示 `#record-pane`
  - 錄製中不能切回檢視，也不能切到別的模式
  - 分頁的 `leave` 在錄製模式下什麼都不做
  - 進入分頁時，若 `record_status().running` 為真，直接進錄製模式
  - 對外提供 `App.openRecorded()`：切到檢視 → enter → 等這一代 Viewer 的 `ready` → `record_load(gen)` → 對 iframe 送 `open_push`
  - `index.html` 加模式切換按鈕、`<div id="record-pane" hidden>`、`record.js` 和 `record.css` 的引用
- **獨占輸出**：`src/n6705c_seq/web/index.html`、`src/n6705c_seq/web/analysis.js`、`tests/js/analysis.test.js`
- **唯讀依賴**：`web/app.js`（不改〔推論〕，見 F16）、契約的 JS 介面
  - K 和 J 之間的契約：J 設定 `App.recordPane = {show(), hide(), running()}`；K 提供 `App.openRecorded()`
- **驗收**：
  - `node --test tests/js` 通過，涵蓋：切模式的呼叫順序；錄製中切分頁不會 leave；`open_push` 只送給精確的 origin，而且帶著當前的 gen；舊世代的 `ready` 被忽略；`RECORD_ENABLED=false` 時看不到模式切換
  - 現有 26 項 JS 測試仍綠
  - `test_web_sources_have_no_cjk_text` 通過
- **大小**：M｜**實機**：否｜**執行者**：Opus（沿用 §11 T6 的外殼分工）｜**規則**：R3 R4 R15 R16

#### J 錄製面板與圖表（Wave 2）
- **目標**：
  - 在 `#record-pane` 裡畫表單：通道 1–4 勾選、V/I、週期、機台 1 或 2（v1 一次只錄一台）
  - 開始和停止鈕跟著 `App.powerAllowed` 和錄製狀態反灰
  - 狀態列：已錄時間、筆數、缺口數；有缺口時顯示警告
  - canvas 畫 min/max 包絡；`preview=true` 時標示「預覽」（i18n key）
  - 只在面板顯示而且正在錄製時，每 `SNAPSHOT_POLL_MS` 輪詢一次 `record_snapshot`
  - 錄完後顯示「一鍵載入」，按下呼叫 `App.openRecorded()`
- **獨占輸出**：`src/n6705c_seq/web/record.js`（新）、`src/n6705c_seq/web/record.css`（新）、`tests/js/record.test.js`（新）
- **驗收**：`node --test tests/js` 通過，涵蓋：舊的 `run_id` 或 `seq` 被丟掉；面板隱藏時停止輪詢；preview 標示會出現；按鈕反灰邏輯正確。另外 CJK 測試和 key 測試都通過
- **大小**：M｜**實機**：否｜**執行者**：Sonnet｜**規則**：R3 R4 R16

#### V Viewer 接收 `open_push`（Wave 2）
- **目標**：
  - `viewer.html` 接受 `open_push{grant,name}`，驗證方式和 `open_result` 相同（source、origin、v、gen），然後走主檔開檔流程（`viewer.html:1798-1803` 的邏輯，抽成共用函式）
  - 有長工作在跑時忽略這則訊息，並顯示 toast
  - `shell_stub.html` 加一顆按鈕，用來模擬推送
- **獨占輸出**：`src/n6705c_seq/analysis/web/viewer.html`、`src/n6705c_seq/analysis/web/shell_stub.html`
- **驗收**：
  - 用假外殼人工檢查：推送後能開檔，舊 gen 的推送被忽略
  - `tests/analysis/parity_tools` 的雜湊檢查不受影響（`original_before_python.html` 不動）
  - 全套 pytest 綠
- **大小**：S–M｜**執行者**：Sonnet（沿用 §11 T5）｜**規則**：R1 R15

### Backend（Wave 3，依 H0 結果分支，兩支互斥）

#### B-ELOG（H0 成功時）
- **目標**：
  - 實作 `record/backend.py`：`ElogSession` 設定讀回 → `ElogRecorder.run(tail=<H0 結果>)`
  - sink 收到資料時做 `elog_block` 重排，再寫進 `DlogWriter.append` 和 `Envelope.append`
  - 事件寫進旁檔的 gaps 和 notes；旁檔還要存要求值和讀回值、IDN、UTC 時間
  - 停止時 finalize 存檔
- **獨占輸出**：`src/n6705c_seq/record/backend.py`、`tests/record/test_backend_elog.py`
- **唯讀依賴**：C1、W、S 的成品，`execute.connect_group`，`InstrumentGroup.demo`
- **驗收**：
  - 用 fake 錄 3 s 後停止：.dlog 的筆數和預期相符（誤差不超過 1 個讀取間隔），`parse_dlog` 讀得出來，旁檔欄位齊全
  - sink 寫檔失敗時中止，`.part` 留著且能 `recover_part`
  - `+323` 在開始前就回 `bad_config`
  - 全套綠
- **大小**：L｜**實機**：開始條件是 H0 PASS｜**執行者**：Codex；**雙審**｜**規則**：R6 R9 R10 R17 R18

#### B-DLOG（H0 失敗時）
- **目標**：
  - 實作 `record/backend.py`：`DlogSession`（含 C2 的修正）＋約 40 Hz 的 `MEAS:VOLT?`、`MEAS:CURR?` 迴圈，輸出到 `Envelope`，`preview=True`
  - 停止後 `_untimed` → `transfer` → 旁檔
  - 套用 `DLOG_FALLBACK_MAX_BYTES` 上限
  - 讓 fake 在 ABOR 或時間到時產生真格式的 .dlog
- **獨占輸出**：`src/n6705c_seq/record/backend.py`、`src/n6705c/core/fake.py`（Wave 3 這段時間）、`tests/record/test_backend_dlog.py`
- **驗收**：用 fake 錄完後下載的 .dlog 能被 `parse_dlog` 讀出；預覽快照的 `preview` 為 true；下載失敗時儀器上的檔保留；全套綠
- **大小**：M–L｜**實機**：開始條件是 H0 FAIL｜**執行者**：Codex；**雙審**｜**規則**：R6 R9 R10 R12

### 收尾

#### I 整合與端到端（Wave 4）
- **獨占輸出**：`tests/test_record_integration.py`
- **目標**：
  - 用 demo（fake）走完整條路：`record_start` → 錄 2 s → `record_stop` → idle → `analysis_enter` → `record_load` → 真的 server `/open` 解析成功
  - 再跑一次 `--selftest`
  - 發現問題就退回該檔的擁有者修
- **驗收**：
  - `python3 -m pytest -q` 全綠，`node --test tests/js` 全綠
  - `n6705c-seq-gui --selftest` 結束碼 0
  - 人工在 macOS 點測兩種模式和一鍵載入
- **大小**：M｜**執行者**：Opus；Claude 驗收｜**規則**：R16

#### D 文件與打包（Wave 4，和 I 並行）
- **獨占輸出**：`README.md`、`HANDOFF.md`、`HANDOFF_WINDOWS.md`（錄製驗收清單）、`packaging/n6705c_seq.spec`（加 `collect_submodules("n6705c_seq.record")`）
- **目標**：寫清楚以下事項
  - **core 同步清單**：`elog.py`、`dlog.py`、`fake.py`、`arb/multi.py` 改了哪些，要手動同步回 `../N6705C-APP`
  - 旗標 `RECORD_ENABLED` 放在哪裡
  - H0 結果
  - 錄製中不能關窗
- **驗收**：`packaging/build.ps1` 能在 CI 的 build-windows 跑過；文件裡的指令都能照抄執行
- **大小**：S｜**執行者**：Haiku｜**規則**：R6 R11

#### R 獨立審查（Wave 5）
- **獨占輸出**：`docs/live-logging-review-codex.md`、`docs/live-logging-review-opus.md`
- **審查重點**：互斥能不能被繞過、單一 worker 的 I/O、資料遺失語意、`.part` 和截尾的安全性、import 邊界、Windows 上刪檔和改名。Codex 和 Opus 各寫一份，互相不看。有擋住合併的問題，退回原擁有者修
- **大小**：M｜**執行者**：Codex＋Opus（**雙審**）

#### HW-P2（使用者做，不寫程式）
用實機錄一台機台：先 USB 30 分鐘，再 LAN 30 分鐘；自寫的 .dlog 分別用 Viewer 和 14585A 開。全部通過後，由使用者決定是否把 `RECORD_ENABLED` 改成 True。

---

## 4. 依賴圖與並行波次

```
Wave 0 ─┬─ T0 契約 (L, Opus＋Codex 唯讀) ───────────────────────────────┐
        ├─ H0s ELOG 閘門腳本 (M) ──► 【使用者實機跑 H0】──┐               │
        └─ HAs ARB 實測腳本 (M) ──► 【使用者實機跑 H-ARB】─┼─► A3(可選)    │
                                                      │               │
Wave 1 (等 T0)  ─┬─ A1 ARB 必要修補 (M) ────────────────┼──────┐        │
                ├─ C1 ELOG recorder (M) ──────────────┼───┐  │        │
                ├─ C2 DLOG 兩個 bug (S) ──────────────┼─┐ │  │        │
                ├─ W  .dlog 寫檔器 (M) ───────────────┼┐│ │  │        │
                └─ [A2 可選 S] [X1 可選 S]              ││││ │  │        │
                                                      ││││ │  ▼ (使用者決策 4：P2 等 A1 合併)
Wave 2 (等 A1)  ─┬─ S 快照 (S) ────────────────────────┼┼┼┼─┤        │
                ├─ G bridge＋gate 測試 (M-L) ──────────┼┼┼┼─┼────┐   │
                ├─ K 分頁外殼 (M) ─────────────────────┼┼┼┼─┼──┐ │   │
                ├─ J 錄製面板 (M) ─────────────────────┼┼┼┼─┼─┐│ │   │
                └─ V Viewer open_push (S-M) ───────────┼┼┼┼─┼┐││ │   │
                                                      ▼▼▼▼ ▼││││ │   │
Wave 3 (等 H0 結果＋C1/C2＋W＋S)  ── B-ELOG ‖ B-DLOG（二選一，L）──┤││││ │
                                                               ▼▼▼▼▼
Wave 4 ─┬─ I 整合 e2e (M, Opus)
        └─ D 文件／打包 (S, Haiku)
Wave 5 ─── R 雙審 (Codex ‖ Opus) ──► 退回各擁有者修 ──► HW-P2（使用者實機）──► 使用者決定是否打開 RECORD_ENABLED
```

| 波次 | 開始條件 | 可同時進行的任務 | 最大並行數 |
|---|---|---|---|
| Wave 0 | 使用者說開工 | T0、H0s、HAs | 3 |
| Wave 1 | T0 合併 | A1、C1、C2、W（加上可選的 A2、X1） | 4（含可選 6） |
| Wave 2 | A1 合併（決策 4） | S、G、K、J、V | 5 |
| Wave 3 | H0 結果出來＋C1／C2／W／S 合併 | B-ELOG **或** B-DLOG | 1 |
| Wave 4 | B、G、K、J、V 合併 | I、D | 2 |
| Wave 5 | I 通過 | R（Codex、Opus 各一份） | 2 |

- **H0 分支**：Wave 3 是唯一依 H0 結果分岔的地方。
  - H0 PASS → B-ELOG，`tail` 和讀取參數用 H0 量到的值。
  - H0 FAIL → B-DLOG，C13 的上限生效，旁檔和 UI 標示「預覽」。
  - 兩支都寫 `record/backend.py`，所以只會做其中一支。
  - 其他任務都和 H0 結果無關，可以先做。C1 的 `tail` 兩種策略都要實作，所以不用等 H0。
- **只在硬體結果之後才能開始的任務**：B-*（等 H0）、A3（等 HAs 的 `seqnum`）、HW-P2，以及打開 `RECORD_ENABLED`。
- **關鍵路徑**：T0 → A1 → S → B → I → R → HW-P2。
  - 實際上通常會卡在 **H0s → 使用者跑 H0（≥ 2 h，含 USB 和 LAN 各 1 h 長錄）→ B**。所以 **H0s 放在 Wave 0，越早交給使用者越好**。
  - 〔推論〕排程的不確定性主要來自實機可用的時間，而不是程式量。

### 合併順序（整合點）
1. T0
2. H0s、HAs（隨時可以合併，和程式碼無關）
3. A1
4. C2 → C1 → W（A2、X1 可以插在任何位置）
5. S → G → V → K → J（G 先合併，K 和 J 的人工點測才接得上真的 bridge；JS 單元測試本來就用 stub，不受順序影響）
6. B-*
7. I → D
8. R 的修補

每次合併前都要：從最新的 `main` rebase，跑 `python3 -m pytest -q` 和 `node --test tests/js`，兩者都全綠。

---

## 5. H0 判定準則（T0 會照抄進契約）

**PASS** 需要同時滿足：
1. 用 1 通道 1 ms、以及 4 通道 V+I 在最短週期（819.2 µs）兩種設定，每 250 ms 讀一次，USB 和 LAN 各連續錄 ≥ 1 h：
   - `records_received` 和 `records_produced` 的差不超過 1 個讀取間隔的筆數
   - 沒有發生溢位
   - 積壓超過 2 個讀取間隔的筆數時，從沒讀到 0 筆
2. `stall` 停 5 s 後，資料一筆不少。
3. `tail` 子項能判定兩種尾段策略中哪一種能拿到完整尾段。

**FAIL**：§4.0.2 的現象（積壓中讀到 0 筆，或回 4 bytes 空區塊）能重現，而且單改一個變數（首讀時機、格式、要求筆數）都避不掉。結果是走 B-DLOG。

**PASS 但需要調參數**：例如要求筆數必須小於 16384 才正常。這種情況把那個值寫進契約常數，然後走 B-ELOG。

---

## 6. 界線規則（任務標註會引用這些編號）

| # | 規則 | 依據 |
|---|---|---|
| R1 | `analysis/*` 只准 import stdlib、numpy、`n6705c_seq.analysis.*`。不得 import `n6705c`、`record`、`webgui`、`web_*`、`execute` | `tests/analysis/test_boundary.py`、HANDOFF.md:195 |
| R2 | `webgui` 從 analysis 只 import `server`、`gate`、`contracts`。錄製相關一律經過 `web_record.py` | 升級報告.md:83 |
| R3 | `webgui.py`、`web_*.py`、`web/*`、`form.py`、`list_form.py`、`synth.py`、`listplan.py`、`packaging/gui_entry.py` 不得有中文；所有字串用 `tr()` 或 `App.t()`。`record/*.py` 不在掃描範圍內，但比照辦理 | `test_webgui.py:146-153` |
| R4 | 用到的 `tr()`、`data-i18n`、`.t()` key 都必須已在 `i18n.STRINGS`。**新 key 只有 T0 能加**，缺 key 回報 T0 | `test_webgui.py:156-176` |
| R5 | 公開 `Api` 方法：完整集合有測試，selftest 分類必須互斥且涵蓋全部。新方法預設放禁止清單 | `test_webgui.py:119-127`、`test_selftest_api_classes.py`、`analysis/contracts.py:30-43` |
| R6 | `src/n6705c/core/**` 是 `../N6705C-APP` 的手動同步複本。只能新增關鍵字參數或新符號，不能改舊簽章；每一處修改都列進 D 的同步清單；不改舊專案；不 editable 安裝 `n6705c-app` | HANDOFF.md:20-21,220 |
| R7 | `analysis/contracts.py` 是凍結契約，只有 T0 能改。**唯一例外**：G 在 Wave 2 只改 `SELFTEST_JS_API_*` 兩個 tuple | `analysis/contracts.py:5` |
| R8 | gate 的唯一取資格點是 `_reserve`。worker 在 `finally` 裡 `_release`；主工作用 `_untimed`、`try/finally: _finishing`；不得繞過 gate，也不得因逾時而釋鎖 | `contracts.py:72-81`、升級報告.md §5 |
| R9 | VISA session 不是執行緒安全的。一次錄製的所有 I/O（含 ABOR 和尾段讀取）都在同一個 worker；停止只設旗標 | HANDOFF.md:71,252 |
| R10 | 每一步設定都把 `SYST:ERR?` 讀到 0；`+310`、`+323` 照 C7 處理；儀器回報的錯誤碼可能是正數，不能用 `< 0` 判斷 | HANDOFF.md:252、`elog.py:256-258` |
| R11 | 文件裡的 Codex 指令一律加 `</dev/null` | 使用者全域規則、HANDOFF.md:148 |
| R12 | 使用者沒說開工就不寫程式；使用者沒說 commit 就不 commit；每個任務只能改自己的獨占檔 | HANDOFF.md:146-147 |
| R13 | 實機腳本對儀器的動作要最小化：開輸出前需要 `--yes` 或互動確認；`finally` 一律 ABOR 並關輸出；不准對同一台主機做 Wave 外的 `*RST` | §12、§10 的慣例 |
| R14 | `LIST_VERIFIED`、`ANALYSIS_ENABLED` 不動；`RECORD_ENABLED` 預設 False | `app.js:4`、`contracts.py:19` |
| R15 | Viewer 頁面只放在 `analysis/web/`（可以有中文）。`web/` 不能放 Viewer 的東西 | 升級報告.md:152 |
| R16 | 每一波結束時，既有測試都要全綠。誰的測試檔壞了，由那個測試檔的擁有者修 | 升級報告.md:108 |
| R17 | 只對「有對應旁檔」的自家 `.part` 做截尾；其他 .dlog 一律不改寫 | HANDOFF.md:254 |
| R18 | 寫檔器存原始值，不做負電流替換；`sense_minmax=0` | HANDOFF.md:254 |

---

## 7. 檔案所有權矩陣與衝突檢查

| 檔案 | Wave 0 | Wave 1 | Wave 2 | Wave 3 | Wave 4–5 |
|---|---|---|---|---|---|
| `docs/live-logging-contract.md`、`record/__init__.py`、`record/contracts.py`、`tests/record/test_record_contracts.py` | T0 | | | | |
| `i18n.py`、`docs/analysis-contract.md` | T0 | | | | |
| `analysis/contracts.py` | T0（Msg、註解） | | G（只改 SELFTEST tuple） | | |
| `scripts/hw_elog_h0.py`、`tests/test_hw_elog_h0.py` | H0s | | | | |
| `scripts/hw_arb_trig_check.py`、`tests/test_hw_arb_trig_check.py` | HAs | | | | |
| `execute.py`、`web_seq.py`、`web_list.py`、`cli.py`、`tests/test_execute.py`、`test_web_seq.py`、`test_web_list.py`、`test_cli.py` | | A1 | | | |
| `core/fake.py` | | A1 | | B-DLOG（只在 H0 FAIL 時） | |
| `core/arb/multi.py`、`tests/core/test_arb_multi.py` | | A2 | | | |
| `core/arb/sequence.py`、`tests/core/test_arb_sequence.py` | | | A3（等 HAs） | | |
| `core/elog.py`、`tests/core/test_elog_recorder.py` | | C1 | | | |
| `core/dlog.py`、`tests/core/test_dlog_session.py` | | C2 | | | |
| `record/dlogfile.py`、`tests/record/test_dlogfile.py` | | W | | | |
| `analysis/core.py`、`tests/analysis/test_core.py` | | X1（可選） | | | |
| `record/snapshot.py`、`tests/record/test_snapshot.py` | | | S | | |
| `web_record.py`、`webgui.py`、`analysis/gate.py`（預設不改）、`tests/test_web_record.py`、`test_record_gate.py`、`test_webgui.py` | | | G | | |
| `web/index.html`、`web/analysis.js`、`tests/js/analysis.test.js` | | | K | | |
| `web/record.js`、`web/record.css`、`tests/js/record.test.js` | | | J | | |
| `analysis/web/viewer.html`、`analysis/web/shell_stub.html` | | | V | | |
| `record/backend.py` | | | | B-ELOG ⊕ B-DLOG | |
| `tests/record/test_backend_elog.py` ／ `test_backend_dlog.py` | | | | 各自的 B | |
| `tests/test_record_integration.py` | | | | | I |
| `README.md`、`HANDOFF.md`、`HANDOFF_WINDOWS.md`、`packaging/n6705c_seq.spec` | | | | | D |
| `docs/live-logging-review-*.md` | | | | | R |
| **凍結**（沒列出的檔都不得改，例如 `web/app.js`、`web/layout.js`、`web/seq.js`、`web/list.js`、`style.css`、`analysis/server.py`、`analysis/storage.py`、`form.py`、`plan.py`、`synth.py`、`listplan.py`、`e36441a.py`、`core/` 其餘檔案、`pyproject.toml`） | — | — | — | — | — |

**衝突檢查結果**：我逐列核對過，**同一波內沒有任何檔案有兩個擁有者**。

有 3 個跨波共用的檔，處理方式如下（都改切法解決，不是容忍重疊）：

1. **`analysis/contracts.py`**：T0（Wave 0）只改 Msg 和註解；G（Wave 2）只改 SELFTEST tuple。兩者時間不重疊、改的區段不同。
   - 這段不能交給 T0：如果 T0 先把 `record_*` 加進 tuple，在 G 合併前 `test_selftest_api_classes` 會因為「stale names」而失敗（F6）。所以必須讓「加方法」和「分類」在同一個任務裡完成。
2. **`core/fake.py`**：A1（Wave 1）補 ARB 和 SLEW 指令；B-DLOG（Wave 3，只在 H0 失敗時）補真格式的 .dlog。兩者時間不重疊。
   - C1 需要的故障注入改成在測試裡包一層 wrapper，**不碰 fake**。這是為了避免和 A1 搶同一個檔而特意調整的切法。
3. **`record/backend.py`**：B-ELOG 和 B-DLOG 依 H0 結果二選一，不會同時發生。如果將來兩支都要（例如 P5），改成 `backend_elog.py` 和 `backend_dlog.py`，`backend.py` 只負責分派，這需要重新經過 T0。

其他可能想加進共用檔的需求，處理方式如下：

- **i18n**：統一由 T0 加齊。
- **`app.js`**：K 判定不用改（F16，〔推論〕）。如果實作時發現非改不可，回報 T0 重新分派，不得順手改。
- **`webgui.py`**：只有 G 能改，而且只改 2 行。

---

## 8. 驗收清單（每一波、最終）

- [ ] `git status` 只出現該任務的獨占檔
- [ ] 對照 `docs/live-logging-contract.md` 逐條核對簽章和回傳
- [ ] `python3 -m pytest -q` 全綠（基準：838 過／7 跳過，只能增加）
- [ ] `node --test tests/js` 全綠（基準：26 過）
- [ ] `test_web_sources_have_no_cjk_text`、`test_web_translation_keys`、`test_strings_and_public_surface`、`test_every_public_js_api_method_is_classified`、`tests/analysis/test_boundary.py` 全部通過
- [ ] 改到 core 的任務：在 D 的同步清單裡留了紀錄
- [ ] 最終：`n6705c-seq-gui --selftest` 結束碼 0；`n6705c-seq run examples/powerup.csv --demo --yes` 結束碼 0；macOS 人工點測檢視→錄製→停止→一鍵載入；Windows 11 依 `HANDOFF_WINDOWS.md` 新增的錄製項目驗收
- [ ] 實機：H0 報告、H-ARB 報告、HW-P2 報告都已貼回

---

## 9. 執行者分工（沿用 HANDOFF §11 的邏輯）

| 執行者 | 任務 | 理由 |
|---|---|---|
| Opus | T0（寫）、K、I、R 的其中一份、各任務的審查 | 契約、外殼、整合，比照 §11 的 T0、T6 |
| Codex | H0s、HAs、A1、C1、W、G、B-*、X1、T0 唯讀挑錯、R 的其中一份 | 儀器 I/O、並行、檔案格式，比照 §11 的 T2、T3 |
| Sonnet | A2、A3、C2、S、J、V | 中小型、界線清楚，比照 §11 的 T4、T5 |
| Haiku | D | 文件與打包，比照 §11 的 T7 |

**雙審**（Opus＋Codex 都要審）：T0、C1、W、G、B-*、R。

Codex 的呼叫方式：`codex exec --skip-git-repo-check -s workspace-write "<任務>" </dev/null > out.txt 2>&1`。任務說明裡要寫明只准改哪些檔、不准 commit、最後跑 `python3 -m pytest -q`。

---

## 10. P3 以後（只列大綱）

- **P3 多台**：每台一個 worker、一個 VISA、一個檔；各自有時間軸；旁檔記下跨台的 skew 估計；`TRIGGER_SKEW_SECONDS` 和長時間漂移要另外實測留證。
- **P4 邊上電邊錄**：同一個 power token 下，ARB 和 ELOG 共用一次觸發；需要改 gate 的持有者語意，以及 `run_functions` 和錄製 worker 怎麼合併。這屬於新契約。
- **P5 DLOG 串流下載／ACQ 高速**：`MMEM:DATA?` 分段讀取，取代整檔讀進記憶體；數位化器高速擷取。
- **最後 HIST**。
- 另外不在任何階段裡、需要另外開工的小修：Viewer 讀兩個 `>I`（X1）、Windows 不穩定測試（HANDOFF.md:208）。

---

## 11. 需要使用者決定（最多 3 項）

1. **B-ELOG 要不要在 H0 結果出來前先做？** 建議：不要。fake 上做得出來，但如果 H0 失敗，整支會白做。如果實機要等很久，可以改成先做、合併時再看 H0 結果。
2. **A2 和 A3（不在 GUI 路徑上的 ARB 修正）要不要這次一起做？** 建議：A2 跟 Wave 1 一起做，因為它小而且獨立；A3 等 HAs 的實測結果。
3. **錄製檔存在哪裡？** 建議：按「開始」時跳出原生另存對話框（和 `seq_save_csv` 一致）。另一個選項是固定資料夾加自動命名，那樣 `record_start` 就不用開對話框。
