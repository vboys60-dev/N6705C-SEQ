# 離線分析分頁｜T0 介面契約（v1）

日期：2026-10-29（修訂：Codex 第 7 輪 9 項必修；T1/T2 後 5 項：`_finishing` 範圍、legacy 測試、T4 HTTP 原檢查、CSV 容差、未搬工具；Codex 第 8 輪 5 項：`_untimed` 原子回傳 bool、主工作 try/finally 重新計時、主工作內部 I/O 例外明列、取代報告 §5 計時起點、`/release` 例外）　擁有者：T0　來源：`報告分析升級版/升級報告.md`（§5、§7、§9）

本文件與 `src/n6705c_seq/analysis/contracts.py` 是**同一份契約的兩半**：數字、列舉、型別以 `contracts.py` 為準（程式直接 import），語意與流程以本文件為準。兩者衝突時，回報 T0 修正，不得各自解讀。

- 任何任務要改這裡的名稱、數字或語意 → 回報 T0 改契約，不得私下改。
- 標 **〔預設，待使用者確認〕** 的項目，是 §9 未決題目照報告建議值先定的；使用者改判時只改常數或該節，不影響其他介面。
- 程式識別字一律英文；`web/*` 內不得有中文（`test_web_sources_have_no_cjk_text`）。

---

## 0. §9 決策對照

| 題 | 狀態 | 契約採用 |
|---|---|---|
| 1 鎖的範圍 | 已定案 | 進分析分頁即持有仲裁器（`analysis` 狀態），離開才釋放 |
| 2 匯出中切頁 | 已定案 | 切離＝取消進行中工作 |
| 3 子進程 | 已定案 | 第一版同進程；轉 worker 門檻見 §7.3 |
| 4 互斥範圍 | 已定案 | 只保證單一 GUI 進程內；`cli.py` 不動 |
| 5 `/buckets` | 〔預設，待使用者確認〕 | **解除暫緩**：路由開放，Viewer 在需要的原始範圍超過 `MAX_SAMPLES_SPAN` 時改用 `/buckets` |
| 6 效能驗收 | 已定案（最大 10M 起算、30M 不 OOM）；其餘數字〔預設，待使用者確認〕 | §7 |
| 7 全部關閉 | 〔預設，待使用者確認〕 | 分析狀態下一律拒絕（`_reserve` 回 `None`）；無緊急流程 |
| 8 外觀 | 〔預設，待使用者確認〕 | 語言強制跟 SEQ；主題 Viewer 獨立，存 `analysis_prefs.json` |
| 9 獨立版 Viewer | 〔預設，待使用者確認〕 | SEQ 內建版為唯一正本；獨立版（8765 埠、`啟動 DLOG Viewer.command`）凍結不維護，不進 repo |
| 10 比對檔／Marker 匯入 | 〔預設，待使用者確認〕 | 也走原生開檔對話框（`OpenPurpose.DLOG` / `MARKERS`） |

---

## 1. 仲裁器狀態機（T2 實作於 `analysis/gate.py`）

### 1.1 狀態

`State`：`idle | power | analysis | draining | closing | fault`。合法轉移只有 `contracts.TRANSITIONS` 列出的；其他一律拒絕（不強轉）。

| 狀態 | 意義 | 進入 | 離開 |
|---|---|---|---|
| `idle` | 都沒在做 | 啟動；`power` 收尾完；`draining` 完成 | → `power`、→ `analysis`、→ `closing` |
| `power` | 任何對儀器發命令的工作（含確認對話框開著、收尾關輸出、關連線） | `_reserve("power")` 成功 | → `idle`（持有者 `_release`）、→ `fault`（收尾逾時：最近一次 `_finishing` 後 60 s 內沒有成功的 `_untimed` 或 `_release`，§1.3）。關窗被拒，不直接進 `closing` |
| `analysis` | 分析分頁啟用中 | `analysis_enter()` 成功，`gen += 1` | → `draining`（`analysis_leave`）、→ `closing`（關窗） |
| `draining` | 離開分析中：拒絕新請求與新電源工作，等進行中工作停止 | `analysis_leave(gen)` 成功 | 工作全停且 `release_resources()` 為真 → `idle`（有 `close_pending` 則 → `closing`）；逾時 → `fault`（有 `close_pending` 則自動接續 → `closing`） |
| `closing` | 關窗收尾，拒絕一切 | `idle`／`analysis`／`draining`(close_pending)／`fault` 收到關窗 | 終態，進程退出 |
| `fault` | 工作卡住、無法確認安全 | `power` 收尾逾時；`draining` 逾時 | → `closing`（使用者關窗）；其餘只能重開程式 |

### 1.2 不變式

1. **單一鎖**：`gate` 內一把 `threading.Lock`。「檢查資格＋登記」（`_reserve`、`analysis_enter`、`job_begin`、`issue_grant`）與狀態轉移在同一把鎖內原子完成。
2. **CAS**：每次轉移都是 `(期望狀態, 期望 gen/token) → 新狀態`；不符就不轉。遲到的 `_release`、`job_end`、`leave`、對話框回傳**不得**改動 `closing` 或 `fault`，也不得影響新世代。
3. **世代 `gen`**：程序內整數，起始 0，**只有** `idle→analysis` 成功時 +1。所有分析請求、授權、`leave` 都帶 `gen`，不符即拒。
4. **不因逾時釋鎖**：逾時只會把狀態推向 `fault`，絕不推回 `idle`。`fault` 時不強制關 memmap。
5. 鎖只在記憶體；不跨進程（§0 題 4）。

### 1.3 三個逾時（`contracts.py`）

| 常數 | 值 | 起算 | 到期 |
|---|---|---|---|
| `DRAINING_TIMEOUT_S` | **30 s** | `analysis_leave` 被接受（進 `draining`）那一刻 | 工作未全停或資源未關 → `fault`（`close_pending` 則接 `closing`） |
| `POWER_FINISH_TIMEOUT_S` | **60 s** | worker **最近一次**呼叫 `_finishing(token)`（可重複呼叫，每次重新起算 60 s） | 期間未收到該 token 的 `_untimed`（成功）或 `_release` → `fault`；`_untimed` 成功後停表，直到下一次 `_finishing` |
| `CLOSING_CLEANUP_TIMEOUT_S` | **10 s** | 關窗背景清理執行緒開始 | 不再等：`server.shutdown(1.0)` 後 `os._exit(2)` |

監看由 gate 的計時執行緒（`threading.Timer` 或單一 watcher）負責；計時到期也走 CAS（例如 power 計時到期時若 token 已釋放，什麼都不做）。

v1 電源工作沒有外部停止訊號。計時範圍以 §1.4 呼叫點表為準（不以「其餘 I/O」概括）：
- **計時中**＝worker 執行緒內，從 `_finishing(token)` 起，到成功的 `_untimed(token)` 或 `_release(token)` 為止。
- **不計時**只有三個主工作呼叫（與現況相同，可跑很久）：`_work` 的 `run_resources`（E36441A）與 `run_plan`、`_list_work` 的 `execute.run_functions`。固定寫法（三處相同）：
  ```python
  if not self._untimed(token):      # 失敗：timer 已轉 fault，或 token 已不是持有者
      return                        # 跳過主工作，直接走外層 finally 清理（_finishing、關連線、_release）
  try:
      run_plan(...)                 # 主工作，不計時
  finally:
      self._finishing(token)        # 正常返回或拋例外，離開即重新計時
  ```
  之後的結果處理、`except` 分支、`finally` 清理全部在計時內。
- **主工作內部的 I/O 一併不計時（明列例外）**：這三個函式在 `execute.py`／`e36441a.py` 內自己做的連線、強制關輸出、關連線，worker 無法插入 `_finishing`，所以和主工作一起停表：
  - `run_resources`：內部 `_sessions`（開 VISA 連線，`e36441a.py:282`）、`run_rows` 內的 `_restore`（關輸出，`e36441a.py:228、233`）、`finally` 內 `t.close()`（`e36441a.py:285–287`）。
  - `run_plan`／`run_functions`：內部 `outputs_off(group)`（`execute.py:290、295`）。
  - 依據：這些模組不在 T2 可改清單，且屬待硬體驗證的路徑，v1 不改；每次儀器 I/O 仍受 VISA 逾時 `timeout_ms=10_000` 限制。卡在 VISA 逾時之外（驅動層無回應）時會停在 `power`，此為已知上限。
  - 主工作之外、由 worker 自己做的收尾（`_work` 的 `_seq_close(group)`、`_list_work` 的 `inst.close()` 迴圈）都在計時內，由 worker 的 `finally` 呼叫 `_finishing`（§1.4 表）。
  - ponytail: 升級路徑＝三個主工作加 keyword-only `on_cleanup: Callable[[], None] | None = None`，在內部收尾前呼叫，worker 傳 `lambda: self._finishing(token)`；要做時回報 T0 改契約。
