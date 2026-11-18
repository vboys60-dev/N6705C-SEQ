# N6705C-SEQ　交接文件

多台 N6705C 的**上電時序**工具。讀 CSV 表（每通道：延遲、低/高電壓、上升/維持/下降時間），在多台主機上依表上電。只做時序，不做量測。設計兩台（8 通道），程式不限台數。

最後更新：2026-11-17　狀態：模擬器全過（pytest 838 過／7 跳過）；GUI 已換成 pywebview。細節看 git log；本檔只留接手需要的事實。

| 區塊 | 狀態 |
|---|---|
| CSV 上電時序 | 單台（4× N6752A）單通道實機跑通（§4.8）；多通道、帶載、跨台同步未量 |
| GUI 版面 | 整頁不捲動：`html/body` 固定視窗高、`overflow:hidden`；內容超高時分頁以 CSS `zoom` 等比縮小（§4.13，`b77a6e6`） |
| ARB List 模式 | **分頁目前反灰鎖定**（`app.js` `LIST_VERIFIED=false`，未實機驗證）；P0、C、A、D、H 前半、I 前半、D-EN、GUI 美化＋中英切換已 commit；等實機跑 H（§9） |
| E36441A | 指令產生＋驗證＋實機驗證腳本已 commit（`1cfbe76`、`dca33f1`），未接 CLI／GUI，核心假設未實機驗證（§10） |
| Codex 審查 | 第 2 輪已核准（§8） |
| 報告分析升級版 | T0–T8 完成，PR #1 已合併，分頁已啟用；Windows 11 實機未驗（§11） |
| 搜尋儀器（下拉選單） | PR #8 已合併 `c7461c6`；離線與 CI 通過，**未接實機、未在 Windows 實際操作**（§12） |
| 即時資料紀錄＋ARB 修補 | **規劃與任務表完成，尚未開工**（§13、`docs/live-logging-task-plan.md`） |

## 0. 先讀這幾件事

1. 儀器控制核心 `n6705c.core` **已複製進本專案**（`src/n6705c/core`），不依賴也不改舊專案 `../N6705C-APP`；核心修正要手動同步。
   - ⚠️ 同一 Python 環境**不要 editable 安裝 `n6705c-app`**：import 名稱同為 `n6705c`，會先找到舊專案那份 core。確認：`python3 -c "import n6705c; print(n6705c.__file__)"` 應指向本專案 `src/`。
2. 舊專案 `HANDOFF.md` 是儀器行為的權威來源：§3.1（`*TRG`）、§3.9（多機不同步）、§4.0（實機發現）。
3. 遠端：私人 repo <https://github.com/vboys60-dev/N6705C-SEQ>（`main`）。換機器 `pip install -e .`。

## 1. 安裝與使用

拉取本次變更後請重新執行 `pip install -e ".[dev]"`，舊的 `n6705c-seq-gui` console script 仍指向已移除的 `n6705c_seq.gui`。

```bash
pip install -e ".[dev]"           # pyvisa、pyvisa-py、pyusb、libusb-package、numpy、pywebview==6.2.1
python3 -m pytest -q              # 576 passed
node --test tests/js              # 5 passed
n6705c-seq check examples/powerup.csv                       # 只驗證、印時間軸
n6705c-seq check plan.csv --suggest-out fixed.csv           # 跨台打平時另存建議分配
n6705c-seq run plan.csv --demo --yes                        # 模擬器
n6705c-seq run plan.csv --resource <VISA1> --resource <VISA2>   # 實機，依機台順序
n6705c-seq-gui                                              # GUI（或雙擊 N6705C-SEQ.command，路徑寫死 /opt/anaconda3）
```

結束碼：0 正常；1 中止／錯誤（或 `--suggest-out` 無解）；2 CSV 有誤；130 Ctrl-C。`run` 預設問 `[y/N]`，`--yes` 跳過。

## 2. CSV 格式

欄位：`enabled,name,delay_ms,low_v,high_v,rise_ms,hold_ms,fall_ms,current_limit_a`（最後一欄可省略／留空＝不限流）。
- **列順序就是通道編號**：第 1–4 列＝機台 1 CH1–4，第 5–8 列＝機台 2。停用列要留著佔位。
- 錯誤訊息指出 `line N, column X`。範例 `examples/powerup.csv`（CH7、CH8 停用）。

## 3. 架構

| 檔案 | 責任 |
|---|---|
| `src/n6705c_seq/plan.py` | `SeqRow`、`load_csv`／`write_csv`、`to_functions`、`order_warnings`、`suggest_assignment`、`timeline` |
| `src/n6705c_seq/execute.py` | `run_plan` → `run_functions`（CSV 與 List 共用）：驗證 → `VOLT`／`CURR` 並開輸出 → load → 每台一道 `INIT:TRAN` 同時啟動（`start_together`）→ 等待 → 關輸出。另有 `connect_group` |
| `src/n6705c_seq/listplan.py`、`synth.py`、`list_form.py`、`web_list.py`、`web/list.js` | List 模式：JSON 計畫、段落→CDW 合成、表單驗證、GUI 分頁（§9） |
| `src/n6705c_seq/e36441a.py` | E36441A：`prepare`／`configure`／`arm`／`restore_commands`、`check()` 純函式（§10） |
| `src/n6705c_seq/cli.py`、`webgui.py`、`web_seq.py`、`form.py`、`web/`、`i18n.py` | `check`／`run`；pywebview GUI 殼、上電時序 bridge、表單驗證、HTML／JS／CSS；GUI 字串 `STRINGS={key:(中,英)}` |
| `scripts/hw_list_check.py`、`scripts/hw_e36441a_check.py` | 實機量測腳本（§9.2.3、§10） |
| `packaging/`、`.github/workflows/ci.yml` | PyInstaller（Windows）、CI（§4.12） |

每個通道 = 一個 `TrapezoidArb`：`start_level=low_v`、`start_time=delay`、`rise_time`、`top_level=high_v`、`top_time=hold`、`fall_time`、`end_time=0`、`terminate_last=True`。

## 4. 關鍵設計決定

- **4.1 延遲折進 `start_time`，`ChannelFunction.delay` 保持 0**：次數 1，兩處都設會延遲兩次（測試守著）。
- **4.2 `run_plan` 自己開輸出**：驗證（機台數、模組電壓／時間上限）都在開任何輸出前完成；任何例外或 Ctrl-C 走 `all_outputs_off()`。`_outputs_off` 讀回 `OUTP?` 確認，未關或讀不到丟 `OutputsNotConfirmedOff`，CLI／GUI 不說「已強制關閉」。`OUTP?` 是命令狀態、不是實際軌壓。驗證被拒丟 `PlanRejected`（訊息「開輸出前被拒絕」）。
- **4.2.2 輸入驗證**：數字須有限且 ≥ 0；`current_limit_a` 填了必須 > 0（0 與「不限制」無法區分）；GUI `form.parse_rows` 與 `load_csv` 規則一致。
- **4.3 跨台時間差軟體消不掉**：實機（F.1.1.0）ARB 在 `INIT:TRAN` 當下就開跑、不等 `*TRG`（§4.10），所以每台一道 `INIT:TRAN (@全部通道)`。跨台各自 VISA 往返，抖動約 0.2–2 ms（`TRIGGER_SKEW_SECONDS=2e-3`）；`fire_together`（每台一條執行緒＋Barrier）只縮小差距。真解法是接線（舊 HANDOFF M11.3）。
- **4.4 跨台打平**：不同機台、延遲差 < 2 ms 的通道標警告（同台沒事）。`suggest_assignment` 把互相打平的通道（連鎖也算）視為一組、同組同台，每台最多 4 個，塞不下改 first-fit-decreasing；無解時 `check` 印 `No frame assignment ...`、不寫檔、`--suggest-out` 結束碼 1。第 1 台沒滿時補停用備用列，讓第 2 台仍從第 5 列開始。⚠️ 使用者要自己把 DUT 電源軌換接到新通道。
- **4.5 浮點陷阱**：`0.031-0.029` 是 `0.00199999…`；`_tied` 先 `round(...,9)`（測試 `test_a_gap_of_exactly_the_skew_is_not_a_tie`）。
- **4.6 沿用舊專案的坑**：不支援的查詢會 **timeout**（`--timeout` 預設 10000 ms）；`OUTP?` 是命令狀態；fall 延遲讓「全部關閉」變慢。
- **4.7 GUI**：pywebview；執行時才連線、跑完就關（VISA session 非執行緒安全）；工作執行緒是 daemon，**執行中拒絕關閉視窗**（closing veto），JS 每 100 ms poll 工作事件。位址、Demo、語言仍存 `~/.n6705c_seq_gui.json`（壞了用預設）；沒有「停止」鈕；「■ 全部關閉」連到已填位址並回讀確認（Demo 只提示、執行中停用）。Windows 強制使用 Edge Chromium，啟動後檢查 WebView2；缺少時顯示下載網址與離線 Standalone Installer 提示後退出。macOS 使用系統 WebKit；Linux GUI 未測。`--selftest` 等 JS bridge ready 回報後關窗，成功結束碼 0，失敗或 30 秒逾時為 1；建置用 `Start-Process -Wait -PassThru` 執行 `N6705C-SEQ.exe --selftest`。視窗、DPI、字型與實機流程由 Claude／使用者驗收。

