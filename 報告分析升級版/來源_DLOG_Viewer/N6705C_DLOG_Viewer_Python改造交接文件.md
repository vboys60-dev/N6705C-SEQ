# N6705C DLOG Viewer — Python 資料層改造｜交接文件

> 狀態：**改造完成，回歸驗證通過**。過程中修掉兩個真 bug（效能退化、匯出空白圖）。
> 人眼確認清單 §4 的兩個既有小問題已依指示處理（2026-09-10，見 §9）。
> 剩下：使用者跑人眼確認清單 §1–3（操作手感）、§6 的效能懸崖使用者已裁示「先不用」。
> 最後更新：2026-09-10

---

## 1. 這個改造在做什麼

原本 `dlog_viewer_mem3_preview.html` 是單一 HTML，把整份 .dlog 讀進瀏覽器記憶體，
撐不住千萬筆級檔案（10M×4ch = 320MB raw），CSV 全量匯出也會卡死分頁。

**改法**：本機跑一個 Python server（stdlib `http.server` + numpy memmap），
負責解析／LOD／統計／校正／CSV 串流。前端 HTML 改成跟這個 server 要資料。

**UI 完全沒動**：繪圖、主題、i18n、Marker Table、軸控制、PNG／HTML 報告全留在瀏覽器，
截圖規則與原版一致。

### 使用者拍板的三件事

1. 檔案上限：千萬筆級（1000 萬以上）
2. CSV 全量匯出不能卡死；圖表截圖規則維持原樣（不搬 Python）
3. 校正只處理**電流負值**，且要寫進匯出檔案；**電壓負值不管**

---

## 2. 檔案結構與啟動

```
N6705C-Dlog/
├── README.md                                          啟動說明
├── 啟動 DLOG Viewer.command                           macOS 雙擊啟動（自動開瀏覽器）
├── 回歸驗證_人眼確認清單.md                            交給使用者操作確認的項目
├── dlog_viewer_mem3_preview.html                      已改造
├── dlog_viewer_mem3_preview.BACKUP-before-python.html 改造前備份（勿刪，見下）
├── server/
│   ├── dlog_core.py          解析 / LOD / 校正 / 統計 / CSV 串流
│   ├── dlog_server.py        HTTP 路由
│   ├── test_dlog_server.py   自我檢查 8 項（約 1.3 秒）
│   ├── parity_tools/         對拍工具 + README（數值回歸防線）
│   └── .cache/<sha1>/        正規化快取（可安全刪，下次開檔重建）
├── N6705C_DLOG_Viewer_架構規格書.md                   .dlog 格式規格 + 前端架構
└── （原《優化步驟手冊》《AI建構提示詞》已於 2026-10 合併／移除，見 §10）
```

**啟動**：

- macOS：雙擊 **`啟動 DLOG Viewer.command`**（會檢查 python3／numpy、起 server、自動開瀏覽器；
  關掉終端機視窗或按 Ctrl+C 即停止）。首次被 Gatekeeper 擋的話，右鍵→開啟→確認一次。
- 手動：

```bash
python3 server/dlog_server.py
# 開 http://127.0.0.1:8765/dlog_viewer_mem3_preview.html
```

⚠️ 必須透過 server 開啟，不能直接雙擊 **HTML**（前端要打同源 API）。

⚠️ **`server/parity_tools/original_before_python.html` 不能刪、不能改**：原版 JS 解析邏輯只存在這份副本，
對拍工具與效能對照組都靠它。根目錄的 `BACKUP-before-python.html` 已於 2026-10-14 套用 Codex 審查修正，
不再是逐位元組的原版，不可拿來對拍。

**依賴**：Python 3 + numpy。無第三方 web 框架（5 條路由不值得引入 FastAPI/Flask）。

---

## 3. 已完成的工作

### 3.1 Server 端

| 端點 | 用途 |
|---|---|
| `POST /open` | 收 .dlog → 回 metadata + LOD |
| `GET /samples` | 原始樣本切片（Float32） |
| `GET /buckets` | min/max 包絡（Float32） |
| `GET /stats` | 區間 min/max/avg（JSON） |
| `GET /export/csv` | CSV 串流下載 |

- 快取以**檔案內容 sha1** 為鍵，存 `.npy`（已轉 little-endian、trailer 排好）+ LOD + meta，
  一律 memmap 開啟，原始樣本不進 RAM。
