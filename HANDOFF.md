# N6705C-SEQ　交接文件

多台 N6705C 的**上電時序**工具。讀 CSV 表（每通道：延遲、低/高電壓、上升/維持/下降時間），
在多台主機上依表上電。第一版只做時序，不做量測。目前設計兩台（8 通道），程式不限台數。

最後更新：2026-10-12　狀態：模擬器全過（46 tests）。**單台實機（4× N6752A）單通道已跑通**（見 §4.8），跨台同步仍未量。Codex 審查第 2 輪已核准（見 §8）。

## 0. 先讀這幾件事

1. 儀器控制核心 `n6705c.core` **已複製進本專案**（`src/n6705c/core`，與舊專案 `../N6705C-APP` 當時內容相同，import 名稱不變）。不再依賴 `n6705c-app`，也**不改**舊專案；之後核心有修正要手動同步。
2. 舊專案的 `HANDOFF.md` 是儀器行為的權威來源，特別是 §3.1（`*TRG`）、§3.9（多機台不同步）、§4.0（實機發現）。
3. 遠端：私人 repo <https://github.com/vboys60-dev/N6705C-SEQ>（`main`）。換機器只要 `pip install -e .`。

## 1. 安裝與使用

```bash
cd N6705C-SEQ
pip install -e ".[dev]"           # 依賴：pyvisa、pyvisa-py、numpy（不需要舊專案）
python3 -m pytest -q              # 46 passed

n6705c-seq check examples/powerup.csv                       # 只驗證、印時間軸，不碰儀器
n6705c-seq check plan.csv --suggest-out fixed.csv           # 有跨台打平時，另存建議的通道分配
n6705c-seq run plan.csv --demo --yes                        # 模擬器
n6705c-seq-gui                                              # 圖形介面（或 python3 -m n6705c_seq.gui）
# 或在 Finder 雙擊 N6705C-SEQ.command
n6705c-seq run plan.csv --resource <VISA1> --resource <VISA2>   # 實機，依機台順序
```

結束碼：0 正常；1 中止／錯誤（或 `--suggest-out` 無解）；2 CSV 有誤；130 Ctrl-C。
`run` 預設會問 `Switch outputs ON and run? [y/N]`，`--yes` 跳過。

## 2. CSV 格式

欄位：`enabled,name,delay_ms,low_v,high_v,rise_ms,hold_ms,fall_ms,current_limit_a`（最後一欄可省略或留空）。

- **列的順序就是通道編號**：第 1–4 列 = 機台 1 的 CH1–4，第 5–8 列 = 機台 2 的 CH1–4。停用的列也要留著佔位。
- 時間單位 ms，電壓 V，`current_limit_a` 單位 A；空白表示不設電流限制。
- 錯誤訊息會指出 `line N, column X`。
- 範例 `examples/powerup.csv` 是使用者截圖那張表（CH7、CH8 停用）。

## 3. 架構

| 檔案 | 責任 |
|---|---|
| `src/n6705c_seq/plan.py` | `SeqRow`、`load_csv`／`write_csv`、`to_functions`、打平偵測 `order_warnings`、`suggest_assignment`、`timeline` |
| `src/n6705c_seq/execute.py` | `run_plan`：驗證 → 設定電壓／電流並開輸出 → load → 每台一道 `INIT:TRAN` 同時啟動（`start_together`）→ 等待 → 關輸出。另有 `connect_group` |
| `src/n6705c_seq/cli.py` | `check`／`run` 兩個子指令 |
| `src/n6705c_seq/gui.py` | tkinter 介面（`n6705c-seq-gui`）：機台 VISA 位址（掃描／測試連線）、可編輯表格、各通道共用時間軸波形、開／存 CSV、執行。位址存在 `~/.n6705c_seq_gui.json`。顏色只是介面用，不寫進 CSV。執行時才連線、跑完就關（不保留連線，避開 VISA session 非執行緒安全） |
| `tests/` | `test_plan.py`（17）、`test_execute.py`（11）、`test_cli.py`（9）、`test_gui.py`（6：波形轉折點、刻度、設定檔） |
| `docs/plan.md` | 實作計畫，已更新到打平建議（Task 5）；測試程式碼以 `tests/` 為準 |

