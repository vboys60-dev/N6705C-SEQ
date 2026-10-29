"""List editor bridge and execution worker."""

from __future__ import annotations

import threading

from n6705c.core.group import CHANNELS_PER_FRAME, InstrumentGroup

from . import e36441a, execute, list_form, synth
from .i18n import tr
from .listplan import ChannelList, load_json, write_json


def _typed(value, kind, name):
    if type(value) is not kind:
        raise TypeError(f"{name} must be {kind.__name__}")


def _plan(raw):
    _typed(raw, dict, "plan")
    for key, kind in (("repeat", int), ("hold_last", bool), ("channels", list)):
        if key in raw:
            _typed(raw[key], kind, key)
    if raw.get("dwell") is not None and type(raw["dwell"]) not in (int, float):
        raise TypeError("dwell must be a number or null")
    for ch in raw.get("channels", []):
        _typed(ch, dict, "channel")
        for key, kind in (("channel", int), ("enabled", bool), ("label", str), ("segments", list)):
            if key in ch:
                _typed(ch[key], kind, key)
        for key in ("delay", "current_limit"):
            if key in ch and not (key == "current_limit" and ch[key] is None) and type(ch[key]) not in (int, float):
                raise TypeError(f"{key} must be a number")
        for seg in ch.get("segments", []):
            _typed(seg, dict, "segment")
            for key, kind in (("shape", str), ("params", dict), ("repeat", int)):
                if key in seg:
                    _typed(seg[key], kind, key)
            if any(type(v) not in (int, float) for v in seg.get("params", {}).values()):
                raise TypeError("segment parameters must be numbers")
    plan = list_form.plan_from_dict(raw)
    if any(ch.channel > 2 * CHANNELS_PER_FRAME for ch in plan.channels):
        raise ValueError(tr("capacity", capacity=2 * CHANNELS_PER_FRAME))
    return plan


def _settings(plan):
    ch = plan.channels[0] if plan.channels else ChannelList(1, enabled=False)
    fmt = list_form.format_number
    return {"channel": ch.channel, "enabled": ch.enabled, "label": ch.label,
            "delay": fmt(ch.delay), "limit": "" if ch.current_limit is None else fmt(ch.current_limit),
            "repeat": str(plan.repeat), "hold_last": plan.hold_last,
            "dwell_mode": "auto" if plan.dwell is None else "manual",
            "dwell": "0.00001024" if plan.dwell is None else fmt(plan.dwell)}


class ListApi:
    def list_meta(self) -> dict:
        return list_form.meta()

    def list_draft(self, rev, shape, params: dict, repeat: str) -> dict:
        _typed(rev, int, "rev")
        return {"rev": rev, **list_form.draft_view(shape, params, repeat)}

    def list_compile(self, rev, plan: dict, settings: dict, segment_index) -> dict:
        _typed(rev, int, "rev")
        if segment_index is not None:
            _typed(segment_index, int, "segment_index")
        try:
            plan = _plan(plan)
            view = list_form.compiled_view(plan, settings, segment_index, self._measured["list"])
            if view["plan"] is None:
                view["error"] = view["message"]
            return {"rev": rev, **view}
        except ValueError as exc:
            return {"rev": rev, "error": str(exc)}

    def list_open_json(self) -> dict:
        path = self._open_path(("JSON (*.json)", "All files (*.*)"))
        if not path:
            return {"cancelled": True}
        try:
            plan = load_json(path)
            if any(ch.channel > 2 * CHANNELS_PER_FRAME for ch in plan.channels):
                raise ValueError(tr("capacity", capacity=2 * CHANNELS_PER_FRAME))
        except ValueError as exc:
            return {"error": str(exc)}
        self._measured["list"] = {}
        return {"plan": list_form.plan_to_dict(plan), "settings": _settings(plan)}

    def list_save_json(self, plan: dict, settings: dict) -> dict:
        try:
            plan = list_form.apply_settings(_plan(plan), list_form.parse_settings(settings))
            path = self._save_path(("JSON (*.json)",), ".json")
            if not path:
                return {"cancelled": True}
            write_json(plan, path)
        except ValueError as exc:
            return {"error": str(exc)}
        return {}

    def list_run(self, plan: dict, settings: dict, resources, demo, capture) -> dict:
        _typed(resources, list, "resources")
        for resource in resources:
            _typed(resource, str, "resource")
        _typed(demo, bool, "demo")
        _typed(capture, bool, "capture")
        try:
            plan = list_form.apply_settings(_plan(plan), list_form.parse_settings(settings))
            compiled = synth.compile_plan(plan)
            if compiled.errors:
                return {"error": "\n".join(compiled.errors), "title": tr("invalid_field")}
            frames = -(-max(compiled.levels) // CHANNELS_PER_FRAME)
            saved_resources = list(resources)
            resources = [r.strip() for r in resources[:frames]]
            if not demo and (len(resources) < frames or not all(resources)):
                return {"error": tr("address_required", frames=frames), "title": tr("missing_address")}
        except ValueError as exc:
            return {"error": str(exc), "title": tr("invalid_field")}
        token = self._reserve("power")
        if token is None:
            return self._power_denied()
        try:
            text = (tr("demo_prefix") if demo else tr("hardware_prefix")) + tr("list_start")
            if plan.hold_last:
                text += tr("list_keep")
            if not self._confirm(tr("confirm"), text + tr("continue")):
                self._release(token)
                return {"cancelled": True}
            self._save_config(saved_resources, demo)
            self._measured["list"] = {}
            self._emit(["measured", "list"])
            threading.Thread(target=self._list_work, args=(plan, compiled, frames, resources, demo, capture, token), daemon=True).start()
        except Exception as exc:
            self._release(token)
            return {"error": str(exc)}
        return {}

    def _list_work(self, plan, compiled, frames, resources, demo, capture, token):
        group = None
        try:
            try:
                if not demo and e36441a.kind_of([e36441a.detect_model(r) for r in resources]) == "E36441A":
                    raise ValueError(tr("e36441a_no_list"))
            except Exception as exc:
                self._emit(["result", "error", tr("run_result"), str(exc)])
                return
            try:
                group = InstrumentGroup.demo(frames=frames, slots=execute.DEMO_SLOTS) if demo else execute.connect_group(resources)
            except Exception as exc:
                self._emit(["result", "error", tr("run_result"), tr("connection_error", error=exc)])
                return
            try:
                functions = synth.to_functions(plan, compiled)
            except ValueError as exc:
                raise execute.PlanRejected(str(exc)) from exc
            cap = {} if capture else None
            execute.run_functions(group, functions, keep_on=plan.hold_last, capture=cap)
            text = tr("list_hold_done") if plan.hold_last else tr("list_done")
            if cap and "error" in cap:
                text += tr("capture_error", error=cap.pop("error"))
            if cap:
                self._measured["list"] = cap
                self._emit(["measured", "list"])
            self._emit(["result", "info", tr("run_result"), text])
        except Exception as exc:
            note = ("" if isinstance(exc, execute.OutputsNotConfirmedOff)
                    else tr("rejected") if isinstance(exc, execute.PlanRejected)
                    else tr("forced_off"))
            self._emit(["result", "error", tr("run_result"), f"{exc}{note}"])
        finally:
            try:
                self._finishing(token)
                if group is not None:
                    for inst in group.instruments:
                        try:
                            inst.close()
                        except Exception as exc:
                            self._emit(["result", "error", tr("run_result"), str(exc)])
            finally:
                self._release(token)