- 電流負值校正**只在 server 端套用**，是「匯出檔不得有負電流」的單一保證點。
- 校正值改用**由索引推導的決定性雜湊**（splitmix64），同一索引在任何切片都得相同值，
  不需快取狀態。取代原本前端的 `Math.random()` + 逐索引快取。

### 3.2 前端切換

- **刪掉**：整份 `DLOG_WORKER_SRC` / `parseDlog` / worker fallback / 逐索引亂數快取 /
  CSV 確認門檻等（消滅「同一份解析邏輯存在兩處」的長期債）。
- **沒動**：`installLod` / `bucketsFromLodLevel` 等本地 LOD 路徑核心（只換資料來源）；
  `draw()` / `drawChartIntoContext()` 等繪圖內容一行未改。
- **新增資料層**：`apiOpen` / `apiSamples` / `apiStats`、raw 視窗快取、point 快取、
  `ensureExportData` 等。
- 取值統一走 `sampleAt()`；CSV 改串流下載；marker 改 async。

### 3.3 Marker 數值改即時重算

原本 marker 建立時就把 voltage/current/power 存成快照且不回溯，
導致**校正開啟前建的 marker，負值會一路寫進 JSON／報告／PNG**。

現在 marker 只存身分（time / ch / kind / note…），數值一律經 `markerValues()` 依目前校正設定
即時取值。畫面、CSV、JSON、報告、PNG 全走同一入口，切換校正開關即時變動。
JSON 匯入也改為忽略檔內數值、只採 time/channel/type/note。

### 3.4 過程中修掉的兩個 bug

**效能退化（2.7×）**：取值收斂到 `sampleAt()` 後，分桶內層迴圈也走那條路，
每個樣本多付一次 Map 查找 + 物件配置。
修法：`computeViewBuckets()` 進迴圈前先把 Float32Array、視窗位移、kind 分支全解析完，
內層只剩陣列索引。單點取值（marker、統計）仍走 `sampleAt()`，每幀只呼叫個位數次不受影響。

> ⚠️ 別改成「每次回傳一個取值閉包」——試過只降到 48.7ms。
> V8 對「每幀換一個函式物件」的呼叫點無法最佳化。程式碼裡有註解，別改回去。

**匯出空白圖**：`ensureExportData()` 備好了 marker 數值與統計，
**卻沒等圖表要用的 raw 視窗**，`drawChartIntoContext()` 沿用上一幀（剛載入時是空陣列）
→ 匯出的 PNG／HTML 報告是空白圖。載入後立刻按匯出就會中。
修法：`rawPending` 改存 promise，`ensureExportData()` 末尾對每條可見曲線跑一次
`computeViewBuckets()` 觸發取數，再 await 全部。修正後報告與原版**逐位元組相同**（只差時間那行）。

### 3.5 驗證結果（都通過）

- **Python 解析器 vs 原版 JS Worker**：929k×1ch、81k×4ch、合成 trailer 檔，逐 float32 相同。
- **`/buckets` vs 原版 `computeViewBuckets`**：三條路徑 × V/I/P，位元組相同。
- **前端 vs server 逐值比對**（`__probe.html` 實跑）：三路徑 × V/I/P × 8 種邊界，24 組全相同；
  校正開啟、raw 視窗跨界、LOD↔raw 交界、M1/M2 拖曳凍結、雙 handle 並存、缺聲道回 NaN — 全過。
- **校正效果**（4ch 檔）：CSV 負電流筆數全部 → 0；電壓負值不動；
  功率剩餘負值筆數 == 該聲道電壓負值筆數（`P=V×I`，電壓負則功率負，這是刻意行為）。
- **四種匯出負值掃描**（81k×4ch 全量 12 欄）：CSV／JSON／Marker Table／HTML 報告／PNG
  電流全正，負號只出現在電壓與電壓為負的功率。
- **HTML 報告與原版逐項對照：逐位元組相同**。
- **Server 自我檢查**：8 項全過（1.3 秒）。

### 3.6 千萬筆實檔（10M×4ch 合成檔，305MB）