本專案只用標準函式庫；核心用 pyvisa、pyvisa-py、numpy。沒有 YAML。Python 3.13、pytest 8.4.2。

每個通道 = 一個 `TrapezoidArb`：`start_level=low_v`、`start_time=delay`、`rise_time`、`top_level=high_v`、`top_time=hold`、`fall_time`、`end_time=0`，`terminate_last=True`。

## 4. 關鍵設計決定

### 4.1 延遲折進 `start_time`，`ChannelFunction.delay` 保持 0
次數是 1，所以只發生一次。兩個地方都設會延遲兩次。測試 `test_only_enabled_rows_become_trapezoids_with_delay_as_start_time` 守著這點。

### 4.2 `run_plan` 自己開輸出
ARB runner 不會自己把輸出打開。所以順序是：驗證（機台數、每台模組的電壓／時間上限）→ `VOLT`（一律）／`CURR`（只有表上有給）→ 開輸出 → load → 每台一道 `INIT:TRAN (@全部通道)`（觸發源設 IMM）。驗證都在**開任何輸出之前**完成。
任何例外或 Ctrl-C 都走 `except BaseException` → `group.all_outputs_off()`。

### 4.2.1 關輸出要「確認」（Codex 審查後加的）
舊專案的 `all_outputs_off()` 設計成**絕不丟例外**，寫失敗只記 log。所以 `run_plan` 用 `_outputs_off`：呼叫後讀回 `OUTP?`，仍為開或讀不到就丟 `OutputsNotConfirmedOff`（含原本的錯誤訊息）。CLI／GUI 遇到這個例外**不再說「輸出已強制關閉」**，要使用者手動確認。注意 `OUTP?` 是命令狀態、不是實際軌壓（舊 §4.0.1），只證明寫入有送到。

### 4.2.2 輸入驗證
- 所有數字必須是有限值（拒絕 `nan`／`inf`）且 ≥ 0。
- `current_limit_a` 若有填必須 > 0；填 `0` 會被拒絕（`ChannelFunction.current` 用 0.0 代表「不限制」，兩者無法區分，所以直接擋）。空白 = 不限制。
- GUI 的 `read_rows` 與 `load_csv` 規則一致。

### 4.3 機台之間的時間差是軟體消不掉的
**實機（F.1.1.0）的 ARB 在 `INIT:TRAN` 當下就開跑，不等 `*TRG`**（見 §4.10），所以改成每台一道 `INIT:TRAN (@全部通道)`。舊 HANDOFF §3.1 的 `*TRG` 說法對 ARB 不成立（對 `ACQ` 仍成立）。跨台是各自的 VISA 往返，實測抖動約 0.2–2 ms（`TRIGGER_SKEW_SECONDS = 2e-3`）。
`fire_together` 用每台一條執行緒加 `threading.Barrier`，只能縮小差距，不能消除。真正的解法是接線（舊 HANDOFF M11.3：數位埠／Trigger In BNC）。

### 4.4 跨台打平的處理（最近做完的功能）
- **偵測**：不同機台上、延遲差 < 2 ms 的通道成對標警告。同一台上打平沒事（同一個 `*TRG`）。
- **建議**：`suggest_assignment` 把互相打平的通道（連鎖也算：A~B、B~C 三個放一起）視為一組，同組必須同台；依延遲順序塞進各台，每台最多 4 個，塞不下時改用 first-fit-decreasing。
- **無解**（例如 5 個通道同延遲）：`check` 印 `No frame assignment ...`，不寫檔，加 `--suggest-out` 時結束碼 1。
- **輸出表**：第 1 台沒滿時補停用的備用列，讓第 2 台仍從第 5 列開始，`run` 的通道對應不變。
- 範例表的建議結果：CH1–3 留在機台 1，CH4–6 全放機台 2（CH4、CH5 的 29 ms 因此同台）。
- ⚠️ **使用者必須自己把 DUT 電源軌接到新的通道**，工具無法換線。