- 確認對話框（worker 啟動前，在 js_api 執行緒）不計時。
- 其餘 worker 段落（偵測 `detect_model`、連線 `connect_group`、`synth.to_functions`、結果處理、`_seq_close`／`inst.close()`）都在計時內，卡住最多 60 s 後進 `fault`，不會永久停在 `power`（報告 §5「收尾逾時」）。

**取代報告 §5 舊說法**：`升級報告.md` §5（第 64 行）寫 power 收尾逾時「從工作已發出停止／結束訊號後起算」「從 `_release` 開始等待 worker 起算」。**本契約規則取代該說法**：計時起點是 worker 每次呼叫 `_finishing(token)`（偵測／連線開始即計時），只有三個主工作停表。理由：v1 電源工作沒有停止訊號，也沒有「等待 worker」的時點——worker 自己呼叫 `_release`，卡住時根本到不了 `_release`；若照舊說法，偵測、連線、關連線卡住都會永久停在 `power`、無法進 `fault`。報告本身由使用者另行更新，衝突時以本契約為準。

### 1.4 事件處理細則

**`_reserve(kind) -> Token | None`**（webgui 方法，委派 gate）
- `kind` 只接受 `"power"`，其他 → `ValueError`。
- 狀態是 `idle` → 轉 `power`，回 `Token(kind, gen=目前 gen, serial=全程序遞增)`；否則回 `None`。
- 呼叫端拿到 `None` 時，依目前狀態選錯誤文字：`power`→既有 `busy_body`；`analysis`→`power_denied_analysis`；`draining`→`analysis_denied_draining`；`closing`→`analysis_denied_closing`；`fault`→`fault_body`。
- 唯一取資格點；公開入口 `seq_run`、`test_connection`、`all_off`（經 `_seq_start`）、`list_run` 都只經此取得。
- 現況在確認對話框**之前**取得資格——維持不變；所以對話框開著時狀態已是 `power`，`analysis_enter` 會被拒。對話框取消 → 立即 `_release(token)`。

**`_finishing(token) -> None`**（webgui，委派 `Gate.finishing`）：（重新）開始 `POWER_FINISH_TIMEOUT_S` 計時，到期時刻＝本次呼叫＋60 s；token 不是目前持有者 → 無作用。

**`_untimed(token) -> bool`**（webgui，委派 `Gate.untimed`；**T2 後續修補新增**）：停止計時。**同一把鎖內原子**檢查並動作：
- `state == power` **且** `token == 持有者`（`Token` 全等比對，含 `gen`、`serial`；舊世代或舊序號必不相等）→ `_finish_epoch += 1`（尚未到期的計時器到期時因 epoch 不符而不動作），回 **True**。
- 其他情況（計時器已先取得鎖轉 `fault`、`closing`、token 已釋放或不是持有者）→ 不改任何狀態，回 **False**。
- 競態結論：計時器到期與 `_untimed` 都在同一把鎖內做 CAS，只有一方生效——計時器先 → 狀態已是 `fault`，`_untimed` 回 False；`_untimed` 先 → epoch 已變，計時器到期不動作。
- 呼叫端規格：回 False 時 worker **必須跳過主工作**（不得對儀器下主工作命令），直接走既有清理（外層 `finally` 的 `_finishing` → 關連線 → `_release`；此時 `_finishing` 無作用、`_release` 回 False，只記錄 worker 已結束）。寫法見 §1.3。只在三個主工作呼叫前使用。

呼叫點（行號對照 7f45af4 實碼；✔＝T2 已實作，⚠＝**T2 後續修補項，現況未涵蓋，需補**）：

| 函式（位置） | 呼叫 | 涵蓋 | 狀態 |
|---|---|---|---|
| `_test`（`web_seq.py:122`） | 每個 frame `for` 內第一行 `_finishing`（`:126`） | 該 frame 的 `detect_model`（`:133`）、`connect_group`（`:137`）、`_seq_close`（`:143`） | ✔ |
| `_off_work`（`web_seq.py:155`） | 進入時 `_finishing`（`:156`）；`_seq_close` 前再一次（`:177`） | 偵測（`:159`）、`off_resources`、`connect_group`（`:167`）、`outputs_off`、`_seq_close` | ✔ |
| `_work`（`web_seq.py:182`） | 外層 `try:`（`:183`）內第一行 `_finishing` | 偵測 `detect_model`（`:186`）、`connect_group`（`:196`） | ⚠ |
| `_work`（E36441A 分支） | `run_resources`（`:189`）前 `if not self._untimed(token): return`；`run_resources` 包 `try/finally`，`finally` 呼叫 `_finishing` | 主工作不計時（含其內部連線／關輸出／關連線，§1.3 例外）；返回後計時涵蓋結果 `_seq_result`（`:190`）、`return`（`:191`）、`except` 的 `_seq_result`（`:192–194`）。此分支沒有 `_seq_close`，直接到外層 `finally` 的 `_release`（`:217–218`） | ⚠ |
| `_work`（N6705C／demo） | `run_plan` 所在 `try:`（`:200`）內 `if not self._untimed(token): return`（`return` 仍經 `:214` 的 `finally` 關連線）；`run_plan`（`:202`）包 `try/finally`，`finally` 呼叫 `_finishing` | 主工作不計時；返回後計時涵蓋結果處理 `:203–209`、`except` 分支 `:210–213` | ⚠ |
| `_work` | `finally` 內 `_seq_close` 前 `_finishing`（`:215`，保留；重新起算無害） | `_seq_close` | ✔ |
| `_list_work`（`web_list.py:139`） | 外層 `try:`（`:141`）內第一行 `_finishing` | 偵測 `e36441a.detect_model`（`:143`）、`execute.connect_group`（`:149`）、`synth.to_functions`（`:154`） | ⚠ |
| `_list_work` | `execute.run_functions`（`:158`）前 `if not self._untimed(token): return`（經 `:171` 的 `finally` 關儀器）；`run_functions` 包 `try/finally`，`finally` 呼叫 `_finishing` | 主工作不計時；返回後計時涵蓋結果處理 `:159–165`、`except` 分支 `:166–170` | ⚠ |
| `_list_work` | `finally` 內關儀器迴圈前 `_finishing`（`:173`） | `inst.close()` 迴圈 | ✔ |

demo 模式走同一組呼叫（`InstrumentGroup.demo` 視同連線）。單段卡住超過 60 s → `fault`；依據：實碼 I/O 逾時 10 s（`e36441a.py:150` `timeout_ms=10_000`），正常單段遠低於 60 s。

**`_release(token) -> bool`**：只有 `狀態==power 且 token==持有者` 才轉 `idle` 並回 True。其他情況（舊 token、`fault`、`closing`）回 False 且不改狀態；`fault` 中若是卡住的那個 token，只記錄「worker 已結束」。worker 一律在 `finally` 呼叫，token 由 `_seq_start`／`list_run` 以執行緒參數傳入。

**關窗 `_on_closing() -> bool`**（`events.closing` 處理函式，**不可同步等待**）
1. 狀態 `closing`：回 `cleanup_done`（第二次事件，destroy 觸發 → True 放行）。
2. `power`：emit `["result","warning",tr("busy_title"),tr("busy_body")]`，回 False（現況行為）。
3. `draining`：設 `close_pending=True`，emit `["result","info",tr("analysis_tab"),tr("closing_wait")]`，回 False；draining 結束後自動進 `closing` 流程。
4. `idle`／`analysis`／`fault`：CAS → `closing`；取消全部 job；若沒有 server 且沒有 job，可直接設 `cleanup_done=True` 回 True（快速路徑，現況測試行為不變）；否則啟動背景清理並回 False。
5. 背景清理：等 job 歸零（上限至截止時間）→ 若歸零則 `server.release_resources()` → `server.shutdown(剩餘時間)` → `cleanup_done=True` → `window.destroy()`。逾時：`server.shutdown(1.0)` 後 `os._exit(2)`。
6. 所有退出路徑（`main()` 在 `webview.start` 返回後、`os._exit` 前）都要呼叫一次冪等的 `_shutdown_analysis()`（server shutdown＋release_resources，各自吞例外）。

**進入 `fault`**：取消所有 job、作廢所有授權、emit `["result","error",tr("fault_title"),tr("fault_body")]`。

### 1.5 poll 事件與前端對應

