# UI 主題改版：可並行工作拆解（WBS）

2026-11-20 規劃，**尚未開工**。來源：`docs/ui-theme-plan.md`（以下稱「計畫」）。使用者已定案計畫 §7 的三個預設值：
1. 淺色主題時圖表也用淺色（`--plot-*`、`--ch*` 跟著主題走）。
2. 第一次啟動跟隨系統主題，只決定一次。
3. CH5–8 的配色照計畫 §2。

本檔只重新切分工作，不改計畫的設計內容。兩份文件有出入時，設計以計畫為準，分工與檔案歸屬以本檔為準。開工、commit、PR 仍依 HANDOFF §9.3 和 §13.7：使用者說「開工 Wave N」才開始該波；使用者說 commit 才 commit。

## 0. 與計畫 P0–P4 的對照

| 本檔項目 | 對應計畫 | 說明 |
|---|---|---|
| **A0 契約** | P0 的契約部分＋P4 的 `PALETTE` | 介面名稱、token 值、訊息格式、按鈕 DOM，以及 `cssVar` 這個工具函式 |
| **B 主題機制** | P0 其餘部分 | 設定檔、`set_theme`、`data-theme`、送 theme 訊息 |
| **C 殼層樣式與版面** | P1＋P3b 的殼層部分 | `style.css` 和三個分頁 css；connbar；膠囊分頁 |
| **D Viewer** | P3a＋P3b 的 Viewer 部分＋P4 的 `mapColor` | `viewer.html` 只由 D 一項修改 |
| **E1 SEQ 畫布** | P2＋P4 的 `seq.js` 部分 | |
| **E2 List／Record 畫布** | P2＋P4 的 `list.js`、`record.js` 部分 | |
| **F 整合收尾** | 計畫 §6 最後一個 PR | HANDOFF、手動驗收、Windows 待驗清單 |

## 1. 依賴圖

```
                  ┌──────────────── A0 契約（Wave 0，凍結介面）────────────────┐
                  │                                                           │
     ┌────────────┼─────────────┬──────────────┬──────────────┐               │
     ▼            ▼             ▼              ▼              ▼               │
     B            C             D              E1             E2              │
  主題機制    殼層樣式版面   Viewer 主題版面  SEQ 畫布      List/Record 畫布   │
     │            │             │              │              │               │
     └────────────┴─────────────┴──────┬───────┴──────────────┘               │
                                       ▼                                      │
                                  F 整合收尾（Wave 2）◄────── 契約變更一律回到 A0
```

- Wave 1 的五項彼此之間**沒有程式依賴**，只依賴 A0 凍結的名稱與值。
- 執行期的串接（B 送出 theme 事件或訊息 → C、D、E 的畫面跟著變）只在整合時驗證。Wave 1 的各項各自用測試替身模擬事件或訊息。
- **關鍵路徑**：A0 → D → F。D 是份量最大的一項：`viewer.html` 有 3413 行，而且要刪除區塊又要保住測試標記。

## 2. A0 要凍結的介面契約

A0 合併之後，下表的內容在 Wave 1 期間**不准修改**。需要改時，處理方式見 §6。

