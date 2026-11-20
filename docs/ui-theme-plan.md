# UI 主題與版面改版：開工計畫

2026-11-20 規劃並完成實作（Wave 0–1 已合併 `feat/ui-theme`；狀態見 HANDOFF §15）。照 HANDOFF §9.3：使用者對每個階段說「開工」才動程式碼；實作交 Codex，審查交 Opus，Claude 驗收；使用者說 commit 才 commit；新工作一律從 main 開分支。視覺參考：`../示波器截圖_html/scope_tool/web/static/css/app.css:4-24`。

## 1. 目標與非目標

**目標**
1. 殼層（SEQ／List／Record）與 Viewer 共用同一個淺色／深色主題，只在一個地方切換，全域生效，並且會記住使用者的選擇。
2. Viewer 圖表改用示波器配色：黑底、格線 `#3a3a3a`、色盲友善的通道色。
3. SEQ／List 的 CH1–4 預設色和 Viewer 的 CH1–4 一致。
4. 版面調整：Viewer 改為滿版 grid；連線區改為單列 connbar；分頁改為膠囊型分段切換。

**非目標**
- 毛玻璃效果（`backdrop-filter`）與 `color-mix()`：Windows WebView2 上未驗證，一律不用。半透明只用 `rgba()`。
- 不追蹤系統主題的即時變化（只在第一次啟動時參考系統設定，見 §3）。
- 不改 parity 來源 `tests/analysis/parity_tools/original_before_python.html`。
- 不動 Viewer 的匯出配色選項（原色／白底列印版／完全負片，`viewer.html:497-499`），只確保白底列印時通道色看得清楚（§4）。
- 不改 List 畫布寫死像素高度的做法（HANDOFF §4.13）。

## 2. Token 表

只定義一組語意 token。殼層的 token 放在 `style.css` 的 `:root`（淺色）和 `:root[data-theme="dark"]`（深色）。Viewer 位於另一個 origin（分析 server），所以在 `viewer.html` 內聯一份相同的值，由測試確保兩邊一致（P3）。

| token | 淺色 | 深色（沿用示波器） | 用途 |
|---|---|---|---|
| `--bg` | `#f5f6f8` | `#1b1f29` | 頁面底色 |
| `--surface` | `#ffffff` | `#262b38` | 面板、輸入框 |
| `--surface-hi` | `#eef0f4` | `#303645` | hover、選中的膠囊 |
| `--border` | `#c9ced8` | `rgba(255,255,255,.16)` | 邊框 |
| `--text` | `#1b1f29`（對 bg 15.2:1） | `#f2f4f8`（15.0:1） | 主要文字 |
| `--text-dim` | `#4a5263`（7.3:1） | `#cdd3de`（11.0:1） | 次要文字（取代 `.muted #666`） |
| `--accent` | `#0a5fc2`（文字 5.7:1） | `#8cc4ff`（9.0:1） | 主要按鈕底色、選中分頁、focus |
| `--accent-text` | `#ffffff`（對 accent 6.1:1） | `#0b1220`（10.2:1） | 主要按鈕文字 |
| `--danger` | `#b42318`（6.1:1） | `#ff8f87`（7.5:1） | 全部關閉、錯誤 |
| `--ok` | `#1e7f4f`（4.6:1） | `#62dd98`（9.7:1） | 連線成功 |
| `--warn` | `#8a5a00`（5.5:1） | `#f7cd5f`（10.9:1） | `.warn` 文字 |
| `--plot-bg` | `#ffffff` | `#000000` | 畫布底色 |
| `--plot-grid` | `#d9d9d9` | `#3a3a3a` | 格線 |
| `--plot-axis` | `#5c5c5c`（6.2:1） | `#8a8a8a`（4.8:1） | 軸線、刻度文字 |
| `--plot-text` | `#333333`（11.7:1） | `#d4d4d4`（11.1:1） | 畫布上的標籤 |

**通道色**：深色組的 CH1–4 是示波器的 Okabe-Ito 系色彩；淺色組由同一色相加深而來。

