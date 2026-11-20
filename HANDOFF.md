# N6705C-SEQ　交接文件

多台 N6705C 的**上電時序**工具。讀 CSV 表（每通道：延遲、低/高電壓、上升/維持/下降時間），在多台主機上依表上電。設計兩台（8 通道），程式不限台數。

最後更新：2026-11-20　`main` b43635e（PR #52 之後）。pytest 1768 過／7 跳過；`node --test tests/js/*.test.js` 90 過；GUI 是 pywebview。細節看 git log；本檔只留接手需要的事實。

| 區塊 | 狀態 |
|---|---|
| CSV 上電時序 | 單台（4× N6752A）單通道實機跑通；N6781A（CH2）實機驗證通過；多通道、帶載、跨台同步未量（§4、§14） |
| ARB List 模式 | 分頁已解鎖（`LIST_VERIFIED=true`）；N6781A 實機驗證 7 種波形、多段、重複、hold_last、感測線；E36441A 仍反灰；H（實機量測）／I 後半等設備（§9） |
| E36441A | 指令產生＋驗證＋實機驗證腳本已完成，核心假設未實機驗證（§10） |
| 報告分析（Viewer 第三分頁） | 已合併並啟用；Windows 11 實機未驗（§11） |
| 搜尋儀器下拉選單 | 已合併；未接實機、未在 Windows 操作（§12） |
| 即時紀錄（Viewer 內錄製） | 程式完成，`RECORD_ENABLED=False`；USB 單通道實機 H1–H7 通過，LAN／≥1 h／14585A 相容性未驗（§13） |
| 審查修復與停止流程 | 全專案審查四輪＋第五輪（Astra 4 項 WARNING）全部處理完（§14） |
| UI 主題（淺／深）與版面 | 已全部合併，使用者已驗證 GUI（§15） |

## 0. 先讀這幾件事

1. 儀器控制核心 `n6705c.core` **已複製進本專案**（`src/n6705c/core`），不依賴也不改舊專案 `../N6705C-APP`；核心修正要手動同步（§13.5）。
   - ⚠️ 同一 Python 環境**不要 editable 安裝 `n6705c-app`**：import 名稱同為 `n6705c`，會先找到舊專案那份。確認：`python3 -c "import n6705c; print(n6705c.__file__)"` 應指向本專案 `src/`。
2. 舊專案 `HANDOFF.md` 是儀器行為的權威來源：§3.1（`*TRG`）、§3.9（多機不同步）、§4.0（實機發現）。
3. 遠端：<https://github.com/vboys60-dev/N6705C-SEQ>（`main`）。換機器 `pip install -e ".[dev]"`。
4. 使用者用繁體中文；每個功能／Track 等使用者說「開工」才動程式碼；commit／push／PR／merge 都等使用者明說。
5. `hw_arb_results/` 是未追蹤的實機輸出資料夾，不要 commit。

## 1. 安裝與使用

```bash
pip install -e ".[dev]"           # pyvisa、pyvisa-py、pyusb、libusb-package、numpy、psutil、pywebview==6.2.1
PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider
node --test tests/js/*.test.js    # 要用 glob，目錄形式會失敗
n6705c-seq check examples/powerup.csv                       # 只驗證、印時間軸
n6705c-seq check plan.csv --suggest-out fixed.csv           # 跨台打平時另存建議分配
n6705c-seq run plan.csv --demo --yes                        # 模擬器
n6705c-seq run plan.csv --resource <VISA1> --resource <VISA2>   # 實機，依機台順序
n6705c-seq-gui                                              # GUI（或雙擊 N6705C-SEQ.command／.bat）
```

結束碼：0 正常；1 中止／錯誤；2 CSV 有誤；130 Ctrl-C。`run` 預設問 `[y/N]`，`--yes` 跳過。

## 2. CSV 格式

欄位：`enabled,name,delay_ms,low_v,high_v,rise_ms,hold_ms,fall_ms,current_limit_a,sense`（最後兩欄可省略／留空）。
- `current_limit_a` 留空＝不寫入限流，儀器沿用目前設定（**不是不限流**）；填了必須 > 0。數字須有限且 ≥ 0。
- `sense`：`""` 維持／`2wire`（`VOLT:SENS:SOUR INT`）／`4wire`（`EXT`）；HIGH 電壓頻寬＋2wire 會被拒；E36441A 列填感測線被擋。
- **列順序就是通道編號**：第 1–4 列＝機台 1 CH1–4，第 5–8 列＝機台 2。停用列要留著佔位。錯誤訊息指出 `line N, column X`。範例 `examples/powerup.csv`。

## 3. 架構

