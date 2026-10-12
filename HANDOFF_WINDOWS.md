# N6705C-SEQ　Windows 版交接文件

通用設計見 `HANDOFF.md`；本文只講 Windows 的散佈、使用、已知問題。更新：2026-10-13（v0.1.3）。

## 1. 三種給 Windows 用戶的形式

| 形式 | 位置 | 使用者需要 | 用法 |
|---|---|---|---|
| exe（onedir zip） | GitHub Release `N6705C-SEQ-<版本>-windows-x64.zip` | 什麼都不用裝 | 解壓 → `N6705C-SEQ.exe`（GUI）／`n6705c-seq-cli.exe` |
| Python 資料夾 | `dist_python/N6705C-SEQ/`（本機產物，未進 git；已同步到 0.1.3，另有 `dist_python/N6705C-SEQ-0.1.3-python.zip`） | Python 3.11+（含 tkinter） | `pip install -r requirements.txt` → `python run.py`（無參數＝GUI，有參數＝CLI） |
| bat 安裝版 | 2026-10-12 做過，**目前資料夾已不存在**（`dist_windows/`） | 無（會自動下載裝 Python 3.12.8） | `install.bat` → `start.bat` |

`run.py` 必須保留 `if __name__ == "__main__"` 與 `multiprocessing.freeze_support()`：VISA 探測用 multiprocessing，Windows 會重新匯入主程式，少了保護會再開一個 GUI。

## 2. 打包與發版（exe）
- 本機：`pip install -e ".[build]"` → `.\packaging\build.ps1`（建置 → `--demo` 自我測試 → 產 zip）。只能在 Windows 打。
- CI：`.github/workflows/ci.yml`，推 `v*` tag 才建 Release。發版：改 `pyproject.toml` version → `git tag vX.Y.Z && git push origin vX.Y.Z`。
- ⚠️ zip 檔名取自 `pyproject.toml` 的 version（目前已改 0.1.2，發版前記得與 tag 一致。
- 沒有程式碼簽章，SmartScreen 會警告。

## 3. 已知問題與處理
1. **系統 VISA 損壞 → 整個程式消失**（`viOpenDefaultRM` access violation／`STATUS_STACK_BUFFER_OVERRUN`，try/except 接不到）。實例：使用者機器只裝了 IVI 共用元件 `visa32.dll`，沒有廠商實作。
   - 修法：`src/n6705c/core/transport.py` 的 `_native_backend_safe()` 在子行程探測（8 s 逾時、快取），不安全就只用 `@py`。`packaging/*_entry.py` 與 `run.py` 都要 `freeze_support()`。
   - 代價：第一次連線可能多等 ≤ 8 s；此時 USB 位址連不上，只有 LAN（`TCPIP0::<ip>::inst0::INSTR`）可用。要用 USB 就得完整安裝 Keysight IO Libraries 或 NI-VISA；或改走 `pyvisa-py` + `pyusb` + libusb（儀器驅動需換成 WinUSB，未實機驗證，見 `HANDOFF.md` §4.12）。
   - 診斷指令：`python -X faulthandler -c "import pyvisa; pyvisa.util.get_debug_info()"`。
2. **GUI 已移除「掃描」按鈕**：VISA 位址改為直接貼上（`ttk.Entry`）。掃描曾在 Tk 主執行緒卡住，後來又踩到上一點的當機。`list_resources` 仍在 core 裡，GUI 不再呼叫。
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

## 5. 目前 git 狀態（交接當下）
- 已推送：`713b47f`（診斷訊息，0.1.3）、tag `v0.1.0`～`v0.1.3`。`dist_python/` 不進 git。
- 工作樹乾淨（除 `dist_python/`）。
- 下一步：在有 Windows＋儀器的環境按「測試連線」，把錯誤視窗內容回報 → 依 §3.4 判讀；確認 CI Release 產物；乾淨 Windows 驗證 exe 的 GUI。