| | CH1 | CH2 | CH3 | CH4 | CH5 | CH6 | CH7 | CH8 |
|---|---|---|---|---|---|---|---|---|
| 深色（對 `#000`） | `#f0e442` 15.9 | `#56b4e9` 9.1 | `#e58fc4` 9.1 | `#3dd6a3` 11.4 | `#e69f00` 9.3 | `#b79cff` 9.2 | `#f4845f` 8.3 | `#a6d854` 12.6 |
| 淺色（對 `#fff`） | `#7a6a00` 5.4 | `#0068a8` 5.9 | `#b4367f` 5.6 | `#00785a` 5.5 | `#9a5b00` 5.4 | `#6a4bc4` 6.2 | `#b8401a` 5.6 | `#4a7a12` 5.1 |

- 所有數值都用 WCAG 相對亮度公式算過。主要文字與按鈕的對比都 ≥4.5:1。深色主題的 `--plot-axis` 是 4.8:1，只用在刻度。
- SEQ 圖表會用通道色畫通道名稱文字（`seq.js:87`），所以通道色也要求 ≥4.5:1，兩組都符合。舊 PALETTE 在白底只有 1.95–4.02:1。
- 已知限制：8 個通道不可能全部對色盲友善。CH1–4 用 Okabe-Ito 系色彩；淺色組的 CH1、CH5、CH8 偏黃綠／棕，彼此較接近，但 SEQ 每個通道有自己的 lane，有名稱標籤可以區分。
- 色值一律寫成小寫 `#rrggbb`，因為 `<input type=color>` 只接受這種格式。

## 3. 主題切換機制

做法完全照語言切換現有的路徑（`app.js:120-129` → `webgui.set_lang` → `form.save_config` → `analysis.js:233,258` postMessage → `viewer.html` `setLang`），加一條平行的主題路徑。

1. **持久化**：沿用現有設定檔 `form.CONFIG = ~/.n6705c_seq_gui.json`（`form.py:17`）；專案裡沒有 `config.json`，這個檔就是現有的設定機制。
   - `load_config` 多回傳 `"theme"`：值為 `"light"`／`"dark"`，其他值一律為 `None`。
   - `save_config(path, resources, demo, lang, theme)` 加上 theme 參數；`webgui._save_config` 一併寫入。
   - 舊設定檔沒有 `theme` 欄位時讀成 `None`，不需要遷移。
2. **初始值**：`theme is None` 時，JS 用 `matchMedia("(prefers-color-scheme: dark)")` 決定一次，但不寫回設定檔，直到使用者自己切換。之後系統主題改變也不跟著變（YAGNI）。
3. **殼層**：`app.js` 在 `pywebviewready` 讀 `config.theme`，設定 `document.documentElement.dataset.theme`。新增 `btn-theme` 鈕，放在 `btn-lang` 旁邊；按下後呼叫 `App.api.set_theme(next)`，再 `emit("theme")`。各分頁訂閱 `theme` 事件並重畫畫布。
4. **Viewer**：
   - `server.url(gen, lang, theme)`，URL hash 多一個 `&theme=`（`server.py:631-632`）。
   - 新增 shell→iframe 訊息 `{type:"theme", theme}`，在 ready 時以及 `App.on("theme")` 時送出，和 lang 的送法相同。
   - Viewer 收到後呼叫既有的 `applyTheme(theme, false)`（`viewer.html:3323`）。
5. **Viewer 原有三個主題的處理**：收斂成 `dark`／`light` 兩個。
   - 刪除 `soft-dark` 的 CSS 區塊（`viewer.html:26-39`）、`CHART_THEMES['soft-dark']`（`:556`）、`themeSelect` 下拉（`:399-403`）和三條 i18n 字串。
   - Viewer 不再自己切換主題，也不讀 `/prefs` 裡的 theme（`:3377` 改為忽略）。`/prefs` 的 fold 照舊。
   - server 的 `_THEMES`（`server.py:31`）和 `_prefs_ok` **不改**：舊 prefs 檔裡存的 `soft-dark` 仍然合法，使用者的 fold 狀態不會丟。
   - 單獨開啟 Viewer（`shellCtx.ok` 為 false）時，從 hash 讀 theme，沒有就用 `matchMedia` 決定。
