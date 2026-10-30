# N6705C DLOG Viewer

Keysight N6705C 資料記錄檔（`.dlog`）的離線分析工具：波形瀏覽、雙游標區間統計、
標記表、CSV／PNG／HTML 報告匯出。

## 啟動

```bash
python3 server/dlog_server.py
```

然後開瀏覽器連 **http://127.0.0.1:8765/dlog_viewer_mem3_preview.html**

> ⚠️ **不能直接雙擊 HTML 開啟。** 解析與資料存取在本機 Python server，
> 前端要打同源 API，用 `file://` 開會整個載入不了。

需求：Python 3 + numpy。沒有其他相依套件（HTTP 走 stdlib 的 `http.server`）。

## 為什麼要有 server

原本整份 `.dlog` 是解析成 `Float32Array` 放在瀏覽器記憶體裡。千萬筆級的檔案
（10M×4ch 的原始資料就 320MB）撐不住，CSV 全量匯出也會撐爆分頁。

現在解析／統計／CSV 串流都在 Python 端以 numpy memmap 處理，原始樣本不進 RAM；
瀏覽器只留一份 LOD 摘要（約佔原始資料的 4.8%）與目前視野附近的樣本。
**繪圖、主題、i18n、標記表、軸控制、PNG／HTML 報告全部仍在瀏覽器**，
截圖規則與改造前一致。

實測 10M 筆 × 4 聲道：開檔 1 秒、分頁記憶體 15.8MB（改造前是 18 秒 / 800MB）。

## 檔案

| 路徑 | 用途 |
|---|---|
| `dlog_viewer_mem3_preview.html` | 前端（UI + 繪圖 + 匯出），單一檔案 |
| `server/dlog_server.py` | HTTP 路由 |
| `server/dlog_core.py` | 解析 / LOD 建置 / 校正 / 統計 / CSV 串流 |
| `server/test_dlog_server.py` | 可執行自我檢查，約 1.3 秒 |
| `server/parity_tools/` | 數值對拍工具（改動 `dlog_core.py` 後應重跑） |
| `dlog_viewer_mem3_preview.BACKUP-before-python.html` | 改造前的單檔版本（2026-10-14 已套用 Codex 審查修正，可獨立使用） |
| `server/parity_tools/original_before_python.html` | 改造前的原樣副本，**請勿刪除、請勿修改** |

`server/parity_tools/original_before_python.html` 是對拍工具唯一的原版 JS 來源，
也是效能對照組，刪掉就無法再驗證「Python 端與原版邏輯逐位元組相同」。

## 快取

開檔時會在 `server/.cache/<檔案內容的 sha1>/` 建立正規化快取
（已轉小端序的 `.npy` + LOD + metadata），大小約等於原始資料量。

**這個目錄可以隨時整個刪掉**，下次開檔會自動重建（10M 筆約需 2.4 秒）。
快取以檔案內容為鍵，所以同一份檔案改名不會重複建，內容不同必定是不同目錄。

## 測試

```bash
python3 server/test_dlog_server.py          # 自我檢查 8 項
cd server/parity_tools && cat README.md     # 數值對拍步驟
```