| 介面 | 凍結內容 | 實作者 | 使用者 |
|---|---|---|---|
| CSS token 名稱與值 | 計畫 §2 全部 token，加上 `--ch1`…`--ch8`。淺色定義在 `:root`，深色定義在 `:root[data-theme="dark"]`。放在 `style.css` 最前面，用 `/* theme-tokens:begin */` 和 `/* theme-tokens:end */` 兩個標記圍住。色值一律小寫 `#rrggbb`，只有深色 `--border` 是 `rgba(...)` | A0 | C、D（內聯一份同樣的值）、E1、E2 |
| `data-theme` | 屬性放在 `<html>` 上，值只能是 `"light"` 或 `"dark"`。`pywebviewready` 之前沒有這個屬性，此時以 `:root`（淺色）為準 | B | C、E1、E2 |
| 殼層事件 | `App.on("theme", fn)`。在 `data-theme` 設好之後才觸發，參數是 `"light"` 或 `"dark"`。設定初始值時也會觸發一次，時間點在 `emit("ready")` 之前 | B | E1、E2（以及 `analysis.js`，由 B 負責） |
| JS 工具函式 | `layout.js` 的 `App.cssVar(name, fallback)`。實作是 `getComputedStyle(document.documentElement).getPropertyValue?.(name)?.trim() \|\| fallback`，不轉小寫，需要小寫時由呼叫端自己轉 | A0 | E1、E2 |
| 設定檔 | `~/.n6705c_seq_gui.json` 新增 `"theme"` 欄位，值為 `"light"`、`"dark"` 或 `null`。`load_config()` 讀到其他值一律當成 `None`。新的簽章是 `save_config(path, resources, demo, lang, theme)` | B（`form.py`） | B |
| JS API | `set_theme(theme)` 只接受 `"light"` 或 `"dark"`，其他值會被拒絕。`config()` 回傳的物件多一個 `theme` 欄位。`set_theme` 放進 `SELFTEST_JS_API_DENIED` | B（`webgui.py`） | B |
| `contracts.py` | `CONFIG_KEYS` 加上 `"theme"`（**移到 B 做**：必須與 `webgui.config()` 和 `test_webgui.py:146,267` 同步，否則 A0 全測試不綠）；新增 `THEMES = ("light", "dark")`；新增 `Msg.THEME = "theme"`，`MSG_FIELDS[Msg.THEME] = {"theme"}`，並加入 `MSG_TO_IFRAME`；`url(gen, lang, theme=None)` 的協定簽章與註解一併更新 | A0 | B、D |
| iframe 訊息 | `{v:1, type:"theme", gen, theme}`，信封格式和 `lang` 相同。ready 時送一次，之後每次 `App.on("theme")` 再送一次 | B（`analysis.js`） | D |
| Viewer URL | `viewer.html#t=…&gen=…&lang=…&theme=…`。`theme` 為 `None` 時不加這一段 | A0（`server.py`） | B、D |
| 按鈕 DOM | `index.html` 在 `btn-lang` 旁加上 `<button id="btn-theme" class="btn" data-i18n="theme_dark">`。之後 id 不准改 | A0 | B（事件）、C（擺位） |
| i18n | 新增 `theme_light` 和 `theme_dark` 兩個 key。之後 `i18n.py` 凍結，Wave 1 不准再加 key | A0 | B |
| 後備色盤 | `form.PALETTE` 改成計畫 §2 深色組的 8 個值 | A0 | E1（`meta.palette`） |

`PALETTE` 提前在 A0 改，是為了讓 `form.py` 在 Wave 1 只有 B 一個擁有者。代價是：A0 合併後到 E1 合併前，SEQ 的預設色會是深色組的值，淺色畫布上的黃色比較看不清楚。因為所有 PR 都先進整合分支（§5），`main` 不會出現這個中間狀態。

## 3. 工作項目

共同規則：
- 「寫入範圍」以外的檔案一律不准改。
- 「新增」的測試檔由該項目獨占。
- 每一項結束時都要在沙盒外跑：`PYTHONPATH=src /opt/anaconda3/bin/python -m pytest -q -p no:cacheprovider` 和 `node --test tests/js/*.test.js`。

### A0 契約（Wave 0）
- **目標**：把 §2 凍結的內容落實成程式碼，並加上測試守住。這一項**不換任何既有畫面的顏色**（`PALETTE` 例外，見 §2）。
- **寫入範圍**：
  - `src/n6705c_seq/analysis/contracts.py`、`docs/analysis-contract.md` §4
  - `src/n6705c_seq/analysis/server.py`（只改 `url`）
  - `src/n6705c_seq/web/style.css`（只在檔案最前面加 token 區塊，既有規則一行都不動）
  - `src/n6705c_seq/web/layout.js`（`cssVar`）
  - `src/n6705c_seq/web/index.html`（只加 `btn-theme` 這一行）
  - `src/n6705c_seq/i18n.py`（兩個 key）
  - `src/n6705c_seq/form.py`（只改第 18 行 `PALETTE`）
- **測試**：
  - `tests/analysis/test_http.py:206`：URL 斷言加上 `&theme=`，並驗證 `None` 時不加。
  - `tests/js/layout.test.js`：`cssVar` 有值、沒有值、沒有 `getPropertyValue` 三種情況。
  - **新增** `tests/test_theme_contract.py`：
    - token 區塊存在，淺色和深色兩組的 token 名稱集合相同；
    - 色值都是小寫；
    - `form.PALETTE` 等於深色組的 `--ch1..8`；
    - `Msg.THEME` 有登記在 `MSG_FIELDS` 和 `MSG_TO_IFRAME`。
  - `test_web_seq.py:105` 會自動通過。
