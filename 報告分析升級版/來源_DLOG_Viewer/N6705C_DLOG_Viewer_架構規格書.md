# N6705C DLOG Viewer — 專案架構與 .dlog 格式規格書

> 本文件用途：作為後續請 AI 協助開發、擴充、或用其他語言（如 Python／PyQt5）重寫本工具時的輸入參考。內容涵蓋 .dlog 二進位格式規格、目前 HTML 版工具的架構、資料結構、功能清單與設計慣例。文件依據 `dlog_viewer_mem3_preview.html`（v1.4 UI + 2026-08 Python 資料層）整理。

---

## 1. 專案概述

**用途**：離線分析 Keysight N6705C 電源分析儀所匯出的 `.dlog` 資料記錄檔案（同時記錄最多 4 通道的電壓／電流波形），提供瀏覽、縮放、雙游標（Marker）量測、標記表格、統計與多格式匯出功能。

**型態**：單一 HTML 前端（`dlog_viewer_mem3_preview.html`，Vanilla JavaScript、無框架，Canvas 2D 繪圖，以 IIFE 包裹）＋ 本機 Python server（資料層）。**必須透過 server 開啟，不能雙擊 HTML**（見 §3.1）。

**目標使用情境**：工程師在離線的量測現場或辦公室電腦上，啟動本機 server 後用瀏覽器分析數據、擷取波形圖與報告。

---

## 2. .dlog 檔案二進位格式規格（已驗證；以 `sense_*` 旗標決定欄位，支援 pre-trigger 緩衝區）

`.dlog` 由兩部分組成：**文字表頭（XML 風格）** + **二進位資料記錄區**。

> **歷史修正（避免重蹈）**：
> 1. v1.0 假設永遠 4 聲道、每筆 32 bytes → 單聲道檔解析失敗。
> 2. v1.1 改成「`<channel id>` 標籤數 × 8 bytes」→ 仍然錯。`<channel>` 標籤只代表機箱插了哪個模組，
>    **不代表這次有記錄**。真正決定欄位的是每個 channel 區塊內的 `<sense_volt>`／`<sense_curr>` 旗標。
>    實例：header 列了 CH1（N6781A，sense 全 0）與 CH3（N6785A，sense 全 1），標籤數 2 → 算出 16 bytes／筆，
>    實際只有 CH3 的 V、I 共 8 bytes，檔案長度恰差 2 倍而報「資料長度不符」。
> 3. 修法：逐一掃描 `<channel id="N">…</channel>` 區塊，依旗標建立 `fields`（如 `[(3,'V'),(3,'I')]`），
>    讀取時依 `fields` 逐一歸位，不用「聲道在清單中的位置」推算，以免長度湊巧對、欄位卻錯位。

```
┌─────────────────────────────────────────┐
│  文字表頭 (UTF-8)                          │
│  ...                                      │
│  </dlog>\n   ← 結束標記，表頭到此為止        │
├─────────────────────────────────────────┤
│  8 bytes：記錄筆數 nRecords (uint64)        │  ← Big-Endian
├─────────────────────────────────────────┤
│  nRecords 筆記錄（觸發後主要資料）           │
│  每筆 = len(fields) × per_field × 4 bytes  │
├─────────────────────────────────────────┤
│  [選用] pre-trigger 緩衝資料               │
│  剩餘位元組 ÷ 每筆長度 = 緩衝筆數             │
└─────────────────────────────────────────┘
```

### 2.1 表頭（文字區）

- 編碼 UTF-8。找到位元組序列 `</dlog>\n`（8 bytes）即表頭結束；`headerEnd = index + 8`。
- 以正規表示式擷取（非嚴謹 XML parser）：

| 欄位 | 正規表示式 | 說明 |
|---|---|---|
| 取樣間隔 | `<tint>([^<]+)</tint>` | 每筆間隔（秒） |
| 宣告總時長 | `<time>([^<]+)</time>` | 使用者設定的預計時長。記錄常提前停止，**實際時長一律用 `(nRecords-1) * tint`**，不可信任此值 |
| 聲道區塊 | `<channel id="N">…</channel>` | id 1~4。**區塊存在 ≠ 有記錄**，見下方 `sense_*` |
| 記錄欄位 | 區塊內 `<sense_volt>1<`、`<sense_curr>1<` | 為 1 才有該欄位。依聲道由小到大、先 V 後 I 排成 `fields` |
| 是否含極值 | `<sense_minmax>([^<]+)</sense_minmax>` | 1 時每欄位是 avg/min/max 三個 float（`per_field=3`），取 avg。**目前沒有此類樣本檔，路徑未經真實資料驗證** |
| 通道型號 | 區塊內 `<model>([^<]+)</model>` | 沒有 sense 欄位的聲道，UI 不顯示 |