| 檔案 | 責任 |
|---|---|
| `src/n6705c_seq/plan.py` | `SeqRow`、`load_csv`／`write_csv`（`atomic_text`：目錄內唯一暫存檔再 replace）、`to_functions`、`order_warnings`、`suggest_assignment`、`timeline` |
| `src/n6705c_seq/execute.py` | `run_plan` → `run_functions`（CSV 與 List 共用，回傳 `GroupArbRunner`，其 `.stopped` 表示是否因停止結束）：驗證 → `CURR`／`VOLT` 並開輸出 → load → 每台一道 `INIT:TRAN`（`start_together`）→ 等待 → 關輸出；另有 `connect_group`、`PlanRejected`／`PlanStopped` |
| `listplan.py`、`synth.py`、`list_form.py`、`web_list.py`、`web/list.js` | List 模式（§9） |
| `e36441a.py` | E36441A：`prepare`／`configure`／`arm`／`restore_commands`、`check()`（§10） |
| `cli.py`、`webgui.py`、`web_seq.py`、`form.py`、`web/`、`i18n.py` | CLI；pywebview 殼、SEQ bridge、表單驗證、HTML／JS／CSS；字串 `STRINGS={key:(中,英)}` |
| `analysis/`、`record/`、`web_record.py` | Viewer 分析與即時紀錄（§11、§13）；`analysis/*` 禁止 import `n6705c.*`、`pyvisa`、`webgui`、`web_seq`、`web_list`、`execute`、`e36441a` |
| `scripts/hw_*.py` | 實機量測腳本（`hw_list_check`、`hw_e36441a_check`、`hw_elog_h0`、`hw_arb_trig_check`） |
| `packaging/`、`.github/workflows/ci.yml` | PyInstaller（Windows）、CI（3 OS × py3.11/3.13＋`build-windows`） |

每個通道 = 一個 `TrapezoidArb`：`start_level=low_v`、`start_time=delay`、`rise_time`、`top_level=high_v`、`top_time=hold`、`fall_time`、`end_time=0`、`terminate_last=True`。

## 4. 關鍵設計決定

