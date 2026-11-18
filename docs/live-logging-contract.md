# 即時資料錄製與 ARB 修補｜Wave 0 T0 契約 v1

日期：2026-11-17。使用者已授權「開工 Wave 0」，本次只做 T0。
數字與型別以 `src/n6705c_seq/record/contracts.py` 為準；語意以本文件為準。
analysis 的 wire 型別另在 `analysis/contracts.py`。衝突、缺少字串或需要跨界改檔時回報 T0，不能各自改介面。

錄製放在 DLOG Viewer 分頁的「錄製」模式，與「檢視」互斥。v1 單台、單 worker，只有量測紀錄，不開關輸出、不執行 ARB、不送 `*RST`；邊上電邊錄留 P4。H0 PASS 才做 B-ELOG；H0 FAIL 做 B-DLOG＋約 40 Hz MEAS 預覽；A1 完成後才開始 P2。
`analysis/*` 不得 import `record` 或 `n6705c`；`record` 不得 import `analysis`、`webgui`、`webview`、`web_*`。寫檔器與快照只用 stdlib／numpy；contracts 只用 stdlib。

## 讀碼核對 F1–F17（不是實機驗收）

| 項 | 實際核對與結論 |
|---|---|
| F1 | `gate.py` 的 reserve 只接受 power；新進 analysis 要 idle。**修正文句**：`enter` 在已是 analysis 時會回同一 gen（冪等），並非完全只接受 idle。現有轉移足夠；**gate.py 不改**，G 補 C3 測試。HANDOFF §13.1 的「需調整」以 §13.6 及此結論為準 |
| F2 | `untimed` 原子檢查 owner 並改 epoch；`finishing` 重設 timer。契約原先只列三個主工作，C4 正式擴充錄製與下載 |
| F3 | power 時 `on_closing()` 回 power；`webgui._on_closing()` 回 False。錄製沿用，收尾也不能關窗 |
| F4 | `issue_grant` 檢查 analysis/gen；Viewer 的 open_result 必須配 pendingReplies；所以新增 C1 的 record_load 和 C2 的 open_push，不能冒用 open_result |
| F5 | analysis/contracts 為凍結契約；T0 只改授權的訊息區和計時註解，SELFTEST tuple 不動 |
| F6 | 公開 Api 集合與 selftest 分類都受測試約束；G 必須同次加入六方法、精確集合及兩個 tuple，T0 不提前分類 |
| F7 | `test_webgui.py` 掃描 CJK 和翻譯 key；新增字串集中 i18n，既有 key 不改 |
| F8 | analysis 邊界測試只允許 stdlib、numpy、analysis 子套件；錄製在外部，往返解析只在測試 import analysis.core |
| F9 | 現有 stop join 後無條件清 thread、sink 拋錯被吞；**補充**：現有 recorder 本身根本不保證送 ABOR，必須由呼叫者另呼叫 session.stop；C7 改由同 worker 保證送出。為配合 C4，C1 新增 keyword-only on_cleanup 及 fetch max_records；只用既有介面無法在阻塞 run 的尾段之前回調 gate |
| F10 | DlogSession.start 無條件 RUNNING；retrieve 在 dlog transfer 失敗後仍刪兩檔；transfer 整塊載入記憶體。C8 修狀態及刪除語意，C13 限大小 |
| F11 | parsing.FIELD_ORDER 是 I_avg/min/max 再 V_avg/min/max，chunk.data 為依通道分開的矩陣；Viewer 每個 channel 是 V,I。W 必須重排，不能只交換整塊通道 |
| F12 | parse_dlog 把兩個 UInt32 合讀為 >Q，將 write 後尾段置前。實際執行 3×8 合成 float32：trigger=0,write=3 和 write=0 的 V1 均為 [0,8,16]；write=1 為 [8,16,0]。**限定**：零筆會報 No records；半筆僅被現有 parser 忽略，不等於安全恢復；recover 仍必須驗旁檔並截尾。trigger≠0 的一般檔仍不受本契約保證 |
| F13 | fake 的未知指令加 -113，沒有 ARB source／slew handler；DLOG 目前是 placeholder。A1／B-DLOG 分波修改 |
| F14 | start_together 只有 TRIG:TRAN:SOUR IMM → INIT:TRAN；A1 補 ARB source 和查錯，真機因果仍未證實。**規劃的簡單插入不足**：run_functions 先在 :315 的 _apply_levels 開輸出，再於 :322 呼叫 start_together；只在後者補指令不能滿足「被拒須在開輸出前中止」，A1 要將 source 準備與 INIT 分開，或以新增 keyword-only callback 把開輸出安排在 source 查錯之後、INIT 之前，保持既有位置參數相容 |
| F15 | spec 只收集 analysis 子模組（另有 VISA/USB 隱藏依賴）；package-data 已有 web/*，D 加 record collect_submodules |
| F16 | app.poll 對未知事件直接 emit，故 record 事件可接入，**app.js 不改**。規劃說所有 section 都空不精確：analysis-tab 已有警告與 iframe，K 保留並加面板。power 時資源輸入不一定全反灰，bridge 必須複製本次 resources/config，不能依賴畫面鎖定 |
| F17 | 現有 tests 沒有 tests/core 的 elog/dlog/arb 測試。未來 core 測試各用唯一 basename，C1 故障注入用 wrapper，不改 fake |

另一處規劃不一致：C2 型別沒有 `RecordStatus.running`。**固定依 `state in ACTIVE_STATES` 判斷**（starting/running/stopping），K/J 不讀不存在的 running 欄位。`App.recordPane.running()` 是 JS 的方法，回上述布林。

範本 `tests/analysis/fixtures/default_20260803_092740.dlog`：UTF-8 標頭到 `</dlog>\n` 共 **3968 bytes**，之後 `>II=(0,81380)`；資料區 **2,604,160 bytes** = 81380×8×4；tint=0.00098303996、time=80、4 個 N6762A、sense_minmax=0。標頭有 `<ident><model>`、channel/frame/gui_chan/gui_frame；option 內 `<1ua>` 等標籤並非嚴格 XML 合法名稱，不能假設整個範本能用標準 XML parser 往返。

## C1 bridge：六個公開方法與所有權

所有 JSON 都用英文欄名、秒為時間單位，code 為 RecordCode.value，error 為已翻譯文字。禁止 JS 傳路徑、grant 的目標、SCPI、trigger source 或任意 backend 名稱。回傳不帶絕對路徑。

| 簽章 | 精確回傳與行為 |
|---|---|
| `record_meta() -> RecordMeta` | `{enabled,snapshot_poll_ms,snapshot_poll_min_ms,snapshot_max_points,max_channels,period_floor_table,period_max_s,period_granularity_s,duration_min_s,duration_max_s,dlog_fallback_max_bytes}`；唯讀，不連線。數字取 contracts；JSON table key 為 "1".."8"，表示總量測參數數目 |
| `record_start(config: dict, resources: list[str], demo: bool) -> dict` | 成功排 worker 回 `{}`；使用者取消或對話框鎖忙回 `{cancelled:true}`；失敗 `{error:str,code:str}`。RECORD_ENABLED=False 優先回 disabled。驗 config → `_reserve("power")` → 原生 SAVE 對話框選 .dlog → 啟動唯一 worker。reserve 失敗依 gate 狀態回 power/analysis/draining/fault/closing。對話框期間持 token、不計時；取消或啟動前例外必釋放 token。副檔名不符時附加 .dlog；最終檔與旁檔若存在必確認覆寫，不能直接覆寫已有 .part。成功只表示已排程，連線/執行錯誤由事件/status 回報 |
| `record_stop() -> dict` | 活躍 run 回 `{}`，只設 threading.Event，立刻返回；stopping 重呼叫冪等。無活躍 run 回 `{error,code:"no_recording"}`。不 join、不送 ABOR、不讀 VISA、不釋 token。旗標關閉也允許停止既有 run |
| `record_status() -> RecordStatus` | 鎖內取得副本、不做 I/O。初值 `{run_id:"",state:"idle",elapsed_s:0,records:0,gaps:0,preview:false}`。terminal 保留到下一次接受開始；禁止直接曝露 Path、例外或 numpy 物件 |
| `record_snapshot(since_seq: int) -> SnapshotResult` | since_seq 為 int（不收 bool）且 ≥-1；-1 強制完整快照，新 run/重新顯示時必用。相等回 `{unchanged:true,seq}`；不同（包括較大）回完整副本。idle 無 backend 時回完整空快照 `{run_id:"",seq:0,preview:false,period_s:0,keys:{}}`。無 I/O，thread safe；呼叫端只會傳有效整數 |
| `record_load(gen: int) -> analysis.contracts.PathResult` | 不彈對話框。成功 `{ok:true,gen,grant,name}`，`self._gate.issue_grant(gen,"open","dlog",last_path)`。只有最後一次 **已 finalize 且 worker 已釋放 power** 的完成檔可載入；新 run 接受時清除 last_path，失敗不退回舊 run。沒有完成檔（含 .part 或目前仍錄製）回 `{ok:false,code:"no_recording",error}`；非 int/bool、錯世代、非 analysis 或授權競態回 stale。grant None 不當成功。不接受 path 參數；每次成功簽發新的單次 grant |

Config 精確欄位：`machine_index:int`（0 或 1，索引 resources）、`channels:list[int]`（不重複、1..4、排序後使用）、`sense_volt:bool`、`sense_curr:bool`（至少一真）、`period_s:number`、`duration_s:number`（有限且 1..3,599,964,000 s）。不收 bool 當數字、不收未知欄位、不能選 E36441A；live 時指定的 resources 項必為非空字串，v1 只連該台，不連其他台；demo 可 resources=[]，用一台 fake。config/resources/demo 接受時複製，後續 GUI 設定變化不影響 run。

**時長上限疑點，尚未修正數值**：`core/dlog.py:77` 的 `DLOG_MAX_DURATION=3_599_964_000.0` 註解寫 99,999 hours，contracts 的 `DURATION_MAX_S` 目前沿用該數字。但 99,999 h × 3600 = **359,996,400 s**，現值大了 10 倍。[Keysight N6705C Operating and Service Guide](https://www.keysight.com/tz/en/assets/9018-04227/service-manuals/9018-04227.pdf) 的 `SENSe:DLOG:TIME`（頁 352）也列 1..359,996,400 s。此為 core／契約待協調修正事項；本次保留常數及上述範圍、不改 core，不代表已確認儀器接受較大上限，A1 也不得據此推定真機能力。

只記 average，不開 sense_minmax。`parameters=len(channels)*(sense_volt+sense_curr)`，ELOG floor=102.4µs×parameters、最大 60 s、格點 20.48µs；低於 floor 回 bad_config，不偷偷改要求。讀回實際 period 必須一致、有限且有效，時間軸用讀回值。J 初值固定 machine_index=0、channels=[1]、V/I 都真、period_s=0.001、duration_s=3600。duration_s 是兩分支的最長錄製時間，worker 到期走同一停止流程；因此 DLOG 分支能在開始前套 C13。bridge 產生非空唯一 run_id（例如 uuid4.hex），注入傳給 backend 的 Config 副本；JS 不提供 run_id。

selftest 分類 **交 G 同次做**：允許 `record_meta,record_status,record_snapshot`；禁止 `record_start,record_stop,record_load`。T0 不改 analysis 的 SELFTEST_JS_API_* tuple、CONFIG_KEYS 或其他公開介面。

## C2 事件、快照、JS 交接與 open_push

`EVENT_RECORD="record"`，queue 事件只有 `["record", RecordStatus]`；backend 的 `emit(status)` 接受 status 副本，bridge 包成事件。接受開始後發 starting，設定/觸發成功發 running，worker 看見 stop 或到 duration 發 stopping，最終 close/release 後發 done 或 failed。持續錄製時可合併狀態更新到約 500ms，狀態改變立即發，不逐筆塞 queue。`elapsed_s` 用 monotonic（起錄後秒數）；records 為已完整寫下的筆數、gaps 為 gap 事件數。`name` 僅 basename；`code/error` 只在失敗出現。`notes` 藉既有 result/warning 事件搭配 record_note 顯示，完整內容入旁檔。

Snapshot 必填：`{run_id,seq,preview,period_s,keys}`；keys 僅選中的 V1,I1…V4,I4。每個 series 是 `{t0,dt,min:[],max:[]}`，t0 是第一 bucket 左緣、dt 為 bucket 寬度；min/max 長度相等且最多 2000，最後 bucket 可以未滿。seq 在同 run 每次可見 append 後遞增；snapshot 回 detached list 副本，非有限值在 JSON 中轉 null（不改磁碟原始 float32）。preview=false 時 period_s 是實際錄製週期，preview=true 時是 MEAS 預覽週期，絕不能用預覽樣本數冒充落檔 records。

S 提供 `Envelope(keys, period, *, run_id="")`、`append(block)`、`snapshot(since_seq)`；block 為欄位順序與 keys 一致的 float32[n,k]，二二合併相鄰 bucket，保留全段極值。backend 傳 run_id；若單獨測試沒傳，使用空字串。無資料 seq=0、各 key 空 min/max；每個 run 重建 Envelope，seq 不跨 run。併發讀寫加鎖，copy 後才放鎖。

輪詢 `SNAPSHOT_POLL_MS=500`，不得低於 `SNAPSHOT_POLL_MIN_MS=250`；只在面板可見且 active 時輪詢。新 run 清掉圖表並以 -1 起查；JS 丟掉 run_id 不符和 seq 不新於已套用序號的完整快照，舊輪詢 Promise/隱藏後的回應用面板請求 epoch 丟掉（unchanged 不含 run_id，不能據此切換 run）。隱藏時清 interval，重顯示先讀 status，再用 -1 取全貌。終態允許主動取最後快照一次，沒有常態輪詢。

K/J：J 設 `App.recordPane={show(),hide(),running()}`；show/hide 冪等並控制繪圖/輪詢；running() 依 ACTIVE_STATES。K 提供 `App.openRecorded() -> Promise<void>`，J 載入按鈕呼叫。K 設模式、J 在 record-pane 內畫表單及圖。RECORD_ENABLED=false 時隱藏模式切換與錄製面板，原 Viewer 保持現況。活躍錄製禁止切檢視模式，但可以離開整個 Viewer 分頁去其他分頁（此時 record 模式 leave 不呼叫 analysis_leave，錄製繼續）；回來讀 status 復原錄製模式。

`open_push` = `{v:1,type:"open_push",gen:int,grant:str,name:str}`，**沒有 id/ok/purpose/path**。K 的 openRecorded 等 run 結束及 App.state=idle，進檢視 → analysis_enter → 載入 iframe → 等精確該 gen 的 ready → record_load(gen) → 確認仍為該 frame/gen/origin → postMessage 到精確 origin，禁用 "*"。PathResult 失敗顯示其 error，不送推送。V 驗 source===window.parent、origin===parent、v/gen、非空 grant、非空 basename name（無 / 或反斜線），通過後共用主檔 `/open` 載入流程。沒有 pending request 也接受；長工作中忽略並顯示 Viewer 自己的雙語 busy toast（V 在自己檔案內字典，不引用 shell i18n），不要中斷既有工作。ready/lang/open_push 不檢查 id，*_result 才對 id。server、storage 不改。

## C3 gate 對照與 G 必補六案

| 分頁模式 | gate 狀態 | 怎麼進入 | 怎麼離開 |
|---|---|---|---|
| 檢視 | `analysis(gen)` | `analysis_enter`（現況） | `analysis_leave` → `draining` → `idle` |
| 錄製：表單（還沒開始錄） | `idle` | 切到錄製模式：先 leave，等到 `App.state==="idle"` | — |
| 錄製：錄製中 | `power`（`_reserve("power")`） | `record_start` | `record_stop` 只設旗標 → worker 自己送 ABOR、補尾段、寫完檔 → `_release` → `idle` |
| 錄製：收尾 | `power`（`_finishing` 計時 60 s） | 停止旗標被 worker 看到 | `_release`；卡住就進 `fault`（現況機制） |

**gate.py 不改**：讀碼確認既有狀態、owner CAS、grant 足夠，不做原子直接交接。idle 空窗其他 power 工作可能先取得 token，record_start 便正常回 power，不能強搶。G 如果發現需要原子交接，回報 T0 改契約，不自己加轉移。

G 的 `tests/test_record_gate.py` 必須用真 gate＋fake/stub backend，涵蓋：

1. 從 analysis 經 draining、idle 到 `record_start` 成功取得 power。
2. 在 analysis 或 draining 狀態呼叫 `record_start`，被拒並回對應錯誤碼。
3. 錄製中呼叫 `analysis_enter` 被拒。
4. 錄製中關窗被拒。
5. 停止 → idle → enter → `record_load` 拿到的 grant 能被 `consume_grant` 接受；舊世代或沒有錄完的檔時被拒。
6. 停止卡住超過 60 s 進 `fault`（用短 timeout monkeypatch）。

另驗 flag=False 拒絕 start、stop 跨執行緒立即返回、舊 token 的 release 不清 fault、快照不做 I/O。六案不是讓 T0 改 gate 的授權。

## C4 計時固定寫法與停止限制

設定／連線開始先 `_finishing(token)`；對話框除外。單一 worker 依下列結構取得／結束主工作：

```python
# Token already reserved before the native dialog. Worker owns all VISA I/O.
inst = None
try:
    self._finishing(token)
    # connect selected instrument; backend configures it in this same worker
    backend = make_backend(inst, cfg, final_path,
                           untimed=lambda: self._untimed(token),
                           finishing=lambda: self._finishing(token))
    result = backend.run(stop, emit_status)
    # retain result privately; no completed-path grant until close/release succeeds
finally:
    # Backend has already armed the finish timer before its own tail/ABOR/finalize.
    # Do not repeatedly reset it in a cleanup loop.
    try:
        if inst is not None:
            inst.close()
    finally:
        self._release(token)
```

backend.run 入口先 `finishing()`，設定與讀回成功後才 `untimed()` → `try/finally: finishing()` 包住錄製迴圈；回 False 就跳過 INIT／錄製，進清理。外層 worker 不包額外的 untimed 區，也不在 run 返回後重設 finishing；設定與收尾計時由 backend 依上述 callback 保證。

backend.run 必須在觀察停止／到期／任何例外後，**先 finishing() 再做 ABOR、尾段補讀、writer.finalize/abort**；不能只靠 run 返回時才開始計時。上例外層不能在 close 前額外重設 timer 來延長卡住的收尾。run 之外失敗（例如 connect/make_backend）仍沿用進 worker 時已啟動的 timer，finally close/release 必走。這些 callback 僅動 gate，不做 VISA I/O。

B-DLOG 的錄製停止／ABOR 在計時內；整檔下載是另一主工作，固定：

```python
if not untimed():
    raise RuntimeError("record owner is no longer active")  # bridge maps to stuck
try:
    transfer_to_local_part()  # blocking on this SAME worker, no other VISA callers
finally:
    finishing()
# sidecar, final rename, close and release remain timed
```

60 s 起算於每次 finishing，僅成功 untimed 或 owner release 停表。fault 後不釋鎖成 idle；worker 遲到結果不能報 done／last_path，應 failed/stuck。**已知上限**：錄製迴圈不計時，stop 只設旗標；如果 worker 卡在 fetch 尚未觀察 stop，gate 不會僅因按停止而啟動 timer。正常靠 VISA timeout（現況 10 s）讓 worker進入 finishing；不能假稱任何 stop 都保證 60 s 轉 fault。G 第六案必須讓 worker 已進入 finishing 才卡住。解決驅動永久卡死需另立契約，不能改 stop 執行緒去送 ABOR。

## C5 Backend Protocol 與資料流

固定模組 `record/backend.py`；兩個 H0 分支只實作其中一個。

```python
make_backend(inst, cfg: Config, final_path: Path, *,
             untimed: Callable[[], bool], finishing: Callable[[], None]) -> Backend
Backend.run(stop: threading.Event, emit: Callable[[RecordStatus], None]) -> RunResult
Backend.snapshot(since_seq: int) -> Snapshot | Unchanged
Backend.preview: bool  # read-only property
```

contracts 的 `MakeBackend` 描述工廠 callable，不是假 backend 函式。inst 為所選 N6705C（stdlib 契約用 Any），由 bridge worker 連線並擁有／關閉；make_backend 和 run 都只在同一 worker 呼叫。建構工廠不另起執行緒、不發儀器 I/O；run 內配置、INIT、觸發、fetch/MEAS、ABOR、下載、寫檔。snapshot 可從 js_api 執行緒呼叫，只讀記憶體，不連線、不讀磁碟。

RunResult 必填 `{run_id,final_path:Path,elapsed_s,records,gaps,preview,notes:list[str]}`，只返回 worker，不送 JS。run 正常返回前必成功 finalize；失敗拋原始例外／core 的 ElogSinkError/ElogFetchError，bridge 依 C10 映射，保留 .part、不能偽裝完成。emit 只報 starting/running/stopping 的 status，terminal 由 bridge 完成 close/release 後發；emit 不呼叫 JS 或儀器。backend 不 release token、不關 caller-owned inst。

B-ELOG：ElogSession 設定／讀回，sink→elog_block→DlogWriter.append→Envelope.append；旁檔保存要求/讀回/IDN/UTC/gaps/notes。所有 I/O 包括磁碟寫入在 worker，sink 必須快；不另啟 ElogRecorder.start 的 thread，而呼叫阻塞 run。
B-DLOG：DlogSession 設定與觸發；單 worker 約 25ms 一輪 MEAS:VOLT?/MEAS:CURR?，實際速度受 I/O 限制，不保證精準 40Hz。MEAS 只入 Envelope、preview=True；**僅模擬器（`session.description` 以 `Demo mode` 開頭）才送 MEAS，真機韌體在 DLOG 執行中回 `+319` 且先吃約 5 s 逾時，會拖慢停止，所以真機不預覽、只記 note**；正式 records 以下載檔為準；到期/stop 後 ABOR，下載到 .part，校驗／持久化／發布後回 RunResult。fake 在 ABOR／到期產生真格式，不能沿用 placeholder。

## C6 DLOG Writer、格式、旁檔與恢復

固定 `record/dlogfile.py`，介面與 contracts 的 Protocol 相同：

```python
DlogWriter(final_path: Path, *, channels: list[int], sense_volt: bool,
           sense_curr: bool, tint: float, models: dict[int,str], sidecar: dict)
.append(block) -> None  # numpy float32[n,n_fields], channels ascending then V,I
.finalize() -> Path
.abort(keep_part: bool = True) -> None
recover_part(part: Path) -> Path | None
elog_block(data: dict[int, ndarray], fields: tuple[str,...], channels: list[int]) -> ndarray
```

elog_block 輸入對應 ElogChunk.data/fields（每通道 n×len(fields)）；只選 V_avg/I_avg，按排序後 channels、每通道 V 再 I 組合 n×n_fields。缺任一所選通道、對齊長度不等或沒有 average 欄位時拒絕，不補零；只有 V 或只有 I 也可。記錄 min/max 設定不開啟，跨任務不可把硬體 min/max 當另一通道。

磁碟格式固定 UTF-8 標頭以 **LF 的 `</dlog>\n`** 結尾 → `struct.pack(">II",0,write)` → 大端 float32 row major。append 接收 float32，轉 endian，不做負電流替換、不裁切極值、不補 NaN。每通道 channel 標頭按 1..4 遞增；啟用才設 sense_volt/sense_curr=1；model 放 `<ident><model>`，文字需 escaping。frame 至少保留 fixture 的 sense_minmax=0、trig_source=1、offset=0、tint、date、time。gui_chan/gui_frame 與其他設定可照 fixture 的結構填有效預設；不可複製 fixture 的通道型號／時間／原路徑冒充本次。

最小必備標頭骨架（channel 對選中通道重複，其他欄位按上述範本補；不需嚴格 XML 解析範本）：

```xml
<!-- N6700X dlog settings -->
<dlog>
  <channel id="1">
    <sense_volt>1</sense_volt><sense_curr>1</sense_curr>
    <ident><model>N6762A</model></ident>
  </channel>
  <frame>
    <sense_minmax>0</sense_minmax><trig_source>1</trig_source><offset>0</offset>
    <tint>0.001</tint><time>00000000000000.000000000</time><date>"UTC date"</date>
  </frame>
</dlog>
```

**time 二選一決策：固定寬度回填，不能省略**。從建檔就保留 24 個 ASCII bytes，初值 `f"{0:024.9f}"`；finalize／recover 用 `f"{N*tint:024.9f}"` 回填並驗長度仍=24，單位秒。這是已保存樣本的長度；有 gap 時不是牆鐘總時間。旁檔另記實際 elapsed_s。tint 用讀回值，輸出精度足以 float roundtrip。前觸發、環形覆寫和 sense_minmax=1 不在 v1 writer 範圍；14585A 相容性仍需 HW-P2，Viewer 往返不能代替它。

命名固定：final X.dlog；暫存 X.dlog.part；旁檔 final X.dlog.json、暫存 X.dlog.json.part。建檔使用 exclusive create，拒絕既有暫存。索引初始 trigger=write=0，append 時不更新 write。finalize 只允許 N≥1 且 ≤2^32-1；關閉一切可阻止 Windows rename 的 handle，回填兩個 >I=(0,N) 和 time、flush/fsync data；先以 `complete=False`、`ended_utc=None` 寫入並 fsync 旁檔暫存，再 `os.replace` 到正式旁檔；接著 `os.replace` 資料檔；最後將 `complete=True` 的旁檔寫入暫存並 fsync，再 replace 到正式旁檔。資料檔 replace 失敗時，保留 `.part` 與 `complete=False` 的正式旁檔並拋錯。資料已就位但最後旁檔更新失敗時，正式旁檔停在 `complete=False`（保守，Viewer 視為未完成），仍拋錯；此時沒有 `.part`，資料在 final，可由使用者／後續流程重寫旁檔。三步成功並 fsync 目錄後才回 final_path；配對 rename **不是原子交易**，以三步發布都成功返回作本次完成標誌，失敗不能宣稱 done。覆寫情境中舊 final 可能已存在，不能只憑 final 存在判斷本次成功；重啟／recover 需核對資料標頭的 run_id 與旁檔。abort(keep_part=True) 關 handle 並保留可恢復檔；False 只刪本 writer 這次建立的暫存，不能刪 final 或別人的檔。

旁檔 schema v1（UTC 用 ISO 8601 的 Z 或 +00:00）：

- `schema_version:1, producer:"n6705c_seq.record", run_id:str, backend:"elog"|"dlog", preview:bool`。
- `name`（basename）、`requested`（原 Config，不含外部路徑）、`actual`（channels/sense_volt/sense_curr/period_s/duration_s/ranges 可用值）、`idn:str, models`（channel 字串鍵）、`started_utc, ended_utc`（結束前可 null）、`elapsed_s, records`。
- `header_bytes:int, record_bytes:int, time_offset:int, time_width:24`、`gaps:[]`、`notes:list[str]`、`complete:bool`。開錄前已持久化 producer/run_id/結構和 requested/actual，結束完整補齊；更新用同目錄暫存＋replace，不能截斷唯一旁檔。
- 每個 gap：`{kind:str,index:int,count:int|null}`，index 是已接收序列下一筆位置，count 是估計缺口數，不可測時 null。空讀/overflow 是偵測證據，不承諾精確定位或遺失筆數。`RecordStatus.gaps=len(gaps)`。已確認不了的時間空缺不補值，只記旁檔與 UI 警告；Viewer 均勻時間軸會壓縮 gap，不能稱完整牆鐘時間軸。

recover_part 只認 basename X.dlog.part 且有匹配的 X.dlog.json.part（或 finalize 第一段發布後的 X.dlog.json），驗 producer/schema/run_id（與標頭的 `<!-- record_run_id=<id> -->` 註解相同，建檔必須寫入）、檔名、標頭/欄位與長度、time_offset/width、trigger=0、sense_minmax=0，且不是活躍 writer（W 以進程內檔案鎖互斥）。不能僅憑有任意 JSON 就截檔，不追隨 symlink、不接受任意外部路徑。只按 `(size-header_bytes-8)//record_bytes` 取完整筆數，截去半筆，回填 N/time，旁檔標 `recovered:true` 並備註，再走相同發布流程。N=0、格式/標記不符、無旁檔或不安全時回 None 且不碰檔；I/O 失敗保留來源並拋錯。普通 .dlog 和無旁檔 .part 一律唯讀，不做通用修復。

W 驗收：V/I 逐點往返、欄位重排、final 無 part、半筆 recover、拒絕陌生／無旁檔／零筆、write=0 part 可 parse；往返測試可 import analysis.core，正式 writer 不得 import analysis。

## C7 core ELOG 錯誤與尾段語意（C1 任務實作）

固定新增符號：`ElogRecorder.run(stop: Event | None = None) -> ElogStats`（阻塞）；`ElogSinkError`、`ElogFetchError`（RuntimeError 子類，保留 __cause__）；`MAX_FETCH_FAILURES=3`。維持既有 constructor positional 參數，只新增 keyword-only `tail="drain_then_abort"`、`on_event=None`、`max_records`（預設 16384，1..16384）、`on_cleanup: Callable[[],None] | None = None`；既有 interval keyword 可傳 H0 值。backend 傳 contracts 的 ELOG_TAIL/ELOG_FETCH_RECORDS/ELOG_FETCH_INTERVAL_S；core 不可 import record。另新增 `ElogSession.fetch(*, max_records: int | None = None)`：None 維持舊行為，非 None 將每次要求限制到該上限；recorder 的普通 fetch／尾段都使用 max_records。不能只存參數卻仍讓 fetch 固定送 16384。

run(stop=None) 用 recorder 內部 Event，有外部 Event 時任一旗標 set 即停止；禁止同一 recorder 重複並行 run/start。start 保留背景相容包裝，內部呼叫同一 run；新 backend **不呼叫 start**。stop(timeout=10.0) 只設旗標、join 自己的背景 thread 並回 bool；thread 未結束回 False 且保留 `_thread`，已結束才清 None；阻塞 run 外部呼叫者用 Event 停止，由呼叫端 worker 管理存活。

`stop()` 設定的內部旗標保留；同一 recorder 停過後不可直接再 `run()` 恢復錄製，該呼叫會直接進入清理。下一次阻塞錄製須建立新 recorder；背景相容路徑的 `start()` 會清旗標，但仍須先重新啟動 session。

run 在離開讀取迴圈、準備尾段／ABOR（含例外）時先呼叫 on_cleanup（若有）；backend 將自己的 finishing 傳入，這是 C4 能在阻塞 run 返回前開始計時的必要掛點。callback 例外不得跳過 ABOR，完成清理後才回報。run 包 finally：最後的 ABOR:ELOG、查錯、尾段和 sink 全在呼叫 run 的執行緒。

- `drain_then_abort`：停止後先 drain 可取得尾段，再 ABOR；`abort_then_drain`：先 ABOR 再嘗試讀尾段。現有 session.fetch 只允許 RUNNING，C1 必須在 core 內補出 abort 後合法尾段讀取路徑，不可直接讓 STOPPED 呼叫既有 fetch 而假稱策略完成。兩策略尾段都最多 16 個 fetch 批次且 10 s monotonic 預算（每批前檢查，單次 I/O 仍受 VISA timeout）；達上限而仍有積壓則記 gap/notes，不得聲稱完整尾段，不能 drain 無限迴圈；H0 決定可取得完整尾段者。
- **T0 第二輪定案**：兩策略成功結束尾段與 ABOR 後，不論因 0 筆、已排空或達上限而結束，都須檢查最後估計積壓。只要大於「2 個讀取間隔的筆數＋實際 ABOR 耗時（含查錯）對應筆數」，發 `on_event("tail_incomplete", next_index, missing_estimate)` 並寫 note，不能聲稱完整尾段。`missing_estimate` 為最後估計積壓的完整筆數，不扣容許值；等於容許值不發。ABOR 耗時以 monotonic 量測，積壓使用 stop 凍結的 acquisition elapsed。既有 `tail_limit` 達上限且仍有積壓時的事件與 note 保留；超過容許值時兩事件可同時存在。contracts 新增事件常數 `ELOG_EVENT_TAIL_INCOMPLETE="tail_incomplete"`、`ELOG_EVENT_TAIL_LIMIT="tail_limit"`，不改 H0 暫定策略。
- sink 拋錯：停止後續 sink、同 worker 嘗試 ABOR 與查錯，再以原始 sink 為 cause 拋 ElogSinkError；ABOR 失敗不能掩蓋原始 sink 例外。
- 讀取連續失敗達 3 次：同 worker ABOR/查錯後拋 ElogFetchError；成功的合法 fetch（包括 0 筆）清連續失敗數。單次/第二次只呼叫 on_error（如有）並發 `on_event("fetch_error", next_index, 0)`；無損成功 fetch 不發 gap。
- 積壓大於 **2 個讀取間隔**估計筆數卻讀 0：`on_event("empty_backlog",next_index,estimated_backlog)`；overflow 新增證據：`on_event("overflow",next_index,estimated_lost)`，不中止。每次觀察一個事件，不能把同一 overflow sticky flag 每輪重複當新遺失；backend 旁檔標記 count 為估計，無法估計時用 null。
- **T0 第二輪定案**：每次錄製的 empty_backlog 缺口基準 B 初值為 0；發出事件時將 B 設為當時估計積壓，之後只有估計積壓 **大於 B 再加 2 個讀取間隔的筆數** 且讀到 0 筆才再發。overflow 事件發生時，B 重置為該次估計已丟筆數（目前溢出容量的總筆數，非事件增量）；成功讀到非 0 筆不重置 B。已丟失的資料不會再讀到，不能把同一永久缺口每輪重複寫入 gaps[]。
- on_event(kind:str,index:int,count:int) 與 on_error 必須快且不做 VISA I/O；callback 失敗需保留 cause、ABOR 後回報，不吞掉持久化錯誤。sink 失敗即 fatal，不能繼續把資料丟掉。
- **每一步設定、INIT、ABOR、0 筆、讀取失敗之後**讀 `SYST:ERR?` 到 0。現有 session.read_errors 有 50 次上限且遇格式錯誤可提早退出，不能宣稱一定 drain 到 0；C1 只能在自己擁有的 elog.py 補嚴格 helper：到上限仍未見 0／格式錯／讀失敗就拋錯，不可繼續當成功，不改 session.py。設定命令逐步查錯，不只整批結尾查。
- +310 **只有 autorange 不支援**時降級為 notes，回讀實際 range，UI warning 與旁檔保存；其他 ELOG 功能不支援為 fatal elog_unsupported。+323（含 INIT 才報）是 bad_config，不能開始報 running；任何非零（正或負）都要處理，不用 `<0` 判斷。原始 code/text 留 notes/error。

缺口偵測是估計，不能從這份程式推斷真機溢位丟新／丟舊；H0 才決定。C1 測試涵蓋三次讀錯／單錯恢復、sink錯、兩 tail、ABOR 同 thread、join timeout 保留 thread，fake 故障用 wrapper 注入，不碰 A1 的 fake。

## C8 DLOG 修正語意（C2）

`start()`：BUS 在 INIT/查錯後送 TRIG:DLOG/查錯，再設定 t0/UTC/RUNNING；IMM 在 INIT 成功後 RUNNING。EXT、VOLTn/CURRn、ARSK/OOOK 等 BUS/IMM 以外的有效來源，保持 ARMED，不設假觸發時戳，不讓 elapsed/is_complete 假稱正在錄。真實外觸發偵測不在 C2 範圍，v1 backend 僅 BUS/IMM。

`retrieve(..., cleanup=True)`：每個 remote 只在自己的 transfer 成功且本地 copy 成功關閉後才列可刪；CSV 成功而 dlog 失敗只刪 CSV、保留儀器 dlog；兩者都成功才都刪。CSV 下載失敗不刪任何未成功檔；cleanup=False 都保留。原有「CSV 成功但 dlog 失敗記 warning 並回成功項目」語意可保留。錄製 backend 自己檢查必須有正式 dlog 才能 done，不能拿 CSV 代替。transfer 仍整塊記憶體，C13 限制保護，串流 P5。

## C9 fake 與 A1

A1 補 `TRIG:ARB:SOUR[?]`，支援通道清單、儲存並回讀 IMM/BUS 等接受值，預設 IMM；**不改 INIT 的行為**。這僅為 fake 的暫定模型，不能當真機語意；HAs 結果後若要模擬不同狀態需回 T0。
依 [Keysight N6705C Operating and Service Guide](https://www.keysight.com/tz/en/assets/9018-04227/service-manuals/9018-04227.pdf) 的 `VOLTage:SLEW`／`VOLTage:SLEW:MAXimum`（頁 382–383），A1 補下列 fake 指令（均接受可選 `SOUR:` 前綴與通道清單）：

| 指令範例 | fake 語意與回傳 |
|---|---|
| `VOLT:SLEW? (@1)` | 回傳通道保存的目前設定速率 `rate`，單位 V/s |
| `VOLT:SLEW? MAX,(@1)` | 回傳通道固定的數值速率上限 `maximum`，單位 V/s；與 override 旗標分開 |
| `VOLT:SLEW:MAX? (@1)` | 回傳最大轉換率覆寫旗標 `override`，僅 `0` 或 `1`；沒有 V/s 單位，絕不能當速率上限 |
| `VOLT:SLEW <rate>,(@1)` | 保存數值速率並設 override=0；`MAX`／`INF` 則保存速率上限並設 override=1，與手冊的耦合語意一致 |

fake 建構預設為 rate=該通道 maximum、override=0（手冊的 rate 預設 MAX、override 預設 OFF）；預設速率已達上限，不發警告。多通道依清單順序回傳，以逗號分隔，旗標亦逐通道回 0/1。這是 fake 模型，不是類比輸出能力證據。本輪**不執行 `*RST`**，只能描述/測試建構預設，不對真機動作。未知指令仍 -113。

A1 在 `start_together` 的 INIT 前送 `TRIG:ARB:SOUR IMM,(@...)` 並 write_checked；ARB source 被拒必在開輸出前中止，檢查現有呼叫順序，不能只在輸出已開後補查。`slew_warnings(group,functions)->list[str]` 分別讀 `VOLT:SLEW:MAX? (@...)` 的 override、`VOLT:SLEW? (@...)` 的 rate，以及 `VOLT:SLEW? MAX,(@...)` 的數值速率上限。**僅 override=0 且 rate < maximum 時**用 `slew_warning`；override=1 表示最大轉換率覆寫已啟用，即使保存的 rate 較低也不發此警告；override=0 且 rate=maximum 亦不警告。例：override=0、rate=2、maximum=100 會警告；override=1、rate=2、maximum=100 不會。

上述三種 slew 查詢（`VOLT:SLEW?`、`VOLT:SLEW? MAX`、`VOLT:SLEW:MAX?`）任一被拒或不支援時，須記 log（含指令、原始回值與錯誤），跳過依賴該值的警告判定，不阻止執行；不可用 `write_checked` 直接拋錯中止。`VOLT:SLEW? MAX` 的查詢支援以 HAs 實測為準；HAs 的 slew 子項須在 reset 前後各 probe 並保留原始回值與錯誤，此新增 probe 被拒亦不使子項失敗。

`slew_warning` 欄位固定 `channel,actual,maximum`：channel 為可讀機台:通道字串，actual=rate，maximum=**`VOLT:SLEW? MAX` 的數值**，後兩者單位皆為 V/s；翻譯明述覆寫已關閉，不把 0/1 填進速率欄。`run_functions(...,on_warning=None)` 只新增 keyword；警告在開輸出前發、不阻止執行，None 寫 log；SEQ/List/CLI 都接上。真機 ARB source 是否造成既有起跑現象、各模組的 slew 查詢支援與回值仍等 HAs，不能將手冊或 fake 當實測。

## C10 錯誤碼與翻譯

| RecordCode.value | i18n key | 語意 |
|---|---|---|
| disabled | record_error_disabled | feature flag 關閉 |
| power | record_error_power | 另一 power 持有者／本次已在錄製 |
| analysis | record_error_analysis | 尚在檢視 |
| draining | record_error_draining | 檢視尚未排空 |
| fault | record_error_fault | 進入前 gate 已 fault |
| closing | record_error_closing | 程式關閉中 |
| bad_config | record_error_bad_config | 欄位、範圍、週期、+323、C13 超限（error 帶具體原因） |
| connect | record_error_connect | 連線／識別失敗 |
| elog_unsupported | record_error_elog_unsupported | ELOG 功能不支援；不含可降級的 autorange +310 |
| fetch | record_error_fetch | ElogFetchError、致命儀器資料讀取／下載失敗 |
| sink | record_error_sink | ElogSinkError、磁碟／寫檔／rename 失敗 |
| stuck | record_error_stuck | 已取得 token 的 run 收尾超時或 untimed 被 fault 拒絕 |
| no_recording | record_error_no_recording | stop 無活躍工作／load 無完成檔／零筆不發布 |
| stale | record_error_stale | gen 無效或 grant 簽發競態 |

`CODE_I18N_KEY` 覆蓋每個 RecordCode；錯誤模板若含 `{error}` 必傳原始具體原因。未預期錯誤按階段 connect/fetch/sink 映射並留 cause，不能回未列的 code。執行例外與 gate fault 同時時，stuck 優先呈現，原始例外保存在 notes。terminal failed 不能帶可載入的 last_path。

所有錄製 shell 字串文字只放在 `i18n.STRINGS`（zh/en）；`contracts.I18N_KEYS: frozenset[str]` 只列完整 key 清單，`record/*.py` 不放中文。涵蓋模式、機台/通道/V/I/週期/時長、開始停止/另存/載入、狀態六種、elapsed/records/gaps、preview/note/軸/檔名、全部錯誤，以及 ARB slew_warning。測試驗各 key 都存在於 i18n.STRINGS 且兩語 placeholder 相同，不在 contracts 複製翻譯；slew_warning 的欄位語意依 C9。J/K/G/A1 只引用它們或既有 key，缺字串回 T0。Viewer 自有字典依 V 的界線維護。新 web 檔案仍不得含 CJK。

## C11 H0 判定準則（逐字照抄 task-plan §5）

**PASS** 需要同時滿足：
1. 用 1 通道 1 ms、以及 4 通道 V+I 在最短週期（819.2 µs）兩種設定，每 250 ms 讀一次，USB 和 LAN 各連續錄 ≥ 1 h：
   - `records_received` 和 `records_produced` 的差不超過 1 個讀取間隔的筆數
   - 沒有發生溢位
   - 積壓超過 2 個讀取間隔的筆數時，從沒讀到 0 筆
2. `stall` 停 5 s 後，資料一筆不少。
3. `tail` 子項能判定兩種尾段策略中哪一種能拿到完整尾段。

**FAIL**：§4.0.2 的現象（積壓中讀到 0 筆，或回 4 bytes 空區塊）能重現，而且單改一個變數（首讀時機、格式、要求筆數）都避不掉。結果是走 B-DLOG。

**PASS 但需要調參數**：例如要求筆數必須小於 16384 才正常。這種情況把那個值寫進契約常數，然後走 B-ELOG。

上述 §4.0.2 指 HANDOFF 引用的舊專案紀錄，不是本文件章號。未滿 PASS 又未證明 FAIL 時為 INCONCLUSIVE，不能擅自選 B-ELOG 或打開旗標。H0 scripts 只產證據，實測由使用者做。

待 H0 回填常數（contracts 現值明確為暫定，不是量測結果）：

| 常數 | 預設 | H0 回填 |
|---|---|---|
| ELOG_TAIL | drain_then_abort | 能保留完整尾段的策略 |
| ELOG_FETCH_RECORDS | 16384 | 每次讀取的要求上限（必要時更小） |
| ELOG_FETCH_INTERVAL_S | 0.250 | 通過 USB/LAN 長錄的間隔 |

## C12 功能旗標

`RECORD_ENABLED=False` 是唯一來源，record_meta.enabled 下發；不加入 CONFIG_KEYS、不改 ANALYSIS_ENABLED/LIST_VERIFIED。實機 H0、HW-P2 和整合審查過後由使用者決定是否啟用，本輪不開旗標。

## C13 H0 FAIL 的 DLOG 上限

`DLOG_FALLBACK_MAX_BYTES=256*1024**2`，本契約將「256 MB」固定為 **268435456 bytes（256 MiB）**，含標頭與 8-byte 索引，僅 B-DLOG 生效。開始前保守估計 `ceil(duration_s/period_s)*n_fields*4 + 1 MiB + 8`；大於上限即 bad_config，不能先錄再期待記憶體夠。設定讀回實際 period/duration 後再估一次，不通過就不能 INIT。固定 sense_minmax=0；長度估計不含旁檔。

下載前若可查 remote 大小再驗 ≤上限，超限不讀整塊、不刪遠端檔；不得為驗大小先 MMEM:DATA? 載入整檔。無可靠大小查詢仍依預先界限，旁檔記下假設；真機 header 上界需 HW-P2 核實，不能聲稱已保證所有儀器檔案。ELOG 寫檔串流不受此 256 MiB 限制，但仍受 UInt32 筆數上限。串流 DLOG 下載留 P5。

## 各任務可改檔案（照 task-plan §7）

下表來源檔以 `src/n6705c_seq/` 為相對根；`core/` 特指 `src/n6705c/core/`。tests／docs／scripts／packaging 與頂層文件相對 repo 根。未列出檔案一律唯讀；不准 git add/commit/checkout、不准 editable 安裝。本契約不授權未開工波次。

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


`analysis/contracts.py` 跨波：T0 僅 OPEN_PUSH/MSG_FIELDS/MSG_TO_IFRAME/OpenPush/計時註解；G 僅 SELFTEST 兩 tuple。`core/fake.py` A1 補 ARB/slew、B-DLOG 才補真正 dlog；C1 故障注入不能改它。B-ELOG/B-DLOG 二選一共用 backend.py。webgui.py 僅 G 的 import 與 Api mixin 兩行；app.js 凍結，若不能完成回報 T0。

本表 R 的 wildcard 展開只有 docs/live-logging-review-codex.md 與 docs/live-logging-review-opus.md；backend 測試為 tests/record/test_backend_elog.py 或 tests/record/test_backend_dlog.py；G 的 test_record_gate.py/test_webgui.py 皆在 tests/；A1 省略 tests/ 前綴的同列檔案皆在 tests/。A3 等 HAs seqnum，不能提前推定 0/1 編號；X1 可選，不阻擋 writer。

## 驗收與未確認事項

T0 必跑全套 pytest（基準 838 過／7 跳過，只能增加）與 node --test tests/js（基準 26 過）。tests/record/test_record_contracts.py 以 AST 驗 contracts 僅 stdlib、record 原始碼不含 CJK、翻譯 key 存在且雙語 placeholder 相同、每個錯誤碼有 key，並保留 F12 往返證據。CJK/key、selftest、公用 Api、analysis 邊界既有測試必須仍綠。測試僅 fake/stub／本地資料，不跑真機、不送 *RST。

尚無 H0/HAs/14585A/Windows rename 實機證據；tail、讀取上限/間隔、overflow 時序、觸發因果皆不得寫成已驗證。雙審仍需另一位獨立審閱者完成；自我檢查與 pytest 不冒充另一份審查。