### 2.2 記錄筆數（8 bytes）

緊接在 `</dlog>\n` 之後，**Big-Endian uint64**。這是「觸發後」主要資料區筆數，不含 pre-trigger 緩衝。

### 2.3 單筆記錄格式（Big-Endian float32）

每筆依 `fields` 順序排列，每欄位 `per_field` 個 float32（預設 1）：

```
bytesPerRecord = len(fields) × per_field × 4
```

範例：4 聲道全開（V+I）＝ 8 欄位 ＝ 32 bytes／筆；僅 CH3 的 V+I ＝ 8 bytes／筆。

主要資料區長度 = `nRecords × bytesPerRecord`；剩餘長度**不足**才視為檔案毀損。

### 2.3.1 Pre-trigger 緩衝區（附加在主要資料區之後）

```
trailerBytes   = 檔案長度 − (recordsStart + nRecords × bytesPerRecord)
trailerRecords = floor(trailerBytes / bytesPerRecord)
```

- 格式與主要資料區相同，時間上代表**更早**的取樣：`最終陣列 = [trailer…] + [主要資料…]`。
- `totalRecords = trailerRecords + nRecords`，之後所有時間軸／統計／匯出都用它。
- 已用真實單聲道檔（`trailerRecords=7`）與 Keysight 官方 CSV 逐筆比對（誤差 < 1e-14）。
  三份實檔也已逐值比對 CSV（最大相對誤差 4.75e-06，在 CSV 6 位有效數字精度內）。

### 2.4 解析參考實作

唯一實作在 `server/dlog_core.py::parse_dlog`（Python + numpy）。前端已不再解析二進位；
改造前的 JS 版只保留在 `dlog_viewer_mem3_preview.BACKUP-before-python.html`，且**不含** `sense_*` 修正，
僅供對拍，不要照抄。

重點：`numpy.frombuffer(buf, dtype='>f4', count=count*len(fields)*per_field, offset=…)`
`.reshape(count, len(fields), per_field)`，取 `[:, :, 0]` 即 avg；trailer 先寫入目標陣列前段，主資料接在後面。

### 2.5 衍生量測換算

- 任一筆記錄 r（0-based，已含 pre-trigger 緩衝樣本）的時間戳：`t = r * tint`（秒，從 0 開始）。
- 實際總時長：`duration = (nRecords - 1) * tint`（此處 `nRecords` 已是含緩衝樣本的 `totalRecords`；**永遠以此為準，不用 `<time>` 宣告值**）。
- 索引 ↔ 時間互轉：`idx = round(t / tint)`，並 clamp 在 `[0, nRecords-1]`。

---

## 3. 應用程式架構

### 3.1 檔案結構（前端 HTML + 本機 Python 資料層）

> **2026-08 更新**：原本是「單一 HTML 檔、整份 .dlog 解析進瀏覽器記憶體」。
> 為了支援千萬筆級檔案，資料層已移到本機 Python server（`http.server` + numpy memmap），
> **UI／繪圖／主題／i18n／匯出全部仍在瀏覽器，一行未改**。詳見
> `N6705C_DLOG_Viewer_Python改造交接文件.md`。必須透過 server 開啟頁面，
> 不能直接雙擊 HTML。

```
N6705C-Dlog/
├── dlog_viewer_mem3_preview.html   前端（結構見下）
├── server/
│   ├── dlog_server.py    HTTP 路由：POST /open、GET /samples /buckets /stats /export/csv
│   ├── dlog_core.py      解析 / LOD 建置 / 電流負值校正 / 統計 / CSV 串流
│   ├── test_dlog_server.py         可執行自我檢查
│   ├── parity_tools/               數值對拍工具（Python 輸出 vs 原版 JS）
│   └── .cache/<sha1>/              正規化快取，可安全刪除
└── dlog_viewer_mem3_preview.BACKUP-before-python.html   改造前版本，對拍基準，勿刪
```

前端 HTML 本身仍是單一檔案：

```
dlog_viewer_mem3_preview.html
├── <style>            全站樣式，含 3 組配色主題（CSS 變數，見 §6）
├── <body>
│   ├── header.topbar         品牌、案件名稱輸入、Offset 開關、語言切換、主題選單、選檔按鈕
│   ├── div.file-info-bar     檔案資訊狀態列（獨立於 header，避免載入檔案時撐開版面）
│   ├── section.trace-panel   12 條曲線（CH1-4 電壓 + CH1-4 電流/功率，右軸下拉選單切換電流/功率）的顯示/顏色/名稱/位移控制 chip
│   ├── section.chart-section 工具列、V/I 軸控制、canvas 圖表、時間捲動條、統計面板、CSV/圖片/報告匯出列
│   ├── section.marker-table-section  Marker Table 開關、JSON 匯入匯出、標記清單表格
│   └── div.toast              浮動提示訊息
└── <script>            全部邏輯（IIFE 包裹）
```

