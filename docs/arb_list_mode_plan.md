# ARB List 模式　實作規劃（交接用）

> 設計決定見 `docs/arb_list_mode.md`（以下簡稱「設計文件」）。本文件只講**怎麼做、誰做哪塊、哪些可以並行**。
> ⛔ 使用者說「開工」之前不准動程式碼。開工後，每個 Track 只准改「檔案」欄列出的檔案。

更新：2026-10-13　狀態：規劃完成並定案，未開工

使用者 2026-10-13 定案：R1 右端取樣；`ARB:TERM:LAST` 一律開（勾選框只決定結束後關不關輸出）；§4.5 驗收門檻（傳輸 ≤ 10 s、對齊 ≤ 1 dwell）；Staircase／Exponential 欄位照 §2.1（Exponential 有 `end_time`）。

## 0. 給接手模型的共同規則

1. **不改 core**（`src/n6705c/core/`）。需要 core 改動（例如 CDW 改二進位傳輸）時，先回報，不要自己改。
2. 只改自己 Track 列出的檔案；需要動到別的 Track 的檔案時，先回報。
3. §2 的合約（資料結構、函式簽名）是 Track 之間的介面，**不准自行改簽名**；要改先回報，由整合者統一修改。
4. 測試用 pytest，硬體一律用 `InstrumentGroup.demo(frames=..., slots=DEMO_SLOTS)`（見 `tests/test_execute.py`）。純邏輯每個模組一個 `tests/test_*.py`，不要做 fixture 框架。
5. 實機只有一組（Windows + USB，見 `HANDOFF_WINDOWS.md`），Track H 需要有人在實機旁。

## 1. 檔案邊界

| 檔案 | 新／改 | 職責 | 不負責 |
|---|---|---|---|
| `src/n6705c_seq/listplan.py` | 新 | 資料結構、各波形的欄位表、JSON 讀寫、載入時的欄位檢查 | 任何數學、任何儀器 |
| `src/n6705c_seq/synth.py` | 新 | 段落 → 點陣列、自動 dwell、補齊到共同週期、警告／擋下、轉成 `ChannelFunction` | 儀器 I/O、GUI |
| `src/n6705c_seq/execute.py` | 改 | 把 `run_plan` 的執行流程抽成 `run_functions`，CSV 與 List 共用 | 波形合成 |
| `src/n6705c_seq/list_gui.py` | 新 | 「List 模式」分頁（`ListTab`）：泳道、段落列、表單、示意圖、統計、執行 | 合成數學（呼叫 synth） |
| `src/n6705c_seq/gui.py` | 改 | 加 `ttk.Notebook` 兩個分頁；「■ 全部關閉」移到共用的連線列 | List 模式的任何邏輯 |
| `scripts/hw_list_check.py` | 新 | 實機量測：傳輸時間、輪間接縫、256 輪對齊、Sine 頻寬 | 產品功能 |
| `examples/list_demo.json` | 新 | 範例檔，各 Track 測試共用 | |
| `tests/test_listplan.py`、`tests/test_synth.py` | 新 | 純邏輯測試 | |
| `tests/test_execute.py`、`tests/test_gui.py` | 改（只加測試） | | |

打包（`packaging/n6705c_seq.spec`）靠 import 自動收進新模組，不用改。

依賴方向（只能往右呼叫）：

```
list_gui.py ──▶ synth.py ──▶ listplan.py
     │              └──────▶ n6705c.core.arb（ConstantDwellArb, ArbSpec）
     └────────▶ execute.run_functions ──▶ core（GroupArbRunner…）
```

## 2. 合約（Track 之間的介面，凍結）

### 2.1 `listplan.py`

