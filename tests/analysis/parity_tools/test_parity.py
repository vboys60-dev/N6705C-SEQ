"""Parity（契約 §8）：Python analysis.core 對 原版 JS（original_before_python.html）。

- JS 端一律從原版 HTML 抽（extract_original_js.py），不從新版 Viewer 抽。
- 需要 node；沒有 node 的 JS 對拍 skip（CI 有裝 node 20）。
- 大檔 74445 需環境變數 N6705C_PARITY_74445=<1GB .dlog 路徑>，未設則 skip。
- 原版 HTML 任何時候不得改（sha1 = contracts.ORIGINAL_HTML_SHA1），不得刪；它是唯一的 JS 對拍基準。
"""
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import dump_py
from adapter import open_entry
from fixtures.make_big import make_big
from n6705c_seq.analysis import core
from n6705c_seq.analysis.contracts import ORIGINAL_HTML_SHA1, STATS_AVG_RTOL

HERE = Path(__file__).resolve().parent
DEFAULT = HERE.parent / "fixtures" / "default_20260803_092740.dlog"
ORIGINAL = HERE / "original_before_python.html"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node 未安裝，略過 JS 對拍")


def _run(args, env=None):
    r = subprocess.run([str(a) for a in args], capture_output=True, text=True, env=env)
    return r


@pytest.fixture(scope="module")
def js_src(tmp_path_factory):
    d = tmp_path_factory.mktemp("js_src")
    r = _run([sys.executable, HERE / "extract_original_js.py", d])
    assert r.returncode == 0, r.stdout + r.stderr
    return d


def _node(script, js_src, *args) -> str:
    env = {**os.environ, "PARITY_SRC": str(js_src)}
    r = _run(["node", HERE / script, *args], env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def _compare_files(js_prefix, py_prefix):
    r = _run([sys.executable, HERE / "compare.py", js_prefix, py_prefix])
    assert r.returncode == 0, r.stdout + r.stderr


def test_original_html_sha1_unchanged():
    assert hashlib.sha1(ORIGINAL.read_bytes()).hexdigest() == ORIGINAL_HTML_SHA1


@needs_node
@pytest.mark.parametrize("case", ["default", "trailer", "mono"])
def test_parse_and_lod_bytes(case, js_src, tmp_path):
    if case == "default":
        dlog = DEFAULT
    elif case == "trailer":
        dlog = make_big(tmp_path / "trailer.dlog", 20, channels=2, trailer=True)
    else:
        dlog = make_big(tmp_path / "mono.dlog", 3000, channels=1)
    js_prefix, py_prefix = tmp_path / "js", tmp_path / "py"

    js_meta = json.loads(_node("run_worker.js", js_src, dlog, js_prefix))
    # 原版 JS 的表示法：fields 為 {ch, kind} 物件，未設 sense_minmax 時 minmax 為 undefined（JSON null）。
    # 正規化成契約的 [[ch, "V"], ...] 與 bool 後再比；數值一致性由下方 .f32 逐位元組比對保證。
    js_meta = dict(js_meta,
                   fields=[[f["ch"], f["kind"]] for f in js_meta["fields"]],
                   minmax=bool(js_meta["minmax"]))
    py_meta = dump_py.dump(dlog, py_prefix, tmp_path / "cache")
    assert py_meta == js_meta, "解析結構（tint/nRecords/fields/…）不一致"
    _compare_files(js_prefix, py_prefix)


@needs_node
def test_buckets_default_cases(js_src, tmp_path):
    _node("run_buckets.js", js_src, DEFAULT, tmp_path / "jsb", HERE / "cases_default.json")
    r = _run([sys.executable, HERE / "compare_buckets.py", DEFAULT, tmp_path / "jsb", tmp_path / "cache"])
    assert r.returncode == 0, r.stdout + r.stderr


@needs_node
def test_buckets_74445_cases(js_src, tmp_path):
    big = os.environ.get("N6705C_PARITY_74445")
    if not big:
        pytest.skip("設定 N6705C_PARITY_74445=<1GB .dlog 路徑> 才跑")
    _node("run_buckets.js", js_src, big, tmp_path / "jsb", HERE / "cases_74445.json")
    r = _run([sys.executable, HERE / "compare_buckets.py", big, tmp_path / "jsb", tmp_path / "cache"])
    assert r.returncode == 0, r.stdout + r.stderr


def _ref_stats(entry, kind, ch, lo, hi):
    v = np.asarray(entry["V"][ch][lo:hi + 1], dtype=np.float64)
    i = np.asarray(entry["I"][ch][lo:hi + 1], dtype=np.float64)
    x = {"V": v, "I": i, "P": v * i}[kind]
    x = x[np.isfinite(x)]
    return x.min(), x.max(), x.mean()


@pytest.mark.parametrize("lo,hi", [(0, 81379), (5000, 25000)])
def test_stats_against_numpy_reference(lo, hi, tmp_path):
    _meta, entry = open_entry(DEFAULT, tmp_path / "cache")
    for ch in entry["meta"]["activeChannels"]:
        for kind in "VIP":
            st = core.compute_stats(entry, kind, ch, lo, hi, False, 0.0)
            mn, mx, avg = _ref_stats(entry, kind, ch, lo, hi)
            assert st["min"] == mn and st["max"] == mx, (kind, ch)
            assert np.isclose(st["avg"], avg, rtol=STATS_AVG_RTOL, atol=0.0), (kind, ch)


@needs_node
def test_csv_number_format_matches_js(js_src, tmp_path):
    out = tmp_path / "js_fmt.txt"
    _node("fmt_check.js", js_src, DEFAULT, out)
    js_lines = out.read_text(encoding="utf-8").split("\n")

    _meta, entry = open_entry(DEFAULT, tmp_path / "cache")
    cols = [{"entry": entry, "kind": k, "ch": 3, "label": "x", "missing": False} for k in "VIP"]
    raw = b"".join(core.iter_csv(entry, cols, 0, len(js_lines) - 1, False, 0.0,
                                 datetime.datetime(2026, 10, 29)))
    py_lines = [l for l in raw.decode("utf-8").split("\r\n")[4:] if l]
    assert len(py_lines) == len(js_lines) == 20000
    bad = [i for i, (a, b) in enumerate(zip(js_lines, py_lines)) if a != b]
    assert not bad, "首個不一致列 %d：js=%r py=%r" % (bad[0], js_lines[bad[0]], py_lines[bad[0]])