### 3.2 JS 內部模組劃分（依程式碼出現順序）

1. **常數與主題表**：`MONO_FONT`, `PAD`（canvas 內距）, `N_BUCKETS`（縮圖抽樣桶數）, `CHART_THEMES`（畫布繪圖用配色，見 §6）
2. **`state` 全域狀態物件**（見 §4）
3. **`I18N` 字典 + `T(key, ...args)`**：中／英文字串查表，支援函式型模板（見 §7）
4. **資料層 API**：`apiOpen`／`apiSamples`／`apiStats`（打本機 server，見 §3.4）、
   `rawWindows`＋`ensureRawWindow`（目前視野附近的原始樣本快取）、
   `pointCache`＋`getPoint`（單點取值）、`invalidateDataCaches`、`installLod`。
   **二進位解析已移到 Python 端**，前端不再有 `parseDlog`／`indexOfBytes`／Worker。
5. **數值讀取入口**：`sampleAt`（統一入口，從 raw 視窗或單點快取取值）、
   `getV`／`getI`／`getVal`（依 kind 分派 V/I/P，P＝即時計算 getV×getI）。
   **電流負值校正已移到 server 端**，改用由索引推導的決定性雜湊，
   前端不再有 `ensureOffsetRandCache` 那組亂數快取。
   ⚠️ 分桶的內層迴圈**刻意不走這條路**——那是唯一的每樣本熱點，
   走 `sampleAt()` 會因每筆的 Map 查找與物件配置慢 7 倍（見交接文件 §3.4）。
6. **色彩工具**：`hexToRgb`, `rgbToHex`, `invertHex`（供「完全負片」匯出模式使用）
7. **`EXPORT_THEMES`**：圖片／報告匯出專用的三種配色（原色-動態跟隨目前主題／白底列印版／完全負片），見 §6.3
8. **動態分桶運算**：`computeViewBuckets`（將目前可視範圍的原始樣本依螢幕寬度分桶，回傳 min/max envelope，**已包含 posOffset**，見 §5.1）、`computeAxisRangeFromBuckets`、`resolveAxisRange`（auto/manual 兩種模式）、`computeRangeStats`（Max/Min/Avg，**不含 posOffset，永遠是真實值**）
9. **Trace chips UI**：`renderChips`, `onChipChange`（勾選/顏色/名稱/位移 4 個控制項的事件處理）
10. **檔案載入**：`fileInput` change handler → `apiOpen`（POST 位元組給 server，收回
    metadata + LOD binary）→ `installLod` → 初始化 `state.view`／`marker1`／`marker2`／
    `vAxis`／`iAxis` → `updateFileInfo`。比對檔（MEM）走同一條路，各自持有一個 handle。
11. **座標轉換**：`domainSpan`, `xOf`, `tOfX`（時間 ↔ 螢幕 X 座標）
12. **繪圖核心**：`draw()`（即時畫面用 canvas 2D context）與 `renderChartForExport(themeKey)`（離屏 canvas，供 PNG／報告截圖使用，邏輯與 `draw()` 對應但改用指定主題色、且會顯式填滿背景）
13. **匯出功能**：
    - 圖片 PNG：`exportImageBtn` → `renderChartForExport` → `toBlob`
    - HTML 報告：`buildStatsRowsHtml`, `buildMarkerTableHtml`, `buildReportHtml` → 內嵌 base64 圖片的獨立 HTML 字串 → Blob 下載
    - CSV：`csvRangeIdx`（依 M1↔M2／目前顯示範圍／全部資料 決定範圍）→ 純文字 CSV（**一律真實數值，不含 posOffset**）
    - JSON：Marker Table 清單匯出/匯入
