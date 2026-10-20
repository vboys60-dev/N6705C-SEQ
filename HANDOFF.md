# N6705C-SEQ　交接文件

多台 N6705C 的**上電時序**工具。讀 CSV 表（每通道：延遲、低/高電壓、上升/維持/下降時間），在多台主機上依表上電。只做時序，不做量測。設計兩台（8 通道），程式不限台數。

最後更新：2026-10-20　狀態：模擬器全過（309 tests）。細節看 git log；本檔只留接手需要的事實。

| 區塊 | 狀態 |
|---|---|
| CSV 上電時序 | 單台（4× N6752A）單通道實機跑通（§4.8）；多通道、帶載、跨台同步未量 |
| ARB List 模式 | P0、C、A、D、H 前半、I 前半、D-EN、GUI 美化＋中英切換已 commit；等實機跑 H（§9） |
| E36441A | 指令產生＋驗證＋實機驗證腳本已 commit（`1cfbe76`、`dca33f1`），未接 CLI／GUI，核心假設未實機驗證（§10） |
| Codex 審查 | 第 2 輪已核准（§8） |

## 0. 先讀這幾件事

1. 儀器控制核心 `n6705c.core` **已複製進本專案**（`src/n6705c/core`），不依賴也不改舊專案 `../N6705C-APP`；核心修正要手動同步。
   - ⚠️ 同一 Python 環境**不要 editable 安裝 `n6705c-app`**：import 名稱同為 `n6705c`，會先找到舊專案那份 core。確認：`python3 -c "import n6705c; print(n6705c.__file__)"` 應指向本專案 `src/`。
2. 舊專案 `HANDOFF.md` 是儀器行為的權威來源：§3.1（`*TRG`）、§3.9（多機不同步）、§4.0（實機發現）。
3. 遠端：私人 repo <https://github.com/vboys60-dev/N6705C-SEQ>（`main`）。換機器 `pip install -e .`。

## 1. 安裝與使用

