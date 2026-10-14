"""The ARB List editor and its compiled waveform preview."""

from __future__ import annotations

import copy
import math
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from n6705c.core.group import CHANNELS_PER_FRAME, InstrumentGroup

from . import execute, synth
from .listplan import FIELDS, SHAPES, TIMES, ChannelList, ListPlan, Segment, load_json, write_json

SHAPE_NAMES = ("階躍", "斜坡", "梯形", "正弦", "三角", "階梯", "指數")
TITLES = {
    "start": "起始電壓", "end": "終止電壓", "offset": "偏移電壓", "amplitude": "振幅",
    "delay": "延遲", "end_time": "尾端維持", "ramp_time": "斜坡時間", "start_time": "起始維持",
    "up_time": "上升時間", "high_time": "高點維持", "down_time": "下降時間",
    "frequency": "頻率", "phase": "相位", "n_steps": "階數", "stair_time": "階梯時間",
    "tau": "時間常數", "exp_time": "指數時間",
}


def _format_number(value):
    """Show the shortest float representation without a trailing .0."""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


class ListTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.plan = ListPlan([ChannelList(i, enabled=i == 1) for i in range(1, len(app.resources) * CHANNELS_PER_FRAME + 1)])
        self.channel_index = 0
        self.segment_index = None
        self._empty_channel = ChannelList(1, enabled=False)
        self.compiled = None
        self.queue = queue.Queue()
        self.measured = {}
        self._pending = None
        self._loading = False
        self.channel_choice = tk.StringVar(self)
        self.enabled = tk.BooleanVar(self)
        self.label = tk.StringVar(self)
        self.delay = tk.StringVar(self)
        self.limit = tk.StringVar(self)
        self.repeat = tk.StringVar(self, value="1")
        self.hold_last = tk.BooleanVar(self)
        self.dwell_mode = tk.StringVar(self, value="auto")
        self.dwell = tk.StringVar(self, value="0.00001024")
        self.capture = tk.BooleanVar(self)
        self.shape = tk.StringVar(self, value=SHAPE_NAMES[0])
        self.segment_repeat = tk.StringVar(self, value="1")
        self.params = {}
        self.statistics = tk.StringVar(self)
        self.message = tk.StringVar(self)
        self._layout()
        self._load_channel()
        for var in (self.enabled, self.label, self.delay, self.limit, self.repeat, self.hold_last, self.dwell_mode, self.dwell):
            var.trace_add("write", lambda *_: self.schedule())
        self.shape.trace_add("write", lambda *_: self._parameter_form())
        self.segment_repeat.trace_add("write", lambda *_: self.schedule())
        self._parameter_form()
        self.schedule()
        self._poll_id = self.after(100, self._poll)
        self.bind("<Destroy>", self._destroyed)

    def _layout(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, height=180, bg="white", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=10, pady=5)
        self.canvas.bind("<Configure>", lambda _e: self.schedule())
        self.canvas.bind("<Button-1>", self._lane_click)
        channel_bar = ttk.Frame(self)
        channel_bar.grid(row=1, column=0, sticky="ew", padx=10)
        ttk.Label(channel_bar, text="編輯通道").pack(side="left")
        self.channel_box = ttk.Combobox(channel_bar, textvariable=self.channel_choice, state="readonly", width=7)
        self.channel_box.pack(side="left", padx=5)
        self.channel_box.bind("<<ComboboxSelected>>", self._choose_channel)
        ttk.Checkbutton(channel_bar, text="啟用", variable=self.enabled).pack(side="left")
        for title, var, width in (("名稱", self.label, 12), ("延遲 (s)", self.delay, 10), ("限流 (A，空白不限)", self.limit, 10)):
            ttk.Label(channel_bar, text=title).pack(side="left", padx=5)
            ttk.Entry(channel_bar, textvariable=var, width=width).pack(side="left")

        strip = ttk.Frame(self)
        strip.grid(row=2, column=0, sticky="ew", padx=10, pady=5)
        for title, move in (("◀◀", "first"), ("◀", -1)):
            ttk.Button(strip, text=title, width=3, command=lambda m=move: self.navigate(m)).pack(side="left")
        self.segment_canvas = tk.Canvas(strip, height=65, bg="white", highlightthickness=0)
        self.segment_canvas.pack(side="left", fill="x", expand=True)
        self.segment_canvas.bind("<Button-1>", self._segment_click)
        for title, move in (("▶", 1), ("▶▶", "last")):
            ttk.Button(strip, text=title, width=3, command=lambda m=move: self.navigate(m)).pack(side="left")
        scroll = ttk.Scrollbar(self, orient="horizontal", command=self.segment_canvas.xview)
        scroll.grid(row=3, column=0, sticky="ew", padx=10)
        self.segment_canvas.configure(xscrollcommand=scroll.set)
        actions = ttk.Frame(self)
        actions.grid(row=4, column=0, sticky="ew", padx=10, pady=4)
        for title, command in (("左移", lambda: self.move_segment(-1)), ("新增", self.add_segment),
                               ("修改", self.modify_segment), ("刪除", self.delete_segment),
                               ("右移", lambda: self.move_segment(1))):
            ttk.Button(actions, text=title, command=command).pack(side="left", padx=3)
        ttk.Label(self, textvariable=self.statistics, justify="left").grid(row=5, column=0, sticky="w", padx=10)
        self.budget = tk.Canvas(self, height=20, bg="#f1f5f9", highlightthickness=0)
        self.budget.grid(row=6, column=0, sticky="ew", padx=10, pady=3)
        settings = ttk.Frame(self)
        settings.grid(row=7, column=0, sticky="ew", padx=10)
        ttk.Label(settings, text="整串重複 (1–256)").pack(side="left")
        ttk.Spinbox(settings, from_=1, to=256, textvariable=self.repeat, width=5).pack(side="left", padx=5)
        ttk.Checkbutton(settings, text="維持最後電壓", variable=self.hold_last).pack(side="left", padx=8)
        ttk.Label(settings, text="dwell").pack(side="left")
        for title, mode in (("自動", "auto"), ("手動", "manual")):
            ttk.Radiobutton(settings, text=title, variable=self.dwell_mode, value=mode).pack(side="left")
        ttk.Entry(settings, textvariable=self.dwell, width=12).pack(side="left")
        ttk.Label(settings, text="s").pack(side="left")

        editor = ttk.Frame(self)
        editor.grid(row=8, column=0, sticky="ew", padx=10, pady=6)
        ttk.Combobox(editor, textvariable=self.shape, values=SHAPE_NAMES, state="readonly", width=8).pack(side="left", anchor="n")
        self.parameter_frame = ttk.Frame(editor)
        self.parameter_frame.pack(side="left", padx=12)
        self.preview = tk.Canvas(editor, height=190, width=330, bg="white", highlightthickness=0)
        self.preview.pack(side="left", fill="x", expand=True)
        self.preview.bind("<Configure>", lambda _e: self.schedule())
        warnings = ttk.Frame(self)
        warnings.grid(row=9, column=0, sticky="ew", padx=10)
        self.warning_text = tk.Text(warnings, height=3, wrap="word", foreground="#b45309", state="disabled")
        self.warning_text.pack(side="left", fill="x", expand=True)
        scrollbar = ttk.Scrollbar(warnings, command=self.warning_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.warning_text.configure(yscrollcommand=scrollbar.set)
        self.message.trace_add("write", lambda *_: self._show_warnings())
        bar = ttk.Frame(self)
        bar.grid(row=10, column=0, sticky="ew", padx=10, pady=6)
        ttk.Button(bar, text="開啟 JSON", command=self.open_json).pack(side="left")
        ttk.Button(bar, text="儲存 JSON", command=self.save_json).pack(side="left", padx=6)
        self.run_button = ttk.Button(bar, text="▶ 執行 List", command=self.run)
        self.run_button.pack(side="right")
        ttk.Checkbutton(bar, text="擷取實測波形（數位化器）", variable=self.capture).pack(side="right", padx=12)

    @property
    def channel(self):
        return self._empty_channel if self.channel_index is None else self.plan.channels[self.channel_index]

    def _parameter_form(self, segment=None):
        for widget in self.parameter_frame.winfo_children():
            widget.destroy()
        shape = SHAPES[SHAPE_NAMES.index(self.shape.get())]
        defaults = {"end": 1, "amplitude": 1, "frequency": 100, "n_steps": 5, "tau": 0.01}
        self.params = {}
        for i, key in enumerate(FIELDS[shape]):
            unit = "s" if key in TIMES else "Hz" if key == "frequency" else "deg" if key == "phase" else "" if key == "n_steps" else "V"
            value = segment.params[key] if segment else defaults.get(key, 0.01 if key in TIMES else 0)
            var = tk.StringVar(self, value=_format_number(value))
            self.params[key] = var
            var.trace_add("write", lambda *_: self.schedule())
            ttk.Label(self.parameter_frame, text=f"{TITLES[key]} ({unit})" if unit else TITLES[key]).grid(row=i, column=0, sticky="w")
            ttk.Entry(self.parameter_frame, textvariable=var, width=12).grid(row=i, column=1, padx=5)
        ttk.Label(self.parameter_frame, text="段落重複次數").grid(row=len(self.params), column=0, sticky="w")
        ttk.Entry(self.parameter_frame, textvariable=self.segment_repeat, width=12).grid(row=len(self.params), column=1, padx=5)
        self.schedule()

    @staticmethod
    def _number(text, title, *, minimum=None, positive=False, integer=False):
        try:
            value = float(text)
            if not math.isfinite(value) or (minimum is not None and value < minimum) or (positive and value <= 0):
                raise ValueError
            if integer and not value.is_integer():
                raise ValueError
        except ValueError:
            requirement = "正整數" if integer and positive else "大於 0 的數字" if positive else "非負數字" if minimum == 0 else "有限數字"
            raise ValueError(f"{title} 必須是有效的{requirement}") from None
        return int(value) if integer else value

    def read_segment(self):
        shape = SHAPES[SHAPE_NAMES.index(self.shape.get())]
        params = {key: self._number(var.get(), TITLES[key], minimum=0 if key in TIMES else None,
                                   positive=key in ("frequency", "tau", "n_steps"), integer=key == "n_steps")
                  for key, var in self.params.items()}
        repeat = self._number(self.segment_repeat.get(), "段落重複次數", positive=True, integer=True)
        return Segment(shape, params, repeat)

    def _read_settings(self):
        delay = self._number(self.delay.get(), "通道延遲", minimum=0)
        limit = self._number(self.limit.get(), "電流限制", positive=True) if self.limit.get().strip() else None
        repeat = self._number(self.repeat.get(), "整串重複次數", positive=True, integer=True)
        if repeat > 256:
            raise ValueError("整串重複次數必須在 1..256")
        dwell = self._number(self.dwell.get(), "dwell", positive=True) if self.dwell_mode.get() == "manual" else None
        ch = self.channel
        ch.enabled, ch.label, ch.delay, ch.current_limit = self.enabled.get(), self.label.get(), delay, limit
        if self.channel_index is None and ch != ChannelList(1, enabled=False):
            self.plan.channels.append(ch)
            self.channel_index = 0
        self.plan.repeat, self.plan.hold_last, self.plan.dwell = repeat, self.hold_last.get(), dwell
        return self.plan

    def _load_channel(self):
        self._loading = True
        ch = self.channel
        self.channel_box.configure(values=[f"CH{i}" for i in range(1, len(self.app.resources) * CHANNELS_PER_FRAME + 1)])
        self.channel_choice.set(f"CH{ch.channel}")
        self.enabled.set(ch.enabled)
        self.label.set(ch.label)
        self.delay.set(_format_number(ch.delay))
        self.limit.set("" if ch.current_limit is None else _format_number(ch.current_limit))
        self._loading = False
        self.select_segment(0 if ch.segments else None)

    def select_channel(self, channel_number):
        try:
            self._read_settings()
        except ValueError as exc:
            self.channel_choice.set(f"CH{self.channel.channel}")
            return messagebox.showerror("欄位錯誤", str(exc), parent=self)
        index = next((i for i, ch in enumerate(self.plan.channels) if ch.channel == channel_number), None)
        if index is None:
            self.plan.channels.append(ChannelList(channel_number, enabled=False))
            index = len(self.plan.channels) - 1
        self.channel_index = index
        self._load_channel()
        self.schedule()

    def _choose_channel(self, _event):
        self.select_channel(self.channel_box.current() + 1)

    def select_segment(self, index):
        self.segment_index = index
        if index is not None:
            segment = self.channel.segments[index]
            self.shape.set(SHAPE_NAMES[SHAPES.index(segment.shape)])
            self.segment_repeat.set(str(segment.repeat))
            self._parameter_form(segment)
            self.segment_canvas.xview_moveto(max(0, index * 110 - 110) / max(1, len(self.channel.segments) * 110))
        self.schedule()

    def navigate(self, move):
        count = len(self.channel.segments)
        if count:
            index = 0 if move == "first" else count - 1 if move == "last" else max(0, min(count - 1, (self.segment_index or 0) + move))
            self.select_segment(index)

    def _segment_click(self, event):
        index = int(self.segment_canvas.canvasx(event.x) // 110)
        if 0 <= index < len(self.channel.segments):
            self.select_segment(index)

    def add_segment(self):
        try:
            segment = self.read_segment()
        except ValueError as exc:
            return messagebox.showerror("欄位錯誤", str(exc), parent=self)
        index = len(self.channel.segments) if self.segment_index is None else self.segment_index + 1
        self.channel.segments.insert(index, segment)
        self.select_segment(index)

    def modify_segment(self):
        if self.segment_index is None:
            return
        try:
            segment = self.read_segment()
        except ValueError as exc:
            return messagebox.showerror("欄位錯誤", str(exc), parent=self)
        self.channel.segments[self.segment_index] = segment
        self.schedule()

    def delete_segment(self):
        if self.segment_index is not None:
            del self.channel.segments[self.segment_index]
            self.select_segment(min(self.segment_index, len(self.channel.segments) - 1) if self.channel.segments else None)

    def move_segment(self, direction):
        if self.segment_index is None:
            return
        index, target = self.segment_index, self.segment_index + direction
        if 0 <= target < len(self.channel.segments):
            segments = self.channel.segments
            segments[index], segments[target] = segments[target], segments[index]
            self.select_segment(target)

    def open_json(self, path=None):
        path = path or filedialog.askopenfilename(parent=self, filetypes=[("JSON", "*.json"), ("所有檔案", "*")])
        if not path:
            return
        try:
            plan = load_json(path)
            capacity = len(self.app.resources) * CHANNELS_PER_FRAME
            if any(ch.channel > capacity for ch in plan.channels):
                raise ValueError(f"這個介面最多支援 {capacity} 個通道")
        except ValueError as exc:
            return messagebox.showerror("JSON 錯誤", str(exc), parent=self)
        self.plan = plan
        self._empty_channel = ChannelList(1, enabled=False)
        self.channel_index = 0 if plan.channels else None
        self.repeat.set(str(plan.repeat))
        self.hold_last.set(plan.hold_last)
        self.dwell_mode.set("auto" if plan.dwell is None else "manual")
        if plan.dwell is not None:
            self.dwell.set(_format_number(plan.dwell))
        self.measured = {}
        self._load_channel()
        self.schedule()

    def save_json(self, path=None):
        try:
            plan = self._read_settings()
            path = path or filedialog.asksaveasfilename(parent=self, defaultextension=".json", filetypes=[("JSON", "*.json")])
            if path:
                write_json(plan, path)
        except ValueError as exc:
            messagebox.showerror("儲存失敗", str(exc), parent=self)

    def schedule(self):
        if self._loading:
            return
        if self._pending is not None:
            self.after_cancel(self._pending)
        self._pending = self.after(150, self.refresh)

    def refresh(self):
        if self._pending is not None:
            self.after_cancel(self._pending)
        self._pending = None
        try:
            self.compiled = synth.compile_plan(self._read_settings())
        except ValueError as exc:
            self.compiled = None
            self.message.set(str(exc))
            self.statistics.set("")
            self.canvas.delete("all")
            self.budget.delete("all")
        else:
            c = self.compiled
            self.message.set("\n".join([f"✘ {e}" for e in c.errors] + [f"⚠ {w}" for w in c.warnings]))
            details = ""
            infos = c.segments.get(self.channel.channel, [])
            if self.segment_index is not None and self.segment_index < len(infos):
                info = infos[self.segment_index]
                details = f"第 {self.segment_index + 1} 段：{info.points:,} 點；要求 {info.requested:.6g} s／實際 {info.actual:.6g} s／誤差 {info.actual - info.requested:+.6g} s\n"
            self.statistics.set(details + f"共同週期 P＝{c.n_points * c.dwell:.6g} s　dwell＝{c.dwell:.6g} s　"
                                f"本通道 {len(self.channel.segments)} 段　點數 {c.n_points:,} / 65,535\n"
                                f"本通道延遲 {c.delay_points.get(self.channel.channel, 0):,} 點（灰）／補齊 {c.pad_points.get(self.channel.channel, 0):,} 點（橙）")
            self._draw_lanes()
            self._draw_budget()
        self._draw_segments()
        try:
            self._draw_curve(self.preview, self.read_segment(), 0, 0, max(self.preview.winfo_width(), 200), 185)
        except ValueError:
            self.preview.delete("all")
        self._button_state()

    def _draw_curve(self, canvas, segment, left, top, width, height):
        if canvas is self.preview:
            canvas.delete("all")
        duration = synth.base_duration(segment)
        values = [synth.value(segment, duration * i / 199) for i in range(200)]
        lo, hi = min(values), max(values)
        span = hi - lo or 1
        coords = [coord for i, value in enumerate(values)
                  for coord in (left + 5 + (width - 10) * i / 199, top + height - 5 - (height - 10) * (value - lo) / span)]
        canvas.create_line(*coords, fill="#2563eb", width=2)

    def _draw_segments(self):
        canvas = self.segment_canvas
        canvas.delete("all")
        for i, segment in enumerate(self.channel.segments):
            canvas.create_rectangle(i * 110 + 2, 1, (i + 1) * 110 - 2, 64,
                                    outline="#dc2626" if i == self.segment_index else "#cbd5e1", width=2)
            self._draw_curve(canvas, segment, i * 110 + 4, 3, 102, 38)
            canvas.create_text(i * 110 + 55, 53, text=f"{i + 1} {SHAPE_NAMES[SHAPES.index(segment.shape)]} ×{segment.repeat}")
        canvas.configure(scrollregion=(0, 0, max(1, len(self.channel.segments) * 110), 65))

    def _draw_budget(self):
        c, canvas = self.compiled, self.budget
        canvas.delete("all")
        width = max(canvas.winfo_width(), 200)
        scale = width / max(65535, c.n_points)
        delay = c.delay_points.get(self.channel.channel, 0)
        pad = c.pad_points.get(self.channel.channel, 0)
        for a, b, color in ((0, delay, "#94a3b8"), (delay, max(delay, c.n_points - pad), "#60a5fa"),
                            (max(delay, c.n_points - pad), c.n_points, "#fb923c")):
            canvas.create_rectangle(a * scale, 0, b * scale, 20, fill=color, outline="")

    def _draw_lanes(self):
        c, canvas = self.compiled, self.canvas
        canvas.delete("all")
        live = [ch for ch in self.plan.channels if ch.enabled]
        width, height = max(canvas.winfo_width(), 400), max(canvas.winfo_height(), 34)
        lane = (height - 32) / max(1, len(live))
        self._lanes = live
        for i, ch in enumerate(live):
            top = i * lane
            canvas.create_rectangle(1, top + 1, width - 2, top + lane - 1,
                                    outline="#dc2626" if ch is self.channel else "#e2e8f0", width=2)
            canvas.create_text(6, top + lane / 2, text=ch.label or f"CH{ch.channel}", anchor="w")
            values = c.levels.get(ch.channel, [])
            if not values:
                continue
            lo, hi = min(values), max(values)
            measured = self.measured.get(ch.channel)
            if measured and measured[1]:
                lo, hi = min(lo, min(measured[1])), max(hi, max(measured[1]))
            x = lambda k: 100 + (width - 110) * k / max(1, c.n_points)
            y = lambda v: top + lane - 7 - (lane - 18) * (v - lo) / (hi - lo or 1)
            coords = [x(0), y(values[0])]
            for k, value in enumerate(values):
                if k and value != values[k - 1]:
                    coords.extend((x(k), y(values[k - 1]), x(k), y(value)))
            coords.extend((x(len(values)), y(values[-1])))
            canvas.create_line(*coords, fill="#2563eb", width=1.5)
            if measured and c.dwell > 0:
                dt, volts = measured
                pts = [p for j, v in enumerate(volts) if j * dt <= c.n_points * c.dwell for p in (x(j * dt / c.dwell), y(v))]
                if len(pts) >= 4:
                    canvas.create_line(*pts, fill="#111827")
        canvas.create_text(width - 10, 12, text=f"× {self.plan.repeat}", anchor="e")
        period = c.n_points * c.dwell
        scale, unit = (1000, "ms") if period < 1 else (1, "s")
        for j in range(5):
            canvas.create_text(100 + (width - 110) * j / 4, height - 16,
                               text=f"{period * scale * j / 4:.4g} {unit}", anchor="e" if j == 4 else "center")

    def _lane_click(self, event):
        live = getattr(self, "_lanes", [])
        lane = (max(self.canvas.winfo_height(), 34) - 32) / max(1, len(live))
        index = int(event.y // lane)
        if 0 <= index < len(live):
            self.select_channel(live[index].channel)

    def _show_warnings(self):
        self.warning_text.configure(state="normal")
        self.warning_text.delete("1.0", "end")
        self.warning_text.insert("1.0", self.message.get())
        self.warning_text.configure(state="disabled")

    def _button_state(self):
        ready = self.compiled is not None and not self.compiled.errors and not self.app.busy
        self.run_button.state(["!disabled" if ready else "disabled"])

    def run(self):
        if self.app.busy:
            return
        self.refresh()
        c = self.compiled
        if c is None or c.errors:
            return
        plan = copy.deepcopy(self.plan)
        frames = -(-max(c.levels) // CHANNELS_PER_FRAME)
        resources = [v.get().strip() for v in self.app.resources[:frames]]
        demo = self.app.demo.get()
        if not demo and not all(resources):
            return messagebox.showerror("未設定位址", f"需要 {frames} 台機台的 VISA 位址", parent=self)
        text = ("【Demo】" if demo else "【實機】") + "將開啟輸出並執行 List"
        if plan.hold_last:
            text += "，結束後輸出維持最後電壓不會關閉"
        if not messagebox.askyesno("確認", text + "，繼續？", icon="warning", parent=self):
            return
        self.app._save_config()
        self.app.busy = True
        self.measured = {}
        self._button_state()
        self.app.run_button.state(["disabled"])
        self.app.off_button.state(["disabled"])
        threading.Thread(target=self._work, args=(plan, c, frames, resources, demo, self.capture.get()), daemon=True).start()

    def _work(self, plan, compiled, frames, resources, demo, capture):
        try:
            group = InstrumentGroup.demo(frames=frames, slots=execute.DEMO_SLOTS) if demo else execute.connect_group(resources)
        except Exception as exc:  # noqa: BLE001 - backend errors vary
            self.queue.put(("error", f"連線失敗：{exc}"))
            return
        try:
            try:
                functions = synth.to_functions(plan, compiled)
            except ValueError as exc:
                raise execute.PlanRejected(str(exc)) from exc
            cap = {} if capture else None
            execute.run_functions(group, functions, keep_on=plan.hold_last, capture=cap)
            text = "List 完成，輸出維持最後電壓（未關閉）。" if plan.hold_last else "List 完成，輸出已關閉。"
            if cap and "error" in cap:
                text += f"\n實測波形擷取失敗：{cap.pop('error')}"
            self.queue.put(("done", text, cap))
        except Exception as exc:  # noqa: BLE001 - execution handles output shutdown
            note = ("" if isinstance(exc, execute.OutputsNotConfirmedOff)
                    else "\n（時序在開輸出前被拒絕）" if isinstance(exc, execute.PlanRejected)
                    else "\n（輸出已強制關閉）")
            self.queue.put(("error", f"{exc}{note}"))
        finally:
            for inst in group.instruments:
                inst.close()

    def _poll(self):
        while not self.queue.empty():
            kind, text, *rest = self.queue.get()
            self.app.busy = False
            self.app.run_button.state(["!disabled"])
            self.app.off_button.state(["!disabled"])
            if rest and rest[0]:
                self.measured = rest[0]
            self.schedule()
            (messagebox.showinfo if kind == "done" else messagebox.showerror)("執行結果", text, parent=self)
        self._button_state()
        self._poll_id = self.after(100, self._poll)

    def _destroyed(self, event):
        if event.widget is self:
            if self._pending is not None:
                self.after_cancel(self._pending)
            self.after_cancel(self._poll_id)