- **唯讀依賴**：計畫 §2、§3.6。
- **前置**：使用者說「開工 Wave 0」。**可並行對象**：無（只有一項）。**大小**：S。
- **驗收**：全部測試通過；`git diff` 只碰到上列檔案和上列段落；用 `grep` 確認 `style.css` 在 token 區塊以外沒有任何變動。
- **分支／PR**：`feat/theme-a0-contract`，PR 目標是 `feat/ui-theme`。

### B 主題機制（Wave 1）
- **目標**：計畫 §3.1–3.4。設定檔讀寫 theme；第一次啟動用 `matchMedia` 決定一次；`btn-theme` 切換主題；設定 `data-theme`；`emit("theme")`；`analysis.js` 把 theme 訊息送進 iframe；Viewer 的 URL 帶上 theme。
- **寫入範圍**：`form.py`（`load_config` 和 `save_config`，不碰 `PALETTE`）、`webgui.py`、`web/app.js`、`web/analysis.js`、`analysis/contracts.py`（僅 `CONFIG_KEYS` 加 `"theme"` 與 `SELFTEST_JS_API_DENIED` 加 `"set_theme"`）、`tests/test_webgui.py` 的 config 斷言。
- **測試**：
  - `tests/test_form.py`：theme 讀寫，包含舊設定檔沒有 theme、theme 為非法值兩種情況。
  - `tests/test_webgui.py`：config 和設定檔的 JSON 斷言都要多一個 `"theme"`。必須新增一個測試：**先設定 theme，再切換語言，確認 theme 沒有被清掉**（計畫 §6 P0 的風險）。
  - `tests/test_selftest_api_classes.py`：`set_theme` 要被分類。
  - `tests/test_analysis_integration.py:76`：多解析 theme。
  - `tests/js/analysis.test.js`：ready 後送出 theme；`App.on("theme")` 時再送一次。B 是這個檔案在 Wave 1 **唯一的擁有者**。
- **唯讀依賴**：`contracts.py`、`server.py`、`index.html`、`i18n.py`。
- **前置**：A0。**可並行對象**：C、D、E1、E2。**大小**：M。
- **驗收**：按下主題鈕後，`<html data-theme>` 改變、設定檔寫入、重開程式後仍是同一個主題；用 `shell_stub` 以外的方式確認 iframe 收到 theme 訊息。
- **分支**：`feat/theme-b-mechanism`。

### C 殼層樣式與版面（Wave 1）
- **目標**：
  - 計畫 P1：四個 CSS 檔裡寫死的顏色全部改成 `var(--…)`。
  - 計畫 P3b 的殼層部分：`fieldset.connection` 改成單列 `.connbar`；`.tabs` 和 `#analysis-modes` 改成膠囊樣式；`.analysis-frame` 的框線改用 token。
- **寫入範圍**：
  - `web/style.css`：token 區塊**以外**的所有規則。token 區塊屬於 A0，C 不准改。
  - `web/seq.css`、`web/list.css`、`web/record.css`
  - `web/index.html`：連線區的 markup 和 `#analysis-modes` 的 class。所有元素 id 都不准改，包括 `btn-theme`。
- **測試**：
  - **新增** `tests/test_css_tokens.py`：這四個 CSS 檔在 token 區塊以外，不准出現任何 `#[0-9a-f]{3,6}`。
  - 既有的 CJK 檢查必須繼續通過（`test_web_seq.py:532-535`、`record.test.js:316-317`），C 不改這兩個測試檔；CSS 的註解一律用英文。
  - `analysis.test.js:257` 檢查 `href="record.css"`，所以 `record.css` 不准改名。
- **唯讀依賴**：token 區塊、`app.js`（tab 和 `aria-selected` 的寫法）、`layout.js` 的 `fitZoom`。
- **前置**：A0。**可並行對象**：B、D、E1、E2。**大小**：M。
- **驗收**：
  - 在 macOS 上分別把 `<html>` 手動設成 `data-theme="light"` 和 `"dark"`（B 還沒合併時用 devtools 設），SEQ、List、Record 三頁都沒有殘留的舊色。
  - 視窗最小時，connbar 換行不會撐出捲軸，`fitZoom` 量到的高度正常。
- **分支**：`feat/theme-c-shell-css`。

