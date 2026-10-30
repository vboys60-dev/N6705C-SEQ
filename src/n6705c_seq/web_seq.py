"""Sequence bridge, independent of the webview shell."""

from __future__ import annotations

import threading

from n6705c.core.group import CHANNELS_PER_FRAME, InstrumentGroup

from . import form
from .e36441a import detect_model, kind_of, off_resources, run_resources
from .execute import DEMO_SLOTS, OutputsNotConfirmedOff, PlanRejected, connect_group, outputs_off, run_plan
from .i18n import tr
from .plan import load_csv, write_csv


class SeqApi:
    def seq_meta(self) -> dict:
        return {"frames": form.FRAMES, "channels_per_frame": CHANNELS_PER_FRAME,
                "palette": list(form.PALETTE), "columns": [list(c) for c in form.COLUMNS],
                "rows": form.default_raw(form.FRAMES * CHANNELS_PER_FRAME)}

    def seq_preview(self, rev, rows: list[dict], hold_dc: bool) -> dict:
        if type(rev) is not int:
            raise TypeError("rev must be an int")
        self._seq_bools(hold_dc)
        try:
            view = form.seq_view(form.parse_rows(rows), hold_dc, self._measured["seq"])
        except ValueError as exc:
            view = {"message": str(exc), "lanes": [], "ticks": [], "t_end": 1.0,
                    "time_axis": tr("time_axis"), "measured_label": tr("measured")}
        return {"rev": rev, **view}

    def seq_open_csv(self, current: list[dict]) -> dict:
        try:
            form.parse_rows(current)
        except ValueError:
            pass
        current = [dict(row) for row in current]
        try:
            path = self._open_path(("CSV (*.csv)", "All files (*.*)"))
            return {"rows": form.rows_to_raw(load_csv(path), current)} if path else {"cancelled": True}
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}

    def seq_save_csv(self, rows: list[dict]) -> dict:
        try:
            parsed = form.parse_rows(rows)
        except ValueError as exc:
            return {"error": str(exc), "title": tr("invalid_field")}
        try:
            path = self._save_path(("CSV (*.csv)",), ".csv")
            if not path:
                return {"cancelled": True}
            write_csv(parsed, path)
            return {}
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}

    def seq_run(self, rows, resources: list[str], demo: bool, hold_dc: bool, capture: bool) -> dict:
        self._seq_resources(resources, demo)
        self._seq_bools(hold_dc, capture)
        try:
            rows = form.parse_rows(rows)
        except ValueError as exc:
            return {"error": str(exc), "title": tr("invalid_field")}
        live = [r for r in rows if r.enabled]
        if not live:
            return {"error": tr("enable_channel"), "title": tr("no_channel")}
        frames = -(-max(r.channel for r in live) // CHANNELS_PER_FRAME)
        saved = list(resources)
        targets = [r.strip() for r in resources[:frames]]
        if not demo and (len(targets) < frames or not all(targets)):
            return {"error": tr("address_required", frames=frames), "title": tr("missing_address")}
        text = ((tr("demo_prefix") if demo else tr("hardware_prefix")) + tr("sequence_start")
                + (tr("sequence_keep") if hold_dc else "") + tr("continue"))
        return self._seq_start(self._work, (rows, frames, targets, demo, hold_dc, capture), saved, demo, text)

    def test_connection(self, resources: list[str], demo: bool) -> dict:
        self._seq_resources(resources, demo)
        saved = list(resources)
        targets = [r.strip() for r in resources]
        return self._seq_start(self._test, (targets, demo), saved, demo)

    def all_off(self, resources: list[str], demo: bool) -> dict:
        self._seq_resources(resources, demo)
        targets = [r.strip() for r in resources if r.strip()]
        if demo or not targets:
            return {"info": tr("off_demo")}
        return self._seq_start(self._off_work, (targets,), None, demo, tr("off_confirm", frames=len(targets)))

    def _seq_bools(self, *values):
        if any(not isinstance(value, bool) for value in values):
            raise TypeError("flags must be bool")

    def _seq_resources(self, resources, demo):
        if not isinstance(resources, list) or any(not isinstance(r, str) for r in resources):
            raise TypeError("resources must be a list of strings")
        self._seq_bools(demo)

    def _seq_start(self, target, args, resources, demo, confirmation=None):
        token = self._reserve("power")
        if token is None:
            return self._power_denied()
        try:
            if confirmation is not None and not self._confirm(tr("confirm"), confirmation):
                self._release(token)
                return {"cancelled": True}
            if resources is not None:
                self._save_config(resources, demo)
            if target == self._work:
                self._measured["seq"] = {}
                self._emit(["measured", "seq"])
            threading.Thread(target=target, args=(*args, token), daemon=True).start()
        except Exception as exc:
            self._release(token)
            return {"error": str(exc)}
        return {}

    def _seq_result(self, kind, text, title=None):
        self._emit(["result", kind, title or tr("run_result"), text])

    def _test(self, targets, demo, token):
        try:
            models = []
            for i, res in enumerate(targets):
                self._finishing(token)
                self._emit(["status", i, ""])
                if not res and not demo:
                    self._emit(["status", i, tr("not_set")])
                    continue
                try:
                    if not demo:
                        models.append(detect_model(res))
                        if models[-1] == "E36441A":
                            self._emit(["status", i, "✔ E36441A"])
                            continue
                    group = InstrumentGroup.demo(frames=1, slots=DEMO_SLOTS) if demo else connect_group([res])
                    try:
                        inst = group.instruments[0]
                        self._emit(["status", i, f"✔ S/N {inst.identity.serial}  "
                                    + " ".join(c.model for c in inst.capabilities.values())])
                    finally:
                        self._seq_close(group)
                except Exception as exc:
                    self._emit(["status", i, f"✘ {str(exc).splitlines()[0][:60] if str(exc) else ''}"])
                    self._seq_result("error", tr("frame_connection_error", number=i + 1, error=exc), tr("connection_failed"))
            try:
                self._kind = kind_of(models)
                self._emit(["kind", self._kind])
            except ValueError as exc:
                self._seq_result("error", str(exc), tr("connection_failed"))
        finally:
            self._release(token)

    def _off_work(self, resources, token):
        self._finishing(token)
        try:
            try:
                if kind_of([detect_model(r) for r in resources]) == "E36441A":
                    off_resources(resources)
                    self._seq_result("info", tr("off_done"))
                    return
            except Exception as exc:
                self._seq_result("error", str(exc))
                return
            try:
                group = connect_group(resources)
            except Exception as exc:
                self._seq_result("error", tr("connection_error", error=exc))
                return
            try:
                outputs_off(group)
                self._seq_result("info", tr("off_done"))
            except Exception as exc:
                self._seq_result("error", str(exc))
            finally:
                self._finishing(token)
                self._seq_close(group)
        finally:
            self._release(token)

    def _work(self, rows, frames, resources, demo, hold_dc, capture, token):
        try:
            self._finishing(token)
            if not demo:
                try:
                    if kind_of([detect_model(r) for r in resources]) == "E36441A":
                        if capture:
                            raise ValueError(tr("e36441a_no_capture"))
                        if not self._untimed(token):
                            return
                        try:
                            run_resources(resources, rows, hold_dc=hold_dc,
                                          on_cleanup=lambda: self._finishing(token))
                        finally:
                            self._finishing(token)
                        self._seq_result("info", tr("sequence_hold_done") if hold_dc else tr("sequence_done"))
                        return
                except Exception as exc:
                    self._seq_result("error", str(exc))
                    return
            try:
                group = InstrumentGroup.demo(frames=frames, slots=DEMO_SLOTS) if demo else connect_group(resources)
            except Exception as exc:
                self._seq_result("error", tr("connection_error", error=exc))
                return
            try:
                cap = {} if capture else None
                if not self._untimed(token):
                    return
                try:
                    run_plan(group, rows, hold_dc=hold_dc, capture=cap,
                             on_cleanup=lambda: self._finishing(token))
                finally:
                    self._finishing(token)
                text = tr("sequence_hold_done") if hold_dc else tr("sequence_done")
                if cap and "error" in cap:
                    text += tr("capture_error", error=cap.pop("error"))
                if cap:
                    self._measured["seq"] = cap
                    self._emit(["measured", "seq"])
                self._seq_result("info", text)
            except Exception as exc:
                note = ("" if isinstance(exc, OutputsNotConfirmedOff)
                        else tr("rejected") if isinstance(exc, PlanRejected) else tr("forced_off"))
                self._seq_result("error", f"{exc}{note}")
            finally:
                self._finishing(token)
                self._seq_close(group)
        finally:
            self._release(token)

    def _seq_close(self, group):
        for inst in group.instruments:
            try:
                inst.close()
            except Exception as exc:
                self._seq_result("error", str(exc))