- 每次轉移（鎖內）依序 emit：`["state", {"state": <值>, "gen": <gen>, "seq": <seq>}]`，若 busy 值改變再 emit `["busy", <bool>]`。
- `seq`（`StatePayload.seq`）：程序內轉移計數，初始事件為 0，每個被接受的轉移在鎖內 +1；同一 `seq` 不重複。
- gate 建構時 emit 一次 `["state", {"state":"idle","gen":0,"seq":0}]`（前端第一次 poll 拿到初值）。
- `busy = state in BUSY_STATES`（`power/draining/closing/fault`）；兩事件衝突時以 `state` 為準。
- **順序防護**：pywebview 的 js_api 呼叫各在自己的執行緒，兩次 `poll()` 回傳可能交錯，`analysis_enter` 的回覆也可能早於排隊中的舊事件。所以前端一律以 `seq` 丟棄舊事件，iframe 是否清空另以 `gen` 判斷（§4.2）。

前端（T6，`web/app.js`）：
```js
App.state = "idle"; App.gen = 0; App.stateSeq = -1; App.busy = false; App.powerAllowed = true;
// on ["state", p]: if (p.seq <= App.stateSeq) return;   // 晚到的舊事件整筆丟棄（不 emit）
//   App.stateSeq=p.seq; App.state=p.state; App.gen=p.gen;
//   App.busy = ["power","draining","closing","fault"].includes(p.state);
//   App.powerAllowed = p.state === "idle"; busyButtons(); emit("state", p)
// on ["busy", b]: 只在尚未收過 state 事件時採用 b；之後一律以 state 推導（仍照常 emit("busy")）
```
| 狀態 | `App.busy` | `App.powerAllowed` |
|---|---|---|
| idle | false | true |
| power | true | false |
| analysis | false | false |
| draining / closing / fault | true | false |

所有電源啟動按鈕看 `!App.powerAllowed`：`#btn-off`、`[data-action='run']`（`app.js:64`）、`#btn-test`（`seq.js:116`）、`seq.js:154,170`、`list.js:56` 的 `runState`，`list.js:331` 改監聽 `state`。

### 1.6 `config()`

回傳 `{"resources", "demo", "lang", "analysis_enabled": contracts.ANALYSIS_ENABLED}`（鍵名 `CONFIG_KEY_ANALYSIS`）。不寫入 SEQ config 檔；`form.load/save_config` 不動。JS 只讀不定義。`tests/test_webgui.py:90` 由 T2 改為四鍵。

`ANALYSIS_ENABLED=False` 時：分頁不顯示、`analysis_enter` 回 `{ok:false, code:"disabled"}`、server 不啟動；仲裁器其餘行為照常。

---

## 2. js_api（T2 實作；T6 呼叫）

公開方法新增 `contracts.JS_API_METHODS` 四個（T2 同步改 `test_strings_and_public_surface` 白名單）。回傳皆 dict；`error` 是在地化文字，`code` 是 `DenyCode` 值；型別錯誤的參數也回 `{ok:false, code:"bad_purpose"|"stale"}`，不丟例外。

| 方法 | 回傳 | 規則 |
|---|---|---|
| `analysis_enter()` | `EnterResult {ok, gen, url}` 或 `{ok:false, error, code}` | 旗標關 → `disabled`；狀態 `analysis` → 冪等，回目前 `gen` 與 `url`；`idle` → server 未起則先起（失敗 → `server`，狀態不變），轉 `analysis`、`gen+=1`；其他狀態 → 對應 `DenyCode` |
| `analysis_leave(gen)` | `{ok:true, gen}` 或 `{ok:false, code:"stale"}` | 立即返回不等待；`analysis` 且 gen 符 → `draining`（作廢授權、取消 job、開始 30 s 計時）；已在 `draining` 同 gen → 冪等 ok；完成以 `state` 事件通知 |
| `analysis_open_path(gen, purpose)` | `PathResult` | `purpose ∈ OpenPurpose`；先驗 `analysis`+gen；開原生對話框（`OPEN_FILE_TYPES`）；取消 → `{ok:false, cancelled:true, gen}`；對話框返回後**在鎖內重驗**狀態與 gen，不符 → `stale` 且不發授權；成功 → `{ok:true, gen, grant, name}` |
| `analysis_save_path(gen, purpose, suggested)` | `PathResult` | `purpose ∈ SavePurpose`；`suggested` 為字串、≤`MAX_SUGGESTED_NAME`、不含 `/ \ :`，否則 `bad_purpose`；對話框帶 `save_filename=suggested`、`SAVE_FILE_TYPES`；副檔名不是 `SAVE_EXT[purpose]`（不分大小寫）就補上；其餘同上 |

`url` 形如 `http://127.0.0.1:<port>/viewer.html#t=<token>&gen=<gen>&lang=<zh|en>`（`ServerHandle.url(gen, lang)` 產生）。token 放 fragment，不會送進 HTTP 請求列。

### 2.1 路徑授權（gate 內）

- `issue_grant(gen, mode, purpose, path) -> Grant`（T2 內部）：`id=secrets.token_urlsafe(16)`、`expires=monotonic()+GRANT_TTL_S`（300 s）；每世代最多 `MAX_GRANTS_PER_GEN`=16，超過丟最舊。
- `consume_grant(grant, gen, mode, purpose) -> path | None`：**先移除再判斷**（單次消耗，失敗也作廢）；需同時符合 gen、mode、purpose、未過期、目前狀態 `analysis` 且 gen 為目前世代。
- `leave`、`fault`、`closing` 作廢全部授權。
- HTTP 端用途對應＝`contracts.ROUTE_GRANT`：`/open`=open/dlog、`/import/markers`=open/markers、`/save`=save/〈`X-Analysis-Purpose` 標頭值〉、`/export/csv`=save/csv。server 以 `consume_grant(grant, gen, mode, purpose)` 呼叫，mode/purpose 一律取自此表（`/save` 取自標頭），不從 grant 推。不符 → 403 `grant`。
- `/save` 的 `X-Analysis-Purpose`（`HEADER_PURPOSE`）必填，值 ∈ `UPLOAD_PURPOSES`（`png`、`html`、`markers`）；缺或其他值（含 `csv`）→ 400 `bad_request`，在讀 body 與 consume 之前判斷（不消耗 grant）。iframe 送的值＝當初 `save_request` 的 `purpose`。
- 前端、HTTP 永遠拿不到完整路徑（只有 `name`=basename）。

---

## 3. 分析 HTTP server（T4 實作於 `analysis/server.py`）

### 3.1 啟動

```python
def start_server(gate: GateForServer, *, cache_root: Path | None = None,
                 prefs_path: Path | None = None, dev: bool = False) -> ServerHandle
```
- `ThreadingHTTPServer((HOST, 0))`、`daemon_threads=True`、`protocol_version="HTTP/1.1"`，serve 於 daemon 執行緒。
- `token = secrets.token_urlsafe(32)`，每次啟動新產生；`origin = f"http://127.0.0.1:{port}"`。
- `cache_root`/`prefs_path` 缺省用 `storage.default_cache_root()`／`storage.default_prefs_path()`；啟動時 `storage.sweep_tmp(cache_root)`。
- `ServerHandle`：`origin`、`token`、`url(gen, lang)`、`release_resources() -> bool`、`self_check() -> bool`（真 HTTP `GET /health`）、`shutdown(timeout)`。
- webgui 接線（T2）：`gate.set_release_resources(server.release_resources)`；`--selftest` 時若旗標開，呼叫 `server.self_check()` 併入結果（T2 用假 server 測，真 server 整合驗收在 T6）。

### 3.2 檢查順序（每個請求）

1. `Host` ≠ `f"127.0.0.1:{port}"` → 403 `host`。
2. 路徑不在 `ROUTES` 也不在靜態白名單 → 404 `not_found`；路徑在但方法不符 → 405 `method`。
3. 靜態檔（`STATIC_FILES`，`dev=True` 再加 `DEV_STATIC_FILES`）：只接受 GET，從 `analysis/web/` 送出；不驗 token、不問仲裁器。`/health` 同樣只驗 Host。
4. `X-Analysis-Token` 以 `hmac.compare_digest` 比對 → 403 `token`。
5. `X-Analysis-Gen` 缺或非整數 → 400 `bad_request`；`X-Analysis-Req`（選填）不符 `REQ_ID_PATTERN` → 400。
6. 依 `ROUTES` 的 `RouteKind`：
   - `JOB`：`job = gate.job_begin(gen, req)`；`None` 時：`gate.state()[0] != analysis` → 423 `locked`，否則 409 `stale_gen`。進行中 JOB 超過 `MAX_CONCURRENT_JOBS`=4 → 503 `overloaded`（立刻 `job_end`）。
   - `CONTROL`（`/cancel`、`/release`）：**不呼叫 `job_begin`、不計入上限、永不回 503**；以 `gate.state()` 驗 `(analysis, gen)`，不符同樣 423／409。處理只做 `gate.cancel_req` 或 `core.release_handle`，不得做 I/O 或等待 job。保留容量的依據：JOB 上限 4 < 瀏覽器每 host 連線上限 6（Chromium／WebView2／WKWebView），所以 4 個長工作占滿時 `/cancel` 仍有連線可用；`ThreadingHTTPServer` 每連線一條執行緒，伺服端無執行緒上限。