| | 舊版 | 新版 |
|---|---|---|
| 載入 | 18.2 s / 800 MB heap | **1.04 s / 15.8 MB heap** |
| `/open` 首解析（含建 LOD + 寫快取） | — | 2.39 s（快取命中 0.15 s） |
| LOD blob | — | 14.53 MB（raw 的 4.76%） |
| 全覽每幀（校正關，12 曲線） | 0.7 ms | 1.1 ms |
| 放大 5 萬筆 raw 每幀 | 39.2 ms | 12.6 ms |
| CSV 全量匯出 10M 筆 | 未觸發（門檻 confirm 阻塞） | 46 s / 1.5 GB 串流落地，負電流 0 |

> 修正原假設：舊版**開得起來**（至少這台機器），只是要 800MB / 18 秒，不是「撐不住」。
> 新版把它降到 15.8MB / 1 秒。

### 3.7 UI（應不受影響，掃過確認）

12 曲線勾選／改色／改名／posOffset、右軸 I↔P 切換、V/I 軸 auto↔手動／滾輪調刻度／拖曳調 Ref、
Marker 右鍵新增 + 吸附、Marker JSON 匯入（竄改數值後匯入一律依現況重算）、
PNG 三種配色（2212×1684）、三主題 × 中英切換 — 全部正確。

---

## 4. 踩過的坑（給接手者，會再浪費時間的才留）

1. **`BaseHTTPRequestHandler` 預設 HTTP/1.0** → 上傳大檔 client 硬等 1 秒才送 body，
   誤判成解析慢。已設 `"HTTP/1.1"`。

2. **`Float32Array` 要求 byte offset 是 4 的倍數** → 二進位框架
   `[4-byte 長度][JSON metadata][payload]` 的 JSON 長度任意，payload 起點不一定對齊，
   `new Float32Array(buf, offset, len)` 直接 `RangeError`。已加 `frame_binary()` 補空白到 4 的倍數。

3. **量效能不要跨環境比絕對值**。背景分頁（`document.hidden`）純數值迴圈比 Node 慢約 2 倍，
   且 Chrome 對背景分頁不觸發 `requestAnimationFrame`（畫面像沒資料）。
   判斷「有沒有變慢」只能拿**同一個瀏覽器裡的舊版**當對照組（`make_probe.py old` 為此而生）。
   §3.4 的效能退化一開始就是被錯誤基準（Node 數字）掩蓋的。

4. **LOD 選層門檻**（`N_BUCKETS=1800`）：`level0`（factor 64）要 count ≥ 115,200；
   `level1`（factor 4096）要 count ≥ 7,372,800。929k 檔全覽用的是 level0 不是 level1。

5. **驗證腳本自己會拿到陳舊資料**：視野改變後 raw 視窗還沒到齊時，
   `computeViewBuckets()` 會**刻意**回上一幀的 `_lastBuckets`（防閃爍的正確行為）。
   自動化比對前必須等 `rawWindows` 真的涵蓋 `[idxLo, idxHi]`，否則誤報 FAIL。

6. **Chrome 擋連續自動下載**：腳本連按三次 CSV 匯出只有第一次落地。
   驗匯出內容改用與按鈕逐字相同的 URL 直接 `fetch`。

---

## 5. 剩下要做的事

1. **請使用者跑 `回歸驗證_人眼確認清單.md` §1–3**：捲動平移／圈選縮放／Ctrl+滾輪的操作手感、
   M1/M2 拖曳、吸附精度 — 這些程式驗不了。

2. ~~§4 兩個既有小問題~~ ✅ 使用者裁示「處理」，已完成，見 §9。

3. ~~§6 效能懸崖~~ 使用者裁示「先不用」——校正預設關閉、放大檢視正常，維持現況。

日常開發：

```bash
python3 server/dlog_server.py                   # 起 server
python3 server/test_dlog_server.py              # 自我檢查
python3 server/parity_tools/make_probe.py new   # 需瀏覽器驗證時，驗完刪 __probe.html
```

⚠️ **改動 `dlog_core.py` 後務必重跑 parity_tools**（步驟見該目錄 README）——
那是唯一能證明「Python 端與原版 JS 逐位元組相同」的防線。

### 重建千萬筆測試檔

合成 10M×4ch 的 .dlog（約 305MB，**寫到專案外**）：header 照 `parity_tools/make_synth.py`
的格式（tint 0.0001、4 聲道各開 `sense_volt`/`sense_curr`），接 `struct.pack('>Q', 10_000_000)`，
再分塊寫 `(n, 8)` 的 `>f4` 陣列（欄位序 CH1V,CH1I,CH2V,CH2I,…）。
波形刻意含方波、稀疏尖峰（驗 LOD 不吃掉尖峰）、少量負電壓、約 30% 負電流。
驗完刪 `server/.cache/<sha1>/`（10M 檔快取有 320MB）。