- **延遲折進 `start_time`，`ChannelFunction.delay` 保持 0**：次數 1，兩處都設會延遲兩次（測試守著）。
- **`run_plan` 自己開輸出**：驗證（機台數、模組電壓／時間上限、耦合、閒置、電子負載、感測線頻寬）都在開任何輸出前完成；任何例外或 Ctrl-C 走 `all_outputs_off()`。`_outputs_off` 讀回 `OUTP?` 確認，未關或讀不到丟 `OutputsNotConfirmedOff`，不說「已強制關閉」。`OUTP?` 是命令狀態、不是實際軌壓。驗證被拒丟 `PlanRejected`。
- **跨台時間差軟體消不掉**：ARB 在 `INIT:TRAN` 當下就開跑、不等 `*TRG`（實機 F.1.1.0 量測：逐通道 INIT 再 `*TRG` 時各通道偏移就是 INIT 間隔；`TRIG:TRAN:SOUR` 對 ARB 無作用）。所以每台一道 `INIT:TRAN (@全部通道)`，同台差 < 0.15 ms。跨台各自 VISA 往返約 0.2–2 ms（`TRIGGER_SKEW_SECONDS=2e-3`，保守估計，**未量**）；`fire_together` 只縮小差距，真解法是接線。
- **跨台打平**：不同機台、延遲差 < 2 ms 的通道標警告。`suggest_assignment` 把打平的通道（連鎖也算）視為一組、同組同台、每台最多 4 個，塞不下改 first-fit-decreasing；無解時 `check` 不寫檔、`--suggest-out` 結束碼 1。⚠️ 使用者要自己把 DUT 電源軌換接到新通道。浮點：`_tied` 先 `round(...,9)`。
- **沿用舊專案的坑**：不支援的查詢會 **timeout**（`--timeout` 預設 10000 ms）；`OUTP?` 是命令狀態；fall 延遲讓「全部關閉」變慢；查不存在的通道會等逾時並留下 `-222`。
- **`*IDN?` 型號欄位在 N6752A（F.1.1.0）是固定亂碼**：`connect_group()` 把 pyvisa `encoding` 設 latin-1 繞過。
- **擷取實測波形**（`run_plan(capture={})`）：機台內建數位化器當示波器；`INIT:ACQ` 要在 ARB 啟動**前**，`start_together` 每台執行緒內 `INIT:TRAN` 後立刻 `*TRG`，等記錄完成**再** `OUTP OFF`；`FORM REAL`＋`FORM:BORD SWAP`，每通道 6000 點。取回失敗只記在 `capture["error"]`。模擬器擷取到平的 0 V，只能實機驗證。E36441A 無數位化器。
- **維持高點 DC**（`--hold-dc`／GUI 勾選）：忽略 hold／fall，改 `RampArb`，結束後**不關輸出**（出錯或停止仍強制關）。
- **韌體差異**：E.02.09 的 `TRIG:ARB:SOUR` 是整台共用，帶通道清單會 `-108`（`execute._set_arb_source_imm` 先試帶通道，遇 `-108` 改送不帶）；序列長度查詢是 `ARB:SEQ:LENG?`；`OUTP:COUP:CHAN?` 回不帶括號的逗號清單（`''`／`'1,2'`），設定也不帶括號、取消耦合送無參數。
- **GUI（pywebview）**：執行時才連線、跑完就關（VISA session 非執行緒安全）；工作執行緒是 daemon，**執行中拒絕關閉視窗**，JS 每 100 ms poll 事件；位址、Demo、語言、主題存 `~/.n6705c_seq_gui.json`（壞了用預設）。「■ 全部關閉」逐台獨立（`OFF_TIMEOUT_MS=3000`）、失敗彙整、有盲關退路。Windows 強制 Edge Chromium、啟動檢查 WebView2。`--selftest` 等 JS bridge ready 後關窗（成功 0、失敗或 30 s 逾時 1）。GUI 的 Python／JS 原始碼不得有 CJK，翻譯集中在 `i18n.py`（測試守著）。
- **GUI 自適應**：`html/body` 固定視窗高、`overflow:hidden`；內容超高時 `layout.js` `fitZoom(panel)` 以 CSS `zoom`（0.5–1）等比縮小。Sequence 圖表最小高 404px（8 通道×44px）。⚠️ 極低解析度與 Windows WebView2 在 CSS zoom 下的 canvas 尺寸／點擊位置未驗。
- **發佈（Windows 11）**：Release `N6705C-SEQ-<版本>-windows-x64.zip`（GUI exe＋`n6705c-seq-cli.exe`）；**USB 儀器需另裝 Keysight IO Libraries 或 NI-VISA**，LAN 走內含 `pyvisa-py`；無簽章。無 VISA 的 USB 路徑（pyusb＋libusb，需 Zadig）未實機驗。`packaging/build.ps1`：建置 → CLI `--demo` 與 GUI `--selftest` → 打包；推 `v*` tag 才建 Release（改 `pyproject.toml` version → `git tag vX.Y.Z && git push origin vX.Y.Z`）。目前最新為 `v0.2.0` pre-release。spec 已排除 `pyvisa_py.testsuite`；`build.ps1` 用 `Resolve-Path`。
- **原生 VISA 探測**（`transport._native_backend_safe()`）：某機器 `pyvisa.ResourceManager("")` 會讓行程當掉，改用子行程探測（8 s、快取），`backend="auto"` 不安全時只留 `@py`；入口需 `multiprocessing.freeze_support()`。
- **實機紀錄**：4× N6752A（F.1.1.0，單通道空接）時序正確、`TRAP:END:TIM 0` 可用、空接約 20 mV 偏移；N6705C MY65000515（E.02.09.3271）CH1=N6791A（**電子負載，不輸出電壓**，已被擋）、CH2=N6781A（SMU）。`DISP:VIEW SCOPe` 在 F.1.1.0 不支援。

## 5. 尚未驗證與下一步

**實機／平台（要人在機器前）**
1. 多通道、帶載、兩台、ms 級精度（需示波器）；**跨台同時 `INIT:TRAN` 的實際時間差**（填回 `TRIGGER_SKEW_SECONDS`，`src/n6705c/core/group.py`；差遠大於 2 ms 或需精準同步再做硬體接線）。
2. Windows 11：打包 exe、`N6705C-SEQ.bat`、錄製與互斥驗收（清單在 `HANDOFF_WINDOWS.md` §6、§8）；`test_save_waiting_for_target_lock_is_cancellable` 修法（PR #17）需在 Windows 重複跑約 30 次零失敗。
3. List 模式 H 實機量測（§9）、E36441A 實機（§10）、即時紀錄 H8（§13）。
4. GUI 停止鈕在 List／E36441A／擷取等待中的實機點擊；耦合通道被一起開啟的真實行為（僅模擬器）。

**程式面可做但不急（有需求再做）**
- settle 的 `time.sleep(SETTLE_SECONDS)` 不能被停止中斷（改 `stop.wait` 會讓 3 個 patch `time.sleep` 的測試失敗，須一併改）。
- ARB 下載（`runner.load()`）期間不逐通道檢查停止：要等這批下載完才關（逐通道檢查會繞過 patch `GroupArbRunner.load` 的測試）。
- 連線階段（`connect_group`／`detect_model`）卡 VISA 逾時時按停止沒有「停止中」回饋。
- `listplan.load_json` 與 `synth._validate` 驗證重複（分層防禦，有改動需求再合併）。
- 檢查與寫入之間的微秒級空隙、數位器 `INIT:IMM:ACQ` 無逐筆停止檢查（不驅動輸出）：不處理。