7. 參數檢查 → 400 / 413。
8. 執行；JOB 路由 `finally: gate.job_end(job)`。例外對應：`AnalysisCancelled`→409 `cancelled`；`LimitExceeded`→413；`DlogFormatError`→422 `format`；未知 handle (`KeyError`)→404 `handle`；`SourceChanged`→409 `source_changed`；`CacheWriteError`→507 `storage`；其他→500 `internal`（記 log，不回 traceback）。

所有錯誤回 JSON `ErrorBody {error, code}`，狀態碼依 `HTTP_STATUS`。所有回應加 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`；靜態檔另加 `Content-Security-Policy: frame-ancestors http://127.0.0.1:* http://localhost:*`。不送任何 CORS 標頭。

### 3.3 路由

二進位格式沿用舊版 `frame_binary`：`[uint32 LE meta 長度][JSON meta，空白補到 4 倍數][float32 LE payload]`。meta 結構＝`contracts.OpenMeta`／`SamplesMeta`／`BucketsMeta`（欄位、型別、單位、預設值見該處；`offset` 是 payload 內**位元組**位移，`length` 是 float32 **個數**）。單位：t 秒、V 伏、I 安、P 瓦（P 以 float64 計算後轉 float32 送出）。

參數：`h`／`hc` 須符 `HANDLE_PATTERN`（40 位小寫 hex），否則 400 `bad_request`（不碰 core）；格式正確但未知 → 404 `handle`（§3.2）。選填的 `hc`（`/export/csv`）**缺省時不驗格式**（視為沒有比對檔）；有給才驗。**例外 `/release`**：`h` 格式不符仍 400，但格式正確而未知（或已釋放）→ **200 `{ok:true}`**，不回 404——釋放是冪等清理，iframe 重載或重送時不應報錯。`keys` 格式 `V1,I1,P3`（每項符 `KEY_PATTERN`，≤`MAX_KEYS`=12，空或任一項不符 → 400）；`lo`、`hi` 必填整數，夾到 `[0, nRecords-1]`；`offset` 值 ∈ `QS_TRUE`（`1`/`true`/`True`）為真，缺或其他值為假；`maxLimit` 浮點，單位 A，預設 `DEFAULT_MAX_LIMIT`=0.0（≤0 時負電流換成 0）；`n` 整數預設 `DEFAULT_BUCKETS`=1800。`/stats` 回 `{key: StatsValue}`。

| 方法 路徑 | 輸入 | 成功回應 | 特有錯誤 |
|---|---|---|---|
| GET `/health` | — | `{ok:true, version:1}` | — |
| POST `/open` | JSON `{grant}` | 二進位：meta（含 `handle`、`fileName`=basename、`lodFactors`、`lodLayout`…）＋ LOD blob | 403 grant、422、409 source_changed、507 |
| POST `/release` | JSON `{h}`（CONTROL） | `{ok:true}`（未知 handle 也 ok，§3.3 參數例外） | `h` 不符 `HANDLE_PATTERN` → 400 |
| GET `/samples` | `h, lo, hi, keys?, offset?, maxLimit?`（keys 缺省＝所有啟用聲道 V、I，依 `activeChannels` 升冪、每聲道 V 再 I） | 二進位：`SamplesMeta` | `contracts.use_buckets(hi-lo+1, len(keys))` 為真 → 413（與 Viewer 同一算式，§7.1），在讀資料前判斷 |
| GET `/buckets` | `h, keys, lo, hi, n?(預設 1800，1..8192), offset?, maxLimit?` | 二進位：meta `{layout}`，每 key 連續 t、min、max 三段 | n 越界 → 400 |
| GET `/stats` | `h, keys, lo, hi, offset?, maxLimit?` | JSON `{"V1":{min,max,avg}, …}`（無有效值為 null） | — |
| POST `/export/csv` | JSON `ExportCsvBody` `{grant, h, hc?, lo, hi, cols:"p:V1,c:I2", offset?=false, maxLimit?=0.0}`，建議帶 `X-Analysis-Req` | 寫檔完成後 `{ok:true, bytes, name}` | 403 grant、409 cancelled（已刪 `.part`） |
| POST `/save` | 原始 body，標頭 `X-Analysis-Grant`、`X-Analysis-Purpose`（png/html/markers）、`Content-Length` 必填 | `{ok:true, bytes, name}` | Purpose 缺或不在 `UPLOAD_PURPOSES` → 400；`Content-Length` > `MAX_UPLOAD_BYTES`(64 MiB) → 413（皆在讀 body 前判斷）；grant 用途不符（含 csv 授權）→ 403 |
| POST `/import/markers` | JSON `{grant}` | `{ok:true, data:<解析後 JSON>}` | 檔 > 4 MiB → 413；非 UTF-8 JSON → 422 |
| GET `/prefs` | — | prefs 物件（缺檔或損壞回 `PREFS_DEFAULT`） | — |
| POST `/prefs` | JSON 完整 prefs（≤64 KiB） | `{ok:true}` | 結構不符 → 400 |
| POST `/cancel` | JSON `{req}`（CONTROL，不受 job 上限） | `{ok: <是否找到>}` | — |

- JSON body 上限 `MAX_JSON_BODY_BYTES`=64 KiB，超過 413。
- CSV 欄位標題、格式、行尾、聲道缺欄留空規則與舊 `dlog_server._handle_export_csv` 完全相同（常數 `CSV_TITLE_LINE`、`CSV_LABEL_FMT`、`CSV_KIND_WORD`；欄位物件 `CsvColumn`：`missing` 預設 False，該來源 `meta.fields` 沒記錄此欄（P 需 V、I 都有）即為 True、整欄留空）；`cols` 有 `c:` 但缺 `hc` → 400；`name` 參數取消（檔名由對話框決定）。
- 寫檔（`/save`、`/export/csv`、`/prefs`）一律 `storage.atomic_write_*`（§6.3）：**同目標互斥＋唯一暫存檔**——持有該目標（`Path.resolve()` 後）的程序內鎖期間，寫 `<目標>.<secrets.token_hex(4)>.part`（`PART_SUFFIX`）→ `os.replace`；失敗或取消只刪自己的暫存檔，原檔不動。同目標的併發請求依序執行、後者覆蓋前者（`/prefs` 為 last-writer-wins）。CSV 為串流寫入，不受 `MAX_RESPONSE_BYTES` 限制。
- server 為每次 `/open` 持有一次 `core.acquire_handle`，`/release` 放掉；每個請求處理期間另外 acquire/release 一次（pin）。`release_resources()`：放掉所有持有，再 `core.close_all()`，回其結果。
- 移除舊的 `SimpleHTTPRequestHandler(directory=...)`；非白名單一律 404。

---

## 4. postMessage（T5 iframe 端、T6 外殼端）

### 4.1 iframe 載入

外殼：`iframe.src = r.url + "&parent=" + encodeURIComponent(location.origin)`；`analysisOrigin = new URL(r.url).origin`；`currentGen = r.gen`。

Viewer：讀 fragment 的 `t`、`gen`、`lang`、`parent`，讀完 `history.replaceState` 清掉 fragment。`parent` 不符 `PARENT_ORIGIN_PATTERN` → 顯示錯誤、停用開檔與存檔。所有 API 請求帶 `X-Analysis-Token`、`X-Analysis-Gen`。

### 4.2 訊息

全部是物件且帶 `v: 1`、`type`、`gen`。**發送一律用精確 targetOrigin**：iframe→外殼用 `parent`；外殼→iframe 用 `analysisOrigin`。**禁止 `"*"`**。

接收端驗證分兩層（任一不符就靜默丟棄）。

共通層：
- 外殼：`event.source === iframe.contentWindow && event.origin === analysisOrigin && data` 是物件 `&& data.v === 1 && data.gen === currentGen && data.type ∈ MSG_TO_SHELL`。
- iframe：`event.source === window.parent && event.origin === parent && data` 是物件 `&& data.v === 1 && data.gen === gen && data.type ∈ MSG_TO_IFRAME`。

類型層（`contracts.MSG_FIELDS` 列每型必填欄位，除 v/type/gen 外）：
| type | 收方 | 驗證 |
|---|---|---|
| `ready` | 外殼 | 無其他必填；**不驗 `id`** |
| `open_request` | 外殼 | `id` 字串符 `REQ_ID_PATTERN`；`purpose ∈ OpenPurpose` |
| `save_request` | 外殼 | `id` 同上；`purpose ∈ SavePurpose`；`suggested` 為字串（長度與字元由 `analysis_save_path` 再驗） |
| `open_result`／`save_result` | iframe | `id` 在自己待回覆清單中，**且**該 id 當初送出的是對應的 `open_request`／`save_request`；`ok` 為布林；處理後自清單移除（同 id 第二次回覆丟棄） |
| `lang` | iframe | `lang ∈ LANGS`（`"zh"`、`"en"`）；**不驗 `id`** |