```python
SHAPES = ("step", "ramp", "trapezoid", "sine", "triangle", "staircase", "exponential")

# 欄位順序＝GUI 表單順序。單位：電壓 V、時間 s、頻率 Hz、相位 deg。
FIELDS: dict[str, tuple[str, ...]] = {
    "step":        ("start", "end", "delay", "end_time"),
    "ramp":        ("start", "end", "delay", "ramp_time", "end_time"),
    "trapezoid":   ("offset", "amplitude", "start_time", "up_time", "high_time", "down_time", "end_time"),
    "sine":        ("offset", "amplitude", "frequency", "phase"),
    "triangle":    ("offset", "amplitude", "frequency", "phase"),
    "staircase":   ("start", "end", "delay", "n_steps", "stair_time", "end_time"),
    "exponential": ("start", "end", "delay", "tau", "exp_time", "end_time"),
}

@dataclass
class Segment:
    shape: str                    # SHAPES 之一
    params: dict[str, float]      # 鍵＝FIELDS[shape]，一個不多一個不少
    repeat: int = 1               # 每段 Repeat Count；Sine/Triangle 的 repeat＝週期數

@dataclass
class ChannelList:
    channel: int                  # 全域通道編號，同 SeqRow.channel（1..4 第一台，5..8 第二台…）
    enabled: bool = True
    label: str = ""
    delay: float = 0.0            # 通道延遲（每輪都有，見設計文件 §2.5）
    current_limit: float | None = None
    segments: list[Segment] = field(default_factory=list)

@dataclass
class ListPlan:
    channels: list[ChannelList]
    repeat: int = 1               # 整串重複 1..256（ARB:COUNT）
    hold_last: bool = False       # True：結束後不關輸出
    dwell: float | None = None    # None＝自動；有值＝手動覆寫

def load_json(path) -> ListPlan     # 錯誤一律 ValueError，訊息帶位置：「CH2 第 3 段：frequency 必須 > 0」
def write_json(plan: ListPlan, path) -> None
```

載入檢查（只檢查格式，不檢查是否塞得下）：shape 合法、欄位齊全且是數字、時間 ≥ 0、`frequency > 0`、`n_steps`／`repeat` 是 ≥ 1 的整數、`plan.repeat` 在 1..256、通道不重複。

JSON 長相（`version` 留著，換格式時才用得到）：

```json
{"version": 1, "repeat": 1, "hold_last": false, "dwell": null,
 "channels": [{"channel": 1, "enabled": true, "label": "VCORE", "delay": 0.0, "current_limit": null,
   "segments": [{"shape": "ramp", "repeat": 1,
                 "params": {"start": 0, "end": 1.0, "delay": 0, "ramp_time": 0.01, "end_time": 0.05}}]}]}
```

### 2.2 `synth.py`

```python
WARN_PTS_PER_PERIOD = 20      # Sine/Triangle 每週期少於 → 警告
BLOCK_PTS_PER_PERIOD = 10     # 少於 → 擋下
WARN_PTS_PER_EDGE = 10        # 最快的 rise/fall 少於 → 警告
DISCONTINUITY_V = 1e-3        # 段落接縫跳動超過 → 警告
SINE_BW_HZ: dict[str, float] = {}   # ponytail: 模組型號 → 實測 -3 dB 頻寬，Track H 量完回填；查不到就不警告

def base_duration(seg: Segment) -> float           # 一次（不含 repeat）的時間；Sine/Triangle＝1/frequency
def value(seg: Segment, u: float) -> float         # 段內時間 u ∈ [0, base_duration] 的理想電壓
def channel_duration(ch: ChannelList) -> float     # delay + Σ base_duration × repeat
def auto_dwell(plan: ListPlan) -> float

@dataclass
class SegmentInfo:
    points: int
    requested: float          # 秒，含 repeat
    actual: float             # 秒，points × dwell

@dataclass
class Compiled:
    dwell: float
    n_points: int             # 每通道點數（補齊後都一樣）
    levels: dict[int, list[float]]          # 全域通道 → 點陣列
    delay_points: dict[int, int]
    pad_points: dict[int, int]
    segments: dict[int, list[SegmentInfo]]
    warnings: list[str]
    errors: list[str]         # 非空＝不能執行

def compile_plan(plan: ListPlan) -> Compiled       # 不丟例外；問題放進 errors
def to_functions(plan: ListPlan, c: Compiled) -> list[ChannelFunction]   # c.errors 非空時丟 ValueError
```

### 2.3 `execute.py`

```python
def run_functions(group, functions, *, wait=True, keep_on=False, capture=None) -> GroupArbRunner
# run_plan 改成：functions = to_functions(rows, hold_dc)；空就 PlanRejected；
#                return run_functions(group, functions, wait=wait, keep_on=hold_dc, capture=capture)
```

### 2.4 `list_gui.py`