**不做**：打平當錯誤加 `--allow-skew`、YAML 設定檔、driver 抽象基底。

## 6. 工作方式

- 實作交 **Codex**（`gpt-6.1-sol`，`-c model_reasoning_effort=medium`；astra＝`gpt-6-astra`），審查／規劃交 **Opus**，Claude 驗收。`codex exec` 結尾**必加 `</dev/null`**（否則卡在 stdin）；審查用 `-s read-only`、實作用 `-s workspace-write`；macOS 無 `timeout`，用 `perl -e 'alarm N; exec @ARGV'`；`codex exec resume` 不支援 `-o`。
- Codex 沙盒禁 socket bind：pytest／node 一律在沙盒外重跑（`PYTHONPATH=src /opt/anaconda3/bin/python -m pytest -q -p no:cacheprovider`）。
- 平行實作：每任務 `git worktree add ../N6705C-SEQ-wt-Tn -b fix/Tn main`；worktree 內 pytest 用 `PYTHONPATH=src`；合併後刪 worktree 與分支。同波內任兩任務不得寫同一檔。
- 驗收：`git status` 確認範圍 → 對照合約 → 讀碼 → 探測邊界 → 跑完整測試（可用突變驗證）。
- 給 Codex 的限制要寫清適用檔案：曾因「註解不可含 CJK」說得過寬，刪掉 ~306 行 `viewer.html` 中文註解（已還原；僅 CJK 規則只適用外部 CSS／`seq.css`／`record.css`／`list.js`）。
- BSD sed 不支援 GNU 語法，批次替換用 Python；CI 結果先確認 `gh run list` 的 headSha 是最新 commit；macOS CI job 偶爾卡 15 分鐘（runner 問題，重跑）。

## 7. 其他文件

`README.md`（使用者說明）、`HANDOFF_WINDOWS.md`（Windows 散佈、驗收清單）、`docs/arb_list_mode*.md`、`docs/analysis-contract.md`、`docs/live-logging-*.md`、`docs/ui-theme-*.md`、`報告分析升級版/升級報告.md`；規劃類文件的舊段落編號（如 §9.3、§13.7、§14.5、§4.13）指向本檔改版前的章節，現在請看 §4、§6、§13、§14。

## 8. Codex／Opus 審查歷程（摘要）

第 1–2 輪（整專案，`e93c66a`、`8789f7d`）核准；Viewer 升級、即時紀錄各有雙審（報告 `docs/analysis-review-*.md`、`docs/live-logging-review-*.md`）；全專案審查四輪見 §14。

## 9. ARB List 模式

每通道一串波形段落，電腦端合成一條 CDW（65,535 點）下載。CSV 時序表不動，GUI 另開分頁。文件：`docs/arb_list_mode.md`（設計）、`docs/arb_list_mode_plan.md`（合約、規則 R1–R7、必測）。實作：`listplan.py`、`synth.py`、`list_form.py`、`web_list.py`、`web/list.js`，`execute.run_functions` 共用。

- 分頁鎖：`web/app.js` 的 `LIST_VERIFIED`（改 `false` 重新鎖，並把 `tests/test_web_seq.py` 末段對 `tab-list` 的斷言改回 `.disabled`）。
- 第一版不做頻寬警告（Triangle 沿用 Sine 門檻）。`DEMO_SLOTS` 為 N6752A。
- **H（實機量測，等設備）**：`python scripts/hw_list_check.py all --resource <VISA位址>`（開輸出前輸入 y；存 CSV 到 `hw_list_results/`；可單跑 `transfer`／`seam`／`align`／`sinebw`）。門檻：transfer ≤ 10 s（否則 dwell 規則改「符合 N 取最大 dwell」並考慮二進位傳輸）；align 的 spread 與 d_k − d_CH1 ≤ 1 dwell；seam 無空隙／突波；sinebw 求 −3 dB。
- **I 後半**（使用者另說開工才做）：回填 `SINE_BW_HZ`、`compile_plan(plan, model=None)`、Sine／Triangle 門檻、dwell 規則定案、版本 0.2.0 後續、README、Windows 打包。
- List 編譯輸出為 `ConstantDwellArb`，電壓跳變皆 Δt=0，slew 前檢視為 required=0、不警告。

## 10. E36441A 支援（無設備）