### 4.5 浮點陷阱
`0.031 - 0.029` 是 `0.00199999…`，剛好 2 ms 的差距會被誤判成打平。`_tied` 先 `round(..., 9)` 再比較。測試 `test_a_gap_of_exactly_the_skew_is_not_a_tie`；我確認過換回未取整的寫法這個測試會失敗。

### 4.6 沿用舊專案的坑
- N6762A 不支援的查詢會 **timeout**，不是報錯（舊 §4.0）。
- `OUTP?` 回的是命令狀態，不是實際軌壓（舊 §4.0.1）。
- fall 延遲會讓「全部關閉」變慢（舊 §3.4）。
- `run` 的 `--timeout` 預設 10000 ms，就是為了這類 timeout。

### 4.7 GUI（`gui.py`，tkinter）
- 執行時才連線、跑完就關；不保留連線（VISA session 非執行緒安全）。工作執行緒是 daemon，所以**執行中拒絕關閉視窗**（否則會跳過 `run_plan` 的關輸出）。
- 位址與 Demo 勾選存在 `~/.n6705c_seq_gui.json`；檔案壞掉就用預設值。
- 刻度 `_tick_step` 是 1/2/5×10^k，長序列（> 40 s）不會當掉。
- 雙擊 `N6705C-SEQ.command` 啟動；路徑寫死 `/opt/anaconda3`，換環境要改。

### 4.8 實機測試紀錄（2026-10-12，單台、CH1、空接、無示波器）
- 機台：`USB0::0x2A8D::0x0F02::MY66102013::INSTR`，4 個 **N6752A**（51 V／10.2 A），韌體 `F.1.1.0`，`*IDN?` 開頭是 `Agilent Technologies`。
- **這台的 `*IDN?` 型號欄位是固定亂碼**（`\xe4\x9b\xcf\xbe\xe1\xc4F`），舊函式庫用 ASCII 解碼會丟 `UnicodeDecodeError`。本專案用 `execute.connect_group()`（把 pyvisa `encoding` 設成 latin-1）繞過，舊專案沒改；連線時會印一行「not an N6705 family mainframe」警告，可忽略。CLI／GUI 都走這個函式。
- 用 `MEAS:VOLT?` 連續取樣（約 40 Hz，沒有示波器）看 0.5 s 延遲、1 s 上升到 1 V、2 s 維持、1 s 下降：起點在 trigger 後約 0.53 s，斜率線性，維持 1.0003 V，下降後回到起始準位並停住，**`TRAP:END:TIM 0` 在 N6752A 上可用**。錯誤佇列前後皆為 `No error`，最後讀回輸出關閉。
- 空接時 `MEAS:VOLT?` 在設定值 0 V 時讀到約 **20 mV**（執行前後相同），應是量測偏移或空載，沒用示波器確認。
- `MEAS:VOLT?` 輪詢只有 ~25 ms 解析度，不能驗證 ms 級精度；`examples/hw_smoke.csv` 是 150 ms 的冒煙測試表。
- **機台內建數位化器可當示波器用**（`docs/hw_smoke_capture.png`）：`SENS:SWE:POIN 6000`、`SENS:SWE:TINT 40.96E-6`、`SENS:SWE:OFFS:POIN 0`、`TRIG:ACQ:SOUR BUS`（皆 `,(@1)`），在 `run_plan` **之前** `INIT:IMM:ACQ (@1)`，同一個 `*TRG` 就會同時啟動 ARB 與擷取，之後 `FETC:ARR:VOLT? (@1)`。`hw_smoke.csv` 實測：0.5 V 交越在 19.42 ms／139.55 ms（程式值 20／140 ms），差 < 0.6 ms；上升、維持、下降都正確。波形前後基線都是 ~20 mV（偏移，非時序問題），使 0.1 V 交越提早約 0.4 ms。
  - 這是**同一台**自己的觸發時間基準，量不到**跨台**差。跨台仍需外部儀器或 §7 第 2 點的做法。
  - `FETC:ARR:CURR?` 預設無效（+303 no valid acquisition），要先啟用電流擷取；沒做。