---

## 6. 已知限制

### ⚠️ 效能懸崖：校正開啟 + 千萬筆全覽（使用者已裁示「先不用」，維持現況）

| 情境（10M×4ch） | 每幀 | 分頁記憶體 |
|---|---|---|
| 校正關、全覽 | 1.1 ms | 15.8 MB |
| 校正開、放大到 5 萬筆 | 正常 | 正常 |
| **校正開、全覽** | **736 ms** | **321 MB** |

原因：前端只有 `apiSamples`、**不呼叫 `/buckets`**。校正開啟時 I/P 用不了 LOD，
`ensureRawWindow` 就索取整個視野的原始樣本——10M 全覽 = 一次 305MB 請求。
（舊版同情境 4876 ms / 800 MB，新版仍快 6.6 倍，但兩者都不好用。）

**修法現成**：`/buckets?offset=1` 端點已存在、已通過對拍，同情境 server 端只要 354ms、
只回約 1800×3 個浮點數。前端加一條 `apiBuckets` 路徑即可。
**沒逕行實作**——這會在「兩層本地快取、重繪一律同步」的定案架構外再加一個非同步資料源，
屬架構決定，另一條路徑是「另建校正後 LOD，以 maxLimit 為快取鍵」。
**2026-09-10 使用者裁示先不做**，維持現況。

**現況可接受的理由**：校正預設關閉、屬特殊測項專用，放大檢視時完全正常。

### 其他

- CSV `toFixed(9)` 量級限制：校正後電流約 1e-8、其功率約 1e-10，會被格式化成
  `0.000000000`（非負，但看不出數值）。沿用原版格式的既有行為。
- 必須透過 server 開啟，不能雙擊 HTML。

---

## 7. 關鍵設計決定

### 7.1 前端保留本地資料（不是每次重繪都跟 server 要）

曾打算讓前端完全不存資料、每次重繪同步取（server 端只要 1.2ms）。**實測後推翻**：
漏算 JSON 序列化 +「每次重繪都要付一次」，同步取數比本地慢一個數量級
（81k×4ch 12 曲線：本地 4.4ms vs 同步 XHR 30.5ms）。
且同步 XHR 規範禁止 `responseType='arraybuffer'`，只能收文字。

**定案：兩層本地快取，重繪一律同步、零網路**

| 目前視野 | 本地資料來源 |
|---|---|
| count ≥ 115,200 | LOD（開檔時一次載入，佔 raw 的 4.8%） |
| count < 115,200 | raw 視窗快取（前後各留一倍 margin，快取視窗 = 3 倍 view） |

只有「視野改變且離開已快取視窗」才觸發非同步取數，取數期間沿用 `_lastBuckets` 防閃爍。
10M×4ch 記憶體約 26MB（LOD 15MB + raw 視窗 11MB），對比 raw 的 320MB。

### 7.2 校正只處理電流

電壓負值不處理（使用者定案）。電壓確有負值（次毫伏等級），
因此 `P=V×I` 在那些樣本上仍會是負的。**這是刻意的邊界行為，不是 bug**，
`test_dlog_server.py` 第 7 項即依此斷言：電流一律 > 0，功率只在對應電壓為負時允許為負。

---

## 8. 明確不做

- 不改 UI 版面、CSS（i18n 字典：改造期間不動，2026-09-10 起經使用者同意可補漏譯，見 §9）
- 不把 PNG／HTML 報告搬到 Python
- 不引入 FastAPI/Flask
- 不做 pywebview/PyInstaller 打包

---

## 9. 改造後的追加修改

### 2026-09-10：人眼清單 §4 兩個既有小問題（使用者裁示「處理」）

都只動 `dlog_viewer_mem3_preview.html`，未碰 `dlog_core.py`，對拍防線不受影響。
已 `test_dlog_server.py` 8/8、HTML JS 語法檢查、瀏覽器實測 EN 模式通過。

