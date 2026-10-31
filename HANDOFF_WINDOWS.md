# N6705C-SEQ　Windows 版交接文件

通用設計見 `HANDOFF.md`；本文只講 Windows 的散佈、使用、已知問題。更新：2026-10-31（環境與測試見 §7）。

## 1. 三種給 Windows 用戶的形式

| 形式 | 位置 | 使用者需要 | 用法 |
|---|---|---|---|
| exe（onedir zip） | GitHub Release `N6705C-SEQ-<版本>-windows-x64.zip` | Microsoft Edge WebView2 Runtime（Windows 11 已預裝） | 解壓 → `N6705C-SEQ.exe`（GUI）／`n6705c-seq-cli.exe` |
| Python 資料夾 | `dist_python/N6705C-SEQ/`（本機產物，未進 git；2026-10-14 已刪除，因為 0.1.3 版沒有 List mode，要發時重新產生；requirements.txt 需含 `pywebview==6.2.1`，run.py 需呼叫 `n6705c_seq.webgui.main`） | Python 3.11+、Microsoft Edge WebView2 Runtime | `pip install -r requirements.txt` → `python run.py`（無參數＝GUI，有參數＝CLI） |
| bat 安裝版 | 2026-10-12 做過，**目前資料夾已不存在**（`dist_windows/`） | 無（會自動下載裝 Python 3.12.8） | `install.bat` → `start.bat` |

