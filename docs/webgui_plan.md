# pywebview GUI　實作規劃與合約（交接用）

> 把 tkinter GUI（`gui.py`、`list_gui.py`）換成 pywebview。**功能 1:1 照搬，不改版面設計。**
> ⛔ 使用者對每個任務說「開工」才動程式碼。開工後每個任務只准改 §1「擁有者」欄是自己的檔案。
> 本文件 §2–§4 是任務之間的介面，**凍結**；要改先回報，由 Claude 統一修改。

更新：2026-10-21　狀態：W0 合約（本文件）

使用者 2026-10-21 定案：功能 1:1；不打包 WebView2（偵測不到就報錯退出）；Codex 實作、Opus 審查、Claude 驗收；使用者說 commit 才 commit；允許 Codex 並行。

## 0. 共同規則

1. **不改** `src/n6705c/core/`、`plan.py`、`listplan.py`、`synth.py`、`execute.py`、`e36441a.py`、`cli.py`。需要改先回報。
2. **`tests/test_gui.py` 到 W4 之前一律不動**，而且必須照舊全過。tk 版要一直能跑。
3. 除了 `i18n.py`，**新檔一律不准有 CJK 字元**（中文只放在 `STRINGS`）。每個任務在自己的測試檔加 CJK 掃描與 `tr("…")` key 掃描（照 `test_gui.py` 的 `test_list_sources_have_no_cjk_text`、`test_gui_translation_keys`）。
4. 硬體一律用 `InstrumentGroup.demo(frames=..., slots=DEMO_SLOTS)` 或 monkeypatch；CI 不開視窗。
5. 每個任務最後跑 `python3 -m pytest -q`（W1-c 另跑 `node --test tests/js`），貼結果；不准 commit。
6. 不加 §1 沒列的依賴。JS 不用框架、不用 CDN、不用 bundler。

### 0.1 並行方式

- 每個 Codex 任務一個 worktree：`git worktree add ../N6705C-SEQ-wt-<任務> HEAD`，在裡面做、不 commit。
- Claude 驗收後用 `git -C <wt> add -A && git -C <wt> diff --cached HEAD > <任務>.patch`，`git apply` 到主工作目錄（不 commit）。檔案互斥，所以同一波的 patch 可直接疊。
- **第 2 波的 worktree 要從第 1 波的成果開**，所以第 1 波驗收後需要使用者先 commit。
- Codex 指令：`codex exec --skip-git-repo-check -s workspace-write -C <wt> "<任務>" < /dev/null > out.txt 2>&1`（一定要 `< /dev/null`）。

## 1. 任務與檔案擁有權

| 波 | 任務 | 擁有（只准改這些） | 驗收 |
|---|---|---|---|
| 1 | **1a 殼與打包** | `src/n6705c_seq/webgui.py`（新）、`src/n6705c_seq/web/index.html`、`app.js`、`style.css`（新），以及 `seq.js`、`list.js`、`seq.css`、`list.css`（新，**只放一行註解的空殼**）、`i18n.py`、`pyproject.toml`、`packaging/*`、`tests/test_webgui.py`（新） | §5.1 |
| 1 | **1b 上電時序資料層** | `src/n6705c_seq/form.py`（新）、`gui.py`（只改成呼叫 `form`）、`tests/test_form.py`（新） | §5.2 |
| 1 | **1c List 資料層＋排版** | `src/n6705c_seq/list_form.py`（新）、`list_gui.py`（只改成呼叫 `list_form`）、`src/n6705c_seq/web/layout.js`（新）、`tests/test_list_form.py`（新）、`tests/js/layout.test.js`（新）、`.github/workflows/ci.yml`（加 node 測試步驟） | §5.3 |
| 2 | **2a 上電時序分頁** | `src/n6705c_seq/web_seq.py`（新）、`web/seq.js`、`web/seq.css`、`tests/test_web_seq.py`（新）；`i18n.py` 只准在 `"open_csv"` 那行**正下方**插入 `web_seq_*` key | §5.4 |
| 2 | **2b List 分頁** | `src/n6705c_seq/web_list.py`（新）、`web/list.js`、`web/list.css`、`tests/test_web_list.py`（新）；`i18n.py` 只准在 `"open_json"` 那行**正下方**插入 `web_list_*` key | §5.5 |
| — | **整合**（Claude） | `webgui.py` 的 `class Api` 組合一行、跨分頁冒煙 | §5.6 |
| 3 | **W4 切換**（另開工） | 入口、spec、刪 tk／sv-ttk、測試移植、README、HANDOFF、`N6705C-SEQ.command` | §5.7 |