1. **EN 模式下比對檔按鈕／狀態文字仍是中文** → 補進 i18n 字典。
   - 新增 key（中英各一份）：`compareFileBtn`、`compareClearBtn`、`compareNoFile`、
     `compareFileInfo`（函式）、`compareNeedMain`、`compareLoaded`、`compareLoadFailed`。
   - 比對檔資訊列改走新的 `updateCompareFileInfo()` 單一入口（載入／移除／`applyLanguage` 三處呼叫）。
     **順帶修掉一個潛在 bug**：原本切語言時 `[data-i18n]` 會把已載入的檔名洗成「尚未載入」。
   - `#compareFileName` 刻意不加 `data-i18n`（由 `updateCompareFileInfo()` 專屬管理）。

2. **PNG「白底列印版」marker 數值標註在白底上對比偏低** → `EXPORT_THEMES.light`
   新增 `annotText:'#1a1a1f'`；`drawChartIntoContext()` 內 M1/M2 數值文字若 `theme.annotText`
   存在就改用深色（哪條曲線靠上方彩色圓點分辨）。深色主題與完全負片維持原本彩色文字。

### 2026-09-10：新增 macOS 雙擊啟動檔

`啟動 DLOG Viewer.command`（已 `chmod +x`）：檢查 python3／numpy（缺 numpy 自動 pip 安裝）→
背景等 1.5s 後 `open` 瀏覽器 → 前景 `exec python3 server/dlog_server.py`。
純 shell、無新依賴。

---

## 10. 改造前的歷史修正（原《優化步驟手冊》精華，該文件已刪除）

> 原手冊的 P1–P5 是對**改造前**的 HTML 做的修改（LOD 繞過負值校正、worker fallback detach、
> JSON 匯入驗證 `time_s`、死代碼清除、`draw()` 去重、CSS 合併、統計面板拖曳快取……），
> 改造時 `DLOG_WORKER_SRC`／`parseDlog`／worker fallback／CSV 確認門檻已整段刪除，
> 其步驟已無對應程式碼，故不保留。仍有教訓價值的只有下面三件。

### 10.1 欄位佈局必須讀 `sense_*` 旗標（根因修復）

`74445_…dlog` 曾解析失敗：「期望 14868208 bytes，實際剩餘 7434104 bytes」（恰差 2 倍）。
原因是用 `<channel id>` 標籤數 ×8 推算每筆長度，但標籤只代表機箱插了哪個模組。
實檔 CH1（N6781A，sense 全 0）＋ CH3（N6785A，sense 全 1）實際只有 CH3 的 V、I。
現行 `dlog_core.py` 依 `<sense_volt>`／`<sense_curr>` 建 `fields`、依 `<sense_minmax>` 決定 `per_field`。
`sense_minmax=1` 路徑**沒有真實樣本驗證過**。詳見規格書 §2。

### 10.2 資料欄位別用「逐欄位手動列舉」轉手

當時 `fields` 在 parser→UI 的兩個轉手點被漏列，`state.fields` 變 `undefined`；
`undefined && x` 短路成 `undefined` 而非 `false`，使「資料沒傳到」被偽裝成「看起來還好」。
教訓：轉手點用展開（`{...parsed}`）；判斷函式遇缺失要明確回傳 boolean 並 `console.warn`。

### 10.3 UI 折疊面板（仍在 HTML 內）

匯出區包進 `<details id="exportFold">`，載入檔案成功後自動收合；電流負值校正說明用巢狀 `<details id="offsetHintFold">`
（checkbox 刻意放在 `<summary>` 外，避免搶焦點）。折疊狀態存 `localStorage`，key 為 `dlogViewerFold:<id>`。
`.chart-wrap` 高度為 `clamp(360px, calc(100vh - 300px), 860px)`，搭配 `ResizeObserver`。

### 2026-10-06：Codex 審查後修正兩項（只動 `dlog_viewer_mem3_preview.html`，未碰 `dlog_core.py`）

1. **自訂時間範圍反向**：`applyRangeBtn` 改成先 clamp 再檢查 `t0<t1`（原本輸入完全在記錄範圍外會變成 `[20,10]`）。
2. **比對檔比主檔短時顯示最後一筆**：新增 `idxAtTime()`／`valAtTime()`，M1/M2 讀值、marker 圓點、`markerValues()`、右鍵吸附、
   `rangeIdxOf()`（統計）在時間點超出該曲線記錄範圍時回 NaN／無統計，不再夾到最後一筆。
   部分重疊的區間仍照常統計重疊部分。`/stats` 端點本身仍會 clamp，靠前端不送出完全無交集的區間。