- 無 ARB，只有 FIX／STEP／LIST（LIST ≤ 100 點、dwell 10 ms–3600 s、解析 1 ms）；沒有上升時間參數，只有 `VOLT:SLEW:RIS/FALL`（V/s，0.002–INF）：以 `|ΔV|/rise` 換算，`high<low` 時 RIS／FALL 對調，0 或 ΔV=0 送 INF。SLEW 也作用於輸出開／關，是通道持久狀態。`OUTP:DEL:RISE` 不用；delay 用 `TRIG:DEL`。範圍 0–32 V、0–10 A、每通道 160 W、全機 400 W。資料表值（**未核實**）：上升 settling 約 80 ms、下降空載 500 ms，`check()` 對 rise < 80 ms、fall < 500 ms 警告。沒有「list 執行中」狀態位元，完成靠計時。
- 流程：`prepare_commands`（SLEW 先 INF 再 VOLT 到 low）→ 等穩定 → `configure_commands`（`TRIG:SOUR BUS`、`TRIG:DEL`、SLEW、LIST、`VOLT:MODE LIST` 最後）→ `arm_commands`（INIT）→ 每台一次 `*TRG`；`restore_commands`。`hold_dc` 時 LIST 只有 high 一點。已接 CLI／GUI：`*IDN?` 分派機型、混用機型在送任何指令前拒絕、GUI 隱藏擷取、關輸出逐通道回讀、執行前檢查 `INIT:CONT?`＝0、`OUTP:COUP:CHAN?`＝NONE、`OUTP:TRAC?`／`OUTP:PAIR?`＝OFF、`check()` 拒絕負值。
- **未驗證的假設（實機優先序）**：(1) `*TRG` 前 INIT 不會動；(2) **LIST 電壓切換會套用 SLEW**（不成立 → 改多點階梯）；(3) dwell 從切換點起算；(4) `TRIG:DEL` 精度、`VOLT:MODE LIST`＋`CURR:MODE FIX` 是否報 801、restore 順序；(5) 跨機 skew；(6) `OUTP:PAIR?` 回 `0` 會被拒、`INIT:CONT? (@n)` 是否接受通道清單。
- 待辦：有設備時 `python scripts/hw_e36441a_check.py --resource <VISA位址> [--csv out.csv]`（對**整台**送 `*RST`；IDN 不含 E36441A 拒絕；結束一律 ABOR／OUTP OFF／SLEW INF），依結果定案 `configure_commands`。

## 11. 報告分析（Viewer 第三分頁「離線分析報告」）

把 DLOG Viewer（Python server 版）做成 SEQ 第三分頁，給超過 1000 萬筆的 `.dlog` 用。PR #1–#4 已合併，`contracts.ANALYSIS_ENABLED=True`（`analysis/contracts.py`，改 `False` 隱藏分頁）。契約：`docs/analysis-contract.md`（數字與型別以 `contracts.py` 為準）；設計：`報告分析升級版/升級報告.md`。

- 架構：iframe＋同進程背景 HTTP server（`127.0.0.1` 隨機埠＋token）；開檔走原生對話框拿路徑再 mmap；下載走原生另存；偏好存 `analysis_prefs.json`（SEQ config 會整檔重寫，不共用）；不用 localStorage。
- **互斥（硬性）**：電源控制中不可分析，分析開著時不可有電源自動化。`analysis/gate.py` 六態仲裁 `idle | power | analysis | draining | closing | fault`（帶世代、compare-and-set；`closing` 終態、`fault` 只能走向 `closing`；逾時不釋鎖）；不靠前端灰按鈕。
- 測試注意：`test_webgui` 掃 `web/*` 不准有中文，Viewer HTML 放 `analysis/web/`；`js_api` 有公開方法白名單測試；`original_before_python.html`（`tests/analysis/parity_tools/`）是搬移前基準檔，勿動。
- 已知取捨：CSV 與原 JS 容許最後一位差 1；分析中來源檔被截短在 mac／Linux 會 SIGBUS；CSV 匯出以點擊當下的設定快照為準（使用者決定維持）。
- Windows CI 曾因測試假設（mmap 不能刪／截短、cp1252、CRLF）失敗，已修（`pytest-timeout`、`.gitattributes -text`、win32 skip）；專案為公開 repo。
- 待辦：Windows 11 實機驗收（`HANDOFF_WINDOWS.md` §6，16 項）。

## 12. 搜尋儀器下拉選單

按「搜尋儀器」列出 `N6705C  S/N …  (USB0::...)`，點選填入位址；Demo 不掃描。流程：`web_seq.scan_instruments` → power token → `transport.scan_resources`（子行程，20 s）→ `["scan", [[addr, label],...]]` → `seq.js`。

- **別改壞**：候選後端要在**父行程**算好再傳進 daemon 子行程（子行程內 `VisaTransport` 要指定明確後端，不可 `auto`）；序列埠（ASRL）要在送 `*IDN?` **之前**濾掉；只送 `*IDN?`；用 `<select>` 不用 `<datalist>`；地址清單到手後逾時要回傳部分結果。
- `list_resources` 合併所有後端（原本第一個成功就停）；新依賴 `psutil`（pyvisa-py 廣播 VXI-11 需要）；沒裝 `zeroconf`，探索不到 HiSLIP。
- 已知限制：LAN 廣播跨不了子網路；同一台可能出現兩筆；列舉階段有後端卡住時該次其他後端結果會丟失。
- **未驗**：實機 LAN 廣播與 USB 列舉、Windows 原生 VISA 當機路徑、打包 exe 的子行程掃描。沒搜到就對照 `HANDOFF_WINDOWS.md` §3.4。