- **`DISP:VIEW SCOPe` 在韌體 F.1.1.0 不支援**（-224），只有 `METER1`／`METER4`；前面板 Scope View 只能手動切。`HCOP:SDUM:DATA?` 可回 320×240 PNG 螢幕截圖。

### 4.10 實測發現：ARB 在 INIT 當下就開跑（已修）
- 示波器量到 CH1→CH2 起點差 23.9 ms（設定 20 ms）。數位化器重現：用舊作法（逐通道 `arm`＝逐通道 `INIT:TRAN`，再一個 `*TRG`）時，CH1/CH2/CH3 相對 `*TRG` 偏移 −36／−14／−1 ms，剛好是各通道 INIT 間隔。
- 驗證：arm 好、**不送 `*TRG`**，輸出自己升到 1.8 V；觸發源改成 PIN1／EXT 也一樣。這台的 `TRIG:TRAN:SOUR` 對 ARB 沒有作用。
- 修法：`start_together`——每台一道 `INIT:TRAN (@1,2,3)`，不再送 `*TRG`，跨台用 `fire_together` 的 barrier 同時送。實測 3 通道（CH1 1 ms／CH2、CH3 21 ms）50% 上升／下降點都在設定值 0.15 ms 內。
- 之前單通道 hw_smoke「差 0.58 ms」其實就是 INIT→`*TRG` 的間隔，不是儀器延遲。

### 4.11 維持高點 DC 選項（`--hold-dc`／GUI 勾選）
- 忽略 hold／fall：改用 `RampArb`（delay → rise → 高點，`end_time=0`），`terminate_last` 把高點留成 DC；`run_plan(hold_dc=True)` 結束後**不關輸出**（出錯仍強制關）。
- 實機：CH1 升到 1 V 後 3 s 仍 1.0004 V，斷線重連後輸出仍開著。GUI 有「■ 全部關閉」按鈕（連到已填的位址，`outputs_off` 關閉並讀回確認；Demo 模式不可用，執行中停用）；CLI 沒有對應指令。

### 4.9 GUI 擷取實測波形（先做過、取消，後來重新加回）
`run_plan(capture={})` 與 GUI 勾選「擷取實測波形（數位化器）」：實測電壓以黑線疊在預期波形上。實作在 `execute._arm_capture`／`_fetch`。
- 擷取在 `start_together` 的每台執行緒裡，於 `INIT:TRAN` 後立刻送 `*TRG`（ARB 在 INIT 起跑，擷取等 `*TRG`），所以擷取時間軸比 ARB 起點晚一個 INIT→`*TRG` 間隔（同台實測 < 0.15 ms）。
- `INIT:ACQ` 要在 ARB 啟動**之前**；要等整段記錄完成**再** `OUTP OFF`（否則擷取作廢），所以擷取時會多等到記錄結束。
- 取回用 `FORM REAL`＋`FORM:BORD SWAP` 二進位（模擬器只回二進位）；每通道 6000 點、`SENS:SWE:TINT` 取 20.48 µs 的整數倍。
- 取回失敗只記錄在 `capture["error"]`，不影響輸出開關。模擬器擷取到平的 0 V，只能在實機驗證。
- 實機（3 通道）：50% 上升／下降點與設定相差 < 0.15 ms；`hold_dc` 時末端維持高點。

## 4.12 發佈與打包（Windows 11）

- 使用者下載 GitHub Release 的 `N6705C-SEQ-<版本>-windows-x64.zip`，解壓後執行 `N6705C-SEQ.exe`（GUI）或 `n6705c-seq-cli.exe`。
- **USB 儀器要先在該電腦裝 Keysight IO Libraries 或 NI-VISA**，執行檔不含 VISA 驅動。LAN（`TCPIP...`）由內含的 `pyvisa-py` 處理。
- 沒有程式碼簽章，SmartScreen 會警告。
- `packaging/`：`n6705c_seq.spec`（PyInstaller onedir，GUI + CLI 兩個 exe）、`build.ps1`（建置 → 用 `--demo` 自我測試 → 打包 zip）、`gui_entry.py`／`cli_entry.py`。
- `.github/workflows/ci.yml`：push／PR 在 Windows、macOS、Ubuntu × Python 3.11、3.13 跑 pytest；通過後建 Windows zip；推 `v*` tag 才建立 Release。
- 發版：改 `pyproject.toml` 的 version → `git tag vX.Y.Z && git push origin vX.Y.Z`。
- 已驗證：CI 全綠、macOS 本機打包的 CLI／GUI 能跑。**未驗證**：真正在 Windows 11 開 GUI、接實機。