依賴方向（只能往右呼叫）：

```
web/*.js ──bridge──▶ webgui.Api(SeqApi, ListApi, _Core)
                          │  web_seq.SeqApi ──▶ form ──▶ plan, i18n
                          │  web_list.ListApi ─▶ list_form ──▶ listplan, synth, i18n
                          └──────────────────▶ execute, e36441a, n6705c.core
gui.py ──▶ form        list_gui.py ──▶ list_form      （tk 版過渡期共用同一套驗證）
```

`form.py`、`list_form.py` **不准 import tkinter 或 webview**。`web_seq.py`、`web_list.py` 不 import `webgui`（mixin 只用 `self._…`，見 §3.2）。

## 2. 資料層合約（第 1 波）

### 2.1 `form.py`（1b）

從 `gui.py` 搬出、行為不變。`gui.py` 保留同名名稱（`waveform`、`_tick_step`、`CONFIG`、`_load_config`、`FRAMES`、`PALETTE`、`DEFAULTS`、`COLUMNS`、`TITLES`、`NUMERIC`），測試照舊 monkeypatch `gui.CONFIG`。

```python
FRAMES = 2
CONFIG: Path = Path.home() / ".n6705c_seq_gui.json"
PALETTE, DEFAULTS, COLUMNS, TITLES, NUMERIC      # 原樣從 gui.py 搬

RAW_KEYS = ("enabled", "name", "delay", "low", "high", "rise", "hold", "fall", "limit")
# raw row：{"enabled": bool, 其餘: str}；數值是畫面單位字串（ms、V、A），limit 空字串＝不限流

def waveform(row: SeqRow, t_end: float, hold_dc: bool = False) -> list[tuple[float, float]]
def tick_step(span_ms: float) -> int                 # gui._tick_step = form.tick_step
def load_config(path: Path) -> dict                  # {"resources": list[str], "demo": bool, "lang": "zh"|"en"}；壞檔回預設
def save_config(path: Path, resources: list[str], demo: bool, lang: str) -> None
def default_raw(count: int) -> list[dict]            # 對應 gui._table() 的初值
def parse_rows(raw: list[dict]) -> list[SeqRow]
def rows_to_raw(rows: list[SeqRow], current: list[dict]) -> list[dict]
def seq_view(rows: list[SeqRow], hold_dc: bool, measured: dict) -> dict   # §2.3
```

- `parse_rows`：規則＝現在的 `App.read_rows`（停用列容錯、`invalid_nonnegative`／`invalid_limit` 訊息、ms→s）。**另外**先檢查結構：`raw` 必須是 list、每項是 dict、key 恰為 `RAW_KEYS`、`enabled` 是 bool、其餘是 str；不符丟 `TypeError`（英文訊息，這是程式錯誤不是使用者錯誤）。
- `rows_to_raw`：規則＝現在的 `App.fill`（`len(rows) > len(current)` 丟 `csv_capacity`；時間與電壓 `.10g`，**電流限制用 `:g`**（`gui.py:257`）；多出的列只把 `enabled` 設 False，其他欄保留 `current` 的值）。回傳新 list，不改 `current`。
- `gui.App.read_rows`／`fill` 改成「把 tk 變數組成 raw → 呼叫 `form`」，行為不變。

### 2.2 `list_form.py`（1c）