14. **統計面板**：`updateStatsPanel`（M1/M2 數值、ΔT、每條曲線 Max/Min/Avg 表格）
15. **縮放／平移／歷史**：`syncRangeInputs`, `pushHistory`, `updateBackButton`, 全部按鈕事件
16. **時間軸捲動條**：`updateScrollbar`, 拖曳滑塊／點擊軌道 事件（取代原本的手掌拖曳平移，見 §5.3）
17. **V/I 軸控制**：`syncAxisInputs`, `adjustAxisScale`（滾輪縮放刻度）, `setAxisManual`（手動輸入 Ref/perDiv）, `beginAxisDrag`（拖曳調整 Ref，見 §5.4）
18. **滑鼠互動**：`mousedown`/`mousemove`/`mouseup`（marker 拖曳、圈選放大 box-zoom、V/I 軸拖曳）、`wheel`（**需按住 Ctrl/Cmd 才觸發縮放**，防呆設計）、`contextmenu`/`tryAddMarker`（右鍵新增 Marker Table 標記）
19. **Marker Table**：`renderMarkerTable`, 註記輸入／刪除事件
20. **`applyLanguage()`**：切換 `state.lang` 後，重新套用所有 `data-i18n*` 屬性與重繪動態內容
21. **`applyTheme()`**：切換 `state.theme` 後，設定 `<html data-theme>`、更新「尚未自訂顏色」曲線的預設色、重繪
22. **Init**：讀取 `localStorage` 還原上次的語言／主題 → `initTraces()` → 渲染 → `applyLanguage()` → `scheduleRedraw()`

### 3.3 渲染迴圈

`scheduleRedraw()` 用 `requestAnimationFrame` 節流，每次觸發依序執行 `draw()` → `updateStatsPanel()`（內部也會呼叫 `updateCsvExportHint()`）→ `updateScrollbar()`。所有會影響畫面的狀態變更（縮放、平移、勾選曲線、改顏色、加 Marker…）最後都呼叫 `scheduleRedraw()`。

### 3.4 資料流：重繪一律同步、零網路

**重繪不可以是非同步的**——拖曳 M1/M2、切主題、勾曲線這些操作都會重繪，但根本沒有
改變視野。因此前端保留兩層本地快取，`draw()` 全程同步取值：

| 目前視野筆數 | 資料來源 |
|---|---|
| ≥ 115,200（`LOD_F0 64 × N_BUCKETS 1800`） | **LOD**：開檔時隨 `/open` 一次載入，佔原始資料 4.8% |
| < 115,200 | **raw 視窗**：目前視野前後各留一倍 margin（快取視窗＝3 倍視野） |

只有「視野改變且離開已快取視窗」才會觸發一次非同步取數。取數期間 `computeViewBuckets()`
回傳 `trace._lastBuckets`（上一幀結果），避免空白閃爍，資料到達後自動重繪。

**server 端點**

| 端點 | 用途 |
|---|---|
| `POST /open` | body＝.dlog 位元組 → `[4-byte meta 長度][JSON metadata][LOD binary]` |
| `GET /samples` | 原始樣本切片，Float32 binary |
| `GET /buckets` | min/max 包絡（前端目前未使用，供對拍與未來擴充） |
| `GET /stats` | 區間 min/max/avg，JSON |
| `GET /export/csv` | CSV 串流下載，記憶體用量與筆數無關 |

所有回傳數值的端點都吃 `offset`／`maxLimit`——電流負值校正只在 server 端套用，
這是「匯出檔案不得出現負電流」的單一保證點。

⚠️ **匯出（PNG／HTML 報告）前必須先 `await ensureExportData()`**：渲染流程是同步的，
raw 視窗還沒到齊時會畫出空白圖表。改造前資料全在記憶體不存在這個中間狀態，
這是非同步資料層特有的坑。

---

## 4. 核心資料結構（`state` 物件）