只有 `*_result` 以 `id` 配對；`ready`、`lang` 沒有 `id`。

| type | 方向 | 欄位 | 處理 |
|---|---|---|---|
| `ready` | iframe→外殼 | — | 外殼可在此後送 `lang` |
| `open_request` | iframe→外殼 | `id, purpose`(OpenPurpose) | 外殼呼叫 `analysis_open_path(gen, purpose)`，以 `open_result` 回覆 |
| `save_request` | iframe→外殼 | `id, purpose`(SavePurpose), `suggested` | 外殼呼叫 `analysis_save_path(gen, purpose, suggested)`，以 `save_result` 回覆 |
| `open_result` / `save_result` | 外殼→iframe | `id, ok, error?, code?, cancelled?, grant?, name?` | 欄位即 `PathResult`；iframe 依 `id` 對回 Promise |
| `lang` | 外殼→iframe | `lang`("zh"/"en") | Viewer 即時切語言；Viewer 自己的語言切換 UI 隱藏 |

- `id`：iframe 每次載入內唯一（例 `"r"+遞增數`），≤64 字元。不設逾時（對話框可能開很久）；iframe 重載即全部作廢。
- 拿到 `grant` 後，iframe 再呼叫對應 HTTP 路由（§3.3）。
- 外殼：使用者切離分析頁 → 先 `iframe.src="about:blank"` 再 `analysis_leave(currentGen)`，`currentGen=null`；之後收到的舊訊息因 gen/source 不符被丟。
- **iframe 由 state 事件清空的條件**（世代＋順序防護）：事件先通過 §1.5 的 `seq` 過濾，且 `currentGen !== null && p.gen === currentGen && p.state !== "analysis"`（例如同世代的 `fault`、`closing`）。`p.gen !== currentGen` 的事件（初始 `idle` gen 0、上一世代的 `draining`／`idle`）**一律不動 iframe**。依據：`gen` 只在 `idle→analysis` +1，所以帶目前 gen 的非 analysis 事件必定發生在本次 enter 之後。
- `analysis_enter` 被拒 → `App.showResult("warning", t("analysis_tab"), r.error)`，退回原分頁。

---

## 5. 取消語意

- **來源**：`leave`（該世代全部 job）、`closing`／`fault`（全部 job）、`POST /cancel {req}`（單一 job）。都只是設 `JobToken.cancel`。
- **檢查點**：core 每處理 ≤`CORE_CHUNK_RECORDS`（1,000,000 筆）、雜湊每 `HASH_CHUNK_BYTES`（8 MiB）、CSV 每塊（`CSV_ROWS_PER_CHUNK`=20,000 列）、快取寫入每塊都檢查一次，觸發即丟 `AnalysisCancelled`。從設旗標到丟例外 ≤ `CANCEL_LATENCY_S`（2 s，10M×4ch 開發機）。
- **副作用**：快取建置被取消 → 刪 tmp 目錄、不發布；存檔／CSV 被取消 → 刪 `.part`、目標檔不動。
- **回應**：尚未送出標頭 → 409 `cancelled`；若連線已斷，忽略寫入錯誤。
- **卡住**：取消後仍不結束的工作不被強制終止；`draining` 只會逾時轉 `fault`，不釋鎖；memmap 留給進程退出回收。
- 電源工作 v1 沒有取消。

---

## 6. 核心 API（`analysis/core.py`）與 storage（`analysis/storage.py`）

### 6.1 T1 初版（legacy，只搬不改邏輯）

`core.py` = `來源_DLOG_Viewer/server/dlog_core.py` 原樣，只調 import。公開：`DlogFormatError, LOD_F0=64, LOD_F1=4096, parse_dlog(buf: bytes), build_lod(V, I, n, active_channels), open_dlog(data: bytes, cache_root: Path) -> (meta, lod_blob), get_handle(digest, cache_root), offset_uniform, apply_current_offset, get_values, compute_buckets, compute_stats, ctime_like, iter_csv`（簽名同來源）。

呼叫 core 的兩種測試，規則不同：
- `tests/analysis/test_core_legacy.py`：**直接呼叫 core**（`core.open_dlog(bytes, cache_root)`、`core.get_handle` 等），不經 adapter。
- parity 工具（`tests/analysis/parity_tools/**`，含 `test_parity.py`、`dump_py.py`）：**只經** `tests/analysis/parity_tools/adapter.py` 取得 entry：
```python
def open_entry(dlog_path: Path, cache_root: Path) -> tuple[dict, dict]  # (meta, entry)
```

T3 把 `open_dlog` 改成路徑 API（§6.2）時，必須在同一變更內同步改 `test_core_legacy.py`（所有 `core.open_dlog(x.read_bytes(), …)` 改傳路徑；`get_handle` 改名時一併）與 `adapter.py`；parity 工具其餘檔不需改。兩檔都在 T3 可改清單內。

### 6.2 T3 最終版

以 `contracts.CoreApi` 為準：
```python
open_dlog(path, cache_root, *, cancel=None, progress=None) -> tuple[dict, bytes]
acquire_handle(digest, cache_root) -> Entry      # refcount +1，未知 → KeyError
release_handle(entry) -> None
close_all() -> bool                              # 關閉所有零引用映射；仍有引用回 False
get_values(entry, kind, ch, lo, hi, offset, max_limit, *, cancel=None) -> np.ndarray
compute_buckets(entry, kind, ch, idx_lo, idx_hi, n_buckets, offset, max_limit, *, cancel=None) -> (t, mn, mx)
compute_stats(entry, kind, ch, idx_lo, idx_hi, offset, max_limit, *, cancel=None) -> {min,max,avg}
iter_csv(primary, columns, idx_lo, idx_hi, offset, max_limit, now, *, cancel=None) -> Iterator[bytes]
```
`get_handle` 保留為 `acquire_handle` 的相容別名直到 T4 改完（T3 決定何時移除，並同步 adapter）。`progress(stage, pct)`：stage ∈ `"hash"|"parse"|"lod"`，pct 0–100。meta 鍵與舊版相同，另加 `fileName`、`cacheVersion`(=`CACHE_VERSION`=1)。

資料形狀（以 `來源_DLOG_Viewer/server/dlog_core.py` 實碼為準，寫在 `contracts.py`）：
- `DlogMeta`（`meta.json`）：`tint` float 秒、`declaredTime` float 秒或 None、`nRecords` int（含 pre-trigger trailer）、`channelModels` 長度 4（索引 ch-1；無 `<model>` 時啟用聲道為 `"CH<n>"`、未啟用為 None）、`activeChannels` 升冪 int、`fields` `[[ch, "V"|"I"], ...]`（檔案順序；JSON 陣列）、`minmax` bool、`cacheVersion` int（T3 起）。
- `OpenMeta`＝`DlogMeta`＋`handle`（sha1 hex 40 字）、`lodFactors` `{"level0":64,"level1":4096}`、`lodLayout: list[LodLayoutItem]`、`fileName`（basename）。LOD blob 順序：`activeChannels` 升冪 × V、I、P × level 0、1 × min、max，float32 LE；level0 長度 `max(1, ceil(n/64))`、level1 `max(1, ceil(len0/64))`。
- `Entry`：`meta: DlogMeta`、`dir: Path`、`V`／`I: {ch: float32 memmap[nRecords]}`（未記錄欄位為 0）、`lod`（鍵 `f"{key}_l{0|1}{min|max}"`）、`digest`（T3 起必有）。T3 可加 `_` 開頭的私有鍵，其他模組不得讀。
- `CsvColumn`：`entry`、`kind ∈ V/I/P`、`ch` 1–4、`label`（`CSV_LABEL_FMT`，p→`File1`、c→`File2`）、`missing`（預設 False）。數值行為（解析、LOD、校正、分桶三條路徑、CSV 格式）**不得改變**（§8 parity）。