### 4.8 實機測試紀錄（2026-10-12，單台、CH1、空接）
- 機台 `USB0::0x2A8D::0x0F02::MY66102013::INSTR`，4× **N6752A**，韌體 `F.1.1.0`。**`*IDN?` 型號欄位是固定亂碼**，ASCII 解碼會丟 `UnicodeDecodeError`；`execute.connect_group()` 把 pyvisa `encoding` 設 latin-1 繞過（連線會印一行「not an N6705 family mainframe」，可忽略）。
- `MEAS:VOLT?` 取樣（~40 Hz）：0.5 s 延遲、1 s 上升到 1 V、2 s 維持、1 s 下降皆正確，**`TRAP:END:TIM 0` 在 N6752A 可用**，錯誤佇列乾淨。空接 0 V 時讀到約 20 mV（偏移）。
- **機台內建數位化器可當示波器**（`docs/hw_smoke_capture.png`）：`SENS:SWE:POIN 6000`、`SENS:SWE:TINT 40.96E-6`、`TRIG:ACQ:SOUR BUS`，在 `run_plan` **之前** `INIT:IMM:ACQ`，同一 `*TRG` 同啟動 ARB 與擷取，再 `FETC:ARR:VOLT?`。`examples/hw_smoke.csv` 實測交越差 < 0.6 ms。這只量同台，量不到跨台差。`FETC:ARR:CURR?` 需先啟用電流擷取（沒做）。
- `DISP:VIEW SCOPe` 在 F.1.1.0 不支援（-224）；`HCOP:SDUM:DATA?` 可回 320×240 PNG。

### 4.9 GUI 擷取實測波形
`run_plan(capture={})`／GUI 勾選：實測電壓以黑線疊在預期波形上（`execute._arm_capture`／`_fetch`）。擷取在 `start_together` 每台執行緒內於 `INIT:TRAN` 後立刻 `*TRG`（同台差 < 0.15 ms）；`INIT:ACQ` 要在 ARB 啟動**前**，要等記錄完成**再** `OUTP OFF`；`FORM REAL`＋`FORM:BORD SWAP`，每通道 6000 點。取回失敗只記在 `capture["error"]`。模擬器擷取到平的 0 V，只能實機驗證。

### 4.10 實測：ARB 在 INIT 當下就開跑
逐通道 `INIT:TRAN` 再一個 `*TRG` 時，CH1/2/3 相對 `*TRG` 偏移 −36／−14／−1 ms（就是各通道 INIT 間隔）；不送 `*TRG` 輸出照升，`TRIG:TRAN:SOUR`（PIN1／EXT）對 ARB 無作用。修法 `start_together`：每台一道 `INIT:TRAN (@…)`，不再送 `*TRG`。實測 3 通道 50% 點在設定值 0.15 ms 內。

### 4.11 維持高點 DC（`--hold-dc`／GUI 勾選）
忽略 hold／fall，改 `RampArb`，`terminate_last` 留高點；`run_plan(hold_dc=True)` 結束後**不關輸出**（出錯仍強制關）。實機 3 s 後仍 1.0004 V，斷線重連後輸出仍開。

### 4.12 發佈與打包（Windows 11）
- Release 的 `N6705C-SEQ-<版本>-windows-x64.zip`：`N6705C-SEQ.exe`（GUI）、`n6705c-seq-cli.exe`。**USB 儀器需另裝 Keysight IO Libraries 或 NI-VISA**；LAN 走內含 `pyvisa-py`。無 VISA 的 USB 路徑（pyusb＋libusb DLL，`transport._register_libusb()`，Windows 需 Zadig 換 WinUSB）**未實機驗證**。無簽章，SmartScreen 會警告。
- `packaging/build.ps1`：建置 → CLI `--demo` 與 GUI `--selftest` 自測 → 打包。CI：Windows／macOS／Ubuntu × Python 3.11、3.13；推 `v*` tag 才建 Release。發版：改 `pyproject.toml` version → `git tag vX.Y.Z && git push origin vX.Y.Z`。
- 2026-10-12 Windows 11 本機打包成功、exe 能開、`--demo` 輸出正確；**未接實機**。spec 已排除 `pyvisa_py.testsuite`（否則 PyInstaller 分析當機）；`build.ps1` 用 `Resolve-Path` 絕對路徑（UNC 工作目錄下相對路徑會被誤判成模組）。
- **原生 VISA 探測**（`transport._native_backend_safe()`）：該機 `pyvisa.ResourceManager("")` 會直接讓行程當掉（`STATUS_STACK_BUFFER_OVERRUN`，系統只有 IVI 轉發 DLL、沒有廠商實作），所以用子行程探測（逾時 8 s、結果快取），`backend="auto"` 在不安全時只留 `@py`；入口檔需 `multiprocessing.freeze_support()`。舊專案若有同一份 `transport.py` 建議手動同步。

### 4.13 GUI 自適應（不出現整頁捲軸）
- `style.css`：`html, body {height:100%}`、body 為 flex column＋`overflow:hidden`，`#seq-tab`、`#list-tab` 為 `flex:1; min-height:0`。
- `layout.js` 的 `fitZoom(panel)`：先 `zoom=1` 量 `scrollHeight`，再設 `zoom = clamp(clientHeight/scrollHeight, 0.5, 1)`；隱藏分頁不動。Sequence 在 `seq.js`（觀察 `#seq-tab`），List 在 `list.js`（觀察 body、`drawDraft` 末尾、root 寬度變化後）呼叫。
- Sequence 圖表 `flex:1`、**最小高度 404px**（8 通道 × 44px＋座標軸）。低於 44px/通道時 `seq.js` 會隱藏「延遲／持續」標籤、波形範圍也會壓平，所以不能再縮小圖表，改用整體縮放。
- List 畫布是寫死像素高度（lanes 150、draft 190 起），因此只能用縮放；真正自適應需重寫繪圖。
- ⚠️ 已在 macOS 目標視窗看過正常；**極低解析度（縮到 0.5 倍）與 Windows WebView2 下畫布尺寸（`clientWidth`）在 CSS zoom 的行為未驗證**，若畫面模糊或滑鼠點擊位置偏移，先查這裡。

## 5. ⚠️ 尚未驗證

1. 多通道、帶載、兩台、ms 級精度（需示波器）。
2. **跨台同時 `INIT:TRAN` 的實際時間差**沒量過（同台 < 0.15 ms），2 ms 是保守估計。
3. 建議分配只看起點先後，不處理維持與下降階段；不知道哪條電源軌接哪個通道。
4. GUI 版面只在 macOS 看過（自適應縮放見 §4.13）；Windows 11 執行檔只在 CI 與本機打包驗過。
5. `fire_together` barrier 逾時 5 s、各台 `*TRG` 無整體逾時，上限靠 VISA `--timeout`。

## 6. 沒做、也沒被要求

打平當錯誤並加 `--allow-skew`；量測主機端兩台 `*TRG` 實際間隔；硬體同步接線（M11.3）；量測／記錄；YAML 設定檔。

## 7. 建議的下一步

1. 兩台各一通道量跨台起點差，填回 `TRIGGER_SKEW_SECONDS`（`src/n6705c/core/group.py`）；差遠大於 2 ms 或需精準同步再做硬體接線。
2. List 模式：有設備時跑 H 實機量測，再開工 I 後半（§9.2.3）。
3. E36441A：有設備時跑 `scripts/hw_e36441a_check.py`，依結果定案 configure_commands（§10.4；CLI／GUI 已接）。

## 8. Codex 審查

- 第 1 輪（整個專案）：3 CRITICAL、6 WARNING，已修 8 項（`e93c66a`）。
- 第 2 輪（`8789f7d`）：`_outputs_off` 要求回傳通道＝`populated_channels`、新增 `PlanRejected`、`_wait` 查詢失敗讓整個執行失敗、GUI 設定檔型別檢查、逾時訊息改全域通道號。`VERDICT: APPROVED`。

## 9. ARB List 模式（2026-10-13 起）

**GUI 分頁目前鎖定反灰**：H 實機驗證前不開放。啟用：`web/app.js` 的 `LIST_VERIFIED` 改 `true`，並把 `tests/test_web_seq.py` 末段對 `tab-list` 的斷言改回 `!…disabled`。

每通道一串波形段落，電腦端合成一條 CDW（65,535 點）下載。CSV 時序表不動，GUI 另開分頁。文件：`docs/arb_list_mode.md`（設計理由）、`docs/arb_list_mode_plan.md`（合約 §2、規則 R1–R7 §3、Track §5、必測 §6）。

| Track | 內容 | 狀態 |
|---|---|---|
| P0 / C | `listplan.py`、`execute.run_functions` | ✅ `3032c1a` |
| A / D | `synth.py`（第一版不做頻寬警告，Triangle 沿用 Sine 門檻）／`web_list.py`＋`web/list.js` 分頁（原實作已切換） | ✅ `c051338` |
| H | `scripts/hw_list_check.py`（傳輸時間、輪間接縫、256 輪對齊、Sine 頻寬） | 前半 ✅ `82bb688`；實機等設備 |
| I | 整合、回填實測常數、版本 0.2.0 | 前半 ✅ `82bb688`；後半等 H |
| 其他 | List 模式英文化＋預覽標註 `ed7b357`；sv-ttk 主題＋DPI＋中英切換 `ce60f70`；小通道隱藏標註 `47d80ca` | ✅ |