```python
SHAPE_NAMES = ("Step", "Ramp", "Trapezoid", "Sine", "Triangle", "Staircase", "Exponential")
def format_number(value) -> str                       # = list_gui._format_number
def number(text, title, *, minimum=None, positive=False, integer=False)   # = ListTab._number（保留 staticmethod 包裝）
def meta() -> dict    # {"shapes": [[key, name], ...], "fields": {shape: [{"key", "unit", "default"}]}}；unit/default 規則＝list_gui._parameter_form
def read_segment(shape: str, params: dict[str, str], repeat: str) -> Segment          # = ListTab.read_segment
def parse_settings(raw: dict) -> dict                 # 驗證 = ListTab._read_settings 的前半
def apply_settings(plan: ListPlan, settings: dict) -> ListPlan                          # 回傳深拷貝，不改入參
def plan_to_dict(plan: ListPlan) -> dict              # 與 write_json 同結構（不含 version）
def plan_from_dict(raw: dict) -> ListPlan             # 驗證規則＝listplan.load_json（用暫存檔或抽共用都可，但不准改 listplan.py）
def draft_view(shape: str, params: dict[str, str], repeat: str) -> dict                 # §2.4
def compiled_view(plan: ListPlan, channel: int, segment_index: int | None, measured: dict) -> dict  # §2.5
```

- `settings` raw：`{"channel": int, "enabled": bool, "label": str, "delay": str, "limit": str, "repeat": str, "hold_last": bool, "dwell_mode": "auto"|"manual", "dwell": str}`。型別不符丟 `TypeError`。
- `apply_settings`：找到 `channel` 就更新該通道（enabled、label、delay、current_limit）與 plan（repeat、hold_last、dwell）；找不到就新增 `ChannelList(channel, ...)`，但若結果等於 `ChannelList(channel, enabled=False)` 則不新增。
  已知與 tk 版的差別：tk 切到新通道時會先塞一個停用的空通道進 plan，web 版不塞，所以存出的 JSON 少了這些空項目（讀回結果相同）。
- `list_gui.py` 只把 `_number`、`read_segment`、`_read_settings` 的驗證、`_format_number`、`SHAPE_NAMES` 改成呼叫 `list_form`；**繪圖不動**。

### 2.3 `seq_view` 回傳（給 `seq.js` 畫圖）

```text
{"message": str,                 # 驗證錯誤或 order_warnings（同 App.draw：前 3 條加 ⚠，其餘用 more_warnings）
 "t_end": float, "ticks": [float],               # ms；ticks 用 tick_step，最後一格由 JS 另畫 t_end
 "time_axis": str, "measured_label": str,
 "lanes": [{"channel": int, "name": str, "low": float, "high": float,
            "points": [[ms, V]],                 # waveform() 轉 ms
            "delay": float, "top0": float, "top1": float,   # ms，同 App.draw 的 t_top0/t_top1
            "delay_text": str, "hold_text": str|null, "levels_text": str,
            "measured": [[ms, V]] | null}]}      # 抽點後 ≤ 2000 點、只留 ≤ t_end
```
停用通道不出現在 `lanes`。驗證失敗時 `lanes` 為空、`message` 是錯誤文字。顏色、細通道隱藏標註是 JS 的事。

### 2.4 `draft_view` 回傳（表單即時預覽，未 commit 的段落）

```text
{"error": str|null, "segment": {"shape", "params": {k: float}, "repeat": int} | null,
 "preview": {"samples": [200 floats], "duration": float, "lo": float, "hi": float, "periodic": bool,
             "levels": [{"text": str, "value": float}],      # 週期波 1 條，其他 2 條
             "combined": str,                                # 兩條太近時合併用的文字
             "times": [{"text": str, "seconds": float}],     # 依序排列的時間區間
             "amplitude": {"text": str, "base": float, "peak": float} | null,
             "extras": str} | null}
```
文字全由 Python 依目前語言格式化（規則＝`ListTab._draw_curve`）。切語言時 JS 重新呼叫即可。

### 2.5 `compiled_view` 回傳

