# N6705C-SEQ　交接文件

多台 N6705C 的**上電時序**工具。讀 CSV 表（每通道：延遲、低/高電壓、上升/維持/下降時間），在多台主機上依表上電。只做時序，不做量測。設計兩台（8 通道），程式不限台數。

最後更新：2026-10-31　狀態：模擬器全過（576 tests）；GUI 已換成 pywebview。細節看 git log；本檔只留接手需要的事實。

| 區塊 | 狀態 |
|---|---|
| CSV 上電時序 | 單台（4× N6752A）單通道實機跑通（§4.8）；多通道、帶載、跨台同步未量 |
| GUI 版面 | 整頁不捲動：`html/body` 固定視窗高、`overflow:hidden`；內容超高時分頁以 CSS `zoom` 等比縮小（§4.13，`b77a6e6`） |
| ARB List 模式 | **分頁目前反灰鎖定**（`app.js` `LIST_VERIFIED=false`，未實機驗證）；P0、C、A、D、H 前半、I 前半、D-EN、GUI 美化＋中英切換已 commit；等實機跑 H（§9） |
| E36441A | 指令產生＋驗證＋實機驗證腳本已 commit（`1cfbe76`、`dca33f1`），未接 CLI／GUI，核心假設未實機驗證（§10） |
| Codex 審查 | 第 2 輪已核准（§8） |
| 報告分析升級版 | **只有規劃，未開工，未改任何程式碼**；計畫與素材在 `報告分析升級版/`（§11） |

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
- **Codex 全專案審查（5 輪，合併前）**：修了 VISA 連線建構失敗沒關（`execute.py` `close_all`／`run_callback`）、關連線失敗或 `on_cleanup` 拋錯時漏關／漏還原、Viewer 校正快取被過期回應覆寫（cache epoch）、並行名額提早釋放、開檔／匯出同時進行錯亂（一次只跑一個）、匯出用到過期統計（最多重試 5 輪後放棄）、CSV 比對取樣進位改 half-up、檔頭搜尋上限 `DLOG_MAX_HEADER_BYTES`（1 MiB）。第 5 輪後剩的一點（統計請求失敗仍匯出）已修但**未再送 Codex 複審**。契約 `analysis-contract.md` 已於 2026-10-31 同步（`DLOG_MAX_HEADER_BYTES`、`apiFetch` 第 4 參數、開檔與 CSV 匯出互斥）。
- **合併後 Windows CI（2026-10-31 已解決）**：
  - 合併後 Windows 卡死、gate 時間測試偶發失敗，原因是測試在 Windows 的假設不成立（mmap 不能刪／截短、cp1252、CRLF、win32 MessageBox），沒有產品問題。PR #3 已修：CI 加 `pytest-timeout`、`.gitattributes` `-text`、selftest 補 `webview.renderer = "edgechromium"`、gate 時間測試放寬、兩個截短／刪除 mmap 檔的測試在 win32 skip。
  - 專案改為公開 repo，Actions 不再受額度限制。PR #1–#4 全部合併（#4 加 `N6705C-SEQ.bat`），六個測試組合與 `build-windows` 全過。
  - 仍待：Windows 11 實機驗收（`HANDOFF_WINDOWS.md` §6）、`N6705C-SEQ.bat` 在 Windows 實測。