要求：
- 來源檔以 `np.memmap(path, mode="r")` 讀；sha1 以 `HASH_CHUNK_BYTES` 分塊；雜湊前與解析後比對 `(st_size, st_mtime_ns)`，變了丟 `SourceChanged`。
- V/I 以 `np.lib.format.open_memmap` 寫入 `<root>/<sha1>.tmp-<pid>-<rand>/`，LOD 分塊計算；完成後關閉所有映射再 `os.replace` 到 `<root>/<sha1>/`；目標已存在（他人先建好）則丟棄 tmp。
- 同 digest 併發建置：程序內 per-digest 鎖；後到者等待後直接命中。
- 引用計數／pin：被引用的 digest 不得被 LRU 淘汰；Windows 上淘汰／取代前必須先關映射。
- 快取上限 `CACHE_MAX_BYTES`（5 GiB）LRU（以 `meta.json` mtime 當最近使用）；`cacheVersion` 不符或檔案缺損 → 刪掉重建。
- 上限：`get_values` 範圍 > `CORE_MAX_VALUES_SPAN`（4,000,000）丟 `LimitExceeded`；`compute_buckets` 的 `n_buckets` > `MAX_BUCKETS` 丟 `LimitExceeded`；`compute_stats` 與校正開啟的逐筆分桶路徑以 `CORE_CHUNK_RECORDS` 分塊，不一次 materialize 整段；`iter_csv` 比對檔取樣率不同時以索引直接取值，不取 `lo2..hi2` 整段。

### 6.3 storage（T3）

```python
default_cache_root() -> Path   # Win %LOCALAPPDATA%\N6705C-SEQ\analysis-cache；mac ~/Library/Caches/N6705C-SEQ/analysis-cache；其他 $XDG_CACHE_HOME 或 ~/.cache/N6705C-SEQ/analysis-cache
default_prefs_path() -> Path   # Win %APPDATA%\N6705C-SEQ\analysis_prefs.json；mac ~/Library/Application Support/N6705C-SEQ/analysis_prefs.json；其他 ~/.config/N6705C-SEQ/analysis_prefs.json
ensure_dir(path) -> Path       # 建立並測試可寫，否則 CacheWriteError（訊息含路徑）
sweep_tmp(cache_root) -> None  # 刪殘留 *.tmp-* 目錄與 *.part
evict_lru(cache_root, max_bytes, pinned: set[str]) -> None
atomic_write_bytes(path, data) -> int
atomic_write_iter(path, chunks: Iterable[bytes], cancel=None) -> int
```
`atomic_write_*`：取 `path.resolve()` 的程序內鎖（`dict[str, threading.Lock]`，以 storage 模組鎖保護建立）→ 在同目錄寫 `f"{path}.{secrets.token_hex(4)}{PART_SUFFIX}"` → `fsync` → `os.replace(tmp, path)` → 放鎖；例外或取消（`AnalysisCancelled`）時刪自己的 tmp 再往上丟。回傳寫入位元組數。`sweep_tmp` 只掃 `cache_root`（使用者目錄的殘留 `.part` 不掃）。
常數：`APP_DIR_NAME`、`CACHE_SUBDIR`、`PREFS_FILENAME`（contracts.py）。

### 6.4 偏好 `analysis_prefs.json`

`{"version": 1, "theme": "dark"|"soft-dark"|"light", "fold": {"<details id>": bool}}`；預設 `PREFS_DEFAULT`。版本不符或損壞 → 用預設、不報錯。語言**不**存（跟 SEQ）。不碰 SEQ 的 `~/.n6705c_seq_gui.json`。

---

## 7. 容量、效能、上限

### 7.1 回應量上限（HTTP 層 T4、核心層 T3）

見 `contracts.py` limits 區：`DEFAULT_BUCKETS 1800`、`MAX_BUCKETS 8192`、`MAX_KEYS 12`、`MAX_SAMPLES_SPAN 2,000,000`、`MAX_RESPONSE_BYTES 64 MiB`、`MAX_JSON_BODY_BYTES 64 KiB`、`MAX_UPLOAD_BYTES 64 MiB`、`MAX_MARKERS_FILE_BYTES 4 MiB`、`MAX_PREFS_BYTES 64 KiB`、`MAX_CONCURRENT_JOBS 4`（只限 JOB 路由）、`SAMPLE_BYTES 4`、`META_RESERVE_BYTES 64 KiB`、`CORE_CHUNK_RECORDS 1,000,000`、`CORE_MAX_VALUES_SPAN 4,000,000`、`CSV_ROWS_PER_CHUNK 20,000`。

`/samples` 回應大小算式（`contracts.samples_response_bytes`）：`META_RESERVE_BYTES`(64 KiB) ＋ `span × n_keys × SAMPLE_BYTES`(4)，`span = hi-lo+1`。

切換規則（`contracts.use_buckets`，Viewer 與 server 共用同一函式的邏輯）：`span > MAX_SAMPLES_SPAN` **或** `samples_response_bytes(span, n_keys) > MAX_RESPONSE_BYTES` → 用 `/buckets`。例：12 keys 時上限 span = (64 MiB − 64 KiB) / 48 = **1,396,736**；2,000,000 × 12 = 96,065,536 B > 67,108,864 B → buckets；預設 8 keys（4ch × V、I）× 2,000,000 = 64,065,536 B → 仍可 `/samples`。Viewer 不得拆成多次 `/samples` 硬拉。`/buckets` 回應最大 8192 × 3 × 12 × 4 B ≈ 1.2 MB，不需切換。

### 7.2 效能預算〔10M／30M 已定案；其餘預設，待使用者確認〕

| 常數 | 值 | 量法 |
|---|---|---|
| `TARGET_MAX_RECORDS` | 10,000,000 筆 × 4ch | 以下全部預算適用 |
| `NO_OOM_RECORDS` | 30,000,000 筆 × 4ch | 冷開檔＋全範圍 stats／buckets 完成、不 OOM |
| `MEM_BUDGET_PEAK_RSS_MB` | **1536 MiB** | **主判定**：子進程峰值 RSS：10M 冷開檔；10M 全範圍 CSV |
| `NO_OOM_PEAK_RSS_MB` | **4096 MiB** | **主判定**：子進程峰值 RSS：30M 冷開檔、全範圍 stats、全範圍 buckets（＝`MIN_RAM_GB`/2，與「30M 不 OOM」一致） |
| `MEM_BUDGET_PEAK_HEAP_MB` | 256 MiB | 輔助判定：子進程 `tracemalloc` 峰值（numpy 配置有計入）：10M、30M 冷開檔；10M 全範圍 CSV |
| `OPEN_COLD_MAX_S` | 60 s | 10M×4ch 快取未命中 |
| `OPEN_WARM_MAX_S` | 3 s | 10M×4ch 快取命中 |
| `GIL_STALL_MAX_MS` | 500 ms | 冷開 10M 時，另一執行緒每 20 ms `sleep` 一次，量到的最大間隔 |
| `CANCEL_LATENCY_S` | 2 s | §5 |
| `CACHE_MAX_BYTES` | 5 GiB | LRU |
| `MIN_RAM_GB` | 8 GB | 驗收機最低 RAM |
| 「清除快取」按鈕 | v1 不做 | |

**規格決定（對照報告 `升級報告.md` §7 T3 驗收「合成 10M×4ch 峰值 RSS 在 T0 定的預算內」）**：記憶體驗收採「RSS 與 tracemalloc 並用」，兩者都要過。RSS 是報告指定的主項，保留；tracemalloc 為輔，用來抓 RSS 看不出的整段 materialize（RSS 含 memmap 頁，heap 暴增可能被頁面回收掩蓋）。

RSS 預算算式：`2 × 原始 V/I 位元組（來源 memmap＋快取 memmap 頁）＋ heap 預算 256 MiB ＋ 直譯器／numpy 基底 256 MiB`，向上取整。原始 V/I＝`筆數 × 4ch × 2 × 4 B`：10M → 305 MiB → 2×305+256+256＝1122 → **1536**；30M → 916 MiB → 2×916+256+256＝2344 → **4096**（8 GB 驗收機的一半）。

量法：每個步驟一個新 `python -I` 子進程，步驟結束時同進程回報兩值。RSS：POSIX `resource.getrusage(RUSAGE_SELF).ru_maxrss`（macOS 單位 bytes、Linux KiB，換算成 MiB）；Windows 以 `ctypes` 呼叫 `GetProcessMemoryInfo` 取 `PeakWorkingSetSize`。不新增相依套件。大檔測試以環境變數啟用：`N6705C_ANALYSIS_BIG=1` 跑 10M；`N6705C_ANALYSIS_BIG=30` 另跑 30M；未設則 skip。合成檔寫到 `tmp_path`，不進 repo。

### 7.3 轉 worker 子進程的門檻（第一版同進程）

任一成立 → 停工回報，重新規劃 `spawn` worker（不自動切換）：
1. 30M×4ch 任一驗收步驟 `MemoryError`／被系統殺掉；或峰值 RSS > `NO_OOM_PEAK_RSS_MB`（4096 MiB）；或 `tracemalloc` 峰值 > `WORKER_SWITCH_HEAP_MB`（1024 MiB）。
2. 10M 冷開檔 `GIL_STALL_MAX_MS` 超標，且 T3 以縮小分塊仍壓不下來。
3. T8 審查認定分析程式碼有可重現的當機路徑會連帶殺掉 GUI。

