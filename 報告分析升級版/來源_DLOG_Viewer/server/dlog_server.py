"""
本機 .dlog 解析伺服器。stdlib-only：http.server + numpy。

GET  /...                     靜態檔案（含 dlog_viewer_mem3_preview.html），從專案根目錄提供
POST /open                    body＝.dlog 原始位元組，header X-File-Name＝顯示用檔名
                              回傳 [4-byte meta 長度][JSON metadata][LOD binary]
GET  /samples?h&lo&hi&...     [lo,hi] 閉區間的原始樣本（各啟用聲道 V/I），Float32 binary
GET  /buckets?h&keys&...      min/max 包絡，Float32 binary
GET  /stats?h&keys&...        區間 min/max/avg，JSON
GET  /export/csv?h&...        CSV 串流下載（Content-Disposition: attachment）

所有回傳數值的端點都吃 offset／maxLimit——電流負值校正一律在 server 端統一套用，
這是「匯出檔案不得出現負電流／負功率」的單一保證點，前端無從繞過。

執行：python3 server/dlog_server.py，然後開瀏覽器連
http://127.0.0.1:8765/dlog_viewer_mem3_preview.html
"""
import datetime
import json
import struct
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import dlog_core  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
CACHE_ROOT = Path(__file__).parent / ".cache"
HOST, PORT = "127.0.0.1", 8765


class BadRequest(ValueError):
    pass


def frame_binary(meta: dict, payload: bytes) -> bytes:
    """
    二進位回應格式：[4-byte meta 長度][JSON metadata][Float32 payload]。

    metadata 以空白補到 4 的倍數——瀏覽器端 `new Float32Array(buf, offset, len)`
    要求 offset 必須是 4 的倍數，否則丟 RangeError；不補的話 payload 起點會落在
    任意位置。JSON 允許尾端空白，補進去不影響解析。
    """
    meta_bytes = json.dumps(meta).encode("utf-8")
    pad = (-len(meta_bytes)) % 4
    meta_bytes += b" " * pad
    return struct.pack("<I", len(meta_bytes)) + meta_bytes + payload


def _parse_keys(raw):
    """keys=V3,I3,P3 → [('V',3), ('I',3), ('P',3)]"""
    out = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        if len(token) < 2 or token[0] not in "VIP" or not token[1:].isdigit():
            raise BadRequest("keys 格式錯誤：%r（應為 V3,I3,P3 這種形式）" % token)
        out.append((token[0], int(token[1:])))
    if not out:
        raise BadRequest("keys 不可為空")
    return out


def _qs_int(qs, name, default=None):
    if name not in qs:
        if default is None:
            raise BadRequest("缺少參數 %s" % name)
        return default
    try:
        return int(qs[name][0])
    except ValueError:
        raise BadRequest("參數 %s 必須是整數" % name)


def _qs_float(qs, name, default=0.0):
    if name not in qs:
        return default
    try:
        return float(qs[name][0])
    except ValueError:
        raise BadRequest("參數 %s 必須是數字" % name)


def _qs_bool(qs, name):
    return (qs.get(name, ["0"])[0]) in ("1", "true", "True")


def _handle(qs, name="h"):
    if name not in qs:
        raise BadRequest("缺少參數 %s" % name)
    try:
        return dlog_core.get_handle(qs[name][0], CACHE_ROOT)
    except KeyError as e:
        raise BadRequest(str(e))


