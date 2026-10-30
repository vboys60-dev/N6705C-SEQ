"""
可執行自我檢查（非測試框架）：起一個測試用 server，實際打 HTTP 驗證各端點。
python3 server/test_dlog_server.py
"""
import http.client
import json
import shutil
import struct
import sys
import threading
import urllib.parse
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import dlog_core  # noqa: E402
import dlog_server  # noqa: E402

ROOT = Path(__file__).parent.parent
TEST_FILE = ROOT / "default_20260803_092740.dlog"
BIG_FILE = ROOT / "74445_PS_SLC_1GB_Z790_2242_1024GB_20260728_10h41m10s.dlog"
MAX_LIMIT = 0.0001

_port = None


def make_synth_trailer_bytes() -> bytes:
    header = (
        "<dlog>\n<tint>0.001</tint><time>1.0</time>\n"
        '<channel id="1"><sense_volt>1</sense_volt><sense_curr>1</sense_curr><model>TESTA</model></channel>\n'
        '<channel id="2"><sense_volt>1</sense_volt><sense_curr>1</sense_curr><model>TESTB</model></channel>\n'
        "</dlog>\n"
    ).encode("utf-8")
    n_main, n_trailer = 20, 5

    def record(base):
        return struct.pack(">ffff", base + 0.1, base + 0.2, base + 0.3, base + 0.4)

    main_data = b"".join(record(1000.0 + i * 10) for i in range(n_main))
    trailer_data = b"".join(record(-1000.0 - i * 10) for i in range(n_trailer))
    return header + struct.pack(">Q", n_main) + main_data + trailer_data


def _split_binary(raw):
    """[4-byte meta 長度][JSON metadata][Float32 payload]"""
    meta_len = struct.unpack_from("<I", raw, 0)[0]
    meta = json.loads(raw[4:4 + meta_len])
    return meta, raw[4 + meta_len:]


def post_open(data, file_name):
    conn = http.client.HTTPConnection("127.0.0.1", _port)
    conn.request("POST", "/open", body=data,
                 headers={"X-File-Name": file_name, "Content-Type": "application/octet-stream"})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    if resp.status != 200:
        return resp.status, json.loads(raw), None
    meta, payload = _split_binary(raw)
    return resp.status, meta, payload


def get(path, **params):
    qs = urllib.parse.urlencode(params)
    conn = http.client.HTTPConnection("127.0.0.1", _port)
    conn.request("GET", path + "?" + qs)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


def get_json(path, **params):
    status, raw = get(path, **params)
    return status, json.loads(raw)


def get_binary(path, **params):
    status, raw = get(path, **params)
    if status != 200:
        return status, json.loads(raw), None
    meta, payload = _split_binary(raw)
    return status, meta, payload


def slice_f32(payload, offset, length):
    return np.frombuffer(payload, dtype="<f4", count=length, offset=offset)


