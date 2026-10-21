"""``n6705c-seq-gui``: edit the table, see every rail's waveform, set the mainframe
addresses and run.  Tkinter with the Sun Valley theme."""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import tkinter as tk
from tkinter import colorchooser, filedialog, font, messagebox, ttk

import sv_ttk

from n6705c.core.group import CHANNELS_PER_FRAME, TRIGGER_SKEW_SECONDS, InstrumentGroup
from n6705c.core.instrument import N6705C

from .e36441a import detect_model, kind_of, off_resources, run_resources
from .execute import DEMO_SLOTS, connect_group, OutputsNotConfirmedOff, PlanRejected, outputs_off, run_plan
from .plan import SeqRow, load_csv, order_warnings, write_csv
from .list_gui import ListTab
from . import form, i18n
from .i18n import tr

FRAMES = form.FRAMES
CONFIG = form.CONFIG
FONT_SCALE = 1.0  # calibration knob: enlarge/shrink every font
PALETTE, DEFAULTS, COLUMNS, TITLES, NUMERIC = form.PALETTE, form.DEFAULTS, form.COLUMNS, form.TITLES, form.NUMERIC
waveform = form.waveform
_tick_step = form.tick_step


def _load_config() -> dict:
    return form.load_config(CONFIG)


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        saved = _load_config()
        i18n.set_lang(saved.get("lang", "zh"))
        root.title(tr("title"))
        self.texts = []
        self.queue: queue.Queue = queue.Queue()
        self.busy = False
        self._pending = None
        self.forms: list[dict] = []
        self.resources = [tk.StringVar(value=r) for r in (saved["resources"] + [""] * FRAMES)[:FRAMES]]
        self.demo = tk.BooleanVar(value=saved["demo"])
        self.hold_dc = tk.BooleanVar(value=False)
        self.capture = tk.BooleanVar(value=False)
        self.measured: dict = {}  # channel -> (sample interval, volts) from the last captured run
        self.conn_status = [tk.StringVar() for _ in range(FRAMES)]
        self.message = tk.StringVar()
        self._connection_bar()
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)
        self.sequence_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.sequence_tab, text=tr("sequence_tab"))
        self._table()
        self._buttons()
        ttk.Label(self.sequence_tab, textvariable=self.message, foreground="#b45309", justify="left").pack(anchor="w", padx=10)
        self.canvas = tk.Canvas(self.sequence_tab, bg="white", height=520, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=10, pady=(2, 6))
        self.canvas.bind("<Configure>", lambda _e: self.schedule())
        self.list_tab = ListTab(self.notebook, self)
        self.notebook.add(self.list_tab, text=tr("list_tab"))
        root.after(100, self._poll)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.schedule()

    def set_kind(self, kind: str) -> None:
        """E36441A has no ARB, digitizer or List-mode page: lock those, keep the sequence."""
        e36 = kind == "E36441A"
        self.notebook.tab(self.list_tab, state="disabled" if e36 else "normal")
        self.capture_check.state(["disabled" if e36 else "!disabled"])
        if e36:
            self.capture.set(False)
            self.notebook.select(self.sequence_tab)

    def _text(self, widget, key, **fmt):
        self.texts.append((widget, key, fmt))
        widget.configure(text=tr(key, **fmt))
        return widget

    def toggle_language(self):
        i18n.set_lang("en" if i18n.lang == "zh" else "zh")
        self._save_config()
        for widget, key, fmt in self.texts:
            widget.configure(text=tr(key, **fmt))
        self.root.title(tr("title"))
        self.notebook.tab(self.sequence_tab, text=tr("sequence_tab"))
        self.notebook.tab(self.list_tab, text=tr("list_tab"))
        self.schedule()
        self.list_tab.relabel()

    def close(self) -> None:
        # The worker is a daemon thread: quitting mid-run would skip run_plan's shutdown.
        if self.busy:
            messagebox.showwarning(tr("busy_title"), tr("busy_body"))
        else:
            self.root.destroy()

    # -- layout ---------------------------------------------------------------

    def _connection_bar(self) -> None:
        bar = self._text(ttk.LabelFrame(self.root), "connection")
        bar.pack(fill="x", padx=10, pady=(10, 4))
        bar.columnconfigure(1, weight=1)
        for i in range(FRAMES):
            self._text(ttk.Label(bar), "frame", number=i + 1, first=i * CHANNELS_PER_FRAME + 1, last=(i + 1) * CHANNELS_PER_FRAME).grid(
                row=i, column=0, padx=6, pady=3, sticky="w"
            )
            box = ttk.Entry(bar, textvariable=self.resources[i], style="Table.TEntry")
            box.grid(row=i, column=1, sticky="ew", padx=6)
            ttk.Label(bar, textvariable=self.conn_status[i], width=34).grid(row=i, column=2, padx=6, sticky="w")
        self._text(ttk.Button(bar, command=self.test_connection), "test_connection").grid(row=1, column=3, padx=6)
        self._text(ttk.Checkbutton(bar, variable=self.demo), "demo").grid(row=0, column=4, rowspan=2, padx=8)
        self.off_button = self._text(ttk.Button(bar, command=self.all_off, style="Danger.TButton"), "all_off_button")
        self.off_button.grid(row=0, column=5, rowspan=2, padx=8)
        self.language_button = self._text(ttk.Button(bar, command=self.toggle_language), "other_language")
        self.language_button.grid(row=0, column=6, rowspan=2, padx=6)

    def _table(self) -> None:
        grid = ttk.Frame(self.sequence_tab)
        grid.pack(fill="x", padx=10)
        self._text(ttk.Label(grid), "frame_channel").grid(row=0, column=0, padx=4)
        self._text(ttk.Label(grid), "enabled").grid(row=0, column=1, padx=4)
        self._text(ttk.Label(grid), "color").grid(row=0, column=2, padx=4)
        heading = "SunValleyBodyStrongFont" if "SunValleyBodyStrongFont" in font.names(self.root) else "TkHeadingFont"
        for c, (_, title) in enumerate(COLUMNS, start=3):
            self._text(ttk.Label(grid, justify="center", font=heading), title).grid(row=0, column=c, padx=3, pady=3)
        for i, raw in enumerate(form.default_raw(FRAMES * CHANNELS_PER_FRAME)):
            f = {key: tk.BooleanVar(value=value) if key == "enabled" else tk.StringVar(value=value)
                 for key, value in raw.items()}
            f["color"] = PALETTE[i % len(PALETTE)]
            self.forms.append(f)
            r = i + 1
            pad = (8, 0) if i % CHANNELS_PER_FRAME == 0 and i else (0, 0)  # gap between mainframes
            ttk.Label(grid, text=f"F{i // CHANNELS_PER_FRAME + 1}:CH{i % CHANNELS_PER_FRAME + 1}").grid(
                row=r, column=0, pady=pad
            )
            ttk.Checkbutton(grid, variable=f["enabled"], command=self.schedule, style="Table.TCheckbutton").grid(row=r, column=1, pady=pad)
            swatch = tk.Label(grid, bg=f["color"], width=6, relief="solid", borderwidth=1, cursor="hand2")
            swatch.grid(row=r, column=2, pady=pad, padx=4)
            swatch.bind("<Button-1>", lambda _e, f=f, s=swatch: self.pick_color(f, s))
            f["swatch"] = swatch
            for c, (key, _) in enumerate(COLUMNS, start=3):
                var = f[key]
                var.trace_add("write", lambda *_: self.schedule())
                ttk.Entry(grid, textvariable=var, width=14 if key == "name" else 9, justify="center", style="Table.TEntry").grid(
                    row=r, column=c, padx=3, pady=pad
                )

    def _buttons(self) -> None:
        bar = ttk.Frame(self.sequence_tab)
        bar.pack(fill="x", padx=10, pady=3)
        self._text(ttk.Button(bar, command=self.open_csv), "open_csv").pack(side="left")
        self._text(ttk.Button(bar, command=self.save_csv), "save_csv").pack(side="left", padx=6)
        self.run_button = self._text(ttk.Button(bar, command=self.run, style="Accent.TButton"), "run_sequence")
        self.run_button.pack(side="right")
        self.capture_check = self._text(ttk.Checkbutton(bar, variable=self.capture), "capture")
        self.capture_check.pack(side="right", padx=12)
        self._text(ttk.Checkbutton(
            bar, variable=self.hold_dc, command=self.schedule
        ), "hold_dc").pack(side="right", padx=12)

    # -- table <-> rows -------------------------------------------------------

    def read_rows(self) -> list[SeqRow]:
        return form.parse_rows([{key: f[key].get() for key in form.RAW_KEYS} for f in self.forms])

    def fill(self, rows: list[SeqRow]) -> None:
        current = [{key: f[key].get() for key in form.RAW_KEYS} for f in self.forms]
        raw = form.rows_to_raw(rows, current)
        for f, r in zip(self.forms, raw[:len(rows)]):
            for key in form.RAW_KEYS:
                f[key].set(r[key])
        for f in self.forms[len(rows):]:
            f["enabled"].set(False)

    def pick_color(self, form: dict, swatch: tk.Label) -> None:
        _, hexcolor = colorchooser.askcolor(form["color"], parent=self.root)
        if hexcolor:
            form["color"] = hexcolor
            swatch.configure(bg=hexcolor)
            self.schedule()

    def open_csv(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), (tr("all_files"), "*")])
        if path:
            try:
                self.fill(load_csv(path))
            except (ValueError, OSError) as exc:
                messagebox.showerror(tr("csv_error"), str(exc))

    def save_csv(self) -> None:
        try:
            rows = self.read_rows()
        except ValueError as exc:
            return messagebox.showerror(tr("invalid_field"), str(exc))
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            try:
                write_csv(rows, path)
            except OSError as exc:
                messagebox.showerror(tr("save_failed"), str(exc))

    # -- chart ----------------------------------------------------------------

    def schedule(self) -> None:
        if self._pending is None:
            self._pending = self.root.after(30, self.draw)

    def draw(self) -> None:
        self._pending = None
        c = self.canvas
        c.delete("all")
        try:
            rows = self.read_rows()
        except ValueError as exc:
            self.message.set(str(exc))
            return
        warnings = order_warnings(rows, TRIGGER_SKEW_SECONDS)
        self.message.set(
            "\n".join(f"⚠ {w}" for w in warnings[:3]) + (tr("more_warnings", count=len(warnings) - 3) if len(warnings) > 3 else "")
        )
        live = [(r, f) for r, f in zip(rows, self.forms) if r.enabled]
        if not live:
            return
        width, height = max(c.winfo_width(), 400), max(c.winfo_height(), 200)
        left, right, top, bottom = 72, 12, 12, 40
        hold_dc = self.hold_dc.get()
        t_end = max((r.delay + r.rise if hold_dc else r.duration) for r, _ in live) * 1.18 * 1000 or 1.0  # ms
        x = lambda ms: left + (width - left - right) * ms / t_end  # noqa: E731
        lane = (height - top - bottom) / len(live)
        # The hold label sits below the mid line; in thin lanes it would cross the next lane, so drop both time labels.
        times = lane >= 30 + int(c.tk.call("font", "metrics", "SunValleyCaptionFont", "-linespace"))

        step = _tick_step(t_end)
        for tick in range(0, int(t_end) + 1, step):
            c.create_line(x(tick), top, x(tick), height - bottom, fill="#e5e7eb")
            c.create_text(x(tick), height - bottom + 12, text=f"{tick:g}", fill="#475569", font="SunValleyBodyFont")
        c.create_text(x(t_end), height - bottom + 12, text=f"{t_end:.1f}", fill="#475569", font="SunValleyBodyFont", anchor="e")
        c.create_text((left + width) / 2, height - 10, text=tr("time_axis"), fill="#475569", font="SunValleyBodyFont")
        for i, (r, f) in enumerate(live):
            color = f["color"]
            y0 = top + i * lane
            c.create_line(left, y0, width - right, y0, fill="#d1d5db")
            lo, hi = r.low_v, max(r.high_v, r.low_v + 1e-9)
            y_hi, y_lo = y0 + 20, y0 + lane - 8
            y = lambda v: y_lo - (y_lo - y_hi) * (v - lo) / (hi - lo)  # noqa: E731
            pts = [(x(t * 1000), y(v)) for t, v in waveform(r, t_end / 1000, hold_dc)]
            c.create_line(*[p for pt in pts for p in pt], fill=color, width=2.5)
            if r.channel in self.measured:
                dt, volts = self.measured[r.channel]
                stride = max(1, len(volts) // max(width, 1))
                got = [(x(i * dt * 1000), y(volts[i])) for i in range(0, len(volts), stride) if i * dt * 1000 <= t_end]
                if len(got) > 1:
                    c.create_line(*[p for pt in got for p in pt], fill="#111827", width=1)
            if r.channel in self.measured:
                c.create_text(left - 6, y0 + lane / 2 + 14, text=tr("measured"), fill="#111827", anchor="e", font="SunValleyCaptionFont")
            c.create_text(left - 6, y0 + lane / 2, text=r.name, fill=color, anchor="e", font="SunValleyBodyStrongFont")
            arrow = dict(fill=color, dash=(4, 3), arrow="both")
            ym = (y_hi + y_lo) / 2  # delay and hold never overlap in time, so they share a line
            if r.delay > 0:
                c.create_line(x(0), ym, x(r.delay * 1000), ym, **arrow)
                if times:
                    c.create_text(x(0) + 2, ym - 8, anchor="w", fill=color, text=tr("delay_annotation", value=r.delay * 1000), font="SunValleyCaptionFont")
            t_top0, t_top1 = (r.delay + r.rise) * 1000, (r.delay + r.rise + (0 if hold_dc else r.hold)) * 1000
            if not hold_dc:
                c.create_line(x(t_top0), ym, x(t_top1), ym, **arrow)
                if times:
                    c.create_text(x((t_top0 + t_top1) / 2), ym + 9, fill=color, text=tr("hold_annotation", value=r.hold * 1000), font="SunValleyCaptionFont")
            c.create_text(
                x(t_top1) + 4, y_hi - 8, anchor="w", fill=color, font="SunValleyCaptionFont",
                text=tr("levels_annotation", high=r.high_v, low=r.low_v),
            )  # fmt: skip

    # -- connection -----------------------------------------------------------

    def _save_config(self) -> None:
        form.save_config(CONFIG, [v.get() for v in self.resources], self.demo.get(), i18n.lang)

    def test_connection(self) -> None:
        if self.busy:
            return
        self._save_config()
        self.busy = True
        targets = [v.get().strip() for v in self.resources]
        for st in self.conn_status:
            st.set("")
        threading.Thread(target=self._test, args=(targets, self.demo.get()), daemon=True).start()

    def _test(self, targets: list[str], demo: bool) -> None:
        models = []
        for i, res in enumerate(targets):
            if not res and not demo:
                self.queue.put(("status", i, tr("not_set")))
                continue
            try:
                if not demo:
                    models.append(detect_model(res))
                    if models[-1] == "E36441A":
                        self.queue.put(("status", i, "✔ E36441A"))
                        continue
                inst = N6705C.demo(DEMO_SLOTS) if demo else connect_group([res]).instruments[0]
                try:
                    self.queue.put(("status", i, f"✔ S/N {inst.identity.serial}  " + " ".join(c.model for c in inst.capabilities.values())))
                finally:
                    inst.close()
            except Exception as exc:  # noqa: BLE001 - backend errors vary
                self.queue.put(("status", i, f"✘ {str(exc).splitlines()[0][:60]}"))
                self.queue.put(("connerr", tr("frame_connection_error", number=i + 1, error=exc)))
        try:
            self.queue.put(("kind", kind_of(models)))
        except ValueError as exc:
            self.queue.put(("connerr", str(exc)))
        self.queue.put(("idle",))

    # -- run ------------------------------------------------------------------

    def run(self) -> None:
        if self.busy:
            return
        try:
            rows = self.read_rows()
        except ValueError as exc:
            return messagebox.showerror(tr("invalid_field"), str(exc))
        live = [r for r in rows if r.enabled]
        if not live:
            return messagebox.showerror(tr("no_channel"), tr("enable_channel"))
        frames = -(-max(r.channel for r in live) // CHANNELS_PER_FRAME)
        resources = [v.get().strip() for v in self.resources[:frames]]
        demo = self.demo.get()
        if not demo and not all(resources):
            return messagebox.showerror(tr("missing_address"), tr("address_required", frames=frames))
        if not messagebox.askyesno(tr("confirm"), (tr("demo_prefix") if demo else tr("hardware_prefix")) + tr("sequence_start") + (tr("sequence_keep") if self.hold_dc.get() else "") + tr("continue"), icon="warning"):
            return
        self._save_config()
        self.busy = True
        self.measured = {}
        self.run_button.state(["disabled"])
        self.off_button.state(["disabled"])
        threading.Thread(
            target=self._work, args=(rows, frames, resources, demo, self.hold_dc.get(), self.capture.get()), daemon=True
        ).start()

    def all_off(self) -> None:
        """Switch every output on every configured mainframe off, e.g. after a DC hold."""
        if self.busy:
            return
        resources = [v.get().strip() for v in self.resources if v.get().strip()]
        if self.demo.get() or not resources:
            return messagebox.showinfo(tr("all_off"), tr("off_demo"))
        if not messagebox.askyesno(tr("confirm"), tr("off_confirm", frames=len(resources)), icon="warning"):
            return
        self.busy = True
        self.run_button.state(["disabled"])
        self.off_button.state(["disabled"])
        threading.Thread(target=self._off_work, args=(resources,), daemon=True).start()

    def _off_work(self, resources: list[str]) -> None:
        try:
            if kind_of([detect_model(r) for r in resources]) == "E36441A":
                off_resources(resources)
                self.queue.put(("done", tr("off_done")))
                return
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", str(exc)))
            return
        try:
            group = connect_group(resources)
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", tr("connection_error", error=exc)))
            return
        try:
            outputs_off(group)
            self.queue.put(("done", tr("off_done")))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", str(exc)))
        finally:
            for inst in group.instruments:
                inst.close()

    def _work(self, rows: list[SeqRow], frames: int, resources: list[str], demo: bool, hold_dc: bool, capture: bool) -> None:
        if not demo:
            try:
                e36 = kind_of([detect_model(r) for r in resources]) == "E36441A"
                if e36:
                    if capture:
                        raise ValueError(tr("e36441a_no_capture"))
                    run_resources(resources, rows, hold_dc=hold_dc)
                    self.queue.put(("done", tr("sequence_hold_done") if hold_dc else tr("sequence_done")))
                    return
            except Exception as exc:  # noqa: BLE001 - run_rows restored what it touched
                self.queue.put(("error", str(exc)))
                return
        try:
            group = InstrumentGroup.demo(frames, slots=DEMO_SLOTS) if demo else connect_group(resources)
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", tr("connection_error", error=exc)))
            return
        try:
            cap: dict | None = {} if capture else None
            run_plan(group, rows, hold_dc=hold_dc, capture=cap)
            text = tr("sequence_hold_done") if hold_dc else tr("sequence_done")
            if cap and "error" in cap:
                text += tr("capture_error", error=cap.pop("error"))
            self.queue.put(("done", text, cap))
        except Exception as exc:  # noqa: BLE001 - run_plan already forced outputs off
            note = (
                "" if isinstance(exc, OutputsNotConfirmedOff)
                else tr("rejected") if isinstance(exc, PlanRejected)
                else tr("forced_off")
            )
            self.queue.put(("error", f"{exc}{note}"))
        finally:
            for inst in group.instruments:
                inst.close()

    def _poll(self) -> None:
        while not self.queue.empty():
            kind, *rest = self.queue.get()
            if kind == "status":
                self.conn_status[rest[0]].set(rest[1])
            elif kind == "kind":
                self.set_kind(rest[0])
            elif kind == "idle":
                self.busy = False
            elif kind == "connerr":
                messagebox.showerror(tr("connection_failed"), rest[0])
            else:
                self.busy = False
                self.run_button.state(["!disabled"])
                self.off_button.state(["!disabled"])
                if len(rest) > 1 and rest[1]:
                    self.measured = rest[1]
                    self.schedule()
                (messagebox.showinfo if kind == "done" else messagebox.showerror)(tr("run_result"), rest[0])
        self.root.after(100, self._poll)


def setup_theme(root: tk.Tk) -> None:
    sv_ttk.set_theme("light", root=root)
    family = "Microsoft JhengHei UI" if sys.platform == "win32" else "PingFang TC" if sys.platform == "darwin" else None
    names = ("SunValleyCaptionFont", "SunValleyBodyFont", "SunValleyBodyStrongFont", "SunValleyBodyLargeFont",
             "SunValleySubtitleFont", "SunValleyTitleFont", "SunValleyTitleLargeFont", "SunValleyDisplayFont",
             "TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont")
    for name in names:
        named = font.nametofont(name, root=root)
        if family:
            named.configure(family=family)
        size = named.cget("size")  # sv-ttk uses pixels (< 0); 1 pt is 1 px on macOS, 4/3 px at 96 dpi elsewhere
        pt = abs(size) * (0.75 if size < 0 and sys.platform != "darwin" else 1)
        named.configure(size=max(1, int(pt * FONT_SCALE + 0.5)))
    font.nametofont("SunValleyBodyStrongFont", root=root).configure(weight="bold")
    style = ttk.Style(root)
    style.configure("Danger.TButton", foreground="#dc2626", font="SunValleyBodyStrongFont")
    style.configure("Table.TEntry", padding=(6, 0, 4, 0))  # compact rows leave height for the chart
    style.configure("Table.TCheckbutton", padding=0)


def main() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    root = tk.Tk()
    setup_theme(root)
    scale = root.winfo_fpixels("1i") / 96 if sys.platform == "win32" else 1
    width = min(round(1180 * scale), root.winfo_screenwidth() - 40)
    height = min(round(900 * scale), root.winfo_screenheight() - 80)
    root.geometry(f"{width}x{height}")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