計算函式寫成「純函式＋路徑參數＋cancel 旗標」形狀，搬 worker 時不改簽名。

---

## 8. Parity（T1 建立、T3 每次改 core 後重跑）

| 項目 | 案例 | 容差 |
|---|---|---|
| 解析＋LOD | 預設樣本 `default_20260803_092740.dlog`（多聲道）；pre-trigger trailer 合成檔（`make_big(..., channels=2, trailer=True)`）；單聲道合成檔（`make_big(..., channels=1)`） | float32 **逐位元組相同**（`np.array_equal(..., equal_nan=True)`） |
| 分桶（校正關） | `cases_default.json` 全部；`cases_74445.json`（需環境變數 `N6705C_PARITY_74445=<1GB 檔路徑>`，否則 skip） | t/min/max 轉 float32 後逐位元組相同 |
| 分桶（校正開） | 上述檔中 `offset:true` 的案例 | 只驗結構：長度相同、t 逐位元組相同、所有值有限；I 的 min/max 全 ≥ 0。**不宣稱與原版相同**（原版用 `Math.random()`） |
| stats | 預設樣本、每個啟用聲道 V/I/P 全範圍與 `[5000,25000]` | 對 numpy float64 參考：min/max 完全相同，avg 相對誤差 ≤ `STATS_AVG_RTOL`(1e-9) |
| CSV 格式 | `fmt_check.js` 對預設樣本 | **不要求逐位元組相同**（使用者決定放寬）。非數字文字（標題、標籤、空格欄、逗號、CRLF）仍逐字元相同；每個數字欄：小數位數相同、縮放後整數差 ≤1，即最後一位小數容許差 `CSV_LAST_DIGIT_TOL`(1)——以 `decimal.Decimal` 換成最後一位的整數比較，`abs(a-b) ≤ 1`。原因：JS `toFixed` 對二進位剛好落在一半的值進位，Python `%.8f`/`%.9f` 捨入到偶數。 |
| 原版 | `original_before_python.html` | sha1 = `ORIGINAL_HTML_SHA1`（`0117e08d…1aff`），任何時候不得改 |

- JS 端一律從 `original_before_python.html` 抽（`extract_original_js.py`），不從新版 Viewer。
- `compare.py`、`compare_buckets.py` 失敗時退出碼非 0；從 repo 根目錄執行：`python tests/analysis/parity_tools/compare.py ...`。
- pytest 包裝 `tests/analysis/parity_tools/test_parity.py`：有 `node` 才跑，否則 skip（波次結束時由該波 core 擁有者手動跑並貼輸出）。
- `test_parity.py::test_csv_number_format_matches_js` 現為逐行字串相等；改成上表容差規則由 **T3** 做（已列入 T3 可改），T3 前此測試可能因對半值失敗，不視為 core 回歸。
- **不搬**（留在 `報告分析升級版/來源_DLOG_Viewer/server/parity_tools/`，凍結）：`dlog_poc.py`、`make_synth.py`（trailer／單聲道案例已由 `fixtures/make_big.py` 取代）、`bench_local.js`、`make_probe.py`、該目錄 `README.md`。T7 在 repo `README.md` 補 parity 指令用法（§11 T7）。

---

## 9. i18n key

`contracts.I18N_KEYS` 共 12 個（`analysis_tab`、`analysis_denied_power`、`power_denied_analysis`、`analysis_denied_draining`、`analysis_denied_closing`、`analysis_disabled`、`analysis_stale`、`analysis_server_failed`、`analysis_crash_note`、`fault_title`、`fault_body`、`closing_wait`），文字照錄。T2 一次加進 `i18n.STRINGS`（文字相同），並加測試：`I18N_KEYS` 每個鍵都在 `STRINGS` 且文字相等。沿用既有鍵：`busy_title`、`busy_body`、`run_result`。

`analysis_crash_note` 由 T6 顯示在分析分頁頂端（同進程當掉時輸出可能維持開啟的說明）。Viewer 內部文字用 Viewer 自己的字典（`analysis/web/viewer.html`），不進 `STRINGS`。

---

## 10. 模組與 import 邊界

- `analysis/*` 只准 import stdlib、numpy、`n6705c_seq.analysis.*`。禁止：`n6705c`（含 `n6705c.core.dlog`，名稱相近勿混）、`pyvisa`、`usb`、`webview`、`n6705c_seq.webgui`、`web_seq`、`web_list`、`execute`、`e36441a`、`form`、`i18n`。
- `webgui.py` 只准 import `analysis.server`、`analysis.gate`、`analysis.contracts`；對話框、語言以 callback 注入 gate／server。
- `analysis.contracts` 只 import stdlib（不含 numpy），可被任何模組 import。
- 測試（T1 建 `tests/analysis/test_boundary.py`）：AST 掃 `analysis/*.py` 的 import；`python -I` 子進程 import `n6705c_seq.analysis.core`（之後各模組存在時一併）後斷言 `pyvisa`、`webview` 不在 `sys.modules`。

---

## 11. 任務界線（每個任務只靠本文件＋contracts.py 動工）

共同規則：只改自己欄的檔；其他檔一律不動（凍結：`web/layout.js`、`docs/` 既有檔、`src/n6705c/**`、`HANDOFF_WINDOWS.md`（T7 完成後例外）、`報告分析升級版/**`）。每波結束跑既有 `pytest` 與 `node --test tests/js` 全綠；誰的測試檔壞由該檔擁有者修。`analysis/contracts.py`、`analysis/__init__.py`、本文件只有 T0 能改。`tests/analysis/` 下的檔名不得與 `tests/` 既有檔名相同。

### T1（Haiku）搬檔＋parity
- 可改：`src/n6705c_seq/analysis/core.py`（新建）、`src/n6705c_seq/analysis/web/viewer.html`（新建，原樣複製 `dlog_viewer_mem3_preview.html`）、`tests/analysis/parity_tools/**`（含 `adapter.py`、`dump_py.py`、`test_parity.py`、原版 HTML）、`tests/analysis/fixtures/**`（含預設樣本複本、`make_big.py`）、`tests/analysis/conftest.py`、`tests/analysis/test_core_legacy.py`、`tests/analysis/test_boundary.py`。
- 依據：§6.1、§8、§10。
- `make_big.py`：`make_big(path: Path, n_records: int, channels: int = 4, trailer: bool = False) -> Path`，串流寫入、不在記憶體組整檔，header 格式同 `make_synth.py`。
- `test_core_legacy.py`：把原 `test_dlog_server.py` 9 項檢查改為直接呼叫 core；1GB `BIG_FILE` 改用 `make_big` 或 skip；不得起 HTTP server。
- 驗收：§8 全部通過；sha1 不變；pytest 全綠。

### T2（Codex）仲裁器
- 可改：`src/n6705c_seq/analysis/gate.py`（新建）、`webgui.py`、`web_seq.py`、`web_list.py`、`i18n.py`、`tests/test_activity*.py`（新建）、`tests/test_webgui.py`、`tests/test_web_seq.py`、`tests/test_web_list.py`。
- 依據：§1、§2、§3.1 接線、§9、§10。gate 對 server 的面以 `contracts.GateForServer` 為準，另加內部 `issue_grant`、`set_release_resources`、`enter`、`leave`、`on_closing` 等（名稱自訂）。
- 對 `analysis.server` 只透過 `contracts.StartServer`/`ServerHandle`；測試用假 server。`analysis.server` 不存在時（T4 前）以 `importlib.util.find_spec` 判斷，`analysis_enter` 回 `code:"server"`。
- 驗收：雙向競態皆被拒；舊 token／舊世代不能解鎖；`TRANSITIONS` 每條（含 `draining` 中關窗、`draining` 逾時接 `fault→closing`、遲到收尾不蓋 `closing`、power 收尾逾時→`fault`）有測試（計時常數以 monkeypatch 縮短）；`_test` 單 frame 卡住、`_off_work` 卡住都在收尾逾時後進 `fault`；`_finishing` 重複呼叫會重新起算；state 事件 `seq` 嚴格遞增、初始為 0；確認對話框開著時 `analysis_enter` 被拒；授權單次、綁世代、綁用途；既有測試全綠。
- **T2 後續修補（7f45af4 未涵蓋，需補；同一可改清單）**：
  - 實作：加 `Gate.untimed(token) -> bool`＋webgui `_untimed(token) -> bool`（§1.4：鎖內原子檢查 `state==power` 且 `token==持有者`，成功 True、否則 False 且不改狀態）；補 §1.4 表中 ⚠ 各列：`_work`、`_list_work` 外層 `try:` 第一行 `_finishing`；三個主工作（`run_resources`、`run_plan`、`execute.run_functions`）一律照 §1.3 固定寫法——`if not self._untimed(token): return`，再以 `try/finally` 包住主工作，`finally` 立即 `_finishing(token)`。
  - 驗收：
    1. `_work`（E36441A 與 N6705C 兩條）、`_list_work` 在 `detect_model` 或 `connect_group` 卡住 → 收尾逾時後進 `fault`。
    2. 主工作（`run_plan`、`run_resources`、`run_functions`）跑超過 `POWER_FINISH_TIMEOUT_S` 不進 `fault`。
    3. `_untimed` 對非持有 token、舊世代 token、已釋放 token 回 False 且狀態不變。
    4. **競態**：計時器先到期轉 `fault` 後 worker 才呼叫 `_untimed` → 回 False，且主工作（以假函式計數）**未被呼叫**、清理（關連線假物件）仍執行、`_release` 回 False、狀態維持 `fault`。反向：`_untimed` 成功後舊計時器到期不轉 `fault`。
    5. **返回後重新計時**：主工作正常返回後，結果處理卡住（以 monkeypatch 讓 `_seq_result`／`_emit` 或 `_seq_close`／`inst.close()` 阻塞）→ 收尾逾時後進 `fault`；主工作**拋例外**後 `except` 分支卡住 → 同樣進 `fault`。三個主工作各一案（E36441A 分支沒有 `_seq_close`，以結果處理卡住驗）。
    6. 主工作後的 `_finishing` 重新計時仍生效；`_finishing` 在 `fault`／非持有者時無作用。