6. **合約**：同步修改 `contracts.py` 和 `docs/analysis-contract.md` §4。
   - `CONFIG_KEYS` 加上 `"theme"`（`contracts.py:24`）。
   - `set_theme` 放進 `SELFTEST_JS_API_DENIED`，理由同 `set_lang`：會寫設定檔（`:43`）。
   - 新增 `Msg.THEME = "theme"`，`MSG_FIELDS` 為 `{"theme"}`，並加入 `MSG_TO_IFRAME`（`:397-412`）。
   - `url` 的協定簽章（`:527`）和註解（`:159`）也要改。

## 4. 通道色統一與 CSV 相容

- **單一來源**：CSS token `--ch1`…`--ch8`。`form.PALETTE`（`form.py:18`）改成 §2 深色組的 8 個值，只作為後備，例如測試環境或 `getComputedStyle` 拿不到值的時候。
- **SEQ**（`seq.js:146`）：預設色取自 `cssVar("--ch"+(i+1)) || meta.palette[i]`。使用者在 `<input type=color>` 改過的列設 `dataset.custom=1`。切換主題時只更新沒改過的列，做法和 Viewer 的 `colorIsDefault` 相同（`viewer.html:3331-3336`）。
- **List**（`list.js:184,212`）：lane 的曲線色從寫死的 `#2563eb` 改為 `--ch{lane.channel}`；lane 本來就有 `channel` 欄位（`list_form.py:208,225`）。
- **Record**（`record.js:249`）：V／I 兩種曲線改用 `--ch{n}`，電流用虛線區分。
- **Viewer**：`--ch1..4` 和 `CHART_THEMES.*.chColors` 改成 §2 的 CH1–4。比對檔的色盤 `COMPARE_CH_COLORS` 不改。白底列印版（`:1503`）的 `mapColor` 改成把深色組映射到淺色組，避免黃色 `#f0e442` 印在白底上看不見。
- **CSV 相容（已查證）**：CSV 沒有 color 欄位。表頭是 `enabled,name,delay_ms,low_v,high_v,rise_ms,hold_ms,fall_ms,current_limit_a`（`examples/*.csv`）。`plan.py`、`listplan.py`、`list_form.py` 也都沒有 color。顏色只存在 UI 裡，重開程式就回到預設值。**不需要遷移，也不需要相容層。** 先前分析提到的「CSV color 欄位」並不存在。
- **測試影響**：`test_web_seq.py:105` 比對的是 `meta palette == form.PALETTE`，兩邊一起改，測試自然通過。另外新增一個測試，斷言 `form.PALETTE` 和 `style.css` 深色組的 `--ch1..8` 相同。

## 5. 版面改動清單

| 頁面 | 改動 |
|---|---|
| 殼層 `index.html`／`style.css` | 原本的 `<fieldset class="connection">`（含 legend 和兩列 grid）改成單列 `.connbar`：`[Frame1 位址][選單][狀態] [Frame2 …] [掃描][測試][Demo] … [主題][語言][全部關閉]`。空間不夠時 `flex-wrap`，元素 id 全部不變。 |
| 分頁 `.tabs` | 改成膠囊分段切換：外框 `border-radius:999px` 加 `--surface` 底色，選中的那一顆用 `--accent` 底和 `--accent-text` 字。`role=tab`／`aria-selected` 不變。 |
| SEQ `seq.css`／`seq.js` | 畫布底色、格線、軸色改讀 token（`seq.js:71-85`）；表格和輸入框改用 token。最小高度 404px 不變。 |
| List `list.css`／`list.js` | 改用 token：`.list-strip` 的 `background: white`、`#cbd5e1`、`#dc2626` 換成 `--accent`、`#f1f5f9` 等。budget 條三色改為 `--plot-axis`／`--ch2`／`--ch5`。 |
| Record `record.css`／`record.js` | `#cbd5e1`、`#b45309`／`#92400e` 換成 `--border`、`--warn`；畫布顏色改讀 token。 |
| 分析分頁 | `.analysis-frame` 的 `#ccc` 換成 `--border`；`#analysis-modes` 兩顆按鈕也改成膠囊。 |
| Viewer `viewer.html` | `.app` 拿掉 `max-width:1180px; margin:0 auto`（`:69-70`），改為滿版 `display:grid; grid-template-rows:auto auto 1fr auto`，讓圖表區吃掉剩餘高度。topbar 拿掉 themeSelect。 |

