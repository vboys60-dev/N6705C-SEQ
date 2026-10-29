"""core 層的 legacy 檢查：原 test_dlog_server.py 的 9 項內容，直接呼叫 analysis.core，不起 HTTP server。

原檔的 HTTP 專屬檢查（Host／token、路由、參數 400、413）不在這裡，歸 T4 的 test_http.py。
原檔的 1GB BIG_FILE 常數從未被 9 項檢查使用，未搬；大檔測試歸 T3（環境變數啟用）。
"""
import datetime
import struct

import numpy as np
import pytest

from fixtures.make_big import make_big
from n6705c_seq.analysis import core

MAX_LIMIT = 0.0001
NOW = datetime.datetime(2026, 10, 29, 12, 0, 0)
WORD = {"V": "Voltage", "I": "Current", "P": "Power"}
ALL12 = [(k, c) for c in (1, 2, 3, 4) for k in ("V", "I")] + [("P", c) for c in (1, 2, 3, 4)]


def _open(path, cache_root):
    meta, _blob = core.open_dlog(path, cache_root)
    return meta, core.get_handle(meta["handle"], cache_root)


def _cols(entry, specs):
    """與 HTTP 層相同的欄位組裝：P 需要 V、I 都有記錄，否則整欄留空。"""
    recorded = {tuple(f) for f in entry["meta"]["fields"]}
    out = []
    for kind, ch in specs:
        needs = ("V", "I") if kind == "P" else (kind,)
        out.append({
            "entry": entry, "kind": kind, "ch": ch,
            "label": "File1 Instrument A Channel %d %s Avg" % (ch, WORD[kind]),
            "missing": any((ch, k) not in recorded for k in needs),
        })
    return out


def _csv_lines(entry, specs, lo, hi, offset=False, max_limit=0.0):
    raw = b"".join(core.iter_csv(entry, _cols(entry, specs), lo, hi, offset, max_limit, NOW))
    return raw.decode("utf-8").split("\r\n")