已知小事：`synth._validate` 與 `listplan.load_json` 重複檢查（因 GUI 直接組計畫不經 `load_json`，可在 I 後半合併）。`execute.py` 訊息維持原樣；GUI 的 Python／JS 原始碼不得有 CJK，翻譯集中在 `i18n.py`（測試守著）。`DEMO_SLOTS` 為 N6752A（實機是 4× N6752A；舊專案那台才是 N6762A）。

### 9.2.3 接手要做
1. **有設備時**：`python scripts/hw_list_check.py all --resource <VISA位址>`（開輸出前輸入 y；存 CSV 到 `hw_list_results/`；可單跑 `transfer`／`seam`／`align`／`sinebw`）。
2. 驗收門檻（規劃 §4.5）：transfer ≤ 10 s（否則 dwell 規則改「符合 N 取最大 dwell」並考慮請 core 改二進位傳輸）；align 的 spread 與 d_k − d_CH1 ≤ 1 dwell；seam 無空隙／突波；sinebw 求 −3 dB 頻率。
3. **I 後半**（使用者另說開工才做）：回填 `SINE_BW_HZ`、`compile_plan(plan, model=None)`、Sine／Triangle 門檻、dwell 規則定案、版本 0.2.0、README、Windows 打包。

### 9.3 工作方式（使用者要求）
- **使用者對每個 Track／功能說「開工」才動程式碼**；沒點名的不碰。
- 實作交 **Codex**，審查交 **Opus**，Claude 驗收並回報；**使用者說 commit 才 commit**。
- Codex 做法：`codex exec --skip-git-repo-check -s workspace-write "<任務>" < /dev/null > out.txt 2>&1`（**一定要 `< /dev/null`**，否則卡在 "Reading additional input from stdin"）。任務寫明只准改哪些檔、不准 commit、最後跑 `python3 -m pytest -q`。系統 python3 若缺 pytest，Codex 會改用 Anaconda Python。
- 驗收清單：`git status` 確認範圍 → 對照合約 → 讀碼 → 探測邊界 → 跑完整測試。規劃有漏的先補規劃再交回修。

## 10. E36441A 支援（2026-10-20 起，使用者目前沒有設備）

### 10.1 已 commit
- `1cfbe76` `scripts/hw_e36441a_check.py`＋`tests/test_hw_e36441a_check.py`：`--resource X [--channel 1] [--only 1|2|3] [--v-low 2 --v-high 5 --rise 0.2 --hold 0.3 --current 0.5] [--csv out.csv] [--yes]`。對**整台**送 `*RST`；IDN 不含 E36441A 拒絕；結束一律 ABOR／OUTP OFF／SLEW INF／還原。取樣間隔受 VISA 限制，太粗回 INCONCLUSIVE，可加大 `--rise`／`--hold`。
- `dca33f1` `e36441a.py`＋`tests/test_e36441a.py`：`prepare_commands`（SLEW 先 INF 再 VOLT 到 low）→ 呼叫端等穩定 → `configure_commands`（TRIG:SOUR BUS、TRIG:DEL、SLEW、LIST、`VOLT:MODE LIST` 最後）→ `arm_commands`（INIT）→ 每台一次 `*TRG`；`restore_commands`；`check(row, hold_dc)` 回 (errors, warnings)。時間用 `.3f`；`hold_dc` 時 LIST 只有 high 一點、dwell＝rise、不用的方向 SLEW 送 INF、`check` 略過 fall。

### 10.2 與 N6705C 的差異（Programming Guide，印刷頁碼）
- 無 ARB，只有 FIX／STEP／LIST（p.111）；LIST ≤ 100 點、dwell 10 ms–3600 s、解析 1 ms（p.66–67）。
- 沒有上升時間參數，只有 `VOLT:SLEW:RIS/FALL`（V/s，0.002–INF，p.113–114）；以 `|ΔV|/rise` 換算，`high<low` 時 RIS／FALL 對調，0 或 ΔV=0 送 INF。
- SLEW 也作用於輸出開／關，是通道持久狀態。`OUTP:DEL:RISE` 會讓 delay 期間輸出變 OFF，**不用**；delay 用 `TRIG:DEL`（p.106，解析度手冊未載）。
- 範圍 0–32 V（SCPI 32.96）、0–10 A（10.3）、每通道 160 W、全機 400 W。
- 資料表（非 PDF，**未經本地核實**）：上升 settling 約 80 ms；下降滿載 60 ms、**空載 500 ms**；1% settling typical。`check()` 對 rise < 80 ms、fall < 500 ms 給警告。
- 沒有「list 執行中」狀態位元（只有 WTG，p.32），完成偵測靠計時＋量測。

### 10.3 未驗證的假設（實機優先序）
1. `*TRG` 前 INIT 不會動（N6705C 就不等，§4.10）。
2. **LIST 的電壓切換會套用 SLEW**。不成立 → `configure_commands` 改多點階梯（100 點、每步 ≥ 10 ms，100 ms 的 rise 約 11 點）。
3. dwell 從切換點起算（`rise+hold`）；若 settle 後才起算，改 dwell＝hold。
4. `TRIG:DEL` 精度；`VOLT:MODE LIST`＋`CURR:MODE FIX` 是否觸發錯誤 801；`restore` 先 OUTP OFF 再還原 SLEW 的順序（OUTP OFF 若受 fall 斜率限制要調）。
5. 同機／跨機 skew（手冊無保證值）。

### 10.4 接手要做
已完成（`5370b76`、`cbf7dbe`、`8732707`，Codex 寫、Opus 審兩輪）：`*IDN?` 分派機型；混用機型與錯誤機型在送任何設定指令前拒絕（CLI／GUI／`run_rows`／`run_plan`；`run_plan` 讀 `inst.identity.model`，拒絕任何 E36441A）；GUI 隱藏擷取；關輸出後逐通道回讀 `OUTP?`（錯誤訊息含 frame 編號，失敗丟 `OutputsNotConfirmedOff`）；`run_rows` 設定前查 `INIT:CONT?`＝0、`OUTP:COUP:CHAN?`＝NONE、`OUTP:TRAC?`／`OUTP:PAIR?`＝OFF，不合規拒絕（回覆會去引號、轉大寫再比）；`check()` 拒絕負的 delay／rise／hold／fall（電壓與電流上限原本就擋），GUI 繞過 CSV 也涵蓋。

待辦：
1. 有設備：`python scripts/hw_e36441a_check.py --resource <VISA位址>`，貼回輸出（含 `--csv`）。
2. 依結果定案 `configure_commands`（§10.3）。同時實機確認執行前檢查的回覆格式：`OUTP:PAIR?` 目前只認 `OFF`（回 `0` 會被拒）、`INIT:CONT? (@n)` 是否接受通道清單（不接受會逾時，但發生在寫入前）。
3. 不做 driver 抽象基底、不移植 listplan（CDW）。`order_warnings`／`suggest_assignment` 因每台 4 通道可沿用，但 skew 值要重測。

## 11. 報告分析升級版（2026-10-29 開工；T0–T8 完成，PR #1 已合併 main `913de2d`）

**目標**：把 DLOG Viewer（Python server 版）做成 SEQ 第三分頁「離線分析報告」，給超過 1000 萬筆的 `.dlog` 用。**使用者已於 2026-10-29 說「開工」，規劃文件在 `報告分析升級版/升級報告.md`，契約在 `docs/analysis-contract.md`（數字與型別以 `src/n6705c_seq/analysis/contracts.py` 為準）。**

**施工進度**（pytest 809 過／7 跳過，`node --test tests/js` 26 過，真實核心 145 過；2026-10-30 驗）：
- T0 契約完成（Codex 審兩輪、修訂後定稿；含 `open_handle`／`adopt` 交接規則、已知風險：分析中來源檔被截短 mac/Linux 會 SIGBUS）。
- T1 搬檔 + core 測試 + parity 工具；T2 六態仲裁器 `analysis/gate.py`（含 `_untimed`／`_finishing` 計時）；T3 `core.py` 路徑＋mmap、`storage.py`；T4 `server.py`；T5 `viewer.html` 改接 postMessage／`/save`／`/prefs` ＋ `shell_stub.html`；T6 殼層 `web/analysis.js`、第三分頁、`App.powerAllowed`、整合測試 `tests/test_analysis_integration.py`。
- **`contracts.ANALYSIS_ENABLED` 已為 `True`**（分頁顯示；macOS 已手動點測無問題）。改回 `False` 可隱藏分頁（位置：`src/n6705c_seq/analysis/contracts.py`）。Windows 11 尚未驗，發版前須先驗。
- **T7（打包與文件，已完成，未實機驗）**：`pyproject.toml` package-data 加 `analysis/web/*`；`packaging/n6705c_seq.spec` datas 逐檔列 `viewer.html`（`shell_stub.html` 不打包）、hiddenimports 收 `n6705c_seq.analysis`；`build.ps1` 加 bundle 內 `viewer.html` 存在檢查；README 補分析分頁、開關位置、測試環境變數、不搬工具清單；`HANDOFF_WINDOWS.md` §6 端到端驗收清單。`original_before_python.html` sha1 `0117e08d…` 搬移前後相同。
- **未做**：Windows 11 實機未測（見 `HANDOFF_WINDOWS.md` §6，16 項）；Windows CI 已通過（見末條）。
- **後續修補（已 commit：`33f58ce`、`3692f91`、`78f36d6`）**：Viewer 8 處 `alert()` 改 `#toast`；`/open` 進度改 `/progress` 輪詢；`--selftest` 往返（enter→ready→leave→idle）與 `selftest_mode` 旗標；`analysis.js` 補 `csv` 授權；`on_cleanup` 收尾計時（`execute.py`／`e36441a.py`）。
- 已知取捨：CSV 與原 JS 容許最後一位差 1；契約取代報告 §5 舊的計時說法（報告檔本身未改）。