```bash
pip install -e ".[dev]"           # pyvisa、pyvisa-py、pyusb、libusb-package、numpy、sv-ttk
python3 -m pytest -q              # 309 passed
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
| `src/n6705c_seq/listplan.py`、`synth.py`、`list_gui.py` | List 模式：JSON 計畫、段落→CDW 合成、GUI 分頁（§9） |
| `src/n6705c_seq/e36441a.py` | E36441A：`prepare`／`configure`／`arm`／`restore_commands`、`check()` 純函式（§10） |
| `src/n6705c_seq/cli.py`、`gui.py`、`i18n.py` | `check`／`run`；tkinter GUI（連線測試、可編輯表格、共用時間軸波形、擷取、中英切換）；GUI 字串 `STRINGS={key:(中,英)}` |
| `scripts/hw_list_check.py`、`scripts/hw_e36441a_check.py` | 實機量測腳本（§9.2.3、§10） |
| `packaging/`、`.github/workflows/ci.yml` | PyInstaller（Windows）、CI（§4.12） |

每個通道 = 一個 `TrapezoidArb`：`start_level=low_v`、`start_time=delay`、`rise_time`、`top_level=high_v`、`top_time=hold`、`fall_time`、`end_time=0`、`terminate_last=True`。

## 4. 關鍵設計決定

- **4.1 延遲折進 `start_time`，`ChannelFunction.delay` 保持 0**：次數 1，兩處都設會延遲兩次（測試守著）。
- **4.2 `run_plan` 自己開輸出**：驗證（機台數、模組電壓／時間上限）都在開任何輸出前完成；任何例外或 Ctrl-C 走 `all_outputs_off()`。`_outputs_off` 讀回 `OUTP?` 確認，未關或讀不到丟 `OutputsNotConfirmedOff`，CLI／GUI 不說「已強制關閉」。`OUTP?` 是命令狀態、不是實際軌壓。驗證被拒丟 `PlanRejected`（訊息「開輸出前被拒絕」）。
- **4.2.2 輸入驗證**：數字須有限且 ≥ 0；`current_limit_a` 填了必須 > 0（0 與「不限制」無法區分）；GUI `read_rows` 與 `load_csv` 規則一致。
- **4.3 跨台時間差軟體消不掉**：實機（F.1.1.0）ARB 在 `INIT:TRAN` 當下就開跑、不等 `*TRG`（§4.10），所以每台一道 `INIT:TRAN (@全部通道)`。跨台各自 VISA 往返，抖動約 0.2–2 ms（`TRIGGER_SKEW_SECONDS=2e-3`）；`fire_together`（每台一條執行緒＋Barrier）只縮小差距。真解法是接線（舊 HANDOFF M11.3）。
- **4.4 跨台打平**：不同機台、延遲差 < 2 ms 的通道標警告（同台沒事）。`suggest_assignment` 把互相打平的通道（連鎖也算）視為一組、同組同台，每台最多 4 個，塞不下改 first-fit-decreasing；無解時 `check` 印 `No frame assignment ...`、不寫檔、`--suggest-out` 結束碼 1。第 1 台沒滿時補停用備用列，讓第 2 台仍從第 5 列開始。⚠️ 使用者要自己把 DUT 電源軌換接到新通道。
- **4.5 浮點陷阱**：`0.031-0.029` 是 `0.00199999…`；`_tied` 先 `round(...,9)`（測試 `test_a_gap_of_exactly_the_skew_is_not_a_tie`）。
- **4.6 沿用舊專案的坑**：不支援的查詢會 **timeout**（`--timeout` 預設 10000 ms）；`OUTP?` 是命令狀態；fall 延遲讓「全部關閉」變慢。
- **4.7 GUI**：執行時才連線、跑完就關（VISA session 非執行緒安全）；工作執行緒是 daemon，**執行中拒絕關閉視窗**；位址、Demo、語言存 `~/.n6705c_seq_gui.json`（壞了用預設）；沒有「停止」鈕；「■ 全部關閉」連到已填位址並回讀確認（Demo／執行中停用）。外觀：`gui.setup_theme()` 套 `sv_ttk`、CJK 字型（Windows JhengHei／mac PingFang）、`FONT_SCALE`、Windows 啟動時 DPI aware；通道高度太小時 `draw()` 不畫延遲／維持文字。Windows 實機外觀（DPI、字型、打包後 sv-ttk）未驗。

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
- `packaging/build.ps1`：建置 → `--demo` 自測 → 打包。CI：Windows／macOS／Ubuntu × Python 3.11、3.13；推 `v*` tag 才建 Release。發版：改 `pyproject.toml` version → `git tag vX.Y.Z && git push origin vX.Y.Z`。
- 2026-10-12 Windows 11 本機打包成功、exe 能開、`--demo` 輸出正確；**未接實機**。spec 已排除 `pyvisa_py.testsuite`（否則 PyInstaller 分析當機）；`build.ps1` 用 `Resolve-Path` 絕對路徑（UNC 工作目錄下相對路徑會被誤判成模組）。
- **原生 VISA 探測**（`transport._native_backend_safe()`）：該機 `pyvisa.ResourceManager("")` 會直接讓行程當掉（`STATUS_STACK_BUFFER_OVERRUN`，系統只有 IVI 轉發 DLL、沒有廠商實作），所以用子行程探測（逾時 8 s、結果快取），`backend="auto"` 在不安全時只留 `@py`；入口檔需 `multiprocessing.freeze_support()`。舊專案若有同一份 `transport.py` 建議手動同步。

## 5. ⚠️ 尚未驗證

1. 多通道、帶載、兩台、ms 級精度（需示波器）。
2. **跨台同時 `INIT:TRAN` 的實際時間差**沒量過（同台 < 0.15 ms），2 ms 是保守估計。
3. 建議分配只看起點先後，不處理維持與下降階段；不知道哪條電源軌接哪個通道。
4. GUI 版面只在 macOS 看過；Windows 11 執行檔只在 CI 與本機打包驗過。
5. `fire_together` barrier 逾時 5 s、各台 `*TRG` 無整體逾時，上限靠 VISA `--timeout`。

## 6. 沒做、也沒被要求

打平當錯誤並加 `--allow-skew`；量測主機端兩台 `*TRG` 實際間隔；硬體同步接線（M11.3）；量測／記錄；YAML 設定檔。

## 7. 建議的下一步

1. 兩台各一通道量跨台起點差，填回 `TRIGGER_SKEW_SECONDS`（`src/n6705c/core/group.py`）；差遠大於 2 ms 或需精準同步再做硬體接線。
2. List 模式：有設備時跑 H 實機量測，再開工 I 後半（§9.2.3）。
3. E36441A：有設備時跑 `scripts/hw_e36441a_check.py`，依結果定案再接 CLI／GUI（§10.4）。

## 8. Codex 審查

- 第 1 輪（整個專案）：3 CRITICAL、6 WARNING，已修 8 項（`e93c66a`）。
- 第 2 輪（`8789f7d`）：`_outputs_off` 要求回傳通道＝`populated_channels`、新增 `PlanRejected`、`_wait` 查詢失敗讓整個執行失敗、GUI 設定檔型別檢查、逾時訊息改全域通道號。`VERDICT: APPROVED`。

## 9. ARB List 模式（2026-10-13 起）

每通道一串波形段落，電腦端合成一條 CDW（65,535 點）下載。CSV 時序表不動，GUI 另開分頁。文件：`docs/arb_list_mode.md`（設計理由）、`docs/arb_list_mode_plan.md`（合約 §2、規則 R1–R7 §3、Track §5、必測 §6）。

| Track | 內容 | 狀態 |
|---|---|---|
| P0 / C | `listplan.py`、`execute.run_functions` | ✅ `3032c1a` |
| A / D | `synth.py`（第一版不做頻寬警告，Triangle 沿用 Sine 門檻）／`list_gui.py`＋分頁 | ✅ `c051338` |
| H | `scripts/hw_list_check.py`（傳輸時間、輪間接縫、256 輪對齊、Sine 頻寬） | 前半 ✅ `82bb688`；實機等設備 |
| I | 整合、回填實測常數、版本 0.2.0 | 前半 ✅ `82bb688`；後半等 H |
| 其他 | List 模式英文化＋預覽標註 `ed7b357`；sv-ttk 主題＋DPI＋中英切換 `ce60f70`；小通道隱藏標註 `47d80ca` | ✅ |

已知小事：`synth._validate` 與 `listplan.load_json` 重複檢查（因 GUI 直接組計畫不經 `load_json`，可在 I 後半合併）；測試直接 `root.destroy()` 時 stderr 會印 `invalid command name`，不影響結果。`execute.py` 訊息與 CSV 分頁維持中文，`list_gui.py` 不得有 CJK（測試守著）。`DEMO_SLOTS` 為 N6752A（實機是 4× N6752A；舊專案那台才是 N6762A）。

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
1. 有設備：`python scripts/hw_e36441a_check.py --resource <VISA位址>`，貼回輸出（含 `--csv`）。
2. 依結果定案 `configure_commands`，再接 CLI／GUI：`*IDN?` 分派機型、**混用機型拒絕**、GUI 隱藏擷取（無數位化器）、`outputs_off` 回讀確認；執行前查 `INIT:CONT?`、`OUTP:COUP:CHAN?`、`OUTP:TRAC?`、`OUTP:PAIR?`，不合規拒絕。
3. 不做 driver 抽象基底、不移植 listplan（CDW）。`order_warnings`／`suggest_assignment` 因每台 4 通道可沿用，但 skew 值要重測。
4. 負值不在 `check()` 擋，依賴 `plan._number`；GUI 若繞過 CSV 建 `SeqRow` 需補檢查。