GUI 需要 Microsoft Edge WebView2 Runtime，不隨 exe 打包；Windows 11 已預裝，離線電腦請使用 [WebView2 Standalone（offline）Installer](https://developer.microsoft.com/microsoft-edge/webview2/)。

`run.py` 必須保留 `if __name__ == "__main__"` 與 `multiprocessing.freeze_support()`：VISA 探測用 multiprocessing，Windows 會重新匯入主程式，少了保護會再開一個 GUI。

## 2. 打包與發版（exe）
- 本機：`pip install -e ".[build]"` → `.\packaging\build.ps1`（建置 → `--demo` 自我測試 → 產 zip）。只能在 Windows 打。
- CI：`.github/workflows/ci.yml`，推 `v*` tag 才建 Release。發版：改 `pyproject.toml` version → `git tag vX.Y.Z && git push origin vX.Y.Z`。
- ⚠️ zip 檔名取自 `pyproject.toml` 的 version（目前 0.1.3），發版前記得與 tag 一致。
- 沒有程式碼簽章，SmartScreen 會警告。

## 3. 已知問題與處理
1. **系統 VISA 損壞 → 整個程式消失**（`viOpenDefaultRM` access violation／`STATUS_STACK_BUFFER_OVERRUN`，try/except 接不到）。實例：使用者機器只裝了 IVI 共用元件 `visa32.dll`，沒有廠商實作。
   - 修法：`src/n6705c/core/transport.py` 的 `_native_backend_safe()` 在子行程探測（8 s 逾時、快取），不安全就只用 `@py`。`packaging/*_entry.py` 與 `run.py` 都要 `freeze_support()`。
   - 代價：第一次連線可能多等 ≤ 8 s；此時 USB 位址連不上，只有 LAN（`TCPIP0::<ip>::inst0::INSTR`）可用。要用 USB 就得完整安裝 Keysight IO Libraries 或 NI-VISA；或改走 `pyvisa-py` + `pyusb` + libusb（儀器驅動需換成 WinUSB，未實機驗證，見 `HANDOFF.md` §4.12）。
   - 診斷指令：`python -X faulthandler -c "import pyvisa; pyvisa.util.get_debug_info()"`。
2. **GUI 已移除「掃描」按鈕**：VISA 位址改為直接貼上。舊版掃描曾在 GUI 主執行緒卡住，後來又踩到上一點的當機。`list_resources` 仍在 core 裡，GUI 不再呼叫。
3. PyInstaller spec 要過濾 `pyvisa_py.testsuite`，否則分析階段當掉；`build.ps1` 用絕對路徑呼叫 exe（UNC 工作目錄下相對路徑會被 PowerShell 5.1 誤判成模組）。細節見 `HANDOFF.md` §4.12–4.13。
4. **USB 找不到設備（待驗證，2026-10-13 使用者回報）**：exe 與 `python run.py` 走 USB 都找不到，但 Keysight Connection Expert 看得到 `USB0::0x...::INSTR`。推測：`_native_backend_safe()` 探測誤判（逾時 8 s 或子行程失敗）→ 退到 `@py` → pyusb 需 WinUSB，Keysight 驅動不是 → 找不到。**這只是推測，尚無證據。**
   - v0.1.3 加了診斷：連線失敗的錯誤列出嘗試過的後端（`['@py']` 或 `['', '@py']`）與各自錯誤；USB/GPIB 位址而原生 VISA 未啟用時會明講；GUI「測試連線」失敗跳完整錯誤視窗。
   - 有環境時判讀：`tried backends ['@py']` ＝ 探測誤判（再拉長逾時或改探測方式）；`['', '@py']` 且都失敗 ＝ 問題在別處。也可直接跑：`python -c "import time,pyvisa;t=time.time();rm=pyvisa.ResourceManager('');print(rm,time.time()-t);print(rm.list_resources())"`。
5. 視窗標題已改為「N6705C 上電時序工具-品勛科技股份有限公司 免費提供」。

## 4. 尚未驗證（Windows）
- 接實機（LAN）：連線、單台／多台、多通道。
- GUI 在使用者機器上「測試連線／執行」走 `@py` 的完整流程（CLI 路徑已驗證不當機）。
- **USB 實機連線**（見 §3.4）：v0.1.3 的診斷訊息尚未在 Windows 上看過。
- v0.1.3 的 exe 由 CI 在 tag 推送後建置，尚未確認 Release 產物。

## 5. 目前狀態
- Windows 部分最後一次改動是 `713b47f`（診斷訊息，0.1.3），tags 有 `v0.1.0`～`v0.1.3`。目前 git 狀態和下一步見 `HANDOFF.md`。
- Windows 待辦：在有 Windows＋儀器的環境按「測試連線」，把錯誤視窗內容回報 → 依 §3.4 判讀；確認 CI Release 產物；在乾淨的 Windows 上驗證 exe 的 GUI。

## 6. Windows 端到端驗收清單（分析分頁，未實機驗）

> **狀態：未實機驗。** 目前只有 macOS 上的 pytest／node 測試與 `build.ps1` 的 bundle 檢查（`viewer.html` 存在）。下列每一項都要在 Windows 11 上實際操作並記錄結果；`ANALYSIS_ENABLED`（`src/n6705c_seq/analysis/contracts.py`）在驗收前維持 `False`，分頁不會出現。
> 註：分頁的契約與設計見 `docs/analysis-contract.md`，這裡只列人要點的步驟。

先決條件：`ANALYSIS_ENABLED = True` 的建置（本機 `pip install -e ".[build]"` 後 `.\packaging\build.ps1`），或 `python run.py` 在同一份程式碼上跑。

| # | 項目 | 操作 | 通過條件 |
|---|---|---|---|
| 1 | iframe 開啟 | 切到「分析」分頁 | Viewer 在 iframe 內載入，無空白、無「連線失敗」；DevTools 無 CSP／混合內容錯誤 |
| 2 | bridge | 分析分頁切入後看 state | 外殼與 Python 的 `analysis_enter` 成功；離開分頁後 `analysis_leave` 回到 idle |
| 3 | 原生開檔 | 在 Viewer 點開主檔 | 跳出 Windows 原生檔案對話框；選檔後能讀；取消對話框時不出現錯誤 toast |
| 4 | 原生另存 | 四類匯出都走另存 | 每一類都跳出原生「另存新檔」，預設檔名正確；取消不留半成品檔 |
| 5 | 匯出 PNG | 匯出目前波形 | 另存後的 PNG 可用看圖程式開啟，內容與畫面一致 |
| 6 | 匯出 HTML 報告 | 匯出報告 | 另存後以瀏覽器（Edge）直接開啟可讀 |
| 7 | 匯出 Marker JSON | 匯出 Marker | 另存的 JSON 可解析；再匯入可還原 Marker |
| 8 | 匯出 CSV | 匯出全範圍 CSV | 另存後前 4 行為標題、Sampling Period、3 欄標籤，列數正確；匯出中可按取消，取消後無 `.part` 殘留 |
| 9 | 唯讀安裝位置 | 把 zip 解到唯讀位置（例如 `C:\Program Files\` 或唯讀網路磁碟），以一般使用者執行，開 `examples`／樣本 `.dlog` | 能開樣本、能匯出；不會寫入安裝資料夾（快取與偏好寫到使用者目錄） |
| 10 | 分析中電源控制反灰 | 分析分頁開著時看「執行」「全部關閉」等按鈕 | 全部反灰；CLI 或其他入口不能啟動電源控制（互斥） |
| 11 | 切頁 | 分析 → 上電時序 → 分析 來回切 | 每次切換都正確 enter／leave；切回電源分頁後按鈕恢復；無殘留 toast 或卡住的 Promise |
| 12 | 關窗 | 分析中直接關視窗（另測電源執行中關窗） | 分析中關窗直接關閉（`analysis → closing`），清理在 10 s 內結束，工作管理員沒有殘留 `N6705C-SEQ.exe`；電源執行中關窗被拒並顯示提示 |
| 13 | 正常退出 | 分析後關窗、再次啟動 | 無錯誤視窗；再次開檔時快取命中 |
| 14 | 快取位置 | 開檔後檢查目錄 | 快取在 `%LOCALAPPDATA%` 下（非安裝資料夾）；偏好 `analysis_prefs.json` 也在使用者可寫目錄 |
| 15 | antivirus | 首次開檔、大檔寫快取時 | 防毒沒有隔離 `N6705C-SEQ.exe` 或 `_internal` 內的 DLL／`viewer.html`；若被擋，記錄防毒名稱與事件 |
| 16 | 刪快取 | 關閉程式後刪除快取目錄，再開同一檔 | 重新建快取成功、不崩潰；分析中若快取被占用（`PermissionError`），該目錄應被跳過而非整個 `/open` 失敗 |

回報格式（§6）：每項寫「通過／失敗／未測」＋Windows 版本、Edge WebView2 版本、是否為唯讀安裝、防毒軟體名稱。失敗項附錯誤視窗文字與 `%LOCALAPPDATA%` 下的日誌（若有）。

## 7. 開發環境與測試（2026-10-31，Parallels ARM 虛擬機上的 Windows 11）

專案放在 Mac 共享資料夾（`\\psf\Home\Desktop\claude-cowork\N6705C-SEQ`，UNC 路徑）。

**這台機器的狀態**
- PATH 上沒有 `python`／`py`／`git`／`node`／`winget`。Python 裝在 `%LOCALAPPDATA%\Programs\Python\Python312`（3.12.8）與 `Python313-x64`（3.13）。因為沒有 `py` 啟動器，`N6705C-SEQ.bat` 在這台會失敗（見 §3 與 bat 內 `py -3`）。
- Git 沒裝，這台不能跑 git 指令。
- Edge WebView2 Runtime 155.0.4283.62（GUI 需要，已有）。
- 系統有 `visa32/64.dll` 與 `C:\Program Files\IVI Foundation\VISA`，是否只是 IVI 共用元件未確認（§3.1）。
- 預設編碼 cp950。

**開發 venv**：`C:\Users\lianshi\venvs\n6705c-seq`（Python 3.12.8，放本機磁碟，不放 UNC）。`pip install -e ".[dev]"` 已裝，版本 0.1.3，含 pywebview 6.2.1。
```powershell
$py = "$env:USERPROFILE\venvs\n6705c-seq\Scripts\python.exe"
& $py -m n6705c_seq.webgui            # GUI（未在此機實際開啟驗證）
& $py -m pytest -q --timeout=120      # 測試
```
ARM 虛擬機上跑的是 x64 的 Python 與 wheel（pip 取到 `win_amd64`）；pyusb／libusb 在此環境未驗，也沒有儀器。

**測試結果**：799 通過、16 跳過、1 失敗（約 52 秒）。
- 已修：`test_web_seq.py`、`test_web_list.py` 讀原始碼的 `read_text()` 沒指定編碼，在 cp950 下 `UnicodeDecodeError`；已改 `encoding="utf-8"`。
- 未修：`tests/analysis/test_server_regressions.py::test_save_waiting_for_target_lock_is_cancellable` 約 30–40% 失敗（單獨跑 10 次：預設 3 次、`PYTHONUTF8=1` 4 次，與編碼無關），`ConnectionResetError: [WinError 10054]`。原因與待決事項見 `HANDOFF.md` §11 最後一條。