## 5. ⚠️ 尚未驗證

1. ~~整個 `run` 沒接過實機~~：單台單通道空接已驗證（§4.8）。**仍未驗證**：多通道、帶載、兩台、ms 級精度（需示波器）。
2. ~~`TRAP:END:TIM 0` 真機行為~~：N6752A 上被接受，結束後停在低準位（§4.8）。
3. **跨台同時發 `INIT:TRAN` 的實際時間差**沒有量過（同台已量：< 0.15 ms）。2 ms 是保守估計，不是量測值。建議用示波器抓兩台的起點差。
4. **建議的分配只看起點先後**，不處理維持與下降階段的順序。
5. 建議結果不知道哪條電源軌實際接哪個通道，只是起點。
6. GUI 版面只在 macOS 用截圖看過；沒有「停止」鈕（時序只有約 100 ms）。
7. ~~`_wait` 把狀態查詢失敗當成完成~~：第 2 輪已修。`_running()` 直接走 `arb.is_running`，查詢失敗會丟例外 → 中止並確認關輸出。
8. Windows 11 執行檔只在 CI 建過，沒在實機開過（§4.12）。
9. `fire_together` 的 barrier 逾時 5 s、各台 `*TRG` 仍沒有整體逾時，實際上限靠 VISA `--timeout`。

## 6. 沒做、也沒被要求的事

- 打平當錯誤並加 `--allow-skew`（使用者選了建議方案，沒選這個）。
- 量測主機端兩台 `*TRG` 的實際送出間隔。
- 硬體同步接線（M11.3）。
- 量測／記錄（第一版範圍明確排除）。
- YAML 設定檔（CSV 已夠用）。

## 7. 建議的下一步（依序）

1. 假負載或空接，單台、單通道跑一次，示波器確認波形與 `TRAP:END:TIM 0` 行為。
2. 兩台各一通道，量跨台起點差，把實測值填回 `TRIGGER_SKEW_SECONDS`（在舊專案 `core/group.py`）。
3. 若實測差遠大於 2 ms 或有需求精準同步，再做硬體接線（M11.3）。
4. 做完上面再回頭跑 Codex 審查第 2 輪（§8）。

## 8. Codex 審查進度（`/codex-review:code`）

- **第 1 輪（整個專案）**：3 CRITICAL、6 WARNING。已修 8 項（關閉視窗中途、0 A 電流限制、關輸出未確認、nan/inf、barrier、長序列刻度、設定檔壞掉、`--suggest-out`），commit `e93c66a`，並各補測試。**未修**：§5 第 7 點。
- **第 2 輪**（複查 `e93c66a`）：2 CRITICAL、2 WARNING，全部已修並補測試（未 commit）：
  - `_outputs_off` 讀回不完整（`{}`、少通道）會被當成已關閉 → 現在要求回傳的通道 = `populated_channels`，否則 `OutputsNotConfirmedOff`。
  - 驗證失敗（空表、台數不足、模組範圍）卻印「輸出已強制關閉」→ 新增 `PlanRejected(ValueError)`，CLI／GUI 改說「開輸出前被拒絕」。
  - §5 第 7 點 → 查詢失敗現在讓整個執行失敗（見上）。
  - GUI 設定檔加型別檢查（`resources` 為字串清單、`demo` 為 bool）。
  - 另採納 SUGGESTION：逾時訊息的通道改為全域編號。
  - 結果：`VERDICT: APPROVED`。
- 第 2 輪沙箱為唯讀，Codex 只跑得了 26 個測試；完整 42 個我這邊全過。