```text
{"ok": bool,                     # 可以執行：編譯成功且沒有 errors
 "message": str,                 # "✘ …" + "⚠ …" 或 apply/編譯的 ValueError 文字
 "statistics": str,              # segment_statistics（有選段落時）+ statistics
 "plan": dict | null,            # apply_settings 之後的 plan（plan_to_dict）；失敗為 null
 "n_points": int, "repeat_text": "× N", "axis": [5 str],
 "budget": {"n_points", "delay", "pad"} | null,
 "lanes": [{"channel", "label", "selected": bool,
            "lo": float, "hi": float,            # 縱軸範圍：預期值＋「全部」實測值（list_gui.py:523–526），裁切前算
            "steps": [[k, V]],                   # 只在電壓改變處取點，加首尾
            "measured": [[k_float, V]] | null}], # k_float = j*dt/dwell，只留一個週期內
 "segments": [{"title": str, "samples": [200 floats]}]}   # 目前通道的段落縮圖；編譯失敗也要有
```

### 2.6 `web/layout.js`（1c）

純函式，不碰 DOM／canvas，座標是 CSS px，文字寬度由呼叫端量好傳入：

```js
// spans: [{start, end, width}]；回傳 [{x, row}]，x 是標籤中心
function placeTimeLabels(spans, left, width) { ... }
```
規則＝`list_gui._draw_curve` 第 429–447 行的換行邏輯，但**有上限**：最多 `spans.length` 列；比可用寬度還寬的標籤靠左 `left + 4` 放、允許超出（不再無限迴圈）。
檔尾用 `if (typeof module !== "undefined") module.exports = { placeTimeLabels };`，讓 node 可以 `require`。

## 3. Bridge 合約

### 3.1 一般規則

- 所有 JS 檔都是**一般 `<script>`，不用 ES module**（Windows 的 `.js` MIME 可能被登錄檔設成 `text/plain`，module script 會被擋）。
- `webview.start(..., http_server=True)`；頁面從 `Path(__file__).parent / "web" / "index.html"` 載入。凍結版的 datas 目的地是 `n6705c_seq/web`。
- JS 等 `pywebviewready` 事件後才呼叫 `window.pywebview.api`。
- Bridge 方法**不因使用者錯誤丟例外**：回 `{"error": str}`；取消回 `{"cancelled": true}`。只有 payload 型別錯（程式錯誤）才丟 `TypeError`，JS 在 `app.js` 的統一處理器顯示。
- 預覽類方法第一個參數是 `rev: int`，原樣放回回應的 `"rev"`。JS 每個面板**各自**一個計數器（seq 預覽、list 編譯、list 表單預覽），收到 `rev` 小於最新值的回應就丟掉。輸入改變（包括改成無效值）就加一。
- 方法名稱：共用方法不加前綴；上電時序 `seq_`；List `list_`。pywebview 只有一個平的命名空間，不准重名。
- 所有非 bridge 的屬性與方法都要底線開頭（pywebview 會把公開屬性遞迴暴露給 JS）。
- 使用者或儀器來的文字一律用 `textContent`，不准 `innerHTML`。

### 3.2 `webgui._Core`（1a）

1a 與 1b 同時從 HEAD 開工，1a 看不到 `form.py`：1a **暫用** `gui.CONFIG`／`gui._load_config()`（測試 monkeypatch `gui.CONFIG`），存檔自己寫一行同格式的 `json.dumps`。整合時由 Claude 改成 `form.load_config`／`form.save_config`。