```js
state = {
  // 檔案 / 原始資料
  fileName: string|null,
  tint: number|null,          // 取樣間隔（秒）
  declaredTime: number|null,  // header <time> 宣告值，僅供顯示比對，不用於計算
  nRecords: number|null,
  channelModels: [CH1型號, CH2型號, CH3型號, CH4型號],

  // 原始樣本已不在前端。改為：
  loaded: boolean,            // 是否已載入檔案（取代舊版「state.V 是否存在」的判斷）
  handle: string|null,        // server 端的資料代號（＝檔案內容 sha1），所有取數端點都吃它
  lod: Map,                   // LOD 摘要，key 為 'p:V1'／'c:I3'（'p'＝主檔案、'c'＝比對檔）
  compare: { fileName, handle, tint, nRecords, fields, minmax } | null,   // 比對檔（MEM）

  activeChannels: [1,2,3,4],  // 實際有記錄資料的聲道 id 清單（來自 server metadata，不一定是全部 4 個）

  // 曲線設定（見 §5.1）
  traces: [ TraceObject × 12 ],  // CH1-4 電壓 + CH1-4 電流 + CH1-4 功率
  rightAxisKind: 'I' | 'P',  // 右軸目前顯示「電流」或「功率」，二擇一（單位不同，不會同時顯示）

  // System A：雙游標量測
  marker1: number|null,   // 時間（秒）
  marker2: number|null,

  // System B：Marker Table（獨立於 marker1/marker2）
  markerTableOn: boolean,
  markers: [ MarkerObject ],   // 最多 10 筆

  offsetMode: boolean,        // 電流負值校正開關（**會回溯既有標記**：marker 只存身分不存數值快照）
  offsetMaxLimit: number,     // 電流負值校正的最大值限制：負值會被 (0, offsetMaxLimit] 區間內的值取代，恆大於 0
  // offsetRandCache 已移除——校正改在 server 端以「由索引推導的決定性雜湊」產生，
  // 同一索引在任何端點、任何切片都得到相同值，不需要快取狀態

  // 縮放 / 平移
  view: { t0: number, t1: number },   // 目前可視時間範圍
  viewHistory: [ {t0,t1} ],           // 供「回上一步」

  // V / 右軸刻度（共用，非各通道獨立；右軸依 rightAxisKind 代表電流或功率）
  vAxis: { auto:boolean, ref:number, perDiv:number, _autoMin, _autoMax },
  iAxis: { 同上 },

  lang: 'zh' | 'en',
  caseName: string,        // 使用者填的案件名稱，用於報告與匯出檔名前綴
  theme: 'dark' | 'soft-dark' | 'light',
}
```

### 4.1 `TraceObject`（`state.traces` 陣列元素，共 12 個：CH1-4 電壓 + CH1-4 電流 + CH1-4 功率）

```js
{
  key: 'v1'|'i1'|'p1'|...'v4'|'i4'|'p4',
  ch: 1|2|3|4,
  kind: 'V'|'I'|'P',          // 'P' = 功率，即時計算 = 電壓 × 電流（套用與電流相同的負值校正）
  label: string,              // 顯示名稱，預設 "CH{n} 電壓/電流/功率"
  labelIsDefault: boolean,    // true=尚未被使用者改過名稱，語言切換時會自動跟著換字；Marker Table 通道名稱一律即時讀這裡
  color: '#rrggbb',
  colorIsDefault: boolean,    // true=尚未被使用者改過顏色，切換主題時會自動跟著換色
  visible: boolean,           // 電壓預設 true，電流／功率預設 false
  dash: boolean,               // 電壓實線（false），電流／功率虛線（true）
  posOffset: number,          // 該通道獨立的「顯示位移」，單位與該通道相同(V/A/W)
                                // ⚠ 純視覺位移：只影響畫面繪製 Y 座標，
                                //    不影響 Marker/統計/CSV 等任何真實數值
}
```

右軸（電流 vs 功率）同一時間只會顯示其中一種 kind 的曲線：`renderChips()`／`draw()`／`renderChartForExport()`／`tryAddMarker()` 篩選右軸曲線時一律用 `t.kind===state.rightAxisKind`（V 軸曲線固定用 `t.kind==='V'`）。

### 4.2 `MarkerObject`（`state.markers` 陣列元素，Marker Table 用，最多 10 筆）

```js
{
  id: number,        // 顯示編號，刪除後會重新排序
  time: number,       // 秒
  ch: 1|2|3|4,
  kind: 'V'|'I'|'P',  // 標記點是點在電壓／電流／功率曲線上
  color: '#rrggbb',   // 新增當下該曲線的顏色（快照）
  note: string,
}
```

> **不存數值快照**：voltage／current／power 一律經 `markerValues()` 依目前校正設定即時取值
> （畫面、CSV、JSON、報告、PNG 同一入口）。否則校正開啟前建立的 marker 會把負電流一路寫進輸出。
> JSON 匯入忽略檔內數值，只採 time／channel／type／note。

通道名稱顯示一律呼叫 `markerChannelLabel(m)`：優先查目前 `state.traces` 中對應 `ch`+`kind` 的 `label`（即時跟隨曲線面板命名變更），找不到才退回組字串。

---

## 5. 關鍵設計慣例（供後續擴充遵循）

### 5.1 「真實值 vs 顯示值」分離原則

- **一律使用真實值**的地方：`computeRangeStats`（統計 Max/Min/Avg）、Marker Table 儲存的 voltage/current、CSV 匯出、JSON 匯出、M1/M2 數值文字標示。
- **套用 `posOffset` 顯示位移**的地方：曲線本身的繪製（`computeViewBuckets` 已內建加總 offset，同時也讓自動刻度範圍正確地把位移後的曲線算進去，避免被裁到畫面外）、M1/M2 圓點在圖上的 Y 座標、Marker Table 標記點在圖上的 Y 座標、右鍵新增標記時判斷「最靠近的曲線」的命中測試。
- 新增任何會在畫布上標示某通道數值位置的功能時，**Y 座標計算務必加上 `(trace.posOffset||0)`，但顯示的文字/寫入的資料維持真實值**。