```python
class ListTab(ttk.Frame):
    def __init__(self, parent, app): ...
    # 從 app 讀：app.resources、app.demo（連線設定）、app.busy（兩個分頁共用，同時只能跑一個）
```

## 3. 合成規則（synth 的實作依據）

| # | 規則 | 理由 |
|---|---|---|
| R1 | **右端取樣**：第 k 點（k 從 0 起）的值＝段內時間 `(k+1)·dwell` 的理想值 | 每段最後一點剛好是該段的終值；Ramp 一定到達目標，「維持最後電壓」維持的就是設定值 |
| R2 | **累計取整**：段落邊界＝`round(累計理想時間 / dwell)`，每段點數＝相鄰邊界相減 | 誤差不會一段一段累積，總誤差 ≤ dwell/2 |
| R3 | 每段點數至少 1；若算出 0，該段放 1 點並警告 | 不默默丟掉使用者設定的段落 |
| R4 | 段內 repeat：`u = τ − T·(⌈τ/T⌉ − 1)`，把 τ 對應回 (0, T] | 一條公式處理所有波形；Sine 整段一次算，頻率不會因每週期取整而偏 |
| R5 | 通道延遲：`round(delay/dwell)` 點，值＝`value(第一段, 0)` | 同 core `delay.py` 的做法：延遲是起始電壓的平線 |
| R6 | 補齊：每通道補「最後一點的值」到 `N = max(各通道點數)` | 設計文件 §2.5 |
| R7 | `ArbSpec(count=plan.repeat, terminate_last=True)`；`ChannelFunction.voltage = levels[0]` | 同 CSV 模式：一直維持最後電壓，結束時由 `run_functions` 決定要不要關輸出（`keep_on=plan.hold_last`） |

`value(seg, u)` 定義（`d`＝delay，`lo/hi` 見各式）：

```
step:        u ≤ d → start；否則 end
ramp:        u ≤ d → start；u ≤ d+ramp_time → 線性內插 start→end；否則 end
trapezoid:   lo=offset, hi=offset+amplitude；依 start_time / up_time / high_time / down_time / end_time 分段（up、down 線性）
sine:        offset + amplitude·sin(2π·f·u + phase·π/180)
triangle:    offset + amplitude·(2/π)·asin(sin(2π·f·u + phase·π/180))     ← 與 sine 同相位定義
staircase:   u ≤ d → start；u ≤ d+stair_time → start + (end−start)·⌈x·n⌉/n，x=(u−d)/stair_time；否則 end
exponential: u ≤ d → start；u ≤ d+exp_time → end + (start−end)·exp(−(u−d)/tau)；否則維持 exp_time 結束時的值
```

時間欄位為 0 的那一段直接跳過（瞬間轉換）；整段 `base_duration == 0` → errors。

## 4. 偽代碼

### 4.1 `synth.compile_plan`

```
def compile_plan(plan):
    errors, warnings = [], []
    live = [ch for ch in plan.channels if ch.enabled and ch.segments]
    if not live: return Compiled(errors=["沒有啟用且有段落的通道"])

    P = max(channel_duration(ch) for ch in live)
    dwell = snap(plan.dwell) if plan.dwell is not None else auto_dwell(plan)    # 手動值也取整到 10.24 µs 格點
    if not CDWELL_DWELL_MIN <= dwell <= CDWELL_DWELL_MAX: errors += [...]

    levels, info = {}, {}
    for ch in live:
        pts = [value(ch.segments[0], 0)] * round(ch.delay / dwell)          # R5
        cum_ideal, cum_pts = ch.delay, len(pts)
        for i, seg in enumerate(ch.segments):
            T = base_duration(seg); want = T * seg.repeat
            cum_ideal += want
            n = max(1, round(cum_ideal / dwell) - cum_pts)                   # R2, R3
            pts += [value(seg, fold((k + 1) * dwell, T)) for k in range(n)]  # R1, R4
            cum_pts += n
            info[ch].append(SegmentInfo(n, want, n * dwell))
            check_segment(seg, dwell, ch, i, warnings, errors)               # 點數／頻寬／邊緣
        check_seams(ch, warnings)                                            # 接縫跳動
        levels[ch.channel] = pts

    N = max(len(p) for p in levels.values())
    if N > CDWELL_MAX_POINTS: errors.append(f"{N:,} 點超過 65,535，請加大 dwell")
    for p in levels.values(): pad_to(p, N)                                  # R6
    return Compiled(dwell, N, levels, ..., warnings, errors)

def auto_dwell(plan):          # 設計文件 §2.3（暫定）
    P = max(channel_duration(ch) for 啟用通道)
    d = max(CDWELL_DWELL_MIN, P / CDWELL_MAX_POINTS)
    return ceil(d / GRID) * GRID          # GRID＝10.24 µs，往上取整才保證塞得下

def check_segment(seg, dwell, ...):
    if seg.shape in ("sine", "triangle"):
        ppp = 1 / (seg.params["frequency"] * dwell)
        if ppp < BLOCK_PTS_PER_PERIOD: errors.append(...)
        elif ppp < WARN_PTS_PER_PERIOD: warnings.append(...)
        bw = SINE_BW_HZ.get(model)        # model 由 GUI 傳入或略過；查不到不警告
    for edge in 該段的 ramp_time / up_time / down_time / tau（> 0 者）:
        if edge / dwell < WARN_PTS_PER_EDGE: warnings.append(...)
```