def test_open_metadata_and_lod_layout(default_dlog, cache_root):
    meta, blob = core.open_dlog(default_dlog, cache_root)
    assert meta["tint"] == 0.00098303996
    assert meta["nRecords"] == 81380
    assert meta["activeChannels"] == [1, 2, 3, 4]
    assert meta["fields"] == [[1, "V"], [1, "I"], [2, "V"], [2, "I"],
                              [3, "V"], [3, "I"], [4, "V"], [4, "I"]]
    assert meta["lodFactors"] == {"level0": 64, "level1": 4096}

    # LOD：4 聲道 × 3 種類 × 2 層 × (min,max) = 48 段，長度要與 blob 對得起來
    assert len(meta["lodLayout"]) == 4 * 3 * 2 * 2
    last = meta["lodLayout"][-1]
    assert last["offset"] + last["length"] * 4 == len(blob)
    l0 = next(e for e in meta["lodLayout"] if e["key"] == "V1" and e["level"] == 0 and e["which"] == "min")
    assert l0["length"] == -(-81380 // 64) == 1272

    # blob 切出來的 level0 min 要與 core 直接讀快取的一致
    entry = core.get_handle(meta["handle"], cache_root)
    got = np.frombuffer(blob, dtype="<f4", count=l0["length"], offset=l0["offset"])
    assert np.array_equal(got, entry["lod"]["V1_l0min"]), "blob 內的 LOD 數值與快取不符"


def test_same_content_is_cache_hit(default_dlog, cache_root):
    meta1, _ = core.open_dlog(default_dlog, cache_root)
    meta_file = cache_root / meta1["handle"] / "meta.json"
    stamp = meta_file.stat().st_mtime_ns
    meta2, _ = core.open_dlog(default_dlog, cache_root)
    assert meta2["handle"] == meta1["handle"]
    assert meta_file.stat().st_mtime_ns >= stamp, "快取命中應更新 LRU 時間"


def test_values_slice_shape(default_dlog, cache_root):
    _, entry = _open(default_dlog, cache_root)
    v1 = core.get_values(entry, "V", 1, 100, 1099, False, 0.0)
    assert v1.dtype == np.float32 and v1.size == 1000


def test_per_sample_buckets_equal_raw_values(default_dlog, cache_root):
    _, entry = _open(default_dlog, cache_root)
    v1 = core.get_values(entry, "V", 1, 100, 1099, False, 0.0)
    t, mn, mx = core.compute_buckets(entry, "V", 1, 100, 1099, 1800, False, 0.0)
    assert t.size == 1000
    assert np.array_equal(mn.astype(np.float32), v1), "逐筆路徑應等於原始值"
    assert np.array_equal(mx.astype(np.float32), v1)


def test_stats_match_numpy(default_dlog, cache_root):
    _, entry = _open(default_dlog, cache_root)
    st = core.compute_stats(entry, "V", 1, 0, 81379, False, 0.0)
    ref = core.get_values(entry, "V", 1, 0, 81379, False, 0.0).astype(np.float64)
    assert abs(st["min"] - ref.min()) < 1e-12
    assert abs(st["max"] - ref.max()) < 1e-12
    assert abs(st["avg"] - ref.mean()) < 1e-9


def test_offset_is_deterministic_across_slices(default_dlog, cache_root):
    _, entry = _open(default_dlog, cache_root)
    a = core.get_values(entry, "I", 1, 0, 4999, True, MAX_LIMIT)
    b = core.get_values(entry, "I", 1, 2000, 6999, True, MAX_LIMIT)
    assert np.array_equal(a[2000:5000], b[0:3000]), "同索引在不同切片必須得到相同校正值"
    assert (a > 0).all(), "校正後電流不得有 <=0"


def test_negative_current_never_escapes(default_dlog, cache_root):
    """只校正電流；電壓負值保留（真實物理現象）。P=V×I 在 V<0 的樣本上仍可為負，這是刻意行為。"""
    _, entry = _open(default_dlog, cache_root)
    for ch in (1, 2, 3, 4):
        st_i = core.compute_stats(entry, "I", ch, 0, 81379, True, MAX_LIMIT)
        assert st_i["min"] > 0, "電流校正後仍有非正值：%r" % st_i
        st_p = core.compute_stats(entry, "P", ch, 0, 81379, True, MAX_LIMIT)
        st_v = core.compute_stats(entry, "V", ch, 0, 81379, True, MAX_LIMIT)
        if st_p["min"] < 0:
            assert st_v["min"] < 0, "CH%d 功率為負但電壓全正，代表電流校正沒生效" % ch
        _t, mn, _mx = core.compute_buckets(entry, "I", ch, 0, 81379, 1800, True, MAX_LIMIT)
        assert (mn > 0).all(), "I%d 分桶後仍有非正值" % ch


def test_csv_12_columns_no_negative_current(default_dlog, cache_root):
    """CSV 12 欄 × 三種範圍全掃：電流欄不得有負號；電壓負值筆數與原始資料相同。"""
    _, entry = _open(default_dlog, cache_root)

    def scan(lo, hi):
        lines = _csv_lines(entry, ALL12, lo, hi, offset=True, max_limit=MAX_LIMIT)
        assert len(lines[3].split(",")) == 13, lines[3]
        rows = [l for l in lines[4:] if l]
        assert len(rows) == hi - lo + 1
        v_neg = [0, 0, 0, 0]
        for line in rows:
            cell = line.split(",")
            for k in range(4):
                volt, curr, power = cell[1 + 2 * k], cell[2 + 2 * k], cell[9 + k]
                assert not curr.startswith("-"), "CH%d 電流欄出現負號：%s" % (k + 1, line)
                if volt.startswith("-"):
                    v_neg[k] += 1
                else:
                    assert not power.startswith("-"), "CH%d 電壓為正但功率為負：%s" % (k + 1, line)
        return v_neg

    scan(20000, 25000)
    scan(40000, 60000)
    v_neg_all = scan(0, 81379)

    for ch in (1, 2, 3, 4):
        ref = int((np.asarray(entry["V"][ch]) < 0).sum())
        assert v_neg_all[ch - 1] == ref, "CH%d 匯出的電壓負值筆數與原始資料不符" % ch
    assert sum(v_neg_all) > 0, "整份檔案都掃不到電壓負值，這份測試資料無法驗證此項"


def test_csv_three_columns_header_and_rows(default_dlog, cache_root):
    _, entry = _open(default_dlog, cache_root)
    lines = _csv_lines(entry, [("V", 1), ("I", 1), ("P", 1)], 0, 2000, offset=True, max_limit=MAX_LIMIT)
    assert lines[0] == "14585A Exported Waveform"
    assert lines[1] == "Sampling Period:0.00098303996"
    assert lines[2] == "Date:" + core.ctime_like(NOW)
    assert lines[3] == ("Time,File1 Instrument A Channel 1 Voltage Avg,"
                        "File1 Instrument A Channel 1 Current Avg,"
                        "File1 Instrument A Channel 1 Power Avg")
    assert len([l for l in lines[4:] if l]) == 2001


def test_bad_input_and_unknown_handle(cache_root, tmp_path):
    bad = tmp_path / "bad.dlog"
    bad.write_bytes(b"not a dlog file")
    with pytest.raises(core.DlogFormatError):
        core.open_dlog(bad, cache_root)
    with pytest.raises(KeyError):
        core.get_handle("nope", cache_root)


def test_out_of_range_stats_clamp(default_dlog, cache_root):
    _, entry = _open(default_dlog, cache_root)
    st = core.compute_stats(entry, "V", 1, 99999999, 99999999, False, 0.0)
    assert st["min"] is not None


def test_trailer_sorted_before_main(tmp_path, cache_root):
    path = make_big(tmp_path / "trailer.dlog", 20, channels=2, trailer=True)
    meta, entry = _open(path, cache_root)
    assert meta["nRecords"] == 25 and meta["activeChannels"] == [1, 2]
    v1 = core.get_values(entry, "V", 1, 0, 24, False, 0.0)
    assert v1[0] < 0 and v1[5] > 0, "trailer 應排在主資料之前"
    vneg = core.get_values(entry, "V", 1, 0, 24, True, MAX_LIMIT)
    assert (vneg[:5] < 0).all(), "電壓負值不該被校正"


def test_partial_channel_and_nan_excluded(cache_root, tmp_path):
    # CH1 記錄 V+I、CH2 只記錄 V；CH1 電壓第 2 筆是 NaN
    hdr = ("<dlog>\n<tint>0.001</tint>\n"
           '<channel id="1"><sense_volt>1</sense_volt><sense_curr>1</sense_curr></channel>\n'
           '<channel id="2"><sense_volt>1</sense_volt><sense_curr>0</sense_curr></channel>\n'
           "</dlog>\n").encode("utf-8")
    recs = [(1.0, 0.5, 3.0), (float("nan"), 0.5, 3.0), (3.0, 0.5, 3.0)]
    part = hdr + struct.pack(">Q", 3) + b"".join(struct.pack(">fff", *r) for r in recs)
    path = tmp_path / "partial.dlog"
    path.write_bytes(part)
    _meta, _blob = core.open_dlog(path, cache_root)
    entry = core.get_handle(_meta["handle"], cache_root)

    assert core.compute_stats(entry, "V", 1, 0, 2, False, 0.0) == {"min": 1.0, "max": 3.0, "avg": 2.0}
    lines = _csv_lines(entry, [("V", 1), ("I", 1), ("P", 1), ("V", 2), ("I", 2), ("P", 2)], 0, 2)
    expect = ["Time"] + ["File1 Instrument A Channel %s %s Avg" % (ch, w) for ch, w in
                         [(1, "Voltage"), (1, "Current"), (1, "Power"),
                          (2, "Voltage"), (2, "Current"), (2, "Power")]]
    assert lines[3].split(",") == expect, lines[3]
    row = lines[4].split(",")
    assert len(row) == 7 and row[4] == "3.000000000" and row[5] == row[6] == "", row


def test_offset_keeps_nan_and_neg_inf():
    neg = core.apply_current_offset(np.array([np.nan, -np.inf, -1.0, 2.0], dtype=np.float32), 0, MAX_LIMIT)
    assert np.isnan(neg[0]) and neg[1] == -np.inf and 0 < neg[2] <= MAX_LIMIT and neg[3] == 2.0, neg