```python
class _Core:
    def __init__(self, config_path: Path | None = None): ...   # None＝gui.CONFIG（整合後改 form.CONFIG）
    # 私有狀態
    _window, _lock, _queue: queue.Queue, _busy: bool, _closing: bool, _kind: str|None,
    _config: dict, _config_path: Path, _measured: {"seq": {}, "list": {}}
    # 私有工具（給 mixin 用）
    def _reserve(self) -> bool            # 鎖內 idle→busy；成功時 _emit(["busy", True])
    def _release(self) -> None            # 鎖內 busy→idle；_emit(["busy", False])
    def _emit(self, event: list) -> None  # 放進 _queue
    def _confirm(self, title: str, text: str) -> bool           # window.create_confirmation_dialog
    def _open_path(self, filetypes: tuple[str, ...]) -> str|None
    def _save_path(self, filetypes: tuple[str, ...], ext: str) -> str|None   # 沒副檔名就補 ext
    def _save_config(self, resources: list[str], demo: bool) -> None
    def _on_closing(self) -> bool         # 鎖內：busy→emit busy_title/busy_body 結果並回 False；否則 _closing=True 回 True
    # bridge
    def config(self) -> dict              # {"resources", "demo", "lang"}
    def strings(self) -> dict             # {"lang": str, "strings": {key: [zh, en]}}（JS 只用來翻固定文字）
    def set_lang(self, lang: str) -> None # i18n.set_lang + 存設定
    def poll(self) -> list[list]          # 取光 _queue
    def selftest_report(self, ok: bool, detail: str) -> None    # 只有 --selftest 時有作用
```

- 檔案對話框結果統一：`None`／空 → `None`；`str` → 本身；序列 → 第一個（macOS 存檔回 `str`、開檔回 tuple）。`filetypes` 用 pywebview 格式 `("CSV (*.csv)", "All files (*.*)")`。
- 鎖只保護狀態轉換，**不包** VISA I/O、對話框、編譯。
- 執行流程：驗證 → 凍結快照 → `_reserve()`（失敗回 `{"error": tr("busy_body")}`）→ `_confirm`（取消就 `_release()`，回 `{"cancelled": true}`）→ 存設定 → 開 daemon thread → worker 在 `finally` 裡**關完儀器後**才 `_release()`。
  確認視窗只出現在現在就有的地方（1:1）：`seq_run`、`list_run`、實機的 `all_off`。`test_connection` 沒有確認視窗（`gui.py:364–372`）。`all_off` 在 Demo 或沒填位址時直接回 `{"info": tr("off_demo")}`，不 reserve（`gui.py:432–435`）。
- `_kind`：`test_connection` 偵測完設定，並 `_emit(["kind", kind])`。執行時 worker **照舊重新偵測**，不信 `_kind`。

### 3.3 Poll 事件（`poll()` 回傳的 list 元素）

| 事件 | 發送者 | JS 處理 |
|---|---|---|
| `["busy", bool]` | `_Core` | `app.js`：`App.busy`，所有執行／全部關閉按鈕跟著停用 |
| `["kind", "N6705C"\|"E36441A"]` | 2a | `app.js` 停用 List 分頁並切回上電時序；`seq.js` 停用擷取並取消勾選 |
| `["status", frame_index, text]` | 2a | `seq.js` 更新連線列狀態 |
| `["result", "info"\|"error"\|"warning", title, text]` | 任何人 | `app.js` 以 `<dialog>` 顯示 |
| `["measured", "seq"\|"list"]` | 2a／2b | 對應分頁重新要預覽 |

### 3.4 上電時序 bridge（2a，`web_seq.SeqApi`）

```python
def seq_meta(self) -> dict          # {"frames", "channels_per_frame", "palette", "columns": [[key, title_key]], "rows": default_raw(...)}
def seq_preview(self, rev, rows: list[dict], hold_dc: bool) -> dict    # {"rev", **form.seq_view(...)}
def seq_open_csv(self, current: list[dict]) -> dict                     # {"rows"} | {"error"} | {"cancelled"}
def seq_save_csv(self, rows: list[dict]) -> dict                        # {} | {"error"} | {"cancelled"}
def seq_run(self, rows, resources: list[str], demo: bool, hold_dc: bool, capture: bool) -> dict
def test_connection(self, resources: list[str], demo: bool) -> dict
def all_off(self, resources: list[str], demo: bool) -> dict
```
Worker 內容與訊息＝現在的 `App._test`／`_work`／`_off_work`（E36441A 分流、混用拒絕、`OutputsNotConfirmedOff`／`PlanRejected` 註記），只把 `queue.put` 換成 `_emit`。擷取結果存 `_measured["seq"]`，然後 `_emit(["measured", "seq"])`。