### 5.2 「已預設 vs 使用者自訂」旗標模式

- `labelIsDefault` / `colorIsDefault` 兩個旗標的設計目的：語言或主題切換時，只更新「使用者沒動過」的曲線名稱/顏色，已被手動改過的維持不變。任何新增的「可被全域設定批次覆蓋」的曲線屬性，建議沿用此模式（一個 `xxxIsDefault` 布林旗標 + 對應的使用者編輯事件裡設為 `false`）。

### 5.3 時間軸導覽：已改為捲動條，不用手掌拖曳

- v1.0 起，主圖區域拖曳固定只做「圈選放大」（box-zoom），**沒有 Shift+拖曳平移**，也沒有在圖表下緣直接拖曳平移。
- 時間平移統一由圖表下方的自製捲動條（`#timeScrollbar` / `#tsTrack` / `#tsThumb`）負責：拖曳滑塊＝平移（span 不變），點擊軌道空白處＝以該點為中心跳轉。
- Ctrl(⌘)+滾輪：縮放時間（有防呆，沒按修飾鍵滾輪不會觸發縮放，只是正常頁面捲動)。

### 5.4 V/I 軸的 Ref（參考位置）調整：拖曳優先於手動輸入

- 滑鼠停在圖表左（V軸）／右（I軸）邊界，上下拖曳可直接調整該軸 Ref；Ctrl+滾輪調整 perDiv（每格刻度）。
- 數字輸入框（Ref / 格）仍保留作為精確輸入的備用方式，兩者同步。
- **注意**：V 軸／I 軸的 Ref 目前是「全部電壓通道共用一條 V 軸、全部電流通道共用一條 I 軸」，**不是每通道獨立的軸**。若使用者需要「單一通道獨立上下移動」，是用 §5.1 提到的 `posOffset`（純視覺位移）達成，而非給每通道各自的 Ref/perDiv——這是刻意的設計取捨（曾規劃過「每通道完全獨立 Y 軸」的做法但因改動範圍過大而採用較輕量的視覺位移方案）。

### 5.5 匯出檔名規則

`exportBaseName()`：若 `state.caseName` 有填，一律加在檔名最前面（過濾檔名不合法字元），格式為 `{案件名稱}_{原始檔名去除.dlog}`；否則就只用原始檔名。所有匯出（CSV/PNG/JSON/HTML 報告）共用此函式。

---

## 6. 配色主題系統

### 6.1 CSS 介面色（`html[data-theme="..."]`）

在 `:root` 定義預設「深色經典」（`dark`）的 CSS 變數，另外兩組主題用屬性選擇器覆寫：

```css
:root{ --bg; --panel; --panel-2; --border; --text; --text-dim; --text-faint; --ch1; --ch2; --ch3; --ch4; --danger; ... }
html[data-theme="soft-dark"]{ /* 覆寫同名變數 */ }
html[data-theme="light"]{ /* 覆寫同名變數 */ }
```

三組主題：`dark`（深色經典，預設）、`soft-dark`（柔和深色，暖炭灰底、降低對比，長時間觀看用）、`light`（淺色紙感，明亮環境用，曲線色刻意加深以維持對比度）。

### 6.2 Canvas 繪圖色（`CHART_THEMES`）

因為 `<canvas>` 無法讀取 CSS 變數，JS 端另外維護一份 `CHART_THEMES` 物件（key 與 `data-theme` 屬性值一致：`dark`／`soft-dark`／`light`），欄位：`bg, grid, axisText, markerLine, markerLabel, badgeFill, badgeText, chColors[4]`。`draw()` 開頭取 `CHART_THEMES[state.theme]` 供整個函式使用；`chColors` 只在 `initTraces()`／`applyTheme()` 更新「預設色」曲線時使用。

### 6.3 圖片／報告匯出配色（`EXPORT_THEMES`，與上面兩者為獨立第三套）

- `normal`：**用 JS getter 動態跟隨 `state.theme`**（`EXPORT_THEMES.normal` 每次存取都即時反映目前頁面主題），語意是「原色（目前主題）」。
- `light`：固定的白底列印版（不受頁面主題影響，永遠白底＋深色曲線原色）。
- `negative`：固定的完全負片（背景/格線/文字/曲線色全部做真正的像素 RGB 反轉，`invertHex()`）。

---