- 資料夾 `報告分析升級版/`：`升級報告.md`（定案、技術棧、互斥界線、T0–T8 並行任務表、開放問題）、`來源_DLOG_Viewer/`（Viewer 全套複製，不含舊單檔版與 `.cache`）、`參考_SEQ現況快照/`（唯讀參考）、`測試樣本/`（2.6MB，太小，不能證明千萬筆）。
- 已定案：用 Python server 版（單檔版 10M 要 800MB、全量 CSV 做不到、負電流校正不完整，已否決）；iframe ＋ 同進程背景 HTTP server（`127.0.0.1` 隨機埠 ＋ token）；開檔走原生對話框拿路徑再 mmap；下載走原生另存；偏好存獨立的 `analysis_prefs.json`（SEQ config 會整檔重寫、丟掉別的區段），不用 localStorage；快取搬到使用者可寫目錄。
- **互斥（硬性）**：電源控制中不可分析，分析分頁開著時不可有任何電源自動化。Python 端用 `idle | power | analysis | draining | closing | fault` 六態仲裁（帶世代、compare-and-set；`closing` 為終態、`fault` 只能走向 `closing`；逾時不釋鎖）取代 `webgui._busy`，不靠前端灰按鈕。`analysis/*` 禁止 import `n6705c.*`、`pyvisa`、`webgui`、`web_seq`、`web_list`、`execute`、`e36441a`。
- 先注意：`test_webgui` 會掃 `web/*` 不准有中文，Viewer HTML 要放 `analysis/web/`；`js_api` 有公開方法白名單測試。
- 未決（Opus 與 Codex 意見不同或需使用者拍板）：分析要不要子進程、`/buckets` 校正全覽是否解除暫緩、互斥是否涵蓋 CLI／多實例、鎖的範圍、效能驗收數字、獨立版 Viewer 是否繼續維護。全文見 `升級報告.md` §6、§9。
- 施工分工（開工後）：Opus＝契約 T0、SEQ 殼層 T6、審查之一；Codex＝互斥 T2、大檔核心 T3、挑錯與審查之一；Sonnet＝server T4、Viewer 改接 T5；Haiku＝搬檔 T1、打包文件 T7。每個任務獨占輸出檔案。
- **審查進度**：Opus＋Codex 已複審三輪（皆 NEEDS_CHANGES，必修逐輪減少），三輪意見都已改進 `升級報告.md`，尚未跑第四輪。第三輪修正重點：狀態表轉移與逾時、T1 改重寫 core 層 pytest（原 9 項都要起 server）、T5 用假外殼驗收、`App.powerAllowed` 反灰、`ANALYSIS_ENABLED` 定義在 Python（`config()` 下發，`test_webgui.py:90` 要同步改）。
- **已定案（使用者指示全採建議值）**：§9 第 1、2、3、4、6 題（進頁即鎖、切頁取消、同進程、單一 GUI 進程、最大檔 10M 起算且 30M 不 OOM）。

- **T8 審查後修補（已 commit）**：舊世代 `/release` 綁 `(gen, digest)`、HTTP socket 逾時與取消、`/save` 取消不覆寫、prefs 目錄先建、`/open` 超限先 release、CSV 24 欄（`MAX_CSV_KEYS`）、tint 驗證、多開快取互踩、補副檔名覆寫確認、對話框加鎖、wheel 只含 viewer.html。審查全文 `docs/analysis-review-codex.md`、`docs/analysis-review-opus.md`。覆寫確認已有中英文（i18n key `overwrite_confirm`）。PR #1 已合併。早期 pywebview 那批已先推到 `origin/main`（`6849055`）。**待決**：Windows 11 實機驗收（`HANDOFF_WINDOWS.md` §6）。
- **Codex 全專案審查（5 輪，合併前）**：修了 VISA 連線建構失敗沒關（`execute.py` `close_all`／`run_callback`）、關連線失敗或 `on_cleanup` 拋錯時漏關／漏還原、Viewer 校正快取被過期回應覆寫（cache epoch）、並行名額提早釋放、開檔／匯出同時進行錯亂（一次只跑一個）、匯出用到過期統計（最多重試 5 輪後放棄）、CSV 比對取樣進位改 half-up、檔頭搜尋上限 `DLOG_MAX_HEADER_BYTES`（1 MiB）。第 5 輪後剩的一點（統計請求失敗仍匯出）已修，2026-10-31 已送 Codex 複審：統計防護與中英訊息鍵無問題、無旗標／進度列／handle 洩漏；唯一意見（P1）是 CSV 匯出在存檔對話框期間若改了偏移設定，仍以點擊當下的快照匯出（CSV 是原始取樣、不用統計，快照內部一致；PNG／報告／JSON 才走五輪統計防護）。使用者已決定（2026-10-31）維持現狀：CSV 以點擊當下的快照匯出，不在對話框後重驗設定。契約 `analysis-contract.md` 已於 2026-10-31 同步（`DLOG_MAX_HEADER_BYTES`、`apiFetch` 第 4 參數、開檔與 CSV 匯出互斥）。
- **合併後 Windows CI（2026-10-31 已解決）**：
  - 合併後 Windows 卡死、gate 時間測試偶發失敗，原因是測試在 Windows 的假設不成立（mmap 不能刪／截短、cp1252、CRLF、win32 MessageBox），沒有產品問題。PR #3 已修：CI 加 `pytest-timeout`、`.gitattributes` `-text`、selftest 補 `webview.renderer = "edgechromium"`、gate 時間測試放寬、兩個截短／刪除 mmap 檔的測試在 win32 skip。
  - 專案改為公開 repo，Actions 不再受額度限制。PR #1–#4 全部合併（#4 加 `N6705C-SEQ.bat`），六個測試組合與 `build-windows` 全過。
  - 仍待：Windows 11 實機驗收（`HANDOFF_WINDOWS.md` §6）、`N6705C-SEQ.bat` 在 Windows 實測。
- **不穩定測試 `test_save_waiting_for_target_lock_is_cancellable`（2026-10-31，Windows 本機 30–40% 失敗，未修）**：`ConnectionResetError`。推論（未用除錯器證實）：測試以 `wait_for(job_count()==1)` 當同步點，但 job 在讀請求內容前就已登記；若 `/cancel` 在 server 仍於 `_body_parts` 讀內容時到達，`_check_io`（`server.py:218`）拋 `_IOStopped`，`_dispatch`（`server.py:299`）不回應就關連線，測試預期的 409 變成連線重置。與編碼無關。**待決**：(1) 改測試，等內容讀完再取消；(2) 改 server，讀內容途中取消也回 409（先對照 `docs/analysis-contract.md`）。環境與其他測試結果見 `HANDOFF_WINDOWS.md` §7。同一台機器另修了兩處測試讀原始碼沒指定 UTF-8（cp950 下失敗）。

## 12. 搜尋儀器下拉選單（2026-11-05，PR #8 已合併 `c7461c6`）
按「搜尋儀器」後，機台 1、2 的下拉選單列出 `N6705C  S/N MY66102013  (USB0::...)`，點選填入純位址；手動貼上照舊。Codex 實作、Opus 審核。使用者已確認離線測試可用，並決定先合併、之後再驗實機。

- **流程**：`web_seq.scan_instruments` → `_seq_start` 取 power token（與執行、分析互斥）→ `_scan_work` → `transport.scan_resources`（子行程，20 s）→ `["scan", [[address, label], ...]]` → `seq.js`。Demo 模式不掃描。
- **別改壞的地方**：
  - 候選後端要在**父行程**算好再傳進子行程：daemon 子行程不能再開探測行程，`_native_backend_safe` 會吞掉錯誤、悄悄丟掉原生 VISA。同理子行程內的 `VisaTransport` 要指定明確後端，不可用 `auto`。
  - 序列埠（ASRL）要在送 `*IDN?` **之前**濾掉。`list_resources` 本身不過濾。
  - 只送 `*IDN?`，不送 `*RST`、`*CLS`。
  - 用 `<select>` 不用 `<datalist>`：後者會依輸入框文字過濾，已存位址會藏住其他儀器。
  - 地址清單到手後，逾時或當機要回傳部分結果，不可拋錯；清單到手前才拋 `TransportError`。
- **`list_resources` 行為改變**：原本第一個後端成功就停（有原生 VISA 的機器問不到 pyvisa-py），現在合併所有後端。`core` 是手動同步的複本，舊專案要同步。
- **新依賴 `psutil`**：pyvisa-py 要有它才會在每張網卡廣播 VXI-11。未裝 `zeroconf`，所以探索不到 HiSLIP。
- **已驗**：pytest 838 過／7 跳過；CI（三平台 × Python 3.11／3.13、`build-windows`）通過；`--selftest` exit 0；macOS 真實掃描約 1.4 s（無儀器）；真 pyvisa 連本機假儀器，只收到 `*IDN?\n`，非 ASCII 回覆解析正確。
- **未驗**：實機 N6705C／E36441A 的 LAN 廣播與 USB 列舉；Windows 原生 VISA 當機路徑（`BrokenPipeError` 只有模擬測試）；打包後 exe 的子行程掃描；加上型號標籤後的選單版面（截圖是標籤加入之前的）。
- **已知限制**：LAN 廣播跨不了子網路；同一台儀器可能出現兩筆（後端寫法不同，或同時接 USB 與 LAN）；列舉階段有後端卡住時，該次其他後端的結果會丟失（`scan_resources` 的 `ponytail:` 註解）。
- **接手要做**：接實機按「搜尋儀器」，確認清單與型號；沒搜到就對照 `HANDOFF_WINDOWS.md` §3.4 判讀。這功能已隨 `v0.2.0` pre-release 發出（2026-11-05，使用者決定先預先發行）；Windows 驗收後才轉正式，見 `HANDOFF_WINDOWS.md` §5。