### 3.5 List bridge（2b，`web_list.ListApi`）

```python
def list_meta(self) -> dict                                           # list_form.meta()
def list_draft(self, rev, shape, params: dict, repeat: str) -> dict   # {"rev", **draft_view(...)}
def list_compile(self, rev, plan: dict, settings: dict, segment_index) -> dict   # {"rev", **compiled_view(...)}
def list_open_json(self) -> dict                                      # {"plan", "settings"} | {"error"} | {"cancelled"}
def list_save_json(self, plan: dict, settings: dict) -> dict
def list_run(self, plan: dict, settings: dict, resources, demo, capture) -> dict
```
- **List 計畫的狀態放在 JS**（已加入的段落用數字存）；草稿欄位是字串，按「新增／更新」時才呼叫 `list_draft` 拿 `segment` 放進計畫。段落的新增／刪除／移動是 `list.js` 裡的陣列操作。
- 切換通道：JS 先呼叫 `list_compile`；有 `error` 就擋下並把下拉選單還原（＝現在的 `select_channel`），沒有就採用回傳的 `plan`。
- `list_run` 自己 `apply_settings`＋`compile_plan`，**不信**畫面上的預覽；開檔清除 `_measured["list"]`。
- Worker 內容＝現在的 `ListTab._work`。

### 3.6 `index.html`／`app.js` 骨架（1a）

- 連線列靜態標記：`#res-0`、`#res-1`、`#conn-0`、`#conn-1`、`#btn-test`、`#chk-demo`、`#btn-off`、`#btn-lang`。`app.js` 只接 `#btn-lang`，其他由 `seq.js` 接。
- 分頁：`<section id="seq-tab">`、`<section id="list-tab">` 是空的，由 `seq.js`／`list.js` 自己建 DOM。載入順序：`app.js`、`layout.js`、`seq.js`、`list.js`。
- 有固定文字的元素用 `data-i18n="key"`，切語言時 `app.js` 更新它們。**切語言不重建 DOM**（輸入欄的內容要保留）。
- 全域物件：

```js
window.App = {
  api: null,                 // pywebviewready 之後 = window.pywebview.api
  lang: "zh", busy: false, kind: null,
  t(key) {},                 // 固定文字（目前語言）
  on(type, fn) {},           // 訂閱 poll 事件，或 "ready"、"lang"
  register(tab) {},          // tab = {init(), relabel()}；ready 後呼叫 init
  showResult(kind, title, text) {},   // <dialog>
  relabel(root) {},          // 翻譯 root 內的 data-i18n
};
```
- 共用 CSS class 由 1a 在 `style.css` 定義：`.btn`、`.btn-accent`、`.btn-danger`、`.field`、`.row`、`.chart`、`.muted`、`.warn`。分頁專屬樣式放 `seq.css`／`list.css`。

## 4. 平台與打包（1a）

- 依賴：`pywebview==6.2.1`（Windows 會帶 `pythonnet`，macOS 會帶 `pyobjc`）。新的 gui-script `n6705c-seq-web = "n6705c_seq.webgui:main"`。`[tool.setuptools.package-data]` 收 `n6705c_seq = ["web/*"]`。
- 視窗：預設 1180×900，不超過螢幕；標題 `tr("title")`；`events.closing += _on_closing`；`confirm_close=False`。
- **Windows 引擎檢查**：`webview.start(func, gui="edgechromium", http_server=True)`；在 `func` 內檢查 `webview.renderer != "edgechromium"` 時，用 `ctypes.windll.user32.MessageBoxW` 顯示新 key `webview2_missing`（含 Microsoft WebView2 下載網址，註明離線環境要用 Standalone Installer），然後關閉退出。macOS 不檢查。
- `--selftest`：JS 在 ready 後呼叫 `strings()`、`config()`，再呼叫 `selftest_report(true, "")`；Python 收到後關窗、結束碼 0；30 秒沒收到或收到 `false` 結束碼 1。
- PyInstaller：spec 加第三個 exe `N6705C-SEQ-web.exe`（windowed），datas 加 `web/*` → `n6705c_seq/web`；webview／clr 用 pyinstaller-hooks-contrib 現成的 hook。`build.ps1` 在 CLI 自測後加：`$p = Start-Process <web exe> -ArgumentList --selftest -Wait -PassThru`，`$p.ExitCode -ne 0` 就 throw（windowed exe 用 `&` 不會等）。若 CI 的 Windows runner 開不了視窗，**回報，不要繞過**。

