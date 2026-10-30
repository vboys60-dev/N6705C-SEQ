# T8 獨立審查：Codex（HEAD 703a744）

VERDICT: NEEDS_CHANGES

- **MAJOR｜`src/n6705c_seq/analysis/server.py:304、378、155`：舊世代 `/release` 可釋放新世代引用。**  
  重現：gen1 請求通過 `_need_session` 後暫停送 body；離頁、進入 gen2 並開啟相同檔案，再補送舊 body。`unhold(h)` 未綁世代，會移除 gen2 的引用；後續 LRU 可關閉、淘汰仍在使用的快取。記憶體探針已重現。**修法：**持有引用以 `(gen, digest)` 索引，釋放只能操作請求所屬世代。

- **MAJOR｜`src/n6705c_seq/analysis/server.py:232、209、510`：阻塞式 HTTP I/O 無法取消。**  
  重現：有效 JOB 宣告 Content-Length 後停止送 body，或要求大型 `/samples` 後停止讀回應。socket 沒有逾時，cancel 無法中斷 `read/write`，`job_end` 無法執行；離頁最後進 fault，關窗則強制退出。**修法：**加入短 socket timeout、分段讀寫與取消／期限檢查；取消時關閉連線。

- **MAJOR｜`src/n6705c_seq/analysis/server.py:506–517`：取消上傳仍會覆寫檔案。**  
  重現：取消發生於最後一次 `rfile.read()` 期間；返回後直接呼叫不帶 cancel 的 `atomic_write_bytes`。離頁已設定取消，仍會發布結果；等待同目標鎖時也無法取消。記憶體探針已確認取消旗標為 True 仍進入寫入。**修法：**改用 `atomic_write_iter(..., cancel=job.cancel)`，讀完 body 後再次檢查取消。

- **MAJOR｜`src/n6705c_seq/analysis/core.py:698`、`src/n6705c_seq/analysis/web/viewer.html:3074、3127`：主檔＋比對檔 CSV 正常操作會超出欄位限制。**  
  重現：載入兩份四聲道檔，取消「僅匯出已勾選曲線」，產生 24 欄；核心以 `MAX_KEYS=12` 拒絕，無法匯出完整比對結果。**修法：**區分單檔查詢與 CSV 欄數上限，CSV 支援兩來源共 24 欄，補整合測試。

- **MINOR｜`src/n6705c_seq/analysis/server.py:541、586`、`src/n6705c_seq/analysis/storage.py:129`：首次使用無法儲存偏好。**  
  重現：使用者設定目錄尚不存在時，更改主題；程式未建立 `prefs_path.parent`，建立 `.part` 失敗，Viewer 又吞掉錯誤，重開即恢復預設。現有測試使用已存在的 `tmp_path`，未涵蓋。**修法：**儲存前建立偏好目錄，測試全新目錄情境。

其餘核對：未發現指定公開電源入口可直接繞過 gate；`_untimed` 的原子判定、import 邊界、flag 關閉、Host/token/grant、靜態白名單與 postMessage 來源檢查符合契約。mmap 發布／淘汰前有關閉映射，CSV 與校正運算已有分塊。

驗證：JS **19/19 通過**；兩項記憶體探針重現成功。環境缺 numpy，未執行完整 Python、10M／30M 或 Windows 實機驗收。全程未修改檔案，未讀其他審查產出。