### T3（Codex）大檔核心
- 可改：`src/n6705c_seq/analysis/core.py`（接手）、`src/n6705c_seq/analysis/storage.py`（新建）、`tests/analysis/test_core_legacy.py`（接手）、`tests/analysis/parity_tools/adapter.py`（接手）、`tests/analysis/parity_tools/test_parity.py`（接手，只改 CSV 比對為 §8 容差）、`tests/analysis/test_core.py`、`tests/analysis/test_storage.py`、`tests/analysis/test_memory.py`。
- 依據：§5、§6.2–6.4、§7、§8。
- 驗收：parity 全過（重跑貼輸出；CSV 依 §8 容差）；`open_dlog` 改路徑 API 時 `test_core_legacy.py` 與 `adapter.py` 同步改完（§6.1）；快取損壞可復原；取消 ≤2 s 且不留半成品；§7.2 預算（峰值 RSS 與 tracemalloc 皆過；大檔測試以環境變數啟用）；同目標併發 `atomic_write_*` 不互刪暫存檔、結果為完整檔；比對檔取樣率不同時不爆記憶體。

### T4（Sonnet）server
- 可改：`src/n6705c_seq/analysis/server.py`（新建）、`tests/analysis/test_server*.py`、`tests/analysis/test_http.py`。
- 依據：§2.1 用途對應、§3、§5、§7.1。先對 `CoreApi`／storage 簽名與假 gate 寫，T3 完成後跑整合。
- 驗收：無 token 403；非 analysis 423；舊 gen 409；Host 不符 403；非白名單 404；超量 413（含 2,000,000×12 keys 依 `use_buckets` 被拒）；4 個 JOB 占滿時 `/cancel` 仍 200 且能取消；`/save` 缺或錯 Purpose 400、用途不符 403；CSV 匯出可取消且無 `.part` 殘留；`shutdown` 在 `CLOSING_CLEANUP_TIMEOUT_S` 內返回。
- 原 `報告分析升級版/來源_DLOG_Viewer/server/test_dlog_server.py` 中**只能走 HTTP 驗**的檢查（T1 的 `test_core_legacy.py` 不涵蓋），T4 逐項改寫為新路由後必須通過：
  1. `keys=X9`（不符 `KEY_PATTERN`）→ 400 `bad_request`：`/buckets`、`/samples`、`/stats` 各驗一次。
  2. `/samples` layout：每項 `offset` 為 payload **位元組**位移、`length` 為 float32 個數；第 k 項 `offset == 前面各項 length 總和 × 4`，最後一項 `offset + length×4 ==` payload 長度；`keys=V1,I1, lo=100, hi=1099` 切出的值與 `core.get_values` 相同。
  3. `/buckets` layout：每 key `offset` 指向 t，min 在 `offset + length×4`、max 在 `offset + 2×length×4`；`length ≤ n`；切出值與 `core.compute_buckets` 相同（含 `offset=1` 時 I 的 min／max 全 > 0）。
  4. Content-Disposition 與 `name`：原版 `GET /export/csv?name=t.csv` 回 `Content-Disposition: attachment; filename*=UTF-8''…`。新版改 `POST /export/csv`＋grant：回應為 JSON，**不得**帶 `Content-Disposition`；回應 `name` ＝授權路徑 basename（含非 ASCII 檔名原樣）；body 多帶 `name` 鍵 → 忽略，不影響實際檔名。輸出檔前 4 行與列數照原檢查（標題、`Sampling Period:0.00098303996`、3 欄標籤、2001 列）。
  5. 壞 handle：原版 `h=nope` → 400。新版：不符 `HANDLE_PATTERN` → 400 `bad_request`；格式正確但未知（40 個 `0`）→ 404 `handle`；例外 `/release` 同一未知 handle → 200 `{ok:true}`；`/export/csv` 缺 `hc`（且 `cols` 無 `c:`）→ 不因格式報 400。

### T5（Sonnet）Viewer 改接
- 可改：`src/n6705c_seq/analysis/web/viewer.html`（接手）、`src/n6705c_seq/analysis/web/shell_stub.html`（新建，`dev=True` 才送出）。
- 依據：§3.3、§4、§6.4、§7.1。開檔（主檔、比對檔、Marker）改 `open_request`＋`/open`／`/import/markers`；PNG、HTML 報告、Marker JSON 改 `save_request`＋`/save`（帶 `X-Analysis-Purpose`＝該 `purpose`）；`/samples`／`/buckets` 依 §7.1 `use_buckets` 算式切換；CSV 改 `save_request`＋`/export/csv`（不再 `location.href`，可按取消呼叫 `/cancel`）；`localStorage` 全換 `/prefs`；語言只聽外殼。
- 驗收：人眼確認清單 §1–3；四類匯出可存檔；`lang` 訊息即時切換；在 `shell_stub.html` 下 targetOrigin、source、gen 及 §4.2 類型層驗證生效（無 id 的 `lang` 被接受、錯型或錯方向訊息被丟）。

### T6（Opus）SEQ 外殼
- 可改：`web/index.html`、`web/app.js`、`web/seq.js`、`web/list.js`、`web/style.css`、`web/seq.css`、`web/list.css`、`web/analysis.js`（新建，不得含中文）、`tests/js/**`、`tests/test_analysis_integration.py`。
- 依據：§1.5、§1.6、§2、§4、§9。分頁清單改資料驅動（`analysis_enabled` 為真才出現 `analysis`）；`App.state/gen/powerAllowed`；按鈕改看 `powerAllowed`；缺 i18n key 回報 T0。
- 驗收：切頁、關窗、故障、直接呼叫 bridge 都守互斥；晚到的初始 `idle` 或舊世代 `draining` 不清掉新 Viewer（§1.5 `seq`、§4.2 `gen`）；`ready` 不帶 id 仍被接受；旗標關閉時畫面與現況相同；`node --test tests/js` 全綠；真 server 的 `--selftest` 整合在此驗。

### T7（Haiku）打包與文件
- 可改：`pyproject.toml`（package-data 加 `analysis/web/*`）、`packaging/**`（spec datas 逐檔列 `analysis/web` 內容；`shell_stub.html` 不打包）、`README.md`、`HANDOFF.md`、`HANDOFF_WINDOWS.md`（完成後）。
- `README.md` 補指令用法：`python -m pytest tests/analysis`；`python tests/analysis/parity_tools/extract_original_js.py`、`compare.py`、`compare_buckets.py`（從 repo 根目錄執行）；大檔環境變數 `N6705C_ANALYSIS_BIG`、`N6705C_PARITY_74445`；並註明未搬的 `dlog_poc.py`、`make_synth.py`、`bench_local.js`、`make_probe.py`、parity `README.md` 留在 `報告分析升級版/來源_DLOG_Viewer/server/parity_tools/`（§8）。
- 驗收：`ORIGINAL_HTML_SHA1` 搬移前後相同；`gui_entry.py` 無中文；Windows onedir 唯讀安裝位置可開樣本、四類匯出、正常退出。

### T8（Codex＋Opus 各一份）審查
- 可改：`docs/analysis-review-codex.md`、`docs/analysis-review-opus.md`。
- 審查面：互斥能否繞過（§1、§3.2）、import 邊界（§10）、峰值記憶體（§7）、取消（§5）、當掉、Windows 刪快取；阻擋問題交回原擁有者。
