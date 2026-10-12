"""``n6705c-seq-gui``: edit the table, see every rail's waveform, set the mainframe
addresses and run.  Plain tkinter -- no extra dependency."""

from __future__ import annotations

import json
import math
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

from n6705c.core.group import CHANNELS_PER_FRAME, TRIGGER_SKEW_SECONDS, InstrumentGroup
from n6705c.core.instrument import N6705C
from n6705c.core.transport import list_resources

from .execute import DEMO_SLOTS, connect_group, OutputsNotConfirmedOff, PlanRejected, outputs_off, run_plan
from .plan import SeqRow, load_csv, order_warnings, write_csv

FRAMES = 2
CONFIG = Path.home() / ".n6705c_seq_gui.json"
PALETTE = ["#e05252", "#4a7fe8", "#55b685", "#e8833a", "#a855f0", "#52b2d0", "#e0b43c", "#dc5090"]

# name, delay, low, high, rise, hold, fall, current limit  (ms / V / A)
DEFAULTS = [
    ("CH1", 0, 0, 12, 3, 60, 3), ("CH2", 5, 0, 5, 2, 55, 2), ("CH3", 10, 0, 3.3, 2, 50, 2),
    ("CH4", 15, 0, 1.8, 1, 45, 1), ("CH5", 8, 0, 24, 4, 65, 4), ("CH6", 20, 0, 9, 3, 40, 3),
    ("CH7", 0, 0, 5, 2, 30, 2), ("CH8", 12, 0, 12, 3, 50, 3),
]  # fmt: skip
COLUMNS = [
    ("name", "通道名稱"), ("delay", "延遲\nDelay(ms)"), ("low", "低點電壓\n(V)"),
    ("high", "高點電壓\n(V)"), ("rise", "上升時間\n(ms)"), ("hold", "維持時間\n(ms)"),
    ("fall", "下降時間\n(ms)"), ("limit", "電流限制\n(A，可空)"),
]  # fmt: skip
TITLES = {k: t.splitlines()[0] for k, t in COLUMNS}
NUMERIC = {"delay": "ms", "low": "V", "high": "V", "rise": "ms", "hold": "ms", "fall": "ms"}


def waveform(row: SeqRow, t_end: float, hold_dc: bool = False) -> list[tuple[float, float]]:
    """Corner points (seconds, volts) of one rail, from t=0 to *t_end*."""
    t1 = row.delay
    t2 = t1 + row.rise
    if hold_dc:
        return [(0, row.low_v), (t1, row.low_v), (t2, row.high_v), (t_end, row.high_v)]
    t3 = t2 + row.hold
    t4 = t3 + row.fall
    return [(0, row.low_v), (t1, row.low_v), (t2, row.high_v), (t3, row.high_v), (t4, row.low_v), (t_end, row.low_v)]


def _tick_step(span_ms: float) -> int:
    """1/2/5 x 10^k ms, at most ~10 ticks, never below 1 ms."""
    raw = max(span_ms / 10, 1)
    decade = 10 ** math.floor(math.log10(raw))
    return int(next(m * decade for m in (1, 2, 5, 10) if m * decade >= raw))