## 7. 多語系（i18n）機制

- `I18N = { zh: {...}, en: {...} }`，`T(key, ...args)` 依 `state.lang` 查表，值可以是字串或函式（函式用於需要帶參數的模板，例如 `msgCsvExported: n => '已匯出 '+n+' 筆資料至 CSV'`）。
- 靜態 HTML 文字：加 `data-i18n="key"`（整個 textContent 替換，因此只能用在「純文字、無子元素」的節點，若原本是文字節點混在其他元素旁，需額外包一層 `<span data-i18n="...">`）。
- Placeholder／title 屬性：`data-i18n-placeholder="key"` / `data-i18n-title="key"`。
- `applyLanguage()` 是唯一進入點：切換語言、重新渲染所有動態內容（trace chips 預設名稱、檔案資訊、統計表頭、Marker Table）、寫回 `localStorage`。
- **新增任何 UI 文字時，一律走 `T()`，不要在程式碼中留下裸露的中/英文字串**（canvas 內的文字也一樣，例如軸標題 `T('axisVTitle')`）。

---

## 8. 已實作功能清單（v1.4）

- .dlog 二進位解析（見 §2），支援可變聲道數（不再假設固定 4 聲道）與 pre-trigger 緩衝樣本，資料在 Python 端以 memmap 處理，筆數不受瀏覽器記憶體限制
- 12 條曲線：CH1-4 電壓／電流／功率（功率＝即時計算 V×I，單位 W，套用與電流相同的負值校正），獨立勾選顯示、顏色、名稱、視覺位移（posOffset）
- 右軸種類切換（電流 A ↔ 功率 W）：下拉選單選擇，兩者單位不同，右軸同一時間只顯示其中一種，切換時重置右軸刻度為自動
- Canvas 波形圖：min-max envelope 動態分桶抽樣（`N_BUCKETS=1800`，避免大檔案卡頓）
- 縮放：Ctrl(⌘)+滾輪、圈選放大（box-zoom）、起訖時間手動輸入、「全部」／「回上一步」
- 時間軸捲動條平移（取代手掌拖曳）
- V/I（或 V/P）軸：自動 autofit ↔ 手動 Ref+perDiv，滾輪調整刻度、拖曳調整 Ref
- System A 雙游標（M1/M2）：拖曳調整、「M1,M2 置中」快速找回、即時顯示交點數值與統計（Max/Min/Avg, ΔT，含功率統計）
- System B Marker Table：右鍵新增（最多 10 點，自動吸附最近樣本，支援電壓／電流／功率三種曲線），通道名稱即時跟隨曲線面板的命名變更，同時顯示電壓/電流/功率三個數值、註記、刪除、JSON 匯入匯出
- 電流負值校正：開啟後電流負值以 `(0, 最大值限制]` 區間內的值取代（恆大於 0、不等於 0，模擬雜訊底值）；值由 server 端依索引決定性雜湊產生，不會每次重繪都變動
- 統計視窗／Marker Table 的電流欄位依數量級自動換算 A/mA/µA/nA 單位、功率欄位同理自動換算 W/mW/µW/nW（共用 `fmtAutoScale()`，分別包成 `fmtCurrentAuto()`／`fmtPowerAuto()`），每級固定小數點後 4 位，比其他數值欄位（只換算到 m）多兩級精度——避免電流負值校正、或功率（=電壓×電流）連帶產生的極小正值被四捨五入到看起來像 0
- 匯出：CSV（可選 M1↔M2／目前顯示範圍／全部資料，可選僅匯出已勾選曲線）、圖片 PNG（三種配色）、HTML 報告（內嵌截圖+統計+選擇性附 Marker Table 清單）、Marker JSON（含 offset 校正設定與功率欄位）
- 案件名稱輸入，套用於報告與匯出檔名前綴
- 中文／英文語言切換，記住偏好
- 三組配色主題（深色經典／柔和深色／淺色紙感），記住偏好
- 檔案資訊獨立狀態列，載入檔案不會撐動上方控制列版面；僅顯示實際有記錄資料的聲道
- header `<time>` 宣告時長與實際時長不符時的智慧提示（提前停止＝資訊提示，非錯誤；實際超過宣告值＝真警示）
- 載入檔案時依實際讀進來的聲道數，自動預設勾選對應聲道的電壓與電流曲線（功率一律維持不勾選，需要時使用者自行開啟；未啟用的聲道無真實資料，一律不勾選）
- 電流負值校正的「最大值限制」輸入框預設值調整為 `0.0001`，但校正開關預設仍維持關閉，需使用者手動開啟
- 「⬇ 匯出圖片 PNG」改為匯出一張直向堆疊的長圖：曲線圖 + M1/M2 統計值表格（必備），若 Marker Table 開關已開啟則再往下附加 Marker Table 清單；HTML 報告的內嵌截圖維持原本「只有曲線圖」（報告本身已有獨立 HTML 統計表格與標記清單，避免重複）