## 13. 即時資料紀錄（Viewer 分頁內錄製）與 ARB 修補

規劃與契約：`docs/live-logging-contract.md`（C1–C13）、`docs/live-logging-task-plan.md`。Wave 0–5、B-DLOG、B-ELOG、審查修正全部合併（PR #11–#29）。**`record/contracts.py` 的 `RECORD_ENABLED=False`，使用者看不到此功能**；`RECORD_BACKEND="elog"`（可切 `"dlog"`），GUI 不可選。打開旗標需使用者決定（建議先有 H8 結果）。

### 13.1 決策與架構
- 錄製放進 Viewer 分頁，分「檢視／錄製」兩模式互斥；錄製拿 power token（沿用 `power` 狀態），停止後釋放並把 `.dlog` 載入同分頁；錄製程式放 `analysis/` 之外（`record/`、`web_record.py`）經 bridge 呼叫。按「開始」時跳原生另存對話框。
- ELOG 串流為主（單一 worker 做全部 VISA I/O；停止只設旗標，同 worker 送 `ABOR:ELOG` 後才釋放 token；每步讀 `SYST:ERR?` 到 0，`+310` 表示不支援 autorange）；DLOG 為退路（錄完下載，真機錄製中**沒有即時曲線**：MEAS 被拒 `+319`，backend 真機不送 MEAS 預覽，`preview=False`）。
- 圖：JS 每 250–500 ms 向 Python 取快照（每條 ≤2000 點 min/max 包絡）；全解析度直接寫檔。存檔為 `.dlog` 相容格式、每台一檔，旁檔 `.dlog.json` 記要求值／回讀值、IDN、notes、gaps、UTC、`complete`；錄製中用 `.part`；發布順序 `complete=False` 旁檔 → `os.replace` → `complete=True` 旁檔；Windows `os.replace` 遇 winerror 5／32／33 重試（`replace_with_retry`）；開始時記正式檔身分、發布前比對（有極短競態窗口，非跨行程鎖）。
- `.dlog` 格式：XML 標頭（`</dlog>\n` 結尾）→ 兩個大端 UInt32（trigger、write）→ 大端 float32 資料，欄位每通道先 V 後 I；原生檔索引 trigger=`0xFFFFFFFF`、write 不等於筆數時以位元組長度定筆數（Viewer `parse_dlog` 與 backend 已改讀兩個 `>I`，其他非零 trigger 拒絕）。環形排序、前觸發、`sense_minmax=1` 無樣本，未驗。
- Gate：`fault` 且 worker 仍活著時擋關窗（`on_closing` 回 `"power"`），worker 卡死則視窗關不掉、只能強殺（刻意取捨）。
- 缺口記法：`overflow`／`empty_backlog`／`tail_limit`／`tail_incomplete` 各記一筆 gap，`fetch_error` 只寫 notes；缺口數不等於遺失次數。

### 13.2 ARB 修補（已合併）
- `start_together` 在 `INIT:TRAN` 前送 `TRIG:ARB:SOUR IMM`（A1，實機驗證：殘留 BUS 會擋住 INIT 起跑，IMM 後 INIT 約 5 ms 起跑、不需 `*TRG`）；`multi.py` 回滾補當前通道、文件修正；slew 前檢（`slew_warnings()`：僅 override=0 且 rate<required 警告，rate≥1e37 視為不限制，瞬間跳階不計）。
- 待 A3（使用者裁決）：sequence 步驟編號官方 0..99、程式送 1..100（`arb/sequence.py`）；`seqnum` 實機證據偏向從 0 起算，單台單次。
- `hw_arb_trig_check.py` 的 `TRIG:ARB:SOUR?`／帶通道寫法在 E.02.09 仍會失敗（CH1 是負載，波形類檢查只能在 CH2 做）。