### D Viewer 主題與版面（Wave 1，關鍵路徑）
- **目標**：計畫 P3a、P3b 的 Viewer 部分，以及 P4 的 Viewer 部分。
  - 把 token 內聯進 `viewer.html`。
  - `CHART_THEMES` 只留 `dark` 和 `light`，值改用 `--plot-*` 和 CH1–4。
  - 刪除 `soft-dark` 主題、`themeSelect` 下拉和三條 i18n 字串。
  - 處理 `theme` 訊息和 URL hash；單獨開啟 Viewer 時改用 `matchMedia`。
  - 忽略 `/prefs` 裡存的 theme。
  - `.app` 改成滿版 grid。
  - 白底列印版的 `mapColor` 改成把深色組映射到淺色組。
- **寫入範圍**：`src/n6705c_seq/analysis/web/viewer.html`（**全專案只有 D 能改**）、`src/n6705c_seq/analysis/web/shell_stub.html`（加一顆送 theme 的按鈕，供手動測試）。
- **測試**：
  - **新增** `tests/js/viewer_theme.test.js`：收到 `{type:"theme"}` 會呼叫 `applyTheme`；非法值會被忽略；hash 帶 theme 時會套用。這個檔案可以複製 `analysis.test.js:559-565` 的切片作法，但**不准修改** `analysis.test.js`，也不准動 `viewer.html` 裡的標記字串。刪除區塊前，先 `grep` 確認沒有刪到標記。
  - **新增** `tests/analysis/test_viewer_tokens.py`：`viewer.html` 和 `style.css` token 區塊的 `--ch1..4`、`--plot-*`，在兩個主題下的值都必須相同。
  - `tests/analysis/test_http.py:524-527` 不改，舊 prefs 裡的 `soft-dark` 仍然合法。
- **唯讀依賴**：`contracts.py`、`style.css` 的 token 區塊、`analysis.js` 送訊息的格式。
- **前置**：A0。**可並行對象**：B、C、E1、E2。**大小**：L。
- **驗收**：
  - 用 hash 和 `shell_stub` 都能切換主題；
  - 深色時圖表是黑底，格線 `#3a3a3a`；
  - 視窗寬 1920 時沒有左右留白；
  - 滿版後 LOD 重新取樣的速度沒有明顯變慢；
  - 白底列印版匯出的 PNG 上，黃色通道看得清楚。
- **分支**：`feat/theme-d-viewer`。

### E1 SEQ 畫布與通道色（Wave 1）
- **目標**：
  - 計畫 P2、P4 的 SEQ 部分：`seq.js` 畫圖時改用 `App.cssVar` 取色；訂閱 `App.on("theme")` 並重畫，分頁隱藏時要在切回來時重畫。
  - 預設通道色改成 `cssVar("--ch"+n) || meta.palette[i]`，轉成小寫。
  - 使用者改過顏色的列設 `dataset.custom=1`。切換主題時只更新沒改過的列。
- **寫入範圍**：`web/seq.js`。
- **測試**：`tests/test_web_seq.py`，E1 是這個檔案在 Wave 1 唯一的擁有者。要新增三個斷言：
  - 測試替身環境下，畫圖不會丟例外，而且用的是 fallback 色；
  - 使用者改過的顏色，切換主題後不會被覆蓋；
  - 沒有改過的列，切換主題後會換成新主題的通道色。
- **唯讀依賴**：`layout.js`、`style.css` 的 token 區塊、`form.PALETTE`。
- **前置**：A0。**可並行對象**：B、C、D、E2。**大小**：M。
- **驗收**：在 devtools 改 `data-theme` 並 `emit("theme")` 後，畫布底色、格線、通道色立刻跟著改變。
- **分支**：`feat/theme-e1-seq`。

### E2 List／Record 畫布與通道色（Wave 1）
- **目標**：計畫 P2、P4 的 List 和 Record 部分。
  - `list.js`：lane 的曲線色從寫死的 `#2563eb` 改成 `--ch{lane.channel}`；budget 條三色改用 token；畫布顏色改用 token。
  - `record.js`：V 和 I 兩條曲線改用 `--ch{n}`，電流用虛線區分。
  - 兩個檔案都要訂閱 `theme` 事件並重畫。
- **寫入範圍**：`web/list.js`、`web/record.js`。
- **測試**：
  - `tests/test_web_list.py`：`list.js` 不准出現 CJK（`:330-335`）。另外新增「切換主題會重畫」的測試。
  - `tests/js/record.test.js`：新增主題相關測試。這兩個測試檔在 Wave 1 都只有 E2 會改。
