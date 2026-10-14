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
from .i18n import tr
from .listplan import FIELDS, SHAPES, TIMES, ChannelList, ListPlan, Segment, load_json, write_json

SHAPE_NAMES = ("Step", "Ramp", "Trapezoid", "Sine", "Triangle", "Staircase", "Exponential")


def _format_number(value):
    """Show the shortest float representation without a trailing .0."""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


class ListTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.texts = []
        self.parameter_texts = []
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

    def _text(self, widget, key, *, parameter=False, unit=""):
        (self.parameter_texts if parameter else self.texts).append((widget, key, unit))
        widget.configure(text=tr(key) + (f" ({unit})" if unit else ""))
        return widget

    def relabel(self):
        for widget, key, unit in self.texts + self.parameter_texts:
            widget.configure(text=tr(key) + (f" ({unit})" if unit else ""))
        self.schedule()

    def _layout(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, height=150, bg="white", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=10, pady=3)
        self.canvas.bind("<Configure>", lambda _e: self.schedule())
        self.canvas.bind("<Button-1>", self._lane_click)
        channel_bar = ttk.Frame(self)
        channel_bar.grid(row=1, column=0, sticky="ew", padx=10)
        self._text(ttk.Label(channel_bar), "channel").pack(side="left")
        self.channel_box = ttk.Combobox(channel_bar, textvariable=self.channel_choice, state="readonly", width=7)
        self.channel_box.pack(side="left", padx=5)
        self.channel_box.bind("<<ComboboxSelected>>", self._choose_channel)
        self._text(ttk.Checkbutton(channel_bar, variable=self.enabled), "enabled").pack(side="left")
        for title, var, width in (("name", self.label, 12), ("channel_delay_s", self.delay, 10), ("current_limit_optional", self.limit, 10)):
            self._text(ttk.Label(channel_bar), title).pack(side="left", padx=5)
            ttk.Entry(channel_bar, textvariable=var, width=width).pack(side="left")

        strip = ttk.Frame(self)
        strip.grid(row=2, column=0, sticky="ew", padx=10, pady=3)
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
        actions.grid(row=4, column=0, sticky="ew", padx=10, pady=3)
        for title, command in (("move_left", lambda: self.move_segment(-1)), ("add", self.add_segment),
                               ("update", self.modify_segment), ("delete", self.delete_segment),
                               ("move_right", lambda: self.move_segment(1))):
            self._text(ttk.Button(actions, command=command), title).pack(side="left", padx=3)
        ttk.Label(self, textvariable=self.statistics, justify="left").grid(row=5, column=0, sticky="w", padx=10)
        self.budget = tk.Canvas(self, height=20, bg="#f1f5f9", highlightthickness=0)
        self.budget.grid(row=6, column=0, sticky="ew", padx=10, pady=3)
        settings = ttk.Frame(self)
        settings.grid(row=7, column=0, sticky="ew", padx=10)
        self._text(ttk.Label(settings), "sequence_repeat_range").pack(side="left")
        ttk.Spinbox(settings, from_=1, to=256, textvariable=self.repeat, width=5).pack(side="left", padx=5)
        self._text(ttk.Checkbutton(settings, variable=self.hold_last), "hold_last").pack(side="left", padx=8)
        self._text(ttk.Label(settings), "dwell").pack(side="left")
        for title, mode in (("auto", "auto"), ("manual", "manual")):
            self._text(ttk.Radiobutton(settings, variable=self.dwell_mode, value=mode), title).pack(side="left")
        ttk.Entry(settings, textvariable=self.dwell, width=12).pack(side="left")
        ttk.Label(settings, text="s").pack(side="left")

        editor = ttk.Frame(self)
        editor.grid(row=8, column=0, sticky="ew", padx=10, pady=4)
        ttk.Combobox(editor, textvariable=self.shape, values=SHAPE_NAMES, state="readonly", width=12).pack(side="left", anchor="n")
        self.parameter_frame = ttk.Frame(editor)
        self.parameter_frame.pack(side="left", padx=12)
        self.preview = tk.Canvas(editor, height=190, width=330, bg="white", highlightthickness=0)
        self.preview.pack(side="left", fill="x", expand=True)
        self.preview.bind("<Configure>", lambda _e: self.schedule())
        warnings = ttk.Frame(self)
        warnings.grid(row=9, column=0, sticky="ew", padx=10)
        self.warning_text = tk.Text(warnings, height=2, wrap="word", font="TkTextFont", foreground="#b45309", state="disabled")
        self.warning_text.pack(side="left", fill="x", expand=True)
        scrollbar = ttk.Scrollbar(warnings, command=self.warning_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.warning_text.configure(yscrollcommand=scrollbar.set)
        self.message.trace_add("write", lambda *_: self._show_warnings())
        bar = ttk.Frame(self)
        bar.grid(row=10, column=0, sticky="ew", padx=10, pady=4)
        self._text(ttk.Button(bar, command=self.open_json), "open_json").pack(side="left")
        self._text(ttk.Button(bar, command=self.save_json), "save_json").pack(side="left", padx=6)
        self.run_button = self._text(ttk.Button(bar, command=self.run, style="Accent.TButton"), "run_list")
        self.run_button.pack(side="right")
        self._text(ttk.Checkbutton(bar, variable=self.capture), "capture").pack(side="right", padx=12)

    @property
    def channel(self):
        return self._empty_channel if self.channel_index is None else self.plan.channels[self.channel_index]

    def _parameter_form(self, segment=None):
        self.parameter_texts = []
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
            self._text(ttk.Label(self.parameter_frame), key, parameter=True, unit=unit).grid(row=i, column=0, sticky="w")
            ttk.Entry(self.parameter_frame, textvariable=var, width=12).grid(row=i, column=1, padx=5)
        self._text(ttk.Label(self.parameter_frame), "segment_repeat", parameter=True).grid(row=len(self.params), column=0, sticky="w")
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
            requirement = tr("positive_integer") if integer and positive else tr("positive_number") if positive else tr("nonnegative_number") if minimum == 0 else tr("finite_number")
            raise ValueError(tr("number_error", title=title, requirement=requirement)) from None
        return int(value) if integer else value

    def read_segment(self):
        shape = SHAPES[SHAPE_NAMES.index(self.shape.get())]
        params = {key: self._number(var.get(), tr(key), minimum=0 if key in TIMES else None,
                                   positive=key in ("frequency", "tau", "n_steps"), integer=key == "n_steps")
                  for key, var in self.params.items()}
        repeat = self._number(self.segment_repeat.get(), tr("segment_repeat"), positive=True, integer=True)
        return Segment(shape, params, repeat)

    def _read_settings(self):
        delay = self._number(self.delay.get(), tr("channel_delay"), minimum=0)
        limit = self._number(self.limit.get(), tr("current_limit"), positive=True) if self.limit.get().strip() else None
        repeat = self._number(self.repeat.get(), tr("sequence_repeat"), positive=True, integer=True)
        if repeat > 256:
            raise ValueError(tr("repeat_range"))
        dwell = self._number(self.dwell.get(), tr("dwell"), positive=True) if self.dwell_mode.get() == "manual" else None
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
            return messagebox.showerror(tr("invalid_field"), str(exc), parent=self)
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
            return messagebox.showerror(tr("invalid_field"), str(exc), parent=self)
        index = len(self.channel.segments) if self.segment_index is None else self.segment_index + 1
        self.channel.segments.insert(index, segment)
        self.select_segment(index)

    def modify_segment(self):
        if self.segment_index is None:
            return
        try:
            segment = self.read_segment()
        except ValueError as exc:
            return messagebox.showerror(tr("invalid_field"), str(exc), parent=self)
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
        path = path or filedialog.askopenfilename(parent=self, title=tr("open_json"), filetypes=[("JSON", "*.json"), (tr("all_files"), "*")])
        if not path:
            return
        try:
            plan = load_json(path)
            capacity = len(self.app.resources) * CHANNELS_PER_FRAME
            if any(ch.channel > capacity for ch in plan.channels):
                raise ValueError(tr("capacity", capacity=capacity))
        except ValueError as exc:
            return messagebox.showerror(tr("json_error"), str(exc), parent=self)
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
            path = path or filedialog.asksaveasfilename(parent=self, title=tr("save_json"), defaultextension=".json", filetypes=[("JSON", "*.json")])
            if path:
                write_json(plan, path)
        except ValueError as exc:
            messagebox.showerror(tr("save_failed"), str(exc), parent=self)

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
                details = tr("segment_statistics", index=self.segment_index + 1, points=info.points,
                             requested=info.requested, actual=info.actual, error=info.actual - info.requested)
            self.statistics.set(details + tr("statistics", period=c.n_points * c.dwell, dwell=c.dwell,
                                segments=len(self.channel.segments), points=c.n_points,
                                delay=c.delay_points.get(self.channel.channel, 0),
                                padding=c.pad_points.get(self.channel.channel, 0)))
            self._draw_lanes()
            self._draw_budget()
        self._draw_segments()
        try:
            self._draw_curve(self.preview, self.read_segment(), 0, 0, max(self.preview.winfo_width(), 200), 185)
        except ValueError:
            self.preview.delete("all")
        self._button_state()

    def _draw_curve(self, canvas, segment, left, top, width, height):
        annotated = canvas is self.preview
        if annotated:
            canvas.delete("all")
        duration = synth.base_duration(segment)
        values = [synth.value(segment, duration * i / 199) for i in range(200)]
        lo, hi = min(values), max(values)
        if not annotated:
            span = hi - lo or 1
            coords = [coord for i, value in enumerate(values)
                      for coord in (left + 5 + (width - 10) * i / 199,
                                    top + height - 5 - (height - 10) * (value - lo) / span)]
            canvas.create_line(*coords, fill="#2563eb", width=2)
            return

        p, shape = segment.params, segment.shape
        periodic = shape in ("sine", "triangle")
        if periodic or shape == "trapezoid":
            levels = [(tr("offset"), p["offset"]), (tr("offset_amp"), p["offset"] + p["amplitude"])]
        else:
            levels = [(tr("start_label"), p["start"]), (tr("end_label"), p["end"])]
        bounds = values + [v for _, v in levels]
        if periodic:
            bounds.append(p["offset"] - p["amplitude"])
        lo, hi = min(bounds), max(bounds)
        span = hi - lo or 1
        font = "SunValleyCaptionFont"

        def text(x, y, label, tag, **options):
            return canvas.create_text(x, y, text=label, font=font,
                                      tags=("annotation", tag), **options)

        def line(*coords, **options):
            return canvas.create_line(*coords, fill="#94a3b8", tags="annotation", **options)

        def time_label(seconds):
            return f"{seconds * 1000:.4g} ms" if seconds < 1 else f"{seconds:.4g} s"

        # Measure the longest possible level label to reserve its left margin.
        combined = (f"{levels[0][0]} = {levels[1][0]} {levels[0][1]:.4g} V"
                    if levels[0][1] == levels[1][1] else
                    f"{levels[0][0]} {levels[0][1]:.4g} V / {levels[1][0]} {levels[1][1]:.4g} V")
        label_items = [text(0, 0, f"{name} {v:.4g} V", "level_label", anchor="e")
                       for name, v in levels[:1 if periodic else 2]]
        close_levels = not periodic and abs(levels[0][1] - levels[1][1]) / span * (height - 75) < 14
        measured_items = list(label_items)
        if close_levels:
            measured_items.append(text(0, 0, combined, "level_label", anchor="e"))
        margin = max(80, max(canvas.bbox(i)[2] - canvas.bbox(i)[0]
                             for i in measured_items) + 10)
        if close_levels:
            canvas.delete(measured_items[-1])
        x0, x1 = left + margin, left + width - 10
        y0, y1 = top + 30, top + height - 45
        x = lambda t: x0 + (x1 - x0) * t / (duration or 1)
        y = lambda v: y1 - (y1 - y0) * (v - lo) / span

        # Time spans use the same x mapping as the curve. Wide labels go below
        # the first row; measured bounds keep adjacent labels apart.
        times = [(tr("period"), duration)] if periodic else [
            (tr(key), p[key]) for key in FIELDS[shape]
            if key in TIMES and key != "tau" and p[key] > 0]
        elapsed, row_ends = 0.0, {}
        for title, seconds in times:
            start, end = x(elapsed), x(elapsed + seconds)
            elapsed += seconds
            item = text(0, 0, f"{title} {time_label(seconds)}", "time_label")
            box = canvas.bbox(item)
            label_width = box[2] - box[0]
            row = 0 if label_width + 4 <= end - start else 1
            center = (start + end) / 2
            while True:
                label_left = max(left + 4, min(center - label_width / 2,
                                              left + width - 4 - label_width))
                label_left = max(label_left, row_ends.get(row, left) + 4)
                if label_left + label_width <= left + width - 4:
                    break
                row += 1
            center = label_left + label_width / 2
            canvas.coords(item, center, y1 + 18 + row * 18)
            row_ends[row] = canvas.bbox(item)[2]
            line(start, y1 + 6, end, y1 + 6)
            for tick in (start, end):
                line(tick, y1 + 2, tick, y1 + 10)
        # Rare crowded forms get enough space for extra rows without clipping.
        canvas.configure(height=max(190, height + max(0, max(row_ends, default=0) - 1) * 18))

        if not periodic and abs(y(levels[0][1]) - y(levels[1][1])) < 14:
            canvas.delete(label_items[1])
            label_items = label_items[:1]
            canvas.itemconfigure(label_items[0], text=combined)
            guides = [(levels[0][1] + levels[1][1]) / 2]
        else:
            guides = [v for _, v in levels[:1 if periodic else 2]]
        for item, v in zip(label_items, guides):
            canvas.coords(item, x0 - 6, y(v))
            line(x0, y(v), x1, y(v), dash=(3, 3))
        if periodic:
            base, peak = y(p["offset"]), y(p["offset"] + p["amplitude"])
            dimension_x = x1 - 8
            line(dimension_x, base, dimension_x, peak)
            for tick in (base, peak):
                line(dimension_x - 4, tick, dimension_x + 4, tick)
            text(dimension_x - 6, (base + peak) / 2,
                 tr("amplitude_annotation", value=p["amplitude"]), "amplitude_label", anchor="e")
        extras = []
        if periodic:
            extras.append(tr("frequency_phase", frequency=p["frequency"], phase=p["phase"]))
        elif shape == "staircase":
            extras.append(tr("steps_annotation", value=p["n_steps"]))
        elif shape == "exponential":
            extras.append(tr("tau_annotation", value=time_label(p["tau"])))
        if segment.repeat > 1:
            extras.append(f"×{segment.repeat}")
        if extras:
            text(x1, top + 12, "   ".join(extras), "extra_label", anchor="e")
        coords = [coord for i, v in enumerate(values)
                  for coord in (x(duration * i / 199), y(v))]
        canvas.create_line(*coords, fill="#2563eb", width=2)

    def _draw_segments(self):
        canvas = self.segment_canvas
        canvas.delete("all")
        for i, segment in enumerate(self.channel.segments):
            canvas.create_rectangle(i * 110 + 2, 1, (i + 1) * 110 - 2, 64,
                                    outline="#dc2626" if i == self.segment_index else "#cbd5e1", width=2)
            self._draw_curve(canvas, segment, i * 110 + 4, 3, 102, 38)
            canvas.create_text(i * 110 + 55, 53, text=f"{i + 1} {SHAPE_NAMES[SHAPES.index(segment.shape)]} ×{segment.repeat}", font="SunValleyBodyFont")
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
            canvas.create_text(6, top + lane / 2, text=ch.label or f"CH{ch.channel}", anchor="w", font="SunValleyBodyFont")
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
        canvas.create_text(width - 10, 12, text=f"× {self.plan.repeat}", anchor="e", font="SunValleyBodyFont")
        period = c.n_points * c.dwell
        scale, unit = (1000, "ms") if period < 1 else (1, "s")
        for j in range(5):
            canvas.create_text(100 + (width - 110) * j / 4, height - 16,
                               text=f"{period * scale * j / 4:.4g} {unit}", anchor="e" if j == 4 else "center", font="SunValleyBodyFont")

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
            return messagebox.showerror(tr("missing_address"), tr("address_required", frames=frames), parent=self)
        text = (tr("demo_prefix") if demo else tr("hardware_prefix")) + tr("list_start")
        if plan.hold_last:
            text += tr("list_keep")
        if not messagebox.askyesno(tr("confirm"), text + tr("continue"), icon="warning", parent=self):
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
            self.queue.put(("error", tr("connection_error", error=exc)))
            return
        try:
            try:
                functions = synth.to_functions(plan, compiled)
            except ValueError as exc:
                raise execute.PlanRejected(str(exc)) from exc
            cap = {} if capture else None
            execute.run_functions(group, functions, keep_on=plan.hold_last, capture=cap)
            text = tr("list_hold_done") if plan.hold_last else tr("list_done")
            if cap and "error" in cap:
                text += tr("capture_error", error=cap.pop("error"))
            self.queue.put(("done", text, cap))
        except Exception as exc:  # noqa: BLE001 - execution handles output shutdown
            note = ("" if isinstance(exc, execute.OutputsNotConfirmedOff)
                    else tr("rejected") if isinstance(exc, execute.PlanRejected)
                    else tr("forced_off"))
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
            (messagebox.showinfo if kind == "done" else messagebox.showerror)(tr("run_result"), text, parent=self)
        self._button_state()
        self._poll_id = self.after(100, self._poll)

    def _destroyed(self, event):
        if event.widget is self:
            if self._pending is not None:
                self.after_cancel(self._pending)
            self.after_cancel(self._poll_id)