### 4.2 `synth.to_functions`

```
def to_functions(plan, c):
    if c.errors: raise ValueError("; ".join(c.errors))
    return [ChannelFunction(channel=ch.channel, voltage=c.levels[ch.channel][0],
                            current=ch.current_limit or 0.0, output_on=True, arb_enabled=True, delay=0.0,
                            spec=ArbSpec(channel=ch.channel,
                                         waveform=ConstantDwellArb(levels=c.levels[ch.channel], dwell=c.dwell),
                                         count=plan.repeat, terminate_last=True))
            for ch in 啟用通道]
```

位準是否在模組範圍內，交給 `GroupArbRunner.from_plan` 檢查（會變成 `PlanRejected`），synth 不重複檢查。

### 4.3 `execute.run_functions`

把現有 `run_plan` 從 `needed = max(...)` 到結尾整段搬過去，`hold_dc` 改名 `keep_on`。**行為不變**，現有 `tests/test_execute.py` 不改也要全過。

### 4.4 `ListTab` 版面與流程

```
┌ 泳道 canvas：每個啟用通道一條，共用時間軸，畫 Compiled.levels 的階梯（只畫一個週期 P，右上角標「× n」）
│              點泳道 → 切換編輯中的通道（紅框）
├ 段落列：◀◀ ◀ [縮圖][縮圖]… ▶ ▶▶ ｜ 左移 新增 修改 刪除 右移
├ 統計：段落時間（要求／實際／誤差）、總時間 P、段落數、預算條 n_points / 65,535（延遲、補齊另色）
│       整串重複 Count [1..256]　□ 維持最後電壓　dwell：○自動 ○手動 [____]
├ 波形選單 ｜ 參數表單（依 FIELDS[shape] 產生，帶單位） ｜ 示意圖（只畫曲線：value(seg, u) 取 200 點）
└ 警告區（Compiled.warnings／errors）　[開啟 JSON] [儲存 JSON] [▶ 執行 List]

任何修改 → after(150 ms) 防抖 → compile_plan → 重畫泳道、統計、警告；errors 非空時停用「執行」
執行：確認對話框（同 CSV 分頁的寫法）→ app.busy = True → 背景執行緒：
      connect_group → run_functions(group, to_functions(plan, c), keep_on=plan.hold_last, capture=…)
      → 自己的 queue + after 輪詢回報 → 結束時 app.busy = False
```

`gui.py` 的改動只有：連線列下面放 `ttk.Notebook`，原本的表格／按鈕／canvas 改放進第一個分頁「上電時序」，第二個分頁放 `ListTab(nb, self)`；「■ 全部關閉」移到連線列。

### 4.5 `scripts/hw_list_check.py`（實機）

```
transfer：CH1 載入 65,535 點 CDW，計時 runner.load()；另外印出 ASCII 字串長度
seam    ：CH1 0→1 V 斜坡 CDW，COUNT=3，擷取，看輪與輪交界有沒有空隙或突波
align   ：CH1..CH4 同一 dwell，週期 10 ms，每輪開頭放一個 1 V 脈衝，COUNT=256；
          擷取，比較第 1 輪與第 256 輪各通道脈衝的時間差
sinebw  ：CH1 實際負載，Sine offset 2 V、振幅 1 V，f = 10/30/100/300/1k/3k Hz；擬合振幅，印出 -3 dB 頻率
輸出：一律印數字＋存 CSV（不加 matplotlib 依賴）
```