- **唯讀依賴**：同 E1，另加 `list_form.py`（lane 的 `channel` 欄位）。
- **前置**：A0。**可並行對象**：B、C、D、E1。**大小**：M。
- **驗收**：同 E1，範圍是 List 和 Record 兩個分頁。
- **分支**：`feat/theme-e2-list-record`。

### F 整合收尾（Wave 2）
- **目標**：
  - 在 `feat/ui-theme` 上跑全部測試，再到 macOS 實際操作驗收（見 §6）。
  - 在 HANDOFF 新增一節，記錄狀態和 Windows WebView2 的未驗項目：`zoom`、`prefers-color-scheme`、`<input type=color>`、connbar 換行後的 `fitZoom`。
  - 開一個從 `feat/ui-theme` 到 `main` 的 PR。
- **寫入範圍**：`HANDOFF.md`、`README.md`（只在需要時改截圖說明）、`docs/ui-theme-plan.md`（把狀態改成已完成）。
- **前置**：B、C、D、E1、E2 都已合併進 `feat/ui-theme`。**大小**：S。

## 4. 並行波次

| 波次 | 開始條件 | 項目 | 同時進行數 |
|---|---|---|---|
| Wave 0 | 使用者說「開工 Wave 0」 | A0 | 1 |
| Wave 1 | A0 合併進 `feat/ui-theme`，且使用者說「開工 Wave 1」 | B、C、D、E1、E2 | **5（最大並行數）** |
| Wave 2 | Wave 1 全部合併 | F | 1 |

- **關鍵路徑**：A0（S）→ D（L）→ F（S）。B、C、E1、E2 都是 M，會在 D 之前完成，不影響總時程。
- 如果 D 拖得太久，可以把它拆成 D1（主題與 token：`CHART_THEMES`、訊息、hash、`mapColor`）和 D2（滿版 grid、移除 topbar 的下拉）。但兩者都要改 `viewer.html`，所以只能**依序**做，不能並行。拆開只是讓審查變小。

### Wave 1 寫入範圍檢查（兩兩不重疊）

| 檔案 | 擁有者 |
|---|---|
| `form.py` | B（第 18 行 `PALETTE` 已在 A0 改完，B 不碰） |
| `webgui.py`、`app.js`、`analysis.js`、`analysis.test.js`、`test_form.py`、`test_webgui.py`、`test_selftest_api_classes.py`、`test_analysis_integration.py` | B |
| `style.css`（token 區塊以外）、`seq.css`、`list.css`、`record.css`、`index.html`、`test_css_tokens.py`（新增） | C |
| `viewer.html`、`shell_stub.html`、`viewer_theme.test.js`（新增）、`test_viewer_tokens.py`（新增） | D |
| `seq.js`、`test_web_seq.py` | E1 |
| `list.js`、`record.js`、`test_web_list.py`、`record.test.js` | E2 |
| `contracts.py`、`server.py`、`layout.js`、`i18n.py`、`style.css` 的 token 區塊、`analysis-contract.md`、`test_theme_contract.py`、`test_http.py`、`layout.test.js` | **Wave 1 期間凍結**（A0 擁有，只能讀） |

在計畫裡容易互相衝突的三個共用檔，處理方式如下：
- **`style.css`**：A0 只在檔案最前面加 token 區塊，C 擁有其餘規則。用兩個標記把檔案切成兩段，所以兩項不會改到同一行。
- **`index.html`**：`btn-theme` 這顆按鈕由 A0 加入，所以 B 完全不需要改 `index.html`，C 是 Wave 1 唯一的擁有者。
- **`viewer.html`**：只有 D 能改。計畫原本分散在 P3a、P3b、P4 的 Viewer 改動全部併進 D。

## 5. 派工建議

依照 HANDOFF §13.7 和 §14.5：
- 每一項開一個 worktree，從整合分支切出：
  `git worktree add ../N6705C-SEQ-wt-<ID> -b feat/theme-<id>-… feat/ui-theme`
  如果透過 Agent 派工，用 `isolation: "worktree"`。worktree 裡跑 pytest 一律加 `PYTHONPATH=src`。