## 13. 即時資料紀錄（放進 Viewer 分頁）與 ARB 修補（2026-11-17 規劃；**Wave 0–5、審查修正與實機相容修正已合併 main（PR #11–#15）**，見 §13.7–§13.10）

來源：參考舊專案 Keysight 14585A（反編譯、字串擷取），再由 Opus 與 Codex Astra 討論三輪（完整輸出在 scratchpad `r1/r2/r3_final.txt`，不在 repo 內）。Astra 引用的 Keysight 手冊頁碼（p.291、p.311、p.373-374）Opus 沒核對，**待確認**。**使用者尚未說「開工」，下列只是決策。**

### 13.1 使用者決策
1. 即時紀錄放進 **DLOG Viewer 分頁**（雙方原建議另開第四分頁，使用者否決）。分頁內分「檢視／錄製」兩模式、互斥；錄製拿 power token。**`analysis/*` 仍不得 import `n6705c`**（§11），錄製程式放 analysis 之外、經 bridge 呼叫；停止後釋放 token 並把 .dlog 載入同分頁。gate 現在只允許從 IDLE 進分析（`analysis/gate.py:107-117`），需調整並補測試。
2. 硬體閘門 H0 失敗時，接受退路：DLOG 錄完下載 + 約 40 Hz `MEAS:VOLT?`／`MEAS:CURR?` 預覽（畫面要標「預覽」）。
3. 「邊上電邊錄」延到 P4，v1 只做獨立錄製。
4. ARB 必要修補另立一軌，排在即時紀錄 P2 之前。

### 13.2 .dlog 實測（2026-11-17，兩個真實檔）
- 格式：XML 標頭（`</dlog>\n` 結尾）→ 兩個大端 UInt32（trigger index、write index）→ 大端 float32 環形資料。官方公式 `OffsetTime = (-Count + write - trigger) * Period`。
- 兩檔 trigger=0、write=筆數，資料區長度剛好整除，**現有 Viewer 的 `>Q`（`analysis/core.py` 約 93 行）在這兩檔上正確**。前觸發、環形繞回、`sense_minmax=1` **沒有樣本，未驗證**。檔一宣告 3001 s 但只錄 932.5 s（提前中止，Viewer 以實際筆數為準，是對的）。
- 另開小修（不擋即時紀錄）：Viewer 改讀兩個 `>I` 並依索引重排；或先拒絕 trigger≠0 的檔，比靜默讀錯安全。

### 13.3 ARB 差距（對 14585A）
- **必要**：`start_together` 在 `INIT:TRAN` 前補送 `TRIG:ARB:SOUR IMM` 並查錯（現只設 `TRIG:TRAN:SOUR`，`execute.py:174`；這很可能是 §4.10「ARB 在 INIT 就開跑」的原因），fake 補指令；執行前檢查 `VOLT:SLEW?` 與 `VOLT:SLEW:MAX?`，非最大值就警告。
- **可選**：priority／emulation 前檢（`sour:func?`、`diag:emul?`，只對 SMU）；.dlog 回放成 CDW。
- **啟用前必修（GUI 路徑碰不到）**：sequence 步驟編號官方 0..99、程式送 1..100（`arb/sequence.py:185`，推論）；`multi.py:137-144` 回滾漏掉當前通道；`multi.py:1-18,151-160` 文件仍說 `*TRG` 釋放 ARB，與 §4.10 矛盾。
- **不建議**：delta modulation、公式波形庫。
- **未決（要實機）**：`TRIG:ARB:SOUR` 殘留 BUS 時的後果（Opus：逾時；Astra：勾擷取時 `*TRG` 可能放出 ARB；最糟為沒波形卻回報成功）；CDW 65,535 點用 ASCII 約 650 KB，傳輸時間待測。

### 13.4 即時紀錄設計重點
- **風險**：舊專案 `N6705C-APP/HANDOFF.md:464-486` §4.0.2：`FETC:ELOG?` 有時回 4 bytes 空區塊且錯誤佇列乾淨；2 s 取 20–50 筆穩，4–16 s 要 500–2000 筆一律 0 筆，成因不明。**未過 H0 前 ELOG 長錄不可信。**
- 現況：core 已有 `ElogSession`／`ElogRecorder`（`elog.py`）、`DlogSession`（`dlog.py`），fake 已模擬 ELOG／DLOG／MMEM；GUI 沒接。
- v1 用 ELOG 串流；每次 `FETC:ELOG?` 上限 16384 筆（待核手冊）；單一 worker 做所有 VISA I/O；停止只設旗標，同一 worker 送 `ABOR:ELOG` 後才釋放 token；每步讀 `SYST:ERR?` 到 0（`+323` 在 `INIT:ELOG` 才報；`+310` 表示不支援 autorange，要上 UI 並寫進標頭）。
- 圖：JS 每 250–500 ms 向 Python 取最新快照（每條 ≤2000 點 min/max 包絡，帶 generation）；全解析度直接寫檔。4 通道 V+I 最短週期 819.2 µs ≈ 39 KB/s ≈ 140 MB/台/小時。
- 存檔：寫 `.dlog` 相容格式、每台一檔；trigger=0、write=筆數；欄位每通道先 V 後 I（`analysis/core.py:63-71`，要把 ELOG 順序 `parsing.py:35` 重排）；v1 `sense_minmax=0`；原始值不套負電流替換；錄製中用 `.part`，只對自家 `.part` 截尾（Viewer 會把 write index 之後殘段排到最前，通用截尾不安全）；額外旁檔存要求值／回讀值、主機 IDN、notes、缺口、UTC。
- 要先修：`ElogRecorder` 吞 sink 錯誤、`join` 逾時仍清執行緒（`elog.py:518-522,545-549`）；DLOG 外部觸發未到就標 RUNNING（`dlog.py:323-328`）；DLOG 下載失敗仍刪儀器原檔（`dlog.py:430-436`）。
- 多台：各自時間軸；跨台 skew（`group.py` `TRIGGER_SKEW_SECONDS` 只是估計）與長時間漂移要量測留證。

### 13.5 階段與硬體驗證
P0 H0＋`TRIG:ARB:SOUR` 驗證 → P1 core 修補 → P2 Viewer 分頁單台 ELOG（H0 失敗改 DLOG＋MEAS）→ P3 多台 → P4 邊上電邊錄 → P5 DLOG 串流下載／ACQ → 最後 HIST。ARB 修補另軌，slew 前檢在 P2 前完成。

H0 方法：單通道固定週期，觸發後立刻每 250 ms fetch，每次只改一個變數（首讀立即 vs 延後 5 s、ASCII vs REAL、要求數小於 vs 等於 16384）；另測停讀 5／12／25 s 後 fetch（緩衝深度與溢位丟新或舊）、`ABOR:ELOG` 後尾段是否保留。其他：`TRIG:ARB:SOUR BUS`→`INIT:TRAN`→延後 `*TRG` 與 EXT 共用 BNC（示波器看）；`ARB:SEQ:RES` 後寫 step 0／1 查 `LEN?`；ELOG 與 ARB 並行；USB／LAN 各長錄 1 小時以上；自寫 .dlog 分別用 Viewer 與 14585A 開。

### 13.6 施工任務表（2026-11-17，Opus high 規劃；全文 `docs/live-logging-task-plan.md`，**尚未開工**）
**使用者定案（2026-11-17，全採建議）**：(1) H0 結果出來前**不做** B-ELOG（H0 失敗會整支白做；若實機要等很久可改成先做、合併時再看 H0）；(2) A2 與 Wave 1 一起做，A3 等 HAs 實測結果；(3) 錄製檔在按「開始」時跳原生另存對話框（同 `seq_save_csv`）。

| 波次 | 開始條件 | 任務 | 最大並行 |
|---|---|---|---|
| Wave 0 | 使用者說「開工」 | T0 契約、H0s／HAs 驗證腳本 | 3 |
| Wave 1 | T0 合併 | A1、A2、C1、C2、W（可選 X1） | 5～6 |
| Wave 2 | A1 合併 | S、G、K、J、V | 5 |
| Wave 3 | H0 結果出來，且 C1／C2／W／S 合併 | B-ELOG 或 B-DLOG（二選一） | 1 |
| Wave 4 | B、G、K、J、V 合併 | I 整合＋selftest、D 文件打包 | 2 |
| Wave 5 | I 通過 | R 雙審（Codex、Opus 各一份） | 2 |