## 5. 驗收

### 5.1 1a
- `n6705c-seq-web` 在 macOS 開得起來：兩個空分頁、連線列、語言切換有效、設定檔有存。
- `tests/test_webgui.py`（假 window）：`_reserve` 並發只有一個成功；busy 時 `_on_closing` 回 False 並發出結果事件；`_reserve` 成功後 `_on_closing` 回 False、`_closing` 後 `_reserve` 失敗；對話框結果三種形式都正規化；`strings()` 涵蓋全部 key；公開屬性只有 §3.2 的 bridge 方法。
- Windows：CI build 成功；**使用者在 Windows 11 實機手動**開 exe、跑 `--selftest`（Claude 在 macOS 做不了）。

### 5.2 1b
- `tests/test_gui.py` 不動且全過。`tests/test_form.py`：`parse_rows` 與舊 `read_rows` 規則一致（停用列容錯、限流空白、負數／NaN／inf 拒絕、ms 換算）、結構錯誤丟 `TypeError`、`rows_to_raw` 容量與精度、`seq_view` 的點數／抽點上限／停用通道排除／錯誤訊息。

### 5.3 1c
- `tests/test_gui.py` 不動且全過。`tests/test_list_form.py`：搬 `test_gui.py` 第 514–595 行標註案例的**數值與文字**期望到 `draft_view`；`apply_settings` 不改入參、新通道規則；`plan_from_dict`／`plan_to_dict` 往返與錯誤位置訊息；`compiled_view` 的 steps 只在改變處、`ok` 與 errors 一致。
- `node --test tests/js` 通過：不重疊；寬度放得下的標籤在範圍內（超寬標籤不受此限，只要求靠左 `left + 4`）；超寬標籤不卡死；列數有上限。CI 加 `actions/setup-node` 與這一步。

### 5.4 2a
- 移植 `test_gui.py` 第 83、696、728 行等 worker 案例成 `tests/test_web_seq.py`（假 window＋monkeypatch）：E36441A 分流、混用拒絕、全部關閉、確認取消會 release、worker 失敗也會 release、快照在確認後不受改變。
- 手動（macOS）：8 列可編輯、顏色、CSV 開存、預覽與警告、維持 DC、Demo 執行、全部關閉、切語言保留輸入、E36441A 鎖定（用 monkeypatch 過的 Demo）。

### 5.5 2b
- 移植第 194、231、262、310、347、413、448 行的行為到 `tests/test_web_list.py`。
- 手動（macOS）：7 種波形、新增／更新／刪除／移動、切通道擋無效值、JSON 開存、統計與預算條、預覽標註、Demo 執行。

### 5.6 整合
`class Api(SeqApi, ListApi, _Core)`；`_Core` 設定檔改用 `form`；兩分頁共用 busy、`kind` 鎖 List、跨分頁執行時另一分頁按鈕停用；`--selftest` 通過；全部 pytest 與 node 測試通過。

### 5.7 W4（另開工）
`n6705c-seq-gui` 指向 web；刪 `gui.py`、`list_gui.py`、`sv-ttk`；`test_gui.py` 裡已被移植的刪掉、只剩 tk 的刪掉；`form`／`list_form` 拿掉相容包裝；更新 README、HANDOFF §3／§4.7、`N6705C-SEQ.command`、spec 改回兩個 exe。

## 6. 手動檢查清單（Windows 11 + macOS，W4 前）

DPI 150%、視窗縮小時按鈕不被切掉、中英文字型、檔案對話框取消、執行中關窗被擋（含 Cmd+Q）、沒有 WebView2 時的錯誤訊息、離線環境開得起來。
