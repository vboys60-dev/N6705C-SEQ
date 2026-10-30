# N6705C-SEQ

多台 Keysight N6705C 主機的上電時序（power-up sequencing）工具：用 CSV 表格定義每個通道的延遲、電壓、上升／維持／下降時間，一鍵依序輸出。

- 每台主機以單一 `INIT:TRAN` 同時啟動所有通道（已用示波器驗證起始時間）。
- 提供 GUI（pywebview）與命令列（CLI）兩種用法。
- 內建模擬器（`--demo`／GUI 的 Demo 模式），沒有儀器也能試跑。

## 環境需求

| 項目 | 版本 | 說明 |
|---|---|---|
| Python | >= 3.11 | 已在 3.13 測試 |
| `pywebview` | 6.2.1 | GUI 用，隨本專案安裝 |
| `pyvisa` | >= 1.14 | 隨本專案安裝 |
| `pyvisa-py` | >= 0.7 | 純 Python 後端；LAN 一定可用，搭配下面兩項也能走 USB（隨本專案安裝） |
| `pyusb` | >= 1.2 | `pyvisa-py` 走 USB（USBTMC）所需（隨本專案安裝） |
| `libusb-package` | >= 1.0 | 附帶 libusb DLL，讓 `pyusb` 不必另裝系統 libusb（隨本專案安裝） |
| `numpy` | >= 1.26 | 隨本專案安裝 |
| `pytest` | >= 8 | 只有跑測試才需要（`dev` extra） |