### 13.3 實機結果（2026-11-18，USB，CH2=N6781A，E.02.09.3271）
- **ELOG 可用**：先前空區塊根因是 ELOG 自動量程（`RANG:AUTO ON` 之後 `FETC:ELOG?` 只回空區塊）；`configure()` 未指定量程時改送 `AUTO OFF`＋`RANG MAX`。
- H0 短測：1 ms、900 s，收 896,845 筆（差 7），速率 996.43–996.55 筆／s，無錯誤。**停止後尾段讀不到**（abort 後 fetch 回 `+303`）→ 停止流程為「drain 到空 → `ABOR:ELOG`」，結尾少 1–2 筆為已知限制；`fetch_tail` 在這台無用。
- B-ELOG H1–H7 全部通過：1 ms／10 ms 各 60 s、真 `RecordApi`（開始→running 0.11 s、停止→done 0.05 s、snapshot 遞增）、`duration` 自動結束、starting 階段停止無殘留、900 s 無 gap、通電 0.5 V 錄 10 s（V 平均與 MEAS 差 ≤ 0.007 mV）。DLOG 週期量化到 10.24 µs 格（要 1 ms 得 1.00352 ms，backend 允許一格差異）。
- **風險**：長錄時鐘漂移（實際速率比 1/period 慢約 8.6 ppm，空區塊隨時間增加），≥1 h 可能誤報 `empty_backlog`／`tail_incomplete`；INIT 傳輸逾時後儀器狀態、無 INIT 時 `ABOR:ELOG` 是否報錯、60 s 大週期格點量化未確認；`hw_elog_h0.py` 的 `elog_arb` 通電探測尚需改成 N6781A 可用（`CURR` 需 current priority）。

### 13.4 H8（待條件具備，各自記錄）
LAN ≥15 min、≥1 h（含 H0 ≥4 h、USB＋LAN）、14585A 開檔（HW-P2）、Windows exe 錄一輪（`HANDOFF_WINDOWS.md` §8）、macOS 旗標暫開手動點過兩種模式、`scan_resources` 找不到 TCPIP 位址需先解。已知缺口：原生 `.part` 無法被 `recover_part()` 復原；覆寫舊檔時第二次 replace 失敗，舊 `complete=True` 旁檔會被 `complete=False` 取代（比配錯安全）。

### 13.5 core 同步回 `../N6705C-APP`（等使用者開口）
APP 是手動副本（非 git）、只能 additive 合入，不要整目錄覆蓋，也不要對 APP 用 git。從本 repo 唯讀核對：

```bash
git log --oneline d6b9d2a..HEAD -- src/n6705c/core/
git diff d6b9d2a..HEAD -- src/n6705c/core/
diff -u ../N6705C-APP/src/n6705c/core/<file> src/n6705c/core/<file>
```

自 `d6b9d2a` 起動到 core 的重點：`dlog.py`（`start()` 的 ARMED／BUS／IMM 處理、`retrieve()` 逐檔追蹤只刪已成功下載者、`wait()` 只在 RUNNING 輪詢）、`elog.py`（`ElogSinkError`／`ElogFetchError`、嚴格 `SYST:ERR?` 解析、`_request_autorange` 容忍 +310、`fetch(max_records)`／`fetch_tail`、`ElogRecorder.run()` 阻塞式與 tail／on_event／on_cleanup、`stop()` 只在 ABOR 成功才 STOPPED、`ElogAlignmentError`）、`fake.py`（slew／ARB source／真格式 `.dlog`）、`arb/multi.py`（`arm()` 回滾含當前通道、文件修正）、`arb/runner.py`（`ARB:SEQ:LENG?`）、`session.py`（`read_errors` 嚴格化）、`instrument.py`／`group.py`（連線建構失敗關閉 transport，PR #47）、`transport.py`（`scan_resources`、原生 VISA 探測）。

## 14. 全專案審查與修復（PR #31–#52）

流程：Codex（sol／astra）審查 → Opus 複核降級 → 任務拆分 → Codex 各開 worktree 並行 → Opus 驗收（突變驗證）→ 合併。

**已修**
- 開輸出前檢查：耦合（`_check_coupling`，接受 `(@1,2)` 與 `1,2`，查詢／解析失敗一律拒絕）、重複機台（位址＋IDN 製造商／序號）、`MAX_FRAMES`、電子負載（N6791A／N6792A，`capability.is_load`）、閒置（`_check_idle`：`STAT:OPER:COND?` 有 WTG-tran／TRAN-active 或 `INIT:CONT:TRAN?`＝1 → 拒絕，不自動 ABOR）；先寫 `CURR` 再寫 `VOLT`。
- 關斷與連線：「全部關閉」逐台獨立＋盲關退路；`all_outputs_off` 加 `ABOR:ACQ`；group／transport 建構失敗回滾且不重複關閉（PR #47，`InstrumentGroup._build`、`from_transport` 探測失敗關 transport）；**不再 `rm.close()`**（pyvisa 共用一個 ResourceManager）。
- `Session.read_errors` 嚴格化（csv strict、兩欄、整數 code、最多 50 筆，否則 `ErrorQueueError`）；ELOG `init_attempted`、`stop()` 重試語意。
- 存檔：補副檔名後再確認覆寫；`atomic_text` 用唯一 `.<名>.<uuid>.tmp` 以 `"x"` 排他建立（PR #47，重疊儲存不互踩、不碰既有 `.tmp`）；DLOG 上限 359_996_400 s（`dlog.py` 與 `record/contracts.py` 各留常值，測試保證相等）；DLOG 尾端不完整改 log 警告、不拋錯。
- **停止涵蓋整個執行週期**：SEQ／List 都有「■ 停止」鈕（`seq_stop` 設共用 Event；List 啟動必須自己 `clear()`）；`_check_stop` 在每筆設定寫入前、`OUTP ON` 前、ARB 下載前、觸發來源設定後每台執行緒的 `INIT:TRAN`／`*TRG` 前，E36441A 在 prepare／configure／arm 每筆寫入前與 `*TRG` 前（PR #41、#48）；停止丟 `PlanStopped`（`PlanRejected` 子類，PR #49），worker 先接住並顯示「已停止」資訊訊息（不再顯示拒絕）；`run_functions` 以 `runner.stopped` 回報「因停止而關輸出」，List／SEQ 完成訊息優先判斷停止（PR #43、#46；E36441A 路徑用 `self._stop.is_set()`）。擷取等待 `stop.wait`；`fire_together` 例外時先 join 已啟動執行緒再重拋。
- 感測線欄（PR #37）、預覽「限流留空」警告（PR #36）。