驗收門檻（2026-10-13 定案）：

- transfer ≤ 10 s：維持「最小 dwell」規則。超過就改成「符合 N 的前提下取最大 dwell」，並回報要不要請 core 改二進位傳輸。
- 對齊誤差 ≤ 1 dwell。

## 5. 並行計畫

```
階段 0（可並行）     階段 1（可並行）          階段 2（單線）
┌──────────────┐    ┌─────────────────┐
│ P0 listplan  │───▶│ A  synth        │──┐
└──────────────┘ ┌─▶│ D  GUI 分頁      │──┼──▶ I 整合
┌──────────────┐ │  └─────────────────┘  │
│ C  execute   │─┴─▶│ H  實機量測（人）│──┘
└──────────────┘    └─────────────────┘
```

| Track | 內容 | 檔案（只准碰這些） | 依賴 | 驗收 |
|---|---|---|---|---|
| **P0** | `listplan.py`、範例 JSON | `listplan.py`、`examples/list_demo.json`、`tests/test_listplan.py` | 無 | JSON 存讀來回一致；每種錯誤都有帶位置的訊息 |
| **C** | 抽出 `run_functions` | `execute.py`、`tests/test_execute.py`（只加） | 無 | 現有測試全過；新增測試：手刻 2 通道 `ConstantDwellArb`、`count=3`，demo 上跑完、指令裡有 `ARB:VOLT:CDW`、`ARB:COUN 3` |
| **A** | `synth.py` | `synth.py`、`tests/test_synth.py` | P0 | 見 §6 |
| **D** | GUI 分頁 | `list_gui.py`、`gui.py`、`tests/test_gui.py`（只加） | P0；A 完成前用 `monkeypatch` 假的 `compile_plan`，A 完成後接上 | 兩個分頁都能開；CSV 分頁行為不變；段落增刪移改、JSON 開存可用 |
| **H** | 實機量測 | `scripts/hw_list_check.py` | C；需要人在 Windows 實機旁 | §4.5 四項的數字回報 |
| **I** | 整合 | 全部（只做串接） | A、C、D、H | 見下 |

**I 整合清單**

1. GUI「執行」接上 `compile_plan → to_functions → run_functions`，用 demo 跑通 `examples/list_demo.json`。
2. 把 H 的數字回填：`SINE_BW_HZ`、dwell 規則定案，更新設計文件 §2.3、§3。
3. 加一個端到端測試：讀 `list_demo.json` → 在 demo 上執行 → 輸出有關閉。
4. 版本號 `pyproject.toml` 改成 0.2.0；README 加 List 模式一節；Windows 打包測一次。

**單一模型接手時**的順序：P0 → C → A → D → H → I。

## 6. `tests/test_synth.py` 必測項目（A 的驗收）

- 單段 ramp 0→1 V、10 ms、dwell 1.024 ms：最後一點 = 1.0（R1），點數 = 10（R2）。
- 三段各 1.536 ms（1.5 個 dwell）、dwell 1.024 ms：點數合計 = 4，不是逐段取整的 6（R2 不累積）。注意 Python `round` 是銀行家捨入（`round(2.5) == 2`），測試要照這個算。
- Sine 1 kHz、repeat 3：點數 ≈ 3 ms / dwell；第一點與 `value(seg, dwell)` 相同（R4 不在週期交界取錯值）。
- Triangle phase 0：在 1/4 週期的值 = offset + amplitude。
- 兩通道長度不同：補齊後點數相同，短的通道尾巴全是自己的最後一點（R6）。
- 通道延遲 5 ms：前面 round(5 ms/dwell) 點等於第一段的起始電壓（R5）。
- 自動 dwell：P = 100 s → dwell = ceil(100/65535/10.24 µs)·10.24 µs，且總點數 ≤ 65,535。
- 手動 dwell 太小 → `errors` 有「超過 65,535」；Sine 每週期 < 10 點 → `errors`；10–20 點 → `warnings`。
- 段落接縫跳 2 V → `warnings` 有接縫警告，`errors` 沒有。
- `to_functions`：`count == plan.repeat`、`terminate_last` 是 True、`voltage == levels[0]`。