---

## 9. 已知設計取捨（非 bug，供後續評估是否要改）

1. V/I 軸 Ref/perDiv 為「電壓共用一組、電流共用一組」，非每通道獨立完整校正軸；每通道獨立需求目前用 `posOffset` 純視覺位移滿足。
2. ~~分桶為 O(n) 線性掃描，尚無 LOD~~ → **已解決**：兩層 LOD 金字塔（§3.4），
   10M×4ch 全覽每幀 1.1ms。分桶數仍固定 `N_BUCKETS=1800`。
3. ~~匯出皆為記憶體中的完整資料~~ → **CSV 已改為 server 端串流**，記憶體與筆數無關。
   PNG／HTML 報告仍在瀏覽器渲染（刻意保留，截圖規則與改造前逐位元組一致）。
3b. **校正開啟 + 超大檔案全覽**是目前唯一的效能懸崖：校正後的值用不了預建 LOD，
   前端會退回索取整段原始樣本（10M 全覽＝一次 305MB 請求、每幀 736ms）。
   校正預設關閉、屬特殊測項專用。升級路徑：讓前端在這種情境改用已存在的
   `/buckets` 端點（server 端同樣條件只要 354ms、回傳約 1800×3 個浮點數）。
4. Marker Table 上限固定 10 點（寫死常數，非 UI 可調）。
5. `localStorage` 只保存語言與主題偏好，不保存曲線設定、Marker、案件名稱等（重新整理頁面即重置，唯一例外是語言/主題）。

---

## 10. Python 資料層的實際做法（2026-08 已實作）

原本這一節寫的是「若要用其他語言重寫的建議」。實際採用的是**混合架構**：
資料層搬到 Python，UI 與繪圖完全留在瀏覽器。以下是已落地的做法。

- **解析**（`server/dlog_core.py::parse_dlog`）：`struct.unpack_from('>Q', ...)` 讀記錄筆數，
  `numpy.frombuffer(buf, dtype='>f4')` 一次讀整個記錄區塊再 reshape 成
  `(count, 欄位數, per_field)`，取 `[:, :, 0]` 即各欄位的 Avg 值。
  pre-trigger trailer 先寫入目標陣列前段、主資料接在後面（見 §2.3.1）。
- **正規化快取**：以**檔案內容 sha1** 為鍵存在 `server/.cache/<sha1>/`，
  內容是已轉小端序的 `V{ch}.npy`／`I{ch}.npy` + `lod.npz` + `meta.json`，
  一律 `mmap_mode='r'` 開啟，**原始樣本不進 RAM**。內容相同必定命中同一份快取。
- **LOD**：兩層 min/max 金字塔（factor 64 與 4096），開檔時隨 `/open` 一次送到前端常駐。
  只佔原始資料的 4.8%（10M×4ch 約 14.5MB），讓大範圍縮放的重繪維持同步、零網路。
- **電流負值校正**：改用 splitmix64 由**索引**推導 (0,1] 均勻值，
  同一索引在任何端點、任何切片恆得同值，因此不需要任何快取狀態，
  也消除了「同一套亂數邏輯跨語言各寫一份」的風險。只在 server 端套用。
- **CSV 匯出**：分塊 yield、chunked transfer，瀏覽器邊收邊寫磁碟，
  記憶體用量與筆數無關（10M 筆 → 1.5GB 檔案，46 秒落地）。
- **不引入 web 框架**：5 條路由用 stdlib `http.server` 就夠，FastAPI/Flask 不值得，
  打包成單一執行檔時也更小。
- **數值一致性怎麼保證**：`server/parity_tools/` 從改造前的備份 HTML 自動抽出原版 JS，
  在 Node 上實際執行，與 Python 輸出做 **float32 逐位元組**比對。
  改動 `dlog_core.py` 後必須重跑。

**若真的要整包重寫成桌面應用**（PyQt5 等），§2 的格式規格與上述解析做法可直接沿用；
繪圖建議用 `pyqtgraph`，但 min-max envelope 分桶（§3.2 第 8 點）與 LOD 仍要保留，
否則大資料量下不可能流暢。主題改用 QSS 切換，i18n 沿用「字典 + key 查表」。

---

*文件版本對應：dlog_viewer_mem3_preview.html（v1.4 UI + 2026-08 Python 資料層）*