def main():
    global _port
    cache_root = Path(__file__).parent / ".cache_test"
    if cache_root.exists():
        shutil.rmtree(cache_root)
    dlog_server.CACHE_ROOT = cache_root
    dlog_core._open_cache.clear()

    httpd = dlog_server.ThreadingHTTPServer(("127.0.0.1", 0), dlog_server.make_handler())
    _port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    try:
        data = TEST_FILE.read_bytes()
        status, meta, lod_blob = post_open(data, TEST_FILE.name)
        assert status == 200, meta
        assert meta["tint"] == 0.00098303996, meta["tint"]
        assert meta["nRecords"] == 81380, meta["nRecords"]
        assert meta["activeChannels"] == [1, 2, 3, 4]
        assert meta["fields"] == [[1, "V"], [1, "I"], [2, "V"], [2, "I"],
                                  [3, "V"], [3, "I"], [4, "V"], [4, "I"]]
        assert meta["fileName"] == TEST_FILE.name
        assert meta["lodFactors"] == {"level0": 64, "level1": 4096}
        h = meta["handle"]

        # LOD layout：4 聲道 × 3 種類 × 2 層 × (min,max) = 48 段，長度要與 blob 對得起來
        assert len(meta["lodLayout"]) == 4 * 3 * 2 * 2, len(meta["lodLayout"])
        last = meta["lodLayout"][-1]
        assert last["offset"] + last["length"] * 4 == len(lod_blob), "LOD blob 長度與 layout 不符"
        l0 = [e for e in meta["lodLayout"] if e["key"] == "V1" and e["level"] == 0][0]
        assert l0["length"] == -(-81380 // 64) == 1272, l0["length"]
        # 抽驗：blob 切出來的 level0 min 要與 server 端直接算的一致
        entry0 = dlog_core.get_handle(h, cache_root)
        lod_ref = dlog_core._lod_level(entry0, "V1", 0)
        got = slice_f32(lod_blob, l0["offset"], l0["length"])
        assert np.array_equal(got, lod_ref["min"]), "blob 內的 LOD 數值與 server 端不符"
        print("[1/8] /open metadata + LOD layout + blob 內容一致 OK")

        status2, meta2, _ = post_open(data, "renamed.dlog")
        assert status2 == 200 and meta2["handle"] == h and meta2["fileName"] == "renamed.dlog"
        print("[2/8] 內容雜湊快取命中，檔名不入快取 OK")

        # /samples：與 LOD 無關的原始值，抽樣核對 min/max 落在合理範圍
        status, smeta, payload = get_binary("/samples", h=h, lo=100, hi=1099, keys="V1,I1")
        assert status == 200, smeta
        assert smeta["lo"] == 100 and smeta["hi"] == 1099
        v1 = slice_f32(payload, smeta["layout"][0]["offset"], smeta["layout"][0]["length"])
        assert v1.size == 1000, v1.size
        print("[3/8] /samples 切片形狀與 layout OK")

        # /buckets：逐筆路徑（count<=n）時 min==max==原始值，與 /samples 對得起來
        status, bmeta, bpayload = get_binary("/buckets", h=h, keys="V1", lo=100, hi=1099, n=1800)
        off, ln = bmeta["layout"][0]["offset"], bmeta["layout"][0]["length"]
        assert ln == 1000, ln
        b_min = slice_f32(bpayload, off + ln * 4, ln)
        b_max = slice_f32(bpayload, off + ln * 8, ln)
        assert np.array_equal(b_min, v1) and np.array_equal(b_max, v1), "逐筆路徑應等於原始值"
        print("[4/8] /buckets 逐筆路徑與 /samples 一致 OK")

        # /stats：與直接用 numpy 算的結果一致
        status, st = get_json("/stats", h=h, keys="V1,I1,P1", lo=0, hi=81379)
        assert status == 200, st
        entry = dlog_core.get_handle(h, cache_root)
        ref = dlog_core.get_values(entry, "V", 1, 0, 81379, False, 0.0).astype(np.float64)
        assert abs(st["V1"]["min"] - ref.min()) < 1e-12
        assert abs(st["V1"]["max"] - ref.max()) < 1e-12
        assert abs(st["V1"]["avg"] - ref.mean()) < 1e-9
        print("[5/8] /stats 與 numpy 直算一致 OK")

        # offset 決定性：同索引在 /samples 與 /buckets 取到同值，且四端點都不得出現負值
        status, s1, p1 = get_binary("/samples", h=h, lo=0, hi=4999, keys="I1",
                                    offset=1, maxLimit=MAX_LIMIT)
        status, s2, p2 = get_binary("/samples", h=h, lo=2000, hi=6999, keys="I1",
                                    offset=1, maxLimit=MAX_LIMIT)
        a = slice_f32(p1, s1["layout"][0]["offset"], s1["layout"][0]["length"])
        b = slice_f32(p2, s2["layout"][0]["offset"], s2["layout"][0]["length"])
        assert np.array_equal(a[2000:5000], b[0:3000]), "同索引在不同切片必須得到相同校正值"
        assert (a > 0).all(), "校正後電流不得有 <=0"
        print("[6/8] offset 決定性（跨切片同值）+ 電流全正 OK")

        # 負值防護。已議定的規格：只校正電流，電壓負值保留（真實物理現象），
        # 因此 P=V×I 在 V<0 的樣本上仍會是負的——這不是 bug，是刻意的邊界行為。
        # 實測 default_20260803_092740.dlog：CH4 有 4.90% 的電壓樣本為負（低至 -1.7mV）。
        status, st2 = get_json("/stats", h=h, keys="I1,I2,I3,I4,V1,V2,V3,V4,P1,P2,P3,P4",
                               lo=0, hi=81379, offset=1, maxLimit=MAX_LIMIT)
        for ch in (1, 2, 3, 4):
            assert st2["I%d" % ch]["min"] > 0, "電流校正後仍有非正值：%r" % st2["I%d" % ch]
            if st2["P%d" % ch]["min"] < 0:
                assert st2["V%d" % ch]["min"] < 0, \
                    "CH%d 功率為負但電壓全正，代表電流校正沒生效" % ch
        status, bm, bp = get_binary("/buckets", h=h, keys="I1,I2,I3,I4", lo=0, hi=81379,
                                    n=1800, offset=1, maxLimit=MAX_LIMIT)
        for lay in bm["layout"]:
            o, ln = lay["offset"], lay["length"]
            assert (slice_f32(bp, o + ln * 4, ln) > 0).all(), "%s 分桶後仍有非正值" % lay["key"]

        status, raw = get("/export/csv", h=h, lo=0, hi=2000, cols="p:V1,p:I1,p:P1",
                          offset=1, maxLimit=MAX_LIMIT, name="t.csv")
        assert status == 200
        lines = raw.decode("utf-8").split("\r\n")
        assert lines[0] == "14585A Exported Waveform", lines[0]
        assert lines[1] == "Sampling Period:0.00098303996", lines[1]
        assert lines[3] == ("Time,File1 Instrument A Channel 1 Voltage Avg,"
                            "File1 Instrument A Channel 1 Current Avg,"
                            "File1 Instrument A Channel 1 Power Avg"), lines[3]
        assert len([l for l in lines[4:] if l]) == 2001

        # 直接檢查匯出檔案的文字內容有沒有負號——這才是「匯出不得出現負值」的實際需求，
        # 且要以前端 csvExportColumns() 的完整 12 欄、三種匯出範圍（M1↔M2／目前視野／
        # 全部）都掃過才算數。欄位順序與前端一致：Time,V1,I1,V2,I2,V3,I3,V4,I4,P1..P4。
        # 注意 toFixed(9) 的量級限制：校正後的電流約 1e-8，其功率約 1e-10，
        # 會被格式化成 0.000000000（非負，但也看不出數值），這是沿用現行 CSV 格式的既有行為。
        cols12 = ",".join(["p:V%d,p:I%d" % (c, c) for c in (1, 2, 3, 4)]
                          + ["p:P%d" % c for c in (1, 2, 3, 4)])

        def scan_csv(label, lo, hi):
            """回傳每個聲道在這段範圍內「電壓欄印出負號」的列數。"""
            status, raw = get("/export/csv", h=h, lo=lo, hi=hi, cols=cols12,
                              offset=1, maxLimit=MAX_LIMIT, name="scan.csv")
            assert status == 200, label
            body = raw.decode("utf-8").split("\r\n")
            assert len(body[3].split(",")) == 13, body[3]
            rows = [l for l in body[4:] if l]
            assert len(rows) == hi - lo + 1, (label, len(rows))
            v_neg = [0, 0, 0, 0]
            for line in rows:
                cell = line.split(",")
                for k in range(4):
                    volt, curr, power = cell[1 + 2 * k], cell[2 + 2 * k], cell[9 + k]
                    assert not curr.startswith("-"), \
                        "[%s] CH%d 電流欄出現負號：%s" % (label, k + 1, line)
                    if volt.startswith("-"):
                        v_neg[k] += 1
                    else:
                        assert not power.startswith("-"), \
                            "[%s] CH%d 電壓為正但功率為負：%s" % (label, k + 1, line)
            return v_neg

        scan_csv("M1↔M2", 20000, 25000)
        scan_csv("目前視野", 40000, 60000)
        v_neg_all = scan_csv("全部", 0, 81379)

        # 電壓負值必須「仍然存在」且筆數與原始資料完全相同——已定案只校正電流，
        # 電壓被抹掉才是錯的（見交接文件 §6.2）。
        for ch in (1, 2, 3, 4):
            ref = int((np.asarray(entry["V"][ch]) < 0).sum())
            assert v_neg_all[ch - 1] == ref, \
                "CH%d 匯出的電壓負值筆數 %d ≠ 原始資料 %d，校正可能誤套用到電壓" \
                % (ch, v_neg_all[ch - 1], ref)
        assert sum(v_neg_all) > 0, "整份檔案都掃不到電壓負值，這份測試資料無法驗證此項"
        print("[7/8] 負值防護：電流在 /stats /buckets /export/csv 一律 > 0；"
              "CSV 12 欄 × 三種範圍全掃無負電流；電壓負值原封保留（%s） OK"
              % "/".join(str(n) for n in v_neg_all))

        # 邊界與錯誤處理
        status4, meta4, _ = post_open(b"not a dlog file", "bad.dlog")
        assert status4 == 400 and "error" in meta4
        status, err = get_json("/buckets", h="nope", keys="V1", lo=0, hi=10)
        assert status == 400 and "error" in err, err
        status, err = get_json("/buckets", h=h, keys="X9", lo=0, hi=10)
        assert status == 400, err
        status, err = get_json("/stats", h=h, keys="V1", lo=99999999, hi=99999999)
        assert status == 200 and err["V1"]["min"] is not None, err

        synth = make_synth_trailer_bytes()
        status3, meta3, _ = post_open(synth, "synth_trailer.dlog")
        assert status3 == 200 and meta3["nRecords"] == 25 and meta3["activeChannels"] == [1, 2]
        entry3 = dlog_core.get_handle(meta3["handle"], cache_root)
        v1s = dlog_core.get_values(entry3, "V", 1, 0, 24, False, 0.0)
        assert v1s[0] < 0 and v1s[5] > 0, "trailer 應排在主資料之前"

        # 電壓不得被校正抹掉（真實物理現象）
        vneg = dlog_core.get_values(entry3, "V", 1, 0, 24, True, MAX_LIMIT)
        assert (vneg[:5] < 0).all(), "電壓負值不該被校正"
        print("[8/8] 錯誤處理 / trailer 排序 / 電壓負值保留 OK")

        # CH1 記錄 V+I、CH2 只記錄 V；CH1 電壓第 2 筆是 NaN
        hdr = ("<dlog>\n<tint>0.001</tint>\n"
               '<channel id="1"><sense_volt>1</sense_volt><sense_curr>1</sense_curr></channel>\n'
               '<channel id="2"><sense_volt>1</sense_volt><sense_curr>0</sense_curr></channel>\n'
               "</dlog>\n").encode("utf-8")
        recs = [(1.0, 0.5, 3.0), (float("nan"), 0.5, 3.0), (3.0, 0.5, 3.0)]
        part = hdr + struct.pack(">Q", 3) + b"".join(struct.pack(">fff", *r) for r in recs)
        status5, meta5, _ = post_open(part, "partial.dlog")
        assert status5 == 200, meta5
        h5 = meta5["handle"]
        status, st = get_json("/stats", h=h5, keys="V1", lo=0, hi=2)
        assert status == 200 and st["V1"] == {"min": 1.0, "max": 3.0, "avg": 2.0}, st
        status, raw = get("/export/csv", h=h5, lo=0, hi=2, cols="p:V1,p:I1,p:P1,p:V2,p:I2,p:P2")
        lines = raw.decode("utf-8").split("\r\n")
        cols = lines[3].split(",")
        assert status == 200 and cols == ["Time"] + [
            "File1 Instrument A Channel %s Avg" % s
            for s in ("1 Voltage", "1 Current", "1 Power", "2 Voltage", "2 Current", "2 Power")], cols
        row = lines[4].split(",")
        assert len(row) == 7 and row[4] == "3.000000000" and row[5] == row[6] == "", row

        # -Inf 是無效樣本，校正不可把它換成正常正值
        neg = dlog_core.apply_current_offset(
            np.array([np.nan, -np.inf, -1.0, 2.0], dtype=np.float32), 0, MAX_LIMIT)
        assert np.isnan(neg[0]) and neg[1] == -np.inf and 0 < neg[2] <= MAX_LIMIT and neg[3] == 2.0, neg
        print("[+] 統計排除 NaN；CSV 固定欄位、未記錄欄位留空；校正保留 NaN/-Inf OK")

        print("\n全部自我檢查通過")
    finally:
        httpd.shutdown()
        shutil.rmtree(cache_root, ignore_errors=True)


if __name__ == "__main__":
    main()