## 6. 分階段

> 並行拆分（A0／B／C／D／E1／E2／F、檔案歸屬與波次）見 `docs/ui-theme-parallel.md`；分工以該檔為準。

每個階段開一個獨立分支和 PR（`feat/theme-p0` 等）。CI 綠燈且使用者說 commit 才合併。每個階段結束都要跑：`python3 -m pytest -q`，以及 `node --test tests/js/`。

### P0：token 與主題機制（還不換任何顏色）
- **改動**：`form.py`（load／save theme）、`webgui.py`（`set_theme`、`_save_config`、viewer url）、`contracts.py`、`analysis/server.py`（`url` 多一個參數）、`app.js`（`btn-theme`、`data-theme`、`emit("theme")`）、`analysis.js`（送 theme 訊息）、`index.html`（加按鈕）、`i18n.py`（`theme_light`／`theme_dark` 字串）、`style.css`（只新增 §2 的 token 定義，不改任何既有規則）、`docs/analysis-contract.md`。
- **測試**：
  - `test_form.py:171-202`：新增 theme 讀寫，包含舊檔沒有 theme、theme 為非法值兩種情況。
  - `test_webgui.py:144-158`：config 與設定檔的 JSON 斷言都要多 `"theme"`。
  - `test_selftest_api_classes.py`：`set_theme` 要被分類。
  - `tests/analysis/test_http.py:206`：URL 斷言加上 `&theme=`。
  - `test_analysis_integration.py:76`：多解析 theme。
  - `analysis.test.js`：ready 後送出 theme，`App.on("theme")` 時再送一次。
- **風險**：所有寫入 `save_config` 的地方都要帶上 theme，否則按一下語言切換就會把 theme 清掉（`webgui.py:166`）。要寫一個測試：先設 theme，再切語言，確認 theme 還在。
- **驗收**：按主題鈕後，`<html data-theme>` 改變、設定檔寫入、重開程式後仍是同一個主題；Viewer iframe 收到 `theme` 訊息（此時畫面顏色還不會變）。

### P1：殼層 CSS 改用 token
- **改動**：`style.css`、`seq.css`、`list.css`、`record.css`，把所有寫死的 `#xxx` 換成 `var(--…)`。
- **測試**：
  - CSS 檔裡**不可以出現中日韓文字**。`test_web_seq.py:532-535`（seq.css）、`record.test.js:316-317`（record.css）會檢查，註解也必須用英文。
  - `analysis.test.js:257`：`href="record.css"` 不可以改名。
  - 新增一個 Python 測試：斷言這 4 個 CSS 檔除了 `style.css` 的 token 區塊以外，沒有任何 `#[0-9a-f]{3,6}`，避免之後有人又寫死顏色。
- **風險**：WebView2 上 `zoom`（HANDOFF §4.13）搭配新的 `border-radius` 和 padding，可能讓 `fitZoom` 量出不同的高度。這要在 P3 版面改動之後一起看。
- **驗收**：淺色和深色兩種主題下，SEQ、List、Record 三頁的截圖都沒有殘留舊色或對比不足的地方。在 macOS 上手動確認。

### P2：畫布改從 token 取色
- **改動**：`layout.js` 新增 `App.cssVar(name, fallback)`：
  `getComputedStyle(document.documentElement).getPropertyValue?.(name)?.trim() || fallback`。
  `seq.js`、`list.js`、`record.js` 畫圖時改用它取色，並訂閱 `App.on("theme", draw)`。