- 任務：T0＝跨任務介面（bridge `record_meta/start/stop/status/snapshot/load`、快照型別 ≤2000 點/條、`record/backend.py` Backend Protocol、`.dlog` 寫檔器介面、gate 轉移、fake 補 `TRIG:ARB:SOUR`／`VOLT:SLEW?`／`VOLT:SLEW:MAX?`、`Msg.OPEN_PUSH`、全部新 i18n key、`RECORD_ENABLED=False`）；A1＝`TRIG:ARB:SOUR IMM`＋slew 前檢；A2＝`multi.py` 回滾補當前通道＋修文件；A3＝sequence 步驟編號（等 HAs）；C1＝`ElogRecorder` 阻塞式、ABOR 由同 worker 送、錯誤語意；C2＝DLOG 兩個 bug；W＝`.part` 寫檔器；X1＝Viewer 讀兩個 `>I`；S＝min/max 包絡快照；G＝`web_record.py` bridge（`webgui.py` 只改 2 行）＋gate 轉移測試；K＝檢視／錄製兩模式；J＝錄製面板與圖表（H0 失敗標「預覽」）；V＝Viewer 接 `open_push`。
- 規則：同波內任兩任務不得寫同一檔；跨波共用 3 檔（`analysis/contracts.py`、`core/fake.py`、`record/backend.py`）已改切法處理；新 i18n key 全由 T0 加齊，`app.js` 凍結（推論）。
- **關鍵路徑**：T0 → A1 → S → B → I → R；實際瓶頸是硬體：H0（USB、LAN 各長錄 1 小時，至少約 2 小時）→ B，所以 H0s 放 Wave 0 先交使用者。
- **推論待 T0 確認**：錄製沿用現有 `power` 狀態（analysis → draining → idle → power，停止後反向），`gate.py` 不加新狀態，只補約 6 個轉移測試；與 §13.1「gate 需調整」的寫法以此為準。

### 13.7 進度與接手要做（2026-11-18 更新）
**工作方式（使用者指定）**：Opus 規劃 → **Codex `gpt-6.1-sol`（`-c model_reasoning_effort=medium`；使用者 2026-11-18 起指定中檔）寫全部程式** → Opus 子代理審查 → 我在沙盒外驗證。使用者說「開工 Wave N」才開波；commit 只在使用者說時做；使用者用繁體中文。Codex 呼叫：`codex exec --skip-git-repo-check -s workspace-write -m gpt-6.1-sol -c model_reasoning_effort=medium "$(cat prompt.txt)" </dev/null > out.txt 2>&1`（`</dev/null` 必加；macOS 無 `timeout`，用 `perl -e 'alarm N; exec @ARGV'`）。Codex 沙盒禁 socket bind，HTTP 測試會失敗（約 8 failed/24 errors，是環境限制），**一律在沙盒外重跑** `/opt/anaconda3/bin/python -m pytest -q -p no:cacheprovider` 與 `node --test tests/js/*.test.js`（要用 glob，目錄形式會失敗）。

**以下 commit 已於 PR #11 合併進 main（分支已刪）**：
- d6b9d2a Wave 0：契約 `docs/live-logging-contract.md`（C1–C13）、`record/contracts.py`、i18n +46 key（之後凍結）、`scripts/hw_elog_h0.py`、`scripts/hw_arb_trig_check.py`。
- 58c1fc2 Wave 1：A1 `TRIG:ARB:SOUR IMM`＋slew 前檢、A2、C1（`ElogRecorder.run/tail/dedupe/tail_incomplete`）、W（`record/dlogfile.py`）、C2 的 DLOG 兩個 bug。
- 7e8c738 Wave 2：S `record/snapshot.py`、G `web_record.py`（RecordApi）、K/J/V（`analysis.js`、`record.js`、`viewer.html` open_push）。Opus 審查（含 must-fix 複審）已核准。`RECORD_ENABLED=False`。
- 7b2a494：`DlogSession.wait()` 在 ARMED/IDLE/ABORTED 不再無限迴圈（回 False）。
- Wave 3 B-DLOG（commit 見 `git log`）：`record/backend.py`、`tests/record/test_backend_dlog.py`、`core/fake.py`（真格式 .dlog）、`NoRecordsError`（`record/contracts.py`）、`web_record._failure_code` 映射 `no_recording`。Opus＋Codex 雙審 → Codex 修六項（零筆錯誤碼、發布半途失敗旁檔 `complete=False`、下載本機寫入錯誤標 `sink`、刪檔測試假陽性、`SIGALRM` 跨平台、INIT 前失敗清空暫存檔）→ Opus 複審 APPROVED。全套 1441 passed／7 skipped，node 67 過。B-ELOG 等 H0 結果。

