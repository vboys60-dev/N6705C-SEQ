# N6705C-SEQ　交接文件

多台 N6705C 的**上電時序**工具。讀 CSV 表（每通道：延遲、低/高電壓、上升/維持/下降時間），
在多台主機上依表上電。第一版只做時序，不做量測。目前設計兩台（8 通道），程式不限台數。

最後更新：2026-10-12　狀態：模擬器全過（29 tests），**尚未接過實機**。

## 0. 先讀這幾件事

1. 這個專案**不改**舊專案 `../N6705C-APP`。它把舊專案的 `core/` 當函式庫用（editable 安裝的 `n6705c-app`）。
2. 舊專案的 `HANDOFF.md` 是儀器行為的權威來源，特別是 §3.1（`*TRG`）、§3.9（多機台不同步）、§4.0（實機發現）。
3. 已 `git init`（2026-10-12）。

## 1. 安裝與使用

```bash
cd N6705C-SEQ
pip install -e . --no-deps        # 舊專案需已安裝（n6705c-app）
python3 -m pytest -q              # 29 passed

n6705c-seq check examples/powerup.csv                       # 只驗證、印時間軸，不碰儀器
n6705c-seq check plan.csv --suggest-out fixed.csv           # 有跨台打平時，另存建議的通道分配
n6705c-seq run plan.csv --demo --yes                        # 模擬器
n6705c-seq-gui                                              # 圖形介面（或 python3 -m n6705c_seq.gui）
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
| `src/n6705c_seq/execute.py` | `run_plan`：驗證 → 設定電壓／電流並開輸出 → load → arm → 同時發 `*TRG` → 等待 → 關輸出 |
| `src/n6705c_seq/cli.py` | `check`／`run` 兩個子指令 |
| `src/n6705c_seq/gui.py` | tkinter 介面（`n6705c-seq-gui`）：機台 VISA 位址（掃描／測試連線）、可編輯表格、各通道共用時間軸波形、開／存 CSV、執行。位址存在 `~/.n6705c_seq_gui.json`。顏色只是介面用，不寫進 CSV。執行時才連線、跑完就關（不保留連線，避開 VISA session 非執行緒安全） |
| `tests/` | `test_plan.py`（15）、`test_execute.py`（7）、`test_cli.py`（8）、`test_gui.py`（1，只測波形轉折點） |
| `docs/plan.md` | 實作計畫，已更新到打平建議（Task 5）；測試程式碼以 `tests/` 為準 |

只用標準函式庫（csv、argparse、threading）。沒有 YAML。Python 3.13、pytest 8.4.2。

每個通道 = 一個 `TrapezoidArb`：`start_level=low_v`、`start_time=delay`、`rise_time`、`top_level=high_v`、`top_time=hold`、`fall_time`、`end_time=0`，`terminate_last=True`。

## 4. 關鍵設計決定

### 4.1 延遲折進 `start_time`，`ChannelFunction.delay` 保持 0
次數是 1，所以只發生一次。兩個地方都設會延遲兩次。測試 `test_only_enabled_rows_become_trapezoids_with_delay_as_start_time` 守著這點。

### 4.2 `run_plan` 自己開輸出
ARB runner 不會自己把輸出打開。所以順序是：驗證（機台數、每台模組的電壓／時間上限）→ `VOLT`（一律）／`CURR`（只有表上有給）→ 開輸出 → load → arm（全部成功或全部不做）→ `*TRG`。驗證都在**開任何輸出之前**完成。
任何例外或 Ctrl-C 都走 `except BaseException` → `group.all_outputs_off()`。

### 4.3 機台之間的時間差是軟體消不掉的
一個 `*TRG` 能同時啟動同一台上所有已 arm 的通道（舊 HANDOFF §3.1）。跨台是各自的 VISA 往返，實測抖動約 0.2–2 ms（`TRIGGER_SKEW_SECONDS = 2e-3`）。
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

## 5. ⚠️ 尚未驗證

1. **整個 `run` 沒接過實機。** 模擬器測試只證明指令序列和流程，不證明軌壓波形。建議先用假負載或空接，接示波器看。
2. **`TRAP:END:TIM 0`** 在真機上的行為沒驗證（`end_time=0` 是否被接受、`terminate_last` 結束後準位）。
3. **同時發 `*TRG` 的實際時間差**沒有量過。2 ms 是保守估計，不是量測值。建議用示波器抓兩台的起點差。
4. **建議的分配只看起點先後**，不處理維持與下降階段的順序。
5. 建議結果不知道哪條電源軌實際接哪個通道，只是起點。

- GUI 版面只在 macOS 用截圖看過；沒有「停止」鈕（時序只有約 100 ms）。

## 6. 沒做、也沒被要求的事

- 打平當錯誤並加 `--allow-skew`（使用者選了建議方案，沒選這個）。
- 量測主機端兩台 `*TRG` 的實際送出間隔。
- 硬體同步接線（M11.3）。
- 量測／記錄（第一版範圍明確排除）。
- YAML 設定檔（CSV 已夠用）。
- 無（`git init` 與 `docs/plan.md` 已完成）。

## 7. 建議的下一步（依序）

1. 假負載或空接，單台、單通道跑一次，示波器確認波形與 `TRAP:END:TIM 0` 行為。
2. 兩台各一通道，量跨台起點差，把實測值填回 `TRIGGER_SKEW_SECONDS`（在舊專案 `core/group.py`）。
3. 若實測差遠大於 2 ms 或有需求精準同步，再做硬體接線（M11.3）。
4. （已完成）`git init`、`docs/plan.md` 已補上打平建議。