def _load_config() -> dict:
    """Saved addresses; a missing or damaged file just means the defaults."""
    try:
        saved = json.loads(CONFIG.read_text())
        resources, demo = saved["resources"], saved["demo"]
        if not (isinstance(resources, list) and all(isinstance(r, str) for r in resources)
                and isinstance(demo, bool)):
            raise TypeError("bad schema")
        return {"resources": resources, "demo": demo}
    except (OSError, ValueError, KeyError, TypeError):
        return {"resources": [], "demo": True}


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("N6705C 上電時序")
        self.queue: queue.Queue = queue.Queue()
        self.busy = False
        self._pending = None
        self.forms: list[dict] = []
        saved = _load_config()
        self.resources = [tk.StringVar(value=r) for r in (saved["resources"] + [""] * FRAMES)[:FRAMES]]
        self.demo = tk.BooleanVar(value=saved["demo"])
        self.hold_dc = tk.BooleanVar(value=False)
        self.capture = tk.BooleanVar(value=False)
        self.measured: dict = {}  # channel -> (sample interval, volts) from the last captured run
        self.conn_status = [tk.StringVar() for _ in range(FRAMES)]
        self.message = tk.StringVar()
        self._connection_bar()
        self._table()
        self._buttons()
        ttk.Label(root, textvariable=self.message, foreground="#b45309", justify="left").pack(anchor="w", padx=10)
        self.canvas = tk.Canvas(root, bg="white", height=520, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.canvas.bind("<Configure>", lambda _e: self.schedule())
        root.after(100, self._poll)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.schedule()

    def close(self) -> None:
        # The worker is a daemon thread: quitting mid-run would skip run_plan's shutdown.
        if self.busy:
            messagebox.showwarning("執行中", "時序執行中，請等它結束再關閉視窗。")
        else:
            self.root.destroy()

    # -- layout ---------------------------------------------------------------

    def _connection_bar(self) -> None:
        bar = ttk.LabelFrame(self.root, text="機台連線（VISA 位址，例：TCPIP0::192.168.1.10::inst0::INSTR 或 USB0::...）")
        bar.pack(fill="x", padx=10, pady=(10, 4))
        bar.columnconfigure(1, weight=1)
        self.boxes = []
        for i in range(FRAMES):
            ttk.Label(bar, text=f"機台 {i + 1}（CH{i * CHANNELS_PER_FRAME + 1}–{(i + 1) * CHANNELS_PER_FRAME}）").grid(
                row=i, column=0, padx=6, pady=3, sticky="w"
            )
            box = ttk.Combobox(bar, textvariable=self.resources[i])
            box.grid(row=i, column=1, sticky="ew", padx=6)
            self.boxes.append(box)
            ttk.Label(bar, textvariable=self.conn_status[i], width=34).grid(row=i, column=2, padx=6, sticky="w")
        ttk.Button(bar, text="掃描", command=self.scan).grid(row=0, column=3, padx=6)
        ttk.Button(bar, text="測試連線", command=self.test_connection).grid(row=1, column=3, padx=6)
        ttk.Checkbutton(bar, text="Demo（模擬器）", variable=self.demo).grid(row=0, column=4, rowspan=2, padx=8)

    def _table(self) -> None:
        grid = ttk.Frame(self.root)
        grid.pack(fill="x", padx=10)
        ttk.Label(grid, text="機台:通道").grid(row=0, column=0, padx=4)
        ttk.Label(grid, text="啟用").grid(row=0, column=1, padx=4)
        ttk.Label(grid, text="顏色").grid(row=0, column=2, padx=4)
        for c, (_, title) in enumerate(COLUMNS, start=3):
            ttk.Label(grid, text=title, justify="center", font=("", 11, "bold")).grid(row=0, column=c, padx=3, pady=3)
        for i in range(FRAMES * CHANNELS_PER_FRAME):
            d = DEFAULTS[i] if i < len(DEFAULTS) else (f"CH{i + 1}", 0, 0, 1, 1, 10, 1)
            form = {
                "enabled": tk.BooleanVar(value=i < len(DEFAULTS)),
                "color": PALETTE[i % len(PALETTE)],
                "name": tk.StringVar(value=d[0]),
                **{k: tk.StringVar(value=f"{v:g}") for k, v in zip(NUMERIC, d[1:])},
                "limit": tk.StringVar(),
            }
            self.forms.append(form)
            r = i + 1
            pad = (8, 0) if i % CHANNELS_PER_FRAME == 0 and i else (0, 0)  # gap between mainframes
            ttk.Label(grid, text=f"F{i // CHANNELS_PER_FRAME + 1}:CH{i % CHANNELS_PER_FRAME + 1}").grid(
                row=r, column=0, pady=pad
            )
            ttk.Checkbutton(grid, variable=form["enabled"], command=self.schedule).grid(row=r, column=1, pady=pad)
            swatch = tk.Label(grid, bg=form["color"], width=6, relief="solid", borderwidth=1, cursor="hand2")
            swatch.grid(row=r, column=2, pady=pad, padx=4)
            swatch.bind("<Button-1>", lambda _e, f=form, s=swatch: self.pick_color(f, s))
            form["swatch"] = swatch
            for c, (key, _) in enumerate(COLUMNS, start=3):
                var = form[key]
                var.trace_add("write", lambda *_: self.schedule())
                ttk.Entry(grid, textvariable=var, width=14 if key == "name" else 9, justify="center").grid(
                    row=r, column=c, padx=3, pady=pad
                )

    def _buttons(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=10, pady=6)
        ttk.Button(bar, text="開啟 CSV", command=self.open_csv).pack(side="left")
        ttk.Button(bar, text="儲存 CSV", command=self.save_csv).pack(side="left", padx=6)
        self.run_button = ttk.Button(bar, text="▶ 執行上電", command=self.run)
        self.run_button.pack(side="right")
        ttk.Checkbutton(bar, text="擷取實測波形（數位化器）", variable=self.capture).pack(side="right", padx=12)
        self.off_button = ttk.Button(bar, text="■ 全部關閉", command=self.all_off)
        self.off_button.pack(side="right", padx=(0, 12))
        ttk.Checkbutton(
            bar, text="維持高點 DC（忽略維持／下降，結束後不關輸出）", variable=self.hold_dc, command=self.schedule
        ).pack(side="right", padx=12)

    # -- table <-> rows -------------------------------------------------------

    def read_rows(self) -> list[SeqRow]:
        rows = []
        for i, f in enumerate(self.forms, start=1):
            on = f["enabled"].get()
            label = f["name"].get() or f"CH{i}"
            vals = {}
            for key, unit in NUMERIC.items():
                try:
                    v = float(f[key].get())
                    if v < 0 or not math.isfinite(v):
                        raise ValueError
                except ValueError:
                    if on:
                        raise ValueError(f"{label}：「{TITLES[key]}」不是有效的非負數字") from None
                    v = 0.0
                vals[key] = v / 1000 if unit == "ms" else v
            limit = f["limit"].get().strip()
            try:
                current = float(limit) if limit else None
                if current is not None and not (math.isfinite(current) and current > 0):
                    raise ValueError
            except ValueError:
                if on:
                    raise ValueError(f"{label}：電流限制必須是大於 0 的數字（不限制請留空）") from None
                current = None
            rows.append(
                SeqRow(i, on, label, vals["delay"], vals["low"], vals["high"], vals["rise"], vals["hold"], vals["fall"], current)
            )
        return rows

    def fill(self, rows: list[SeqRow]) -> None:
        if len(rows) > len(self.forms):
            raise ValueError(f"CSV 有 {len(rows)} 列，這個介面只有 {len(self.forms)} 個通道")
        for f, r in zip(self.forms, rows):
            f["enabled"].set(r.enabled)
            f["name"].set(r.name)
            for key, value in (("delay", r.delay * 1000), ("low", r.low_v), ("high", r.high_v),
                               ("rise", r.rise * 1000), ("hold", r.hold * 1000), ("fall", r.fall * 1000)):  # fmt: skip
                f[key].set(f"{value:.10g}")
            f["limit"].set("" if r.current_limit is None else f"{r.current_limit:g}")
        for f in self.forms[len(rows):]:
            f["enabled"].set(False)

    def pick_color(self, form: dict, swatch: tk.Label) -> None:
        _, hexcolor = colorchooser.askcolor(form["color"], parent=self.root)
        if hexcolor:
            form["color"] = hexcolor
            swatch.configure(bg=hexcolor)
            self.schedule()

    def open_csv(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("All", "*")])
        if path:
            try:
                self.fill(load_csv(path))
            except (ValueError, OSError) as exc:
                messagebox.showerror("CSV 錯誤", str(exc))

    def save_csv(self) -> None:
        try:
            rows = self.read_rows()
        except ValueError as exc:
            return messagebox.showerror("欄位錯誤", str(exc))
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            try:
                write_csv(rows, path)
            except OSError as exc:
                messagebox.showerror("儲存失敗", str(exc))

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
            "\n".join(f"⚠ {w}" for w in warnings[:3]) + (f"\n（另有 {len(warnings) - 3} 項）" if len(warnings) > 3 else "")
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

        step = _tick_step(t_end)
        for tick in range(0, int(t_end) + 1, step):
            c.create_line(x(tick), top, x(tick), height - bottom, fill="#e5e7eb")
            c.create_text(x(tick), height - bottom + 12, text=f"{tick:g}", fill="#475569")
        c.create_text(x(t_end), height - bottom + 12, text=f"{t_end:.1f}", fill="#475569")
        c.create_text((left + width) / 2, height - 10, text="Time (ms) — 所有通道共用時間軸", fill="#475569")
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
                c.create_text(left - 6, y0 + lane / 2 + 14, text="黑線＝實測", fill="#111827", anchor="e", font=("", 8))
            c.create_text(left - 6, y0 + lane / 2, text=r.name, fill=color, anchor="e", font=("", 11, "bold"))
            arrow = dict(fill=color, dash=(4, 3), arrow="both")
            ym = (y_hi + y_lo) / 2  # delay and hold never overlap in time, so they share a line
            if r.delay > 0:
                c.create_line(x(0), ym, x(r.delay * 1000), ym, **arrow)
                c.create_text(x(0) + 2, ym - 8, anchor="w", fill=color, text=f"延遲 {r.delay * 1000:.1f}ms")
            t_top0, t_top1 = (r.delay + r.rise) * 1000, (r.delay + r.rise + (0 if hold_dc else r.hold)) * 1000
            if not hold_dc:
                c.create_line(x(t_top0), ym, x(t_top1), ym, **arrow)
                c.create_text(x((t_top0 + t_top1) / 2), ym + 9, fill=color, text=f"維持 {r.hold * 1000:.1f}ms")
            c.create_text(
                x(t_top1) + 4, y_hi - 8, anchor="w", fill=color, font=("", 10, "bold"),
                text=f"高點 {r.high_v:.2f}V　低點 {r.low_v:.2f}V",
            )  # fmt: skip

    # -- connection -----------------------------------------------------------

    def _save_config(self) -> None:
        CONFIG.write_text(json.dumps({"resources": [v.get() for v in self.resources], "demo": self.demo.get()}))

    def scan(self) -> None:
        found = list_resources()
        for box in self.boxes:
            box["values"] = found
        for var, res in zip(self.resources, found):
            if not var.get():
                var.set(res)
        self.message.set(f"找到 {len(found)} 個 VISA 資源" if found else "找不到 VISA 資源，請手動輸入位址")

    def test_connection(self) -> None:
        if self.busy:
            return
        self._save_config()
        self.busy = True
        targets = [v.get().strip() for v in self.resources]
        for s in self.conn_status:
            s.set("")
        threading.Thread(target=self._test, args=(targets, self.demo.get()), daemon=True).start()

    def _test(self, targets: list[str], demo: bool) -> None:
        for i, res in enumerate(targets):
            if not res and not demo:
                self.queue.put(("status", i, "（未設定）"))
                continue
            try:
                inst = N6705C.demo(DEMO_SLOTS) if demo else connect_group([res]).instruments[0]
                try:
                    self.queue.put(("status", i, f"✔ S/N {inst.identity.serial}  " + " ".join(c.model for c in inst.capabilities.values())))
                finally:
                    inst.close()
            except Exception as exc:  # noqa: BLE001 - backend errors vary
                self.queue.put(("status", i, f"✘ {str(exc)[:60]}"))
        self.queue.put(("idle",))

    # -- run ------------------------------------------------------------------

    def run(self) -> None:
        if self.busy:
            return
        try:
            rows = self.read_rows()
        except ValueError as exc:
            return messagebox.showerror("欄位錯誤", str(exc))
        live = [r for r in rows if r.enabled]
        if not live:
            return messagebox.showerror("沒有通道", "至少要啟用一個通道")
        frames = -(-max(r.channel for r in live) // CHANNELS_PER_FRAME)
        resources = [v.get().strip() for v in self.resources[:frames]]
        demo = self.demo.get()
        if not demo and not all(resources):
            return messagebox.showerror("未設定位址", f"需要 {frames} 台機台的 VISA 位址")
        if not messagebox.askyesno("確認", ("【Demo】" if demo else "【實機】") + "將開啟輸出並執行時序" + ("，結束後輸出維持在高點不會關閉" if self.hold_dc.get() else "") + "，繼續？", icon="warning"):
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
            return messagebox.showinfo("全部關閉", "Demo 模式沒有實機；請取消 Demo 並填入機台位址。")
        if not messagebox.askyesno("確認", f"關閉 {len(resources)} 台機台的所有輸出？", icon="warning"):
            return
        self.busy = True
        self.run_button.state(["disabled"])
        self.off_button.state(["disabled"])
        threading.Thread(target=self._off_work, args=(resources,), daemon=True).start()

    def _off_work(self, resources: list[str]) -> None:
        try:
            group = connect_group(resources)
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", f"連線失敗：{exc}"))
            return
        try:
            outputs_off(group)
            self.queue.put(("done", "所有輸出已關閉（已讀回確認）。"))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", str(exc)))
        finally:
            for inst in group.instruments:
                inst.close()

    def _work(self, rows: list[SeqRow], frames: int, resources: list[str], demo: bool, hold_dc: bool, capture: bool) -> None:
        try:
            group = InstrumentGroup.demo(frames, slots=DEMO_SLOTS) if demo else connect_group(resources)
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", f"連線失敗：{exc}"))
            return
        try:
            cap: dict | None = {} if capture else None
            run_plan(group, rows, hold_dc=hold_dc, capture=cap)
            text = "時序完成，輸出維持在高點（未關閉）。" if hold_dc else "時序完成，輸出已關閉。"
            if cap and "error" in cap:
                text += f"\n實測波形擷取失敗：{cap.pop('error')}"
            self.queue.put(("done", text, cap))
        except Exception as exc:  # noqa: BLE001 - run_plan already forced outputs off
            note = (
                "" if isinstance(exc, OutputsNotConfirmedOff)
                else "\n（時序在開輸出前被拒絕）" if isinstance(exc, PlanRejected)
                else "\n（輸出已強制關閉）"
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
            elif kind == "idle":
                self.busy = False
            else:
                self.busy = False
                self.run_button.state(["!disabled"])
                self.off_button.state(["!disabled"])
                if len(rest) > 1 and rest[1]:
                    self.measured = rest[1]
                    self.schedule()
                (messagebox.showinfo if kind == "done" else messagebox.showerror)("執行結果", rest[0])
        self.root.after(100, self._poll)


def main() -> None:
    root = tk.Tk()
    root.geometry("1180x900")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