- **整合分支 `feat/ui-theme`**：每一項的 PR 都以它為目標。等 F 完成後，再由它開一個 PR 到 `main`。這樣 `main` 不會出現主題做到一半的狀態。如果使用者希望照計畫 §6「每個階段直接 PR 到 main」，波次和檔案歸屬都不用改，只是 A0 到 E1 之間的中間狀態會短暫出現在 `main`。
- Codex 呼叫方式：
  `codex exec --skip-git-repo-check -s workspace-write -m gpt-6.1-sol -c model_reasoning_effort=medium "$(cat prompt.txt)" </dev/null > out.txt 2>&1`
  - `</dev/null` 一定要加。
  - prompt 要寫明：只准改「寫入範圍」裡的檔案；不准 commit；最後要跑全部測試。
  - Codex 沙盒不能 bind socket，所以 HTTP 測試在沙盒裡會失敗，一律到沙盒外重跑。

| 項目 | 實作 | 審查 | 理由 |
|---|---|---|---|
| A0 | Codex（sol medium） | **Opus 審** | 契約錯了，五項並行工作都要跟著重做 |
| B | Codex | **Opus 審** | 設定檔寫入有「切換語言會清掉 theme」的風險，`set_theme` 也牽涉安全分類 |
| C | Codex | Claude 驗收：跑測試、檢查 hex、看截圖 | 純 CSS 和 markup，有測試守著 |
| D | Codex | **Opus 審** | 3400 行的檔案、要刪除區塊、要保住標記、要確認 LOD 效能 |
| E1 | Codex | Claude 驗收：跑測試 | `dataset.custom` 的邏輯有測試守著 |
| E2 | Codex | Claude 驗收：跑測試 | 同上 |
| F | Claude（主 session） | 使用者手動驗收 | |

- **整合者**：主 session 的 Claude。負責：
  - 依序把各項 PR 合併進 `feat/ui-theme`，順序建議是 **C → B → E1 → E2 → D**：越小、越可能影響版面的越早合。
  - 每合一項就跑一次全部測試。
  - 如果真的出現衝突（照理不會），由整合者解，不退回給 Codex。
  - 合併後刪除 worktree 和分支。

## 6. 風險與整合點

**跨項目的測試要在什麼時候跑**
- **每一項的 PR**：全部 pytest，加上 `node --test tests/js/*.test.js`（要用 glob，目錄形式會失敗）。CJK 檢查和 parity 測試都包含在全部 pytest 裡，不需要另外跑。
- **每合併一項進 `feat/ui-theme` 之後**：整合者在沙盒外重跑全部測試。這一步的重點是 `test_theme_contract.py`、`test_viewer_tokens.py`、`test_css_tokens.py` 三個測試之間的交叉檢查。
- **parity**：`tests/analysis/parity_tools/original_before_python.html` 不准修改。D 合併後，要確認 parity 測試仍然通過。
- **F 的手動驗收（macOS）**：
  1. 兩種主題下，四個分頁都沒有殘留舊色。
  2. 切換主題後，Viewer 同步改變。
  3. 重開程式後，主題維持不變。
  4. 刪除設定檔後第一次啟動，主題跟隨系統。
  5. 先切換主題再切換語言，主題不會被清掉。
  6. SEQ、List、Viewer 的 CH1–4 在同一個主題下顏色相同。
  7. 最小視窗下不會出現捲軸，`fitZoom` 正常。

**契約變更的處理規則**
- Wave 1 期間，如果發現 §2 的任何一項需要修改（token 名稱或值、事件名稱、訊息欄位、設定檔欄位、按鈕 id、i18n key）：
  1. 正在做的那一項先停下來，**不准在自己的分支上改凍結檔**。
  2. 回報整合者，開一個小 PR `feat/theme-a0-amend-N`，只改契約，並經過 Opus 審。
  3. 這個 PR 合併後，所有進行中的分支都要 rebase 到新的 `feat/ui-theme`。
- 只新增 token 而不改既有 token 的，也走同樣的流程。

**其他風險**
- **`fitZoom`**：C 的 connbar 和膠囊分頁會改變高度，E2 的 List 畫布用的是寫死的像素高度。兩者的互動只能在整合後看，所以放在 F 驗收。
- **Windows WebView2**：這次全部無法驗證，由 F 列進 HANDOFF。
- **中間狀態**：A0 改了 `PALETTE`，但 E1 還沒換成新的取色方式時，SEQ 淺色畫布上的黃色比較看不清楚。因為改動都先留在整合分支，這個狀態不會進到 `main`。