def make_handler():
    class Handler(SimpleHTTPRequestHandler):
        # 預設的 HTTP/1.0 會讓 BaseHTTPRequestHandler 跳過內建的
        # Expect: 100-continue 回應，curl 對大檔案上傳等不到回應會
        # 卡滿 1 秒才送出 body；HTTP/1.1 才會觸發那段內建邏輯。
        protocol_version = "HTTP/1.1"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

        # ---------------- routing ----------------

        def do_POST(self):
            if self.path == "/open":
                self._guard(self._handle_open)
            else:
                self._send_json({"error": "not found"}, 404)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            route = {
                "/samples": self._handle_samples,
                "/buckets": self._handle_buckets,
                "/stats": self._handle_stats,
                "/export/csv": self._handle_export_csv,
            }.get(parsed.path)
            if route is None:
                return super().do_GET()
            qs = urllib.parse.parse_qs(parsed.query)
            self._guard(lambda: route(qs))

        def _guard(self, fn):
            try:
                fn()
            except (BadRequest, dlog_core.DlogFormatError) as e:
                self._send_json({"error": str(e)}, 400)

        # ---------------- endpoints ----------------

        def _handle_open(self):
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length)
            file_name = self.headers.get("X-File-Name", "unknown.dlog")
            meta, lod_blob = dlog_core.open_dlog(data, CACHE_ROOT)
            meta["fileName"] = file_name
            self._send_bytes(frame_binary(meta, lod_blob), "application/octet-stream")

        def _handle_samples(self, qs):
            entry = _handle(qs)
            n_rec = entry["meta"]["nRecords"]
            lo = max(0, min(n_rec - 1, _qs_int(qs, "lo")))
            hi = max(0, min(n_rec - 1, _qs_int(qs, "hi")))
            offset = _qs_bool(qs, "offset")
            max_limit = _qs_float(qs, "maxLimit")
            keys = _parse_keys(qs.get("keys", [None])[0]) if "keys" in qs else [
                (kind, ch) for ch in entry["meta"]["activeChannels"] for kind in ("V", "I")
            ]
            chunks = []
            layout = []
            cursor = 0
            for kind, ch in keys:
                arr = dlog_core.get_values(entry, kind, ch, lo, hi, offset, max_limit)
                arr = np.ascontiguousarray(arr, dtype="<f4")
                layout.append({"key": "%s%d" % (kind, ch), "offset": cursor, "length": int(arr.size)})
                chunks.append(arr.tobytes())
                cursor += arr.nbytes
            self._send_bytes(
                frame_binary({"lo": lo, "hi": hi, "layout": layout}, b"".join(chunks)),
                "application/octet-stream")

        def _handle_buckets(self, qs):
            entry = _handle(qs)
            keys = _parse_keys(qs.get("keys", [None])[0])
            idx_lo = _qs_int(qs, "lo")
            idx_hi = _qs_int(qs, "hi")
            n_buckets = _qs_int(qs, "n", 1800)
            offset = _qs_bool(qs, "offset")
            max_limit = _qs_float(qs, "maxLimit")
            chunks = []
            layout = []
            cursor = 0
            for kind, ch in keys:
                t, mn, mx = dlog_core.compute_buckets(
                    entry, kind, ch, idx_lo, idx_hi, n_buckets, offset, max_limit)
                triple = [np.ascontiguousarray(a, dtype="<f4") for a in (t, mn, mx)]
                layout.append({
                    "key": "%s%d" % (kind, ch),
                    "offset": cursor,
                    "length": int(triple[0].size),
                })
                for a in triple:
                    chunks.append(a.tobytes())
                    cursor += a.nbytes
            self._send_bytes(frame_binary({"layout": layout}, b"".join(chunks)),
                             "application/octet-stream")

        def _handle_stats(self, qs):
            entry = _handle(qs)
            keys = _parse_keys(qs.get("keys", [None])[0])
            idx_lo = _qs_int(qs, "lo")
            idx_hi = _qs_int(qs, "hi")
            offset = _qs_bool(qs, "offset")
            max_limit = _qs_float(qs, "maxLimit")
            out = {}
            for kind, ch in keys:
                out["%s%d" % (kind, ch)] = dlog_core.compute_stats(
                    entry, kind, ch, idx_lo, idx_hi, offset, max_limit)
            self._send_json(out, 200)

        def _handle_export_csv(self, qs):
            primary = _handle(qs)
            compare = _handle(qs, "hc") if "hc" in qs else None
            n_rec = primary["meta"]["nRecords"]
            idx_lo = max(0, min(n_rec - 1, _qs_int(qs, "lo")))
            idx_hi = max(0, min(n_rec - 1, _qs_int(qs, "hi")))
            offset = _qs_bool(qs, "offset")
            max_limit = _qs_float(qs, "maxLimit")
            file_name = qs.get("name", ["export.csv"])[0]

            # cols=p:V1,p:I1,c:V1 —— 前綴 p＝主檔案、c＝比對檔，順序即輸出欄位順序
            columns = []
            for token in (qs.get("cols", [""])[0]).split(","):
                token = token.strip()
                if not token:
                    continue
                src, _, key = token.partition(":")
                if src not in ("p", "c") or len(key) < 2 or key[0] not in "VIP" or not key[1:].isdigit():
                    raise BadRequest("cols 格式錯誤：%r（應為 p:V1 這種形式）" % token)
                entry = primary if src == "p" else compare
                if entry is None:
                    raise BadRequest("cols 指定了比對檔欄位，但未提供 hc 參數")
                ch = int(key[1:])
                if not 1 <= ch <= 4:
                    raise BadRequest("cols 聲道超出範圍：%r" % token)
                # 固定欄位：該來源沒記錄這個欄位（含整個聲道未啟用；P 需要 V、I 都有）時
                # 仍保留標題、整欄留空——不可照匯，聲道只記錄 V 時 I 陣列是解析時預填的 0
                recorded = {(f[0], f[1]) for f in entry["meta"]["fields"]}
                needs = ("V", "I") if key[0] == "P" else (key[0],)
                missing = any((ch, k) not in recorded for k in needs)
                label = "%s Instrument A Channel %d %s Avg" % (
                    "File1" if src == "p" else "File2", ch,
                    {"V": "Voltage", "I": "Current", "P": "Power"}[key[0]])
                columns.append({"entry": entry, "kind": key[0], "ch": ch, "label": label,
                                "missing": missing})
            if not columns:
                raise BadRequest("沒有可匯出的欄位")

            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(file_name))
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            # 串流輸出：瀏覽器邊收邊寫磁碟，JS 端完全不碰資料，記憶體用量與筆數無關
            for block in dlog_core.iter_csv(primary, columns, idx_lo, idx_hi,
                                            offset, max_limit, datetime.datetime.now()):
                self.wfile.write(b"%x\r\n" % len(block) + block + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

        # ---------------- helpers ----------------

        def _send_bytes(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, obj, status):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main():
    CACHE_ROOT.mkdir(exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), make_handler())
    print("N6705C DLOG server 啟動：http://%s:%d/dlog_viewer_mem3_preview.html" % (HOST, PORT))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