GUI：Windows 需要 **Microsoft Edge WebView2 Runtime**（Windows 11 已預裝）；離線電腦請使用 [WebView2 Standalone（offline）Installer](https://developer.microsoft.com/microsoft-edge/webview2/)。macOS 使用系統 WebKit；Linux GUI 尚未測試。

儀器控制核心（`src/n6705c/core`）已內含在本專案，不需要另外安裝舊專案 `N6705C-APP`。

USB 連線（`USB0::...::INSTR`）有兩條路：

1. 系統安裝 Keysight IO Libraries／NI-VISA（LAN、USB、GPIB 都可用）。
2. 不裝 VISA，改用 `pyvisa-py` + `pyusb` + `libusb-package`（只有 LAN 與 USB，沒有 GPIB）。**Windows 上要先把儀器的 USB 驅動換成 WinUSB**（例如用 Zadig），換了之後 Keysight IO Libraries 就認不到這台儀器，兩條路不能並用。這條路尚未在實機驗證。

只用 LAN（`TCPIP...`）則兩者都不需要。

## 安裝

```bash
cd N6705C-SEQ
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"        # 只跑程式可省略 [dev]
```

## 執行

### GUI

```bash
n6705c-seq-gui
# 或
python3 -m n6705c_seq.webgui
```

macOS 也可雙擊 `N6705C-SEQ.command`（內容寫死用 `/opt/anaconda3` 的 conda base，環境不同請自行改）。

GUI 功能：編輯表格、即時預覽波形、載入／儲存 CSV、勾選 Demo 或填入 VISA 資源、執行；
「維持高點 DC」＝忽略維持／下降，結束後輸出保持開啟；「擷取實測波形」＝用內建數位化器抓實測電壓並疊圖（黑線）；
「■ 全部關閉」＝手動關閉所有通道輸出。

### CLI

```bash
# 只檢查表格、印出時間軸，不碰儀器
n6705c-seq check examples/powerup.csv

# 若跨主機起始時間太接近（< 2 ms），輸出建議的通道重新分配
n6705c-seq check examples/powerup.csv --suggest-out adjusted.csv

# 模擬器試跑（不需儀器）
n6705c-seq run examples/powerup.csv --demo --yes

# 實機：每台主機一個 --resource，依主機順序（第 1 台＝CH1–4，第 2 台＝CH5–8…）
n6705c-seq run examples/powerup.csv \
    --resource "USB0::0x2A8D::0x0F02::MY66102013::INSTR" \
    --resource "TCPIP0::192.168.1.20::INSTR"

# 結束後不關輸出，停在高點電壓（DC）
n6705c-seq run examples/powerup.csv --resource "..." --hold-dc
```

其他選項：`--backend`（預設 `auto`；純 Python 後端用 `@py`）、`--timeout`（I/O 逾時 ms，預設 10000）、`--yes`（略過「確定要開輸出？」提示）。
`python3 -m n6705c_seq.cli ...` 與 `n6705c-seq ...` 等價。

## CSV 格式

```csv
enabled,name,delay_ms,low_v,high_v,rise_ms,hold_ms,fall_ms,current_limit_a
1,CH1,1,0,1.8,8,80,3,
1,CH2,21,0,5.75,8,50,3,0.5
```

- 第 N 個資料列＝全域通道 N；每台主機 4 通道（CH1–4 在第 1 台，CH5–8 在第 2 台…）。
- 時間單位 ms、電壓 V、電流 A；`current_limit_a` 可留空（不設電流限制）。
- `enabled` 為 0 的列不輸出。
- 範例見 `examples/powerup.csv`（8 通道）與 `examples/hw_smoke.csv`（單通道實機煙霧測試）。

## 分析分頁（離線 DLOG 報告）

GUI 的第三分頁「分析」用來開啟 `.dlog` 檔（設計目標為千萬筆等級，超過 1000 萬筆的檔案用這裡看），內嵌 Viewer 顯示波形與統計，並可匯出 PNG、HTML 報告、Marker JSON 與 CSV。

- **與電源控制互斥**：進入分析分頁即鎖住電源控制（執行、全部關閉等反灰），離開分頁才釋放；電源控制執行中無法進入分析。
- 開檔與匯出使用原生對話框。分析期間請勿移動或覆寫來源 `.dlog`；讀取中若被截短，mac／Linux 可能崩潰（見 `docs/analysis-contract.md` §6.6）。
- 快取與偏好放在使用者可寫目錄（Windows：`%LOCALAPPDATA%`；其他：`$XDG_CACHE_HOME` 或 `~/.cache`），不寫入安裝資料夾。
- **開關**：`src/n6705c_seq/analysis/contracts.py` 的 `ANALYSIS_ENABLED`（第 19 行附近）。目前為 `False`，分頁隱藏，畫面與舊版相同；改為 `True` 後重開 GUI 才會出現分頁。
- 架構與契約：`docs/analysis-contract.md`（數字與型別以 `contracts.py` 為準）。
- Windows 上的實機驗收尚未完成，見 `HANDOFF_WINDOWS.md` §6。

## Windows 執行檔

**使用者（不需要 Python）：** 從 GitHub Releases 下載 `N6705C-SEQ-<版本>-windows-x64.zip`，解壓縮後執行 `N6705C-SEQ.exe`（GUI）；命令列版是同資料夾的 `n6705c-seq-cli.exe`。

- GUI 需要 Microsoft Edge WebView2 Runtime（Windows 11 已預裝）；不隨 zip 打包，離線電腦請先用 Standalone（offline）Installer 安裝。
- 連 USB 儀器前，電腦要先安裝 **Keysight IO Libraries Suite** 或 **NI-VISA**（執行檔不含 VISA 驅動）；或改用內含的 `pyvisa-py` + `pyusb` + libusb DLL，但儀器的 USB 驅動須是 WinUSB（見上方，尚未實機驗證）。只用 LAN 且 VISA 資源填 `TCPIP...` 時，兩者都不需要。
- 沒有程式碼簽章，第一次執行 Windows SmartScreen 會警告：點「其他資訊」→「仍要執行」。

**發佈者：**

```bash
# 本機（Windows）：pip install -e ".[build]" 後
.\packaging\build.ps1          # 產生 dist\N6705C-SEQ-<版本>-windows-x64.zip，並先跑 CLI --demo 與 GUI --selftest

# GitHub：改 pyproject.toml 的 version，然後
git tag v0.1.0 && git push origin v0.1.0   # CI 測試 → 建置 → 自動建立 Release 並附上 zip
```

`.github/workflows/ci.yml`：每次 push／PR 在 Windows、macOS、Ubuntu（Python 3.11、3.13）跑 pytest；通過後在 Windows 建置並上傳 zip；推 `v*` tag 時另外發佈 Release。

## 測試

```bash
PYTHONPATH=src python3 -m pytest -q          # 不需儀器、不開 GUI 視窗；大檔與 74445 預設 skip
PYTHONPATH=src python3 -m pytest -q tests/analysis   # 只跑分析模組
node --test tests/js                          # 需要 node（CI 用 node 20）
```

分析模組的選用環境變數（都預設關閉）：

| 變數 | 作用 |
|---|---|
| `N6705C_ANALYSIS_BIG=1` | 10M 筆冷／熱開檔與 12 欄全範圍 CSV 預算（`tests/analysis/test_memory.py`）。使用合成檔，需要磁碟空間與時間 |
| `N6705C_ANALYSIS_BIG=30` | 再加 30M 冷開檔、全範圍統計與分桶的不 OOM 驗證（`test_memory.py`）。需要較多記憶體 |
| `N6705C_ANALYSIS_REAL_CORE=1` | 啟用 `tests/analysis/test_server_core.py`：HTTP server 接真 core／storage 的整合測試 |
| `N6705C_PARITY_74445=<1GB .dlog 路徑>` | 跑 `tests/analysis/parity_tools/test_parity.py` 的 74445 分桶對拍；未設則 skip |

**Parity（Python 對拍原版 JS）**：JS 一律從 `tests/analysis/parity_tools/original_before_python.html` 抽取（sha1 須為 `contracts.ORIGINAL_HTML_SHA1`，不得改、不得刪）。從 repo 根目錄可手動執行：

```bash
python3 tests/analysis/parity_tools/extract_original_js.py [輸出目錄]
python3 tests/analysis/parity_tools/compare.py <js前綴> <py前綴>
python3 tests/analysis/parity_tools/compare_buckets.py <dlog路徑> <js輸出前綴> <快取目錄>
```

**不隨專案搬移的分析工具**（留在 `報告分析升級版/來源_DLOG_Viewer/server/parity_tools/`，不進套件、不進測試）：`dlog_poc.py`、`make_synth.py`、`make_probe.py`（效能對照）、`bench_local.js`，以及該資料夾的 `README.md`。

## 注意

- 同一台主機內各通道同時啟動；**跨主機**的啟動沒有硬體同步，約有 ms 級誤差（見 `HANDOFF.md`），表格中相差 < 2 ms 的跨主機通道會被警告。
- 執行失敗或中斷時會強制關閉所有輸出並讀回確認；`--hold-dc`／GUI 的「維持高點 DC」是唯一會刻意留著輸出的情況，請用「全部關閉」手動關。
- 設計決策、實機發現與未驗證項目都記在 `HANDOFF.md`。