**已知限制（刻意不修）**
- ELOG `session.stop()` 重試仍失敗時 `web_record` 仍 `_release`（ELOG 只量測不驅動輸出）。
- 停止檢查與實際寫入間仍有微秒級空隙；多台同時啟動時可能 A 台已送 `INIT`、B 台才檢查到停止（結果仍是例外＋全部關輸出）。
- 停止後 settle 期間（1 s）要等 sleep 結束才中止，之後不再送 LIST／INIT／`*TRG`。
- 本機若有 `test/record-enabled`（`RECORD_ENABLED=True`）分支不得合併；新工作一律從 `main` 開。

**實機確認（2026-11-19，N6705C E.02.09.3271）**：耦合拒絕、`INIT:CONT:TRAN` 閒置拒絕、CH2 正常執行＋擷取、電子負載被擋、停止鈕後端（1.5 s、`OUTP OFF`、錯誤佇列乾淨）、List 與 SEQ 感測線（INT／EXT 切換生效；HIGH1＋2wire 被拒、4wire 正常）。

## 15. UI 主題與版面改版（PR #44–#52，已完成，使用者已驗證 GUI）

計畫：`docs/ui-theme-plan.md`、`docs/ui-theme-parallel.md`。

- 淺色／深色主題，殼層與 Viewer 同步：`style.css` 的 `/* theme-tokens:begin */…end` 區塊是單一來源（淺在 `:root`、深在 `:root[data-theme="dark"]`，含 `--ch1..8`、`--plot-*`、`--accent`），`viewer.html` 內聯同值（`tests/analysis/test_viewer_tokens.py` 比對）；`tests/test_css_tokens.py` 保證四支 CSS 無寫死顏色、`tests/test_theme_contract.py` 凍結全部 token 色值。
- 機制：`<html data-theme>`、`App.on("theme")`／`emit("theme")`、`App.cssVar(name, fallback)`；iframe 訊息 `{v:1,type:"theme",gen,theme}` 與 viewer URL `&theme=`（`server.url` 對非法值丟 `ValueError`）；設定檔 `theme`（`light`／`dark`／`null`，`null` 首次啟動依 `prefers-color-scheme` 決定一次、不寫回）；`set_theme` 在 `SELFTEST_JS_API_DENIED`，失敗由按鈕 catch 並顯示錯誤、主題不變。`_save_config(resources=None, demo=None, *, theme=None)` 省略的欄位在 `_config_lock` 內讀目前值，`set_lang`／`set_theme` 不會用舊資料覆蓋其他儲存。
- Viewer 只剩 dark／light（soft-dark 與下拉已刪；server `_THEMES` 與 `/prefs` 不變，舊 prefs 的 `soft-dark` 仍合法）；圖表黑底色盲友善配色；圈選框讀 `--accent`；`getComputedStyle` 在測試中只回 fontFamily，所以 canvas 取色要有 fallback。
- 通道色：SEQ／List／Record 取 `--chN`，`form.PALETTE` 只當後備；SEQ 使用者改過的列（`dataset.custom`）切主題不被覆蓋；CSV 無顏色欄位。比對檔（MEM）顏色刻意不隨主題變；白底列印（`printColor`）與報告色點會把深色主題預設色與比對檔螢光色（`COMPARE_PRINT_COLORS`）映成深色版，負片模式用 `invertHex`。
- 版面：Viewer 滿版 grid、連線區單列 `.connbar`、分頁膠囊。
- 實機視覺已由使用者驗證。未驗：Windows WebView2 的 `prefers-color-scheme`、`<input type=color>`、`zoom`。