**接手要做**
1. ~~B-DLOG 雙審~~ 已完成（見上）。
2. 已知缺口（B-DLOG 審查遺留，皆不擋）：原生 `.part` 無法被 `recover_part()` 復原（沒植入 run_id）；（MEAS 連續失敗讓整個 run 失敗的缺口已於 Wave 5 修正，見第 6 項）契約待補：MEAS 連續失敗上限 3、gap index 用預覽序列且 count=null、`envelope.preview=True` 由 backend 設；覆寫舊檔時第二次 replace 失敗，舊錄音的 `complete=True` 旁檔會被新的 `complete=False` 取代（舊資料檔仍在，比配錯安全）；`_disk` 補欄位後無 errno 的 `OSError` 其 `str()` 顯示 `[Errno None]`；`notes` 重複記同一例外、`started_utc` 用 `+00:00` 而其他欄位用 `Z`；缺測試：發布最後一步（標 `complete=True`）失敗。
3. **使用者要在真機跑**（Wave 3 B-ELOG 與 A3 卡在這）：`scripts/hw_elog_h0.py`（elog_arb 需 `--yes`；完整 USB＋LAN × 2 設定 × ≥1 h，總計 ≥4 h）與 `scripts/hw_arb_trig_check.py`（`slew` 項送 `*RST`，需 `--allow-reset`；`seqnum` 結果決定 A3）。H0 PASS → 做 B-ELOG（`backend.py` 目前被 B-DLOG 佔用，若兩支並存要照計畫拆成 `backend_elog.py`／`backend_dlog.py`＋分派的 `backend.py`，需重過 T0）；FAIL → 以 B-DLOG 為準，C13 上限 256 MB 生效。
4. 待決設計：B-ELOG 是否合併 `empty_backlog` 與 `tail_incomplete` 的 gap；長錄時鐘漂移（10 h×100 ppm≈3.6 s）可能造成誤報 `tail_incomplete`；`VOLT:SLEW? MAX` 若不支援，每台每次會多約 10 s 逾時；非 demo 模式現在開兩次 VISA（`detect_model` 再 `connect_group`）。
5. **Wave 4：I、D 已完成並驗收**（I 整合測試 4 個、selftest 結束碼 0；全套 1445 passed／7 skipped，node 67 過；D 文件打包，同步清單已對照 diff 與 APP 現況核對）。D 已補 README 旗標／互斥／關窗說明、Windows 錄製驗收清單（`HANDOFF_WINDOWS.md` §8）及 spec 的 `collect_submodules("n6705c_seq.record")`；Windows 建置、`build-windows` CI 與實機驗收仍待完成。之後是 **Wave 5：R 雙審**。**H0 結果：尚未實測**；目前 B-DLOG 的存在不代表 H0 FAIL，也不代表 ELOG 已驗收。

   **core 同步清單（`../N6705C-APP` 是手動副本，非 git，只能 additive 同步）**：本次只讀核對，未同步 APP。依 `git log`／`git diff d6b9d2a..HEAD`，core 變更來自 `58c1fc2`、`7b2a494`、`34dd510`；並直接比對 APP 現存檔案：`elog.py`、`fake.py`、`arb/multi.py` 與 `d6b9d2a` 相同，`dlog.py` 已含 `7b2a494` 的 `wait()` 修正。以下逐項合入 APP 對應函式，保留 APP 其他功能；不要整目錄覆蓋，也不要對 APP 使用 git 指令。

   - **`src/n6705c/core/dlog.py`**：修改 `DlogSession.start()`，INIT 後設 ARMED 並清 `_t0=0.0`、`trigger_time_utc=""`；BUS 送 `TRIG:DLOG`、IMM 直接計時並轉 RUNNING，其餘外部來源保持 ARMED，不虛構觸發時間或完成時間。修改 `retrieve()`，用 `downloaded` 逐檔追蹤已成功寫入並關閉的本機副本，cleanup 只刪對應遠端檔；raw `.dlog` 下載失敗仍保留儀器原檔，CSV 成功仍可回傳。`wait()` 的區間變更是只在 RUNNING 輪詢，非 COMPLETE 回 False（含 IDLE／CONFIGURED／ARMED／ABORTED、等待中中止），只有 COMPLETE 才等 margin 並回 True；**此函式 APP 已同步，無須重套**。
   - **`src/n6705c/core/elog.py`**：新增公開 `ElogSinkError`、`ElogFetchError`、`MAX_FETCH_FAILURES=3`（含 `__all__`），內部 `_ElogErrorQueueError`、`_validate_max_records()`（只收整數 1..16384），並新增 `csv`、`ScpiError` 匯入。`ElogSession` 新增 `_read_errors()`／`_check_errors()`／`_write_checked()`：以 CSV 嚴格解析 `SYST:ERR?`，須在 50 次內讀到有效 0 結尾；非零錯誤寫 notes，額外錯誤附於首個例外，格式錯誤或無法清空不能視為成功。修改 `_request_autorange()`：僅容忍 +310，記不支援訊息並查回實際 range，檢查通道數、有限值與非負值，其他錯誤仍拋出。`configure()` 的設定與週期回讀、`arm()` 的 INIT、`trigger()` 的 BUS TRIG、`stop()` 的 ABOR 改用嚴格查錯；`trigger()` 保留舊 adapter 的 `check_errors` hook，`stop()` 在 ABOR 前凍結 elapsed，finally 設 STOPPED。
     `fetch(*, max_records=None)` 新增相容的關鍵字上限，仍只允許 RUNNING；新增 `fetch_tail(*, max_records=None)` 只允許 STOPPED，兩者共用 `_fetch()`，使用凍結後的 elapsed、讀前記 worst backlog／overflow、限制單批要求筆數，讀取／解析失敗或零筆時查錯；既有跨通道對齊邏輯保留。
     `ElogRecorder.__init__()` 新增 `tail`（預設 `drain_then_abort`，另支援 `abort_then_drain`）、`on_event(kind,index,count)`、`max_records`、`on_cleanup` 與狀態鎖／owner。新增阻塞 `run(stop=None)`：fetch、sink、callback、尾段與 ABOR 都在同一 worker；拒絕重入，支援外部 stop Event，先呼叫 on_cleanup，再依 tail 策略收尾；保留首個錯誤，清理失敗附註，仍嘗試 ABOR。`is_running` 改為同時檢查 run owner 與背景 thread；`start()` 是背景 wrapper，防重複啟動並清內部 stop；`stop(timeout)->bool` 只設旗標及 join，不做 VISA I/O，逾時／自身執行緒回 False 且保留 thread 參照，完成才清除。直接 `run()` 不清先前 stop 旗標。
     `_run()` 改呼叫 `run()`，ABOR 完才向 on_error 報致命錯誤；`_pump()` 每輪最多 16 批、可觀察 stop。新增 `_stopping()`、`_callback()`、`_event()`、`_fetch_one()`、`_drain_tail()`、`_note_tail_incomplete()`：sink／callback 失敗拋 `ElogSinkError`，一般 fetch 失敗先報 on_error／fetch_error，連續 3 次拋 `ElogFetchError`；錯誤佇列不可驗證立即致命，SCPI +310／+323 原樣拋出。overflow 只報新增超量，empty_backlog 超過已知缺口基線與兩個讀取間隔才報，避免重複事件；尾段最多 16 批／10 s，未清空報 tail_limit；剩餘 backlog 超過「兩個讀取間隔＋ABOR 耗時」容差則寫 notes 並報 tail_incomplete。
   - **`src/n6705c/core/fake.py`**：`FakeChannel` 新增僅限關鍵字欄位 `voltage_slew_maximum=1e6`、`voltage_slew_rate=1e6`、`voltage_slew_override=False`、`arb_trigger_source="IMM"`（是假模型值）。`FakeN6705C.__init__()` 新增 `_dlog_finalized`／`_dlog_triggered`；`_dispatch()` 在下一次交換時觀察 DLOG 到期並產檔。指令 handler `_rst()` 重設 slew 與 ARB source；`_trg()`、`_init_dlog()`、`_trig_dlog()` 追蹤是否已觸發，INIT 清 finalized；`_abort_dlog()` 先凍結檔案再清 running。
     新增 `_volt_slew_max_q()`（`VOLT:SLEW:MAX?` 回 override 的 0／1）、`_volt_slew_q()`（`VOLT:SLEW?` 回目前 rate，帶 MAX 回 maximum）、`_volt_slew()`（數值或 MAX／INF，驗有限值、非負與通道上限，錯誤 -224／-222）；新增 `_arb_source_q()`／`_arb_source()` 儲存及回讀 IMM／BUS／EXT／TRAN／PIN1..7，非法值 -224，**不改 INIT:TRAN 的啟動行為**。
     `_write_dlog_file()` 取代佔位 bytes：ABOR 或到期只凍結一次，未觸發為零筆，以 elapsed／period 算筆數；產原生 XML 標頭（XML escape、UTC 日期）、大端 `>II` 索引 `(0,count)`、每通道 V 再 I 的 float32 資料，minmax 時加上下界；波形沿用 `_elog_voltage_at()`／`_elog_current_at()`。不植入 host recorder 的 run_id，不能用自家 `recover_part()` 復原。新增 `datetime`／`timezone`、`escape` 匯入。
   - **`src/n6705c/core/arb/multi.py`**：修改 `MultiArbRunner.arm()`，在呼叫 runner.arm **之前**加入 attempted，失敗時逐一 best-effort abort 所有已嘗試通道（含當前失敗通道），清理失敗不蓋原例外。模組、`MultiArbRunner`／`GroupArbRunner` docstring 及 `arm()`、`trigger()`、`mark_triggered()`、`skew_seconds` 說明改正：這是逐通道 INIT 的 legacy 路徑，ARB 可能在 INIT 即開始，後送 `*TRG` 不保證共同起點；未設定獨立 ARB source，其影響待 HAs；跨主機 skew 僅名目 LAN 估值，不是量測上界。除 arm 回滾外，其餘是文件修正，沒有新增同步機制。

   從 SEQ repo 根目錄可照抄核對（唯讀）：

   ```bash
   git log --oneline d6b9d2a..HEAD -- src/n6705c/core/
   git diff d6b9d2a..HEAD -- src/n6705c/core/dlog.py src/n6705c/core/elog.py src/n6705c/core/fake.py src/n6705c/core/arb/multi.py
   diff -u ../N6705C-APP/src/n6705c/core/dlog.py src/n6705c/core/dlog.py
   diff -u ../N6705C-APP/src/n6705c/core/elog.py src/n6705c/core/elog.py
   diff -u ../N6705C-APP/src/n6705c/core/fake.py src/n6705c/core/fake.py
   diff -u ../N6705C-APP/src/n6705c/core/arb/multi.py src/n6705c/core/arb/multi.py
   ```

   `diff` 有差異時結束碼為 1，讀檔失敗為 2；本清單不代表 APP 已同步或實機通過。

6. **Wave 5 R 雙審結果**（2026-11-18；報告 `docs/live-logging-review-opus.md`、`docs/live-logging-review-codex.md`；兩份都是 CHANGES REQUIRED、無 Blocker；`RECORD_ENABLED=False` 可合併，**打開旗標前下列必修項要清完**）：
   - **已修並驗收（全套 1466 passed／7 skipped，node 67 過）**：A `.DLOG` 大小寫（對話框接受、backend 拒絕，且在儀器設定後才失敗）；C MEAS 預覽連續失敗／ABOR 後錯誤不得讓已下載的完整 DLOG 失敗；E 長錄結束時才覆蓋他人新建或更換的正式檔（開始時記身分，發布前比對，衝突保留 `.part`）；m1 零筆或 INIT 後失敗的空 `.part`／`.json.part` 殘留擋同名重錄。實作要點：A 在對話框回傳後統一成小寫 `.dlog` 並於碰儀器前驗證；C 預覽失敗只停預覽記 notes、不算 gap，`ABOR:DLOG` 後的錯誤只記 notes，`session.abort()` 例外也只記 notes 並嘗試下載（靠 `_layout` 驗證把關）；E 開始時（確認覆寫之後、連線之前）由 `web_record` 記正式檔與旁檔的 `stat`，`backend` 發布前比對，衝突回 `sink`、保留 `.part` 與 `complete=False` 旁檔（**比對與 replace 之間仍有極短競態窗口，非跨行程鎖**）；m1 零筆或空 `.part` 一律清除，starting 階段停止不送 INIT／ABOR。`web_record` 現在匯入 `record.dlogfile` 的 `_paths`、`_safe_paths`（私有名稱，之後可改成公開）。
   - **登記待辦（使用者決定先不修）**：
     - ~~**B（擋接上 B-ELOG）**~~ **已修（2026-11-18，分支 fix/dlogwriter-publish-order）**：`record/dlogfile.py` 的 `_publish()` 改為先 `complete=False` 旁檔 → `os.replace(part, final)` → `complete=True` 旁檔，writer 與 `recover_part()` 共用；契約 C6 已改。資料換好後最後一步旁檔失敗時，資料在 final、旁檔停在 `complete=False`、無 `.part`，會拋錯並附 note（保守，不複製大檔）。測試補新檔、覆寫、第二步與第三步 sharing violation、半發布恢復。全套 1480 passed／7 skipped。
     - ~~**D（擋打開旗標，需 Windows 實測）**~~ **程式已修（2026-11-18，分支 fix/win-replace-retry）**：`dlogfile.replace_with_retry()` 僅在 Windows 遇 `PermissionError` winerror 5／32／33 時重試（最多 5 次，退避 0.05–0.4 s），writer 與 backend 全部發布路徑都改用；`HANDOFF_WINDOWS.md` §8 第 9 項待 Windows 實測（Defender 開啟、大檔收尾）。
     - ~~**F（擋打開旗標）**~~ **已修（2026-11-18，分支 fix/record-fault-close）**：`Gate.on_closing()` 在 fault 且 owner 仍在、`worker_done` 為 False 時回 `"power"`（沿用既有「忙碌」提示並擋關窗）；worker 遲到返回後 `worker_done=True` 才放行（仍不解鎖故障）。純分析逾時 fault（owner 為空）行為不變。測試：`test_activity_gate.py`、`test_webgui.py` 各補一個。限制：worker 若永遠卡死，視窗關不掉，只能強殺行程（刻意取捨，優先保護儀器與檔案）。
     - ~~Minor：`analysis.js:147` 從檢視切到錄製只等 idle 10 s~~ **已修（2026-11-18）**：改等 32 s（draining 逾時 30 s＋餘裕，fault 事件會更早結束等待）；`record_meta` 失敗改為 try/catch，分析頁仍顯示、錄製視為關閉；JS 測試補一個。原描述：`analysis.js:147` 從檢視切到錄製只等 idle 10 s，比 draining 逾時 30 s 短，可能停在空白 Viewer；旗標關閉時 analysis 初始化仍 await `record_meta`（唯讀、無 I/O，風險低）。
   - 兩份審查一致確認沒問題：互斥無繞過路徑（異常路徑、對話框取消、關窗皆釋放 token，F 例外）、儀器 I/O 全在單一 worker 且 `record_stop` 只設 Event、import 邊界符合契約、i18n 齊全、旗標關閉時面板隱藏、`open_push` 驗證完整。`core/elog.py` 因 B-ELOG 未接線只審了 import 邊界。