- **測試**：
  - `test_web_seq.py:728` 的 `getComputedStyle` 替身只回傳 `{fontFamily}`，沒有 `getPropertyValue`，所以 `cssVar` **必須**用 optional chaining 並退回 fallback，替身不用改。新增一個斷言：替身環境下畫圖不會丟例外，而且用的是 fallback 色。
  - `layout.test.js`：`cssVar` 有值時回傳該值，沒值時回傳 fallback。
  - `list.js` 不可以出現中日韓文字（`test_web_list.py:330-335`）。
- **風險**：畫布不會自己跟著 CSS 變，切換主題時一定要重畫，包括目前隱藏的分頁（切回來時要重畫）。
- **驗收**：切換主題後，三個分頁的畫布底色、格線和曲線色都立刻跟著改變。

### P3：Viewer 對齊與版面
- **改動**：
  - `viewer.html`：§2 的 token 內聯；`CHART_THEMES` 只留 `dark`／`light`，值使用 `--plot-*`。
  - 刪除 `soft-dark` 和 `themeSelect`；新增處理 `theme` 訊息與 hash；`.app` 改為滿版 grid。
  - 殼層 `index.html`／`style.css`：connbar 與膠囊分頁。
- **測試**：
  - `analysis.test.js:559-565` 用標記字串切出 Viewer 腳本，**標記字串不可以動**；刪除區塊時要先 `grep` 確認沒有刪到任何標記。
  - 新增 `analysis.test.js` 測試：收到 `{type:"theme"}` 會呼叫 `applyTheme`，非法值會被忽略。
  - 新增 Python 測試：`viewer.html` 和 `style.css` 的 `--ch1..4`、`--plot-*` 兩組值必須相同。
  - `tests/analysis/test_http.py:524-527` 不改，舊 prefs 仍然合法。
- **風險**：
  - Viewer 有 3000 多行，`CHART_THEMES` 的引用散在 `:1385、1500、2044`。
  - 滿版之後，畫布寬度變化會觸發重新取樣（LOD），要確認效能。
  - connbar 換行後高度改變，會影響 `fitZoom`。
- **驗收**：殼層切換主題時，Viewer 同步改變；深色主題下圖表為黑底、格線 `#3a3a3a`；視窗寬 1920 時 Viewer 沒有左右留白；最小視窗下 connbar 不會撐出捲軸。

### P4：通道色統一
- **改動**：`form.py:18` PALETTE；`seq.js`（`cssVar` 預設色＋`dataset.custom`）；`list.js`（lane 使用 `--ch{channel}`）；`record.js`；Viewer 白底列印版的 `mapColor`。
- **測試**：
  - `test_web_seq.py:105` 自動跟上。
  - 新增：PALETTE 和 `style.css` 深色組一致。
  - 新增：使用者改過顏色後切換主題，顏色不會被覆蓋。
  - 新增：沒有改過的列，切換主題後會換成新主題的通道色。
- **風險**：輸入框的 `color.value` 必須是小寫 hex。CSS 回傳值必須 `.trim()`，並統一轉成小寫。
- **驗收**：SEQ、List、Viewer 的 CH1–4 在同一個主題下顏色相同；白底列印版匯出的 PNG 上黃色通道看得清楚。

**PR 建議**：P0、P1、P2、P4 各開一個 PR；P3 份量最大，可以拆成 P3a（Viewer 主題與 token）和 P3b（版面）。P4 依賴 P2，P3a 依賴 P0。最後一個 PR 更新 HANDOFF，在新的一節記錄：狀態、Windows WebView2 未驗項目（`zoom`、`prefers-color-scheme`、input color）。

## 7. 開放問題

1. **淺色主題下，圖表也要淺底嗎？** 預設是「跟著主題走」（淺色主題時圖表為白底，使用淺色組通道色）。如果希望圖表永遠像示波器一樣黑底，只要把淺色組的 `--plot-*` 和 `--ch*` 改成深色值即可，成本很低。
2. **第一次啟動要跟隨系統主題，還是固定深色？** 預設是跟隨系統，而且只在第一次決定。
3. **CH5–8 的配色可以接受嗎？** 示波器只有 6 色，所以 CH7 `#f4845f`、CH8 `#a6d854` 是新挑的，CH6 `#b79cff` 借用示波器 FFT 的顏色。如果有既定的配色習慣，請直接指定。