7. push／PR 已完成（PR #11–#15 皆合併）；打開 `RECORD_ENABLED` 仍需實機驗收，由使用者決定。見 §13.10。

### 13.8 要接實機的項目（2026-11-18）

程式面的已知缺口（A–F、小項）都已修並合併；剩下的都需要實機。`RECORD_ENABLED` 維持 `False`，打開前下列 1–4 都要有結果，由使用者決定。

**A. 儀器硬體（N6705C）**

| # | 項目 | 做法 | 目的 |
|---|---|---|---|
| 1 | H0：ELOG 長時間穩定性 | `scripts/hw_elog_h0.py`，≥4 h，USB 與 LAN 各一次 | 決定 B-ELOG 能否開工；失敗退回 DLOG＋MEAS |
| 2 | ARB／TRIG 檢查 | `scripts/hw_arb_trig_check.py`，slew 加 `--allow-reset`，以 seqnum 判定 A3 | 驗證 ARB 修正在真機上的行為 |
| 3 | HW-P2：14585A 相容性 | 用 14585A 開啟錄出的 `.dlog` | 確認寫出的格式真機軟體讀得懂（Viewer 往返不能代替） |
| 4 | 真機錄製驗收 | 旗標暫時打開，接真儀器錄一輪 DLOG | 確認設定、下載、發布在真機可用 |

**B. Windows（不一定要儀器；清單在 `HANDOFF_WINDOWS.md` §8，需先把旗標打開）**

| # | 項目 | 目的 |
|---|---|---|
| 5 | 錄製／收尾中關窗（§8 第 6 項） | 驗證 F 的關窗擋住與提示 |
| 6 | 電源、分析、錄製互斥（第 7 項） | 三者不能同時進行 |
| 7 | 打包 exe 的 record 匯入（第 8 項） | exe 內有 record 模組，Demo 錄一輪無 ImportError |
| 8 | Defender 暫時鎖檔重試（第 9 項） | 驗證 D 的重試、不留 `.part` |

**C. macOS（Demo，不需儀器）**

| # | 項目 |
|---|---|
| 9 | 旗標暫時打開，手動點過檢視與錄製兩種模式的完整流程 |

建議順序：先掛上 1（最久），同時做 5–9，有儀器時再做 2–4。B-ELOG 等 1 通過才開始；core 同步回 `../N6705C-APP` 等使用者開口。

### 13.9 實機快測結果（2026-11-18，N6705C MY65000515，韌體 E.02.09.3271，USB；CH1=N6791A、CH2=N6781A；用 CH2）

**限制**：單台、USB、數秒到數十秒的短測；LAN、≥4 h 長測、14585A 相容性（HW-P2）都沒做，所以 H0 仍是 INCONCLUSIVE，不是 PASS 也不是最終 FAIL。

1. **ELOG 在這台取不到資料**：`FETC:ELOG?` 在錄製中一直回空區塊（`#10`），停止後回 `+303 no valid acquisition`；輸出通電（0.5 V／0.1 A 限制）也一樣，所以不是輸出關閉造成。依既定決策，B-ELOG 暫停，維持 DLOG＋MEAS 路線；要重新評估需先查 ELOG 在此韌體的正確用法。`scripts/hw_elog_h0.py` 新增 `--channel`（原本寫死 CH1，N6781A 的 `elog_arb` 探測還會因 `CURR` 要求 current priority 而 `+315`，腳本的 ARB 通電探測尚未改成 N6781A 可用）。
2. **韌體 E.02.09 與 F.1.1.0 的指令差異（已修）**：`TRIG:ARB:SOUR` 在 E.02.09 是整台共用，帶通道清單會 `-108 Parameter not allowed`（`execute._set_arb_source_imm` 先試帶通道、遇 `-108` 改送不帶通道）；序列長度查詢正確寫法是 `ARB:SEQ:LENG?`（`LEN?` 是 `-113`，已改 `runner.py` 與 `hw_arb_trig_check.py`）。**`hw_arb_trig_check.py` 的 `TRIG:ARB:SOUR?`／`TRIG:ARB:SOUR <src>,(@ch)` 仍帶通道，在此韌體上會失敗，A3 的 seqnum 判定尚未做**。
3. **DLOG 錄製在真機可完成（已修）**：本機 DLOG（BUS 觸發）設定、錄製、下載、發布全程成功，旁檔 `complete=True`。修了兩處假設：(a) 儀器把週期量化到 10.24 µs 格（要 1 ms 得 1.00352 ms）→ backend 允許一格差異並記 note；(b) 原生檔索引 trigger=`0xFFFFFFFF`、write 不等於筆數 → backend 與 Viewer（`parse_dlog` 原本讀 `>Q`）改讀兩個 `>I`，`0xFFFFFFFF` 時以位元組長度定筆數，其他非零 trigger 拒絕；環形排序未驗證（write 與筆數不符時旁檔記 note）。
4. **MEAS 預覽在 DLOG 執行中不可用**：儀器回 `+319 Measurement is not allowed while data logger running`，第一次 MEAS 還要吃一次 VISA 逾時（約 5 s），之後 backend 會停用預覽，錄製本身不受影響；結果是**真機上錄製期間畫面沒有即時曲線**，且 5 s 錄製實際耗時約 10 s。要即時曲線需另找方法（例如 DLOG 中途不可行，只能錄完再載入）。
5. 沒測：透過 GUI／`RecordApi` 走一次完整流程（只跑了 backend）、LAN、Windows。

### 13.10 目前狀態與下一步（2026-11-18 晚，PR #11–#15 皆已合併 main；本機與遠端只剩 `main`）

**驗證基準**：Python 1516 passed／7 skipped、node 68 passed、CI 六組測試（ubuntu／macOS／Windows × 3.11／3.13）與 `build-windows` 皆通過。`RECORD_ENABLED=False`，使用者看不到即時紀錄功能。

**已完成**：Wave 0–5；審查項 A、B（writer 發布順序）、C、D（Windows `os.replace` 重試，程式面）、E（覆寫守衛）、F（fault＋worker 仍活著時擋關窗）、`.part` 殘留、兩個小項；實機相容修正（見 §13.9）。HANDOFF 內對應條目已劃掉。

**下一步**
1. ~~**停止延遲**~~ **已修（2026-11-18，`fix/dlog-stop-latency`）**：真機（`session.description` 非 `Demo mode`）不送 MEAS 預覽，只記 note；模擬器行為不變；待真機確認停止即時。原描述：真機 DLOG 執行中 MEAS 一律被拒（`+319`），第一次 MEAS 吃 VISA 逾時（約 5 s），錯誤清查再吃一次，所以按停止要約 10 s 才生效（`record/backend.py` 預覽迴圈）。建議真機不送 MEAS 預覽（錄製中本來就沒有即時曲線），模擬器行為不變；需要先決定如何判斷是真機（目前 `N6705C` 沒有 demo 旗標），並更新「預覽」相關測試與契約。
2. **ELOG 在 E.02.09 取不到資料（需儀器）**：`FETC:ELOG?` 錄製中回空區塊、停止後 `+303`，通電也一樣。要重新評估 ELOG 在此韌體的正確用法（是否缺少觸發或其他前置指令）後，才決定 B-ELOG 是否開工；在那之前走 DLOG＋MEAS 路線（MEAS 又不可用，見上）。
3. **A3 判定（使用者）**：`seqnum` 證據偏向步驟編號從 0 起算（重設後長度 1、寫步驟 0 長度不變、寫步驟 1 變 2；查步驟 1 回「有效範圍 0 到 0」），腳本自己判 INCONCLUSIVE，單台單次。
4. **下次接上儀器要做**：LAN；從 GUI（`RecordApi`）完整走一次錄製／停止／載入；≥4 h H0；`bus_delay`、`slew`；14585A 開檔（HW-P2）；兩通道以上 DLOG 長錄與覆寫、中途停止；`hw_elog_h0.py` 的 `elog_arb` 通電探測要改成 N6781A 可用（`CURR` 需 current priority）。
5. **Windows／macOS（不需儀器）**：`HANDOFF_WINDOWS.md` §8 第 5–9 項（旗標暫開）、macOS 手動點過兩種模式。
6. **使用者決定**：打開 `RECORD_ENABLED`（需 §13.8 的 1–4 有結果）；是否把 core 修改同步回 `../N6705C-APP`（同步清單見 §13.7，本輪新增動到 `core/**` 的檔案：`n6705c/core/arb/runner.py`）。

**注意**：本機沒有儀器時，真機相關程式只能靠模擬器驗證；模擬器對 `TRIG:ARB:SOUR`、`CURR` 等寫法比真機寬鬆，這次的相容問題就是這樣漏掉的。
