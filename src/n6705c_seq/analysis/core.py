"""
.dlog 解析／正規化快取／LOD 建置。

解析邏輯（parse_dlog + build_lod）逐行對應瀏覽器端 Worker 原始碼
（dlog_viewer_mem3_preview.html 內的 DLOG_WORKER_SRC），已用該 Worker
原始碼在 Node 上實際執行、跟本檔輸出做 float32 逐位元組對拍驗證過
（含單聲道／多聲道／pre-trigger trailer 三種情況），不可獨立修改其中
任一份而不同步驗證另一份。
"""
import hashlib
import json
import re
import struct
from pathlib import Path

import numpy as np

LOD_F0 = 64
LOD_F1 = 4096


class DlogFormatError(ValueError):
    pass


def parse_dlog(buf: bytes) -> dict:
    end_marker = b"</dlog>\n"
    idx = buf.find(end_marker)
    if idx == -1:
        raise DlogFormatError("找不到 </dlog> 結束標記，檔案格式不符")
    header_end = idx + len(end_marker)
    header_text = buf[:header_end].decode("utf-8")

    tint_m = re.search(r"<tint>([^<]+)</tint>", header_text)
    if not tint_m:
        raise DlogFormatError("Header 中找不到 <tint> 欄位")
    tint = float(tint_m.group(1))

    tm = re.search(r"<time>([^<]+)</time>", header_text)
    declared_time = float(tm.group(1)) if tm else None

    fields = []
    for cm in re.finditer(r'<channel id="(\d+)">([\s\S]*?)</channel>', header_text):
        cid = int(cm.group(1))
        if cid < 1 or cid > 4:
            continue
        body = cm.group(2)
        if re.search(r"<sense_volt>\s*1\s*<", body):
            fields.append((cid, "V"))
        if re.search(r"<sense_curr>\s*1\s*<", body):
            fields.append((cid, "I"))
    if not fields:
        for i in range(1, 5):
            fields.append((i, "V"))
            fields.append((i, "I"))

    active_channels = sorted({ch for ch, _ in fields})

    channel_models = []
    for cid in range(1, 5):
        m = re.search(r'<channel id="%d">[\s\S]*?<model>([^<]+)</model>' % cid, header_text)
        if m:
            channel_models.append(m.group(1))
        else:
            channel_models.append(("CH%d" % cid) if cid in active_channels else None)

    mm = re.search(r"<sense_minmax>([^<]+)</sense_minmax>", header_text)
    minmax = bool(mm and int(mm.group(1)) == 1)
    per_field = 3 if minmax else 1

    if header_end + 8 > len(buf):
        raise DlogFormatError("檔案長度不足，缺少記錄筆數欄位")
    n_records = struct.unpack_from(">Q", buf, header_end)[0]
    records_start = header_end + 8
    bytes_per_record = len(fields) * per_field * 4
    main_bytes = n_records * bytes_per_record
    if records_start + main_bytes > len(buf):
        raise DlogFormatError(
            "資料長度不符：期望 %d bytes，實際剩餘 %d bytes"
            % (main_bytes, len(buf) - records_start)
        )

    remain_after_main = len(buf) - (records_start + main_bytes)
    trailer_records = remain_after_main // bytes_per_record
    total_records = trailer_records + n_records

    V = {ch: np.zeros(total_records, dtype=np.float32) for ch in active_channels}
    I = {ch: np.zeros(total_records, dtype=np.float32) for ch in active_channels}

    def read_block(start_off, count, dest_offset):
        if count == 0:
            return
        raw = np.frombuffer(
            buf, dtype=">f4", count=count * len(fields) * per_field, offset=start_off
        ).reshape(count, len(fields), per_field)
        vals = raw[:, :, 0]
        for k, (ch, kind) in enumerate(fields):
            (V if kind == "V" else I)[ch][dest_offset:dest_offset + count] = vals[:, k]

    if trailer_records > 0:
        read_block(records_start + main_bytes, trailer_records, 0)
    read_block(records_start, n_records, trailer_records)

    return {
        "tint": tint,
        "declaredTime": declared_time,
        "nRecords": total_records,
        "channelModels": channel_models,
        "activeChannels": active_channels,
        "fields": fields,
        "minmax": minmax,
        "V": V,
        "I": I,
    }


def build_lod(V: dict, I: dict, n: int, active_channels: list) -> dict:
    lod = {}
    group_size = LOD_F1 // LOD_F0
    for ch in active_channels:
        Vc, Ic = V[ch], I[ch]
        len0 = max(1, -(-n // LOD_F0))

        vMin0 = np.empty(len0, dtype=np.float32); vMax0 = np.empty(len0, dtype=np.float32)
        iMin0 = np.empty(len0, dtype=np.float32); iMax0 = np.empty(len0, dtype=np.float32)
        pMin0 = np.empty(len0, dtype=np.float32); pMax0 = np.empty(len0, dtype=np.float32)
        for b in range(len0):
            start = b * LOD_F0
            end = min(n - 1, start + LOD_F0 - 1)
            vseg = Vc[start:end + 1]
            iseg = Ic[start:end + 1]
            pseg = (vseg.astype(np.float64) * iseg.astype(np.float64)).astype(np.float32)
            vMin0[b] = vseg.min(); vMax0[b] = vseg.max()
            iMin0[b] = iseg.min(); iMax0[b] = iseg.max()
            pMin0[b] = pseg.min(); pMax0[b] = pseg.max()

        len1 = max(1, -(-len0 // group_size))
        vMin1 = np.empty(len1, dtype=np.float32); vMax1 = np.empty(len1, dtype=np.float32)
        iMin1 = np.empty(len1, dtype=np.float32); iMax1 = np.empty(len1, dtype=np.float32)
        pMin1 = np.empty(len1, dtype=np.float32); pMax1 = np.empty(len1, dtype=np.float32)
        for b in range(len1):
            start = b * group_size
            end = min(len0 - 1, start + group_size - 1)
            vMin1[b] = vMin0[start:end + 1].min(); vMax1[b] = vMax0[start:end + 1].max()
            iMin1[b] = iMin0[start:end + 1].min(); iMax1[b] = iMax0[start:end + 1].max()
            pMin1[b] = pMin0[start:end + 1].min(); pMax1[b] = pMax0[start:end + 1].max()

        lod["V%d" % ch] = {"level0": (vMin0, vMax0), "level1": (vMin1, vMax1)}
        lod["I%d" % ch] = {"level0": (iMin0, iMax0), "level1": (iMin1, iMax1)}
        lod["P%d" % ch] = {"level0": (pMin0, pMax0), "level1": (pMin1, pMax1)}
    return lod


# ---------------------------------------------------------------------------
# 正規化快取：以檔案內容 sha1 當快取鍵（content-addressed），內容相同必定
# 命中同一份快取、內容不同必定是不同資料夾，不需要另外比對來源檔 mtime。
# ---------------------------------------------------------------------------

def _digest_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _cache_dir(cache_root: Path, digest: str) -> Path:
    return cache_root / digest


def _is_cached(cache_dir: Path) -> bool:
    return (cache_dir / "meta.json").exists()


def _write_cache(cache_dir: Path, parsed: dict, lod: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    for ch in parsed["activeChannels"]:
        np.save(cache_dir / ("V%d.npy" % ch), parsed["V"][ch])
        np.save(cache_dir / ("I%d.npy" % ch), parsed["I"][ch])

    lod_arrays = {}
    for key, levels in lod.items():
        l0min, l0max = levels["level0"]
        l1min, l1max = levels["level1"]
        lod_arrays[key + "_l0min"] = l0min
        lod_arrays[key + "_l0max"] = l0max
        lod_arrays[key + "_l1min"] = l1min
        lod_arrays[key + "_l1max"] = l1max
    np.savez(cache_dir / "lod.npz", **lod_arrays)

    meta = {
        "tint": parsed["tint"],
        "declaredTime": parsed["declaredTime"],
        "nRecords": parsed["nRecords"],
        "channelModels": parsed["channelModels"],
        "activeChannels": parsed["activeChannels"],
        "fields": parsed["fields"],
        "minmax": parsed["minmax"],
    }
    (cache_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def open_dlog(data: bytes, cache_root: Path) -> tuple:
    """
    解析（或命中快取）一份 .dlog 內容。回傳 (meta, lod_blob)。

    LOD 隨開檔一次送到前端常駐（僅佔 raw 的 4.8%，10M×4ch 約 15MB），讓前端在
    大範圍縮放時能完全本地、同步地算出分桶——實測本地 0.16ms／幀 vs 每幀跟 server
    要 9.4ms／幀，重繪頻繁（拖曳、切主題、勾曲線）時差距會直接反映成操作延遲。

    走二進位而不是 JSON：level0 在千萬筆檔案有 15 萬筆浮點數，
    JSON 會膨脹數倍且前端還要付一次 parse 成本。
    """
    digest = _digest_of(data)
    cache_dir = _cache_dir(cache_root, digest)

    if not _is_cached(cache_dir):
        parsed = parse_dlog(data)
        lod = build_lod(parsed["V"], parsed["I"], parsed["nRecords"], parsed["activeChannels"])
        _write_cache(cache_dir, parsed, lod)

    meta = json.loads((cache_dir / "meta.json").read_text(encoding="utf-8"))
    meta["handle"] = digest
    meta["lodFactors"] = {"level0": LOD_F0, "level1": LOD_F1}

    lod_npz = np.load(cache_dir / "lod.npz")
    layout = []
    chunks = []
    cursor = 0
    for ch in meta["activeChannels"]:
        for kind in ("V", "I", "P"):
            key = "%s%d" % (kind, ch)
            for level, suffix in ((0, "l0"), (1, "l1")):
                for which in ("min", "max"):
                    arr = np.ascontiguousarray(lod_npz[key + "_" + suffix + which], dtype="<f4")
                    layout.append({
                        "key": key, "level": level, "which": which,
                        "offset": cursor, "length": int(arr.size),
                    })
                    chunks.append(arr.tobytes())
                    cursor += arr.nbytes
    meta["lodLayout"] = layout
    return meta, b"".join(chunks)


# ---------------------------------------------------------------------------
# 資料存取層：一律以 memmap 開快取的 .npy，不把原始樣本讀進 RAM。
# ---------------------------------------------------------------------------

_open_cache = {}  # digest -> {"meta":dict, "dir":Path, "V":{ch:memmap}, "I":{ch:memmap}, "lod":NpzFile}


def get_handle(digest: str, cache_root: Path) -> dict:
    entry = _open_cache.get(digest)
    if entry is not None:
        return entry
    cache_dir = _cache_dir(cache_root, digest)
    if not _is_cached(cache_dir):
        raise KeyError("未知的 handle（快取不存在，請重新載入檔案）：%s" % digest)
    meta = json.loads((cache_dir / "meta.json").read_text(encoding="utf-8"))
    entry = {
        "meta": meta,
        "dir": cache_dir,
        "V": {ch: np.load(cache_dir / ("V%d.npy" % ch), mmap_mode="r") for ch in meta["activeChannels"]},
        "I": {ch: np.load(cache_dir / ("I%d.npy" % ch), mmap_mode="r") for ch in meta["activeChannels"]},
        "lod": np.load(cache_dir / "lod.npz"),
    }
    _open_cache[digest] = entry
    return entry


# splitmix64：由索引推導 (0,1] 均勻亂數。同一索引不論落在哪個請求切片、哪個端點，
# 都得到完全相同的值，因此不需要任何快取狀態——取代前端舊版「Math.random() + 逐索引快取」。
_SM_PHI = np.uint64(0x9E3779B97F4A7C15)
_SM_M1 = np.uint64(0xBF58476D1CE4E5B9)
_SM_M2 = np.uint64(0x94D049BB133111EB)
_TWO53 = 9007199254740992.0


def offset_uniform(idx: np.ndarray) -> np.ndarray:
    z = idx.astype(np.uint64) + _SM_PHI
    z = (z ^ (z >> np.uint64(30))) * _SM_M1
    z = (z ^ (z >> np.uint64(27))) * _SM_M2
    z = z ^ (z >> np.uint64(31))
    return ((z >> np.uint64(11)).astype(np.float64) + 1.0) / _TWO53


def apply_current_offset(seg: np.ndarray, idx_start: int, max_limit: float) -> np.ndarray:
    """
    電流負值校正：把負值換成 (0, max_limit] 區間內、由索引決定的值。
    max_limit<=0 時退回 0（與前端舊版 maxLimit*(1-Math.random()) 在 maxLimit=0 的結果一致）。
    只動負值，正值原樣保留；電壓不套用此校正（真實物理現象）。
    """
    neg = np.isfinite(seg) & (seg < 0)  # -Inf 是無效樣本，不可換成正常正值
    if not neg.any():
        return seg
    out = np.array(seg, dtype=np.float32)
    if max_limit <= 0:
        out[neg] = 0.0
        return out
    idx = np.nonzero(neg)[0].astype(np.uint64) + np.uint64(idx_start)
    out[neg] = (max_limit * offset_uniform(idx)).astype(np.float32)
    return out


def get_values(entry: dict, kind: str, ch: int, lo: int, hi: int,
               offset: bool, max_limit: float) -> np.ndarray:
    """
    取 [lo, hi] 閉區間的數值。P（功率）以 float64 相乘後回傳 float64——
    與前端 getVal() 的 `getV()*getI()`（JS number 即 float64）對應；
    LOD 內的 P 則是 float32（build_lod 會轉型），兩者精度差異沿襲現行行為。
    """
    if hi < lo:
        return np.zeros(0, dtype=np.float64)
    if kind == "V":
        return np.array(entry["V"][ch][lo:hi + 1], dtype=np.float32)
    cur = entry["I"][ch][lo:hi + 1]
    if offset:
        cur = apply_current_offset(cur, lo, max_limit)
    if kind == "I":
        return np.array(cur, dtype=np.float32)
    volt = entry["V"][ch][lo:hi + 1]
    return volt.astype(np.float64) * cur.astype(np.float64)


def _lod_level(entry: dict, key: str, level: int):
    suffix = "l0" if level == 0 else "l1"
    factor = LOD_F0 if level == 0 else LOD_F1
    mn = entry["lod"][key + "_" + suffix + "min"]
    mx = entry["lod"][key + "_" + suffix + "max"]
    return {"min": mn, "max": mx, "factor": factor, "n": int(mn.size)}


def _buckets_from_lod_level(level, idx_lo, idx_hi, tint, n_buckets):
    """與前端 bucketsFromLodLevel()（HTML L1295）逐行對應。"""
    factor = level["factor"]
    e_lo = max(0, idx_lo // factor)
    e_hi = min(level["n"] - 1, idx_hi // factor)
    count = e_hi - e_lo + 1
    if count <= 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    mn_arr, mx_arr = level["min"], level["max"]
    if count <= n_buckets:
        e = np.arange(e_lo, e_hi + 1)
        raw_start = e * factor
        raw_end = np.minimum(idx_hi, raw_start + factor - 1)
        t = ((np.maximum(idx_lo, raw_start) + raw_end) / 2.0) * tint
        return t, mn_arr[e_lo:e_hi + 1].astype(np.float64), mx_arr[e_lo:e_hi + 1].astype(np.float64)

    bucket_size = -(-count // n_buckets)
    starts = np.arange(e_lo, e_hi + 1, bucket_size)
    ends = np.minimum(e_hi, starts + bucket_size - 1)
    seg = (starts - e_lo).astype(np.intp)
    mn = np.minimum.reduceat(mn_arr[e_lo:e_hi + 1], seg).astype(np.float64)
    mx = np.maximum.reduceat(mx_arr[e_lo:e_hi + 1], seg).astype(np.float64)
    raw_mid = (starts * factor + np.minimum(idx_hi, (ends + 1) * factor - 1)) / 2.0
    return raw_mid * tint, mn, mx


def compute_buckets(entry: dict, kind: str, ch: int, idx_lo: int, idx_hi: int,
                    n_buckets: int, offset: bool, max_limit: float):
    """
    與前端 computeViewBuckets()（HTML L1242）的三條路徑逐行對應，
    差別只在資料來源是 memmap 而非 Float32Array，且不含 posOffset（純視覺位移，留在前端）。
    回傳 (t[], min[], max[])。
    """
    tint = entry["meta"]["tint"]
    n_rec = entry["meta"]["nRecords"]
    idx_lo = max(0, min(n_rec - 1, idx_lo))
    idx_hi = max(0, min(n_rec - 1, idx_hi))
    count = idx_hi - idx_lo + 1
    if count <= 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)

    if count <= n_buckets:
        vals = get_values(entry, kind, ch, idx_lo, idx_hi, offset, max_limit).astype(np.float64)
        t = np.arange(idx_lo, idx_hi + 1, dtype=np.float64) * tint
        return t, vals, vals

    # 校正開啟時，預建的 LOD 摘要未套用校正，與即時取值不一致 → 退回逐筆掃描（同前端 L1263）
    lod_usable = not (offset and kind != "V")
    if lod_usable:
        key = "%s%d" % (kind, ch)
        samples_per_bucket = count / n_buckets
        level = None
        if LOD_F1 <= samples_per_bucket:
            level = _lod_level(entry, key, 1)
        elif LOD_F0 <= samples_per_bucket:
            level = _lod_level(entry, key, 0)
        if level is not None:
            return _buckets_from_lod_level(level, idx_lo, idx_hi, tint, n_buckets)

    bucket_size = -(-count // n_buckets)
    starts = np.arange(idx_lo, idx_hi + 1, bucket_size)
    ends = np.minimum(idx_hi, starts + bucket_size - 1)
    vals = get_values(entry, kind, ch, idx_lo, idx_hi, offset, max_limit).astype(np.float64)
    seg = (starts - idx_lo).astype(np.intp)
    mn = np.minimum.reduceat(vals, seg)
    mx = np.maximum.reduceat(vals, seg)
    t = ((starts + ends) / 2.0) * tint
    return t, mn, mx


_STATS_CHUNK = 4_000_000  # 分塊掃描，避免超大區間一次materialize 出巨大陣列


def compute_stats(entry: dict, kind: str, ch: int, idx_lo: int, idx_hi: int,
                  offset: bool, max_limit: float) -> dict:
    """與前端 computeRangeStats()（HTML L1343）對應：區間內的 min/max/avg。"""
    n_rec = entry["meta"]["nRecords"]
    idx_lo = max(0, min(n_rec - 1, idx_lo))
    idx_hi = max(0, min(n_rec - 1, idx_hi))
    if idx_hi < idx_lo:
        return {"min": None, "max": None, "avg": None}
    mn, mx, total, cnt = np.inf, -np.inf, 0.0, 0
    for start in range(idx_lo, idx_hi + 1, _STATS_CHUNK):
        end = min(idx_hi, start + _STATS_CHUNK - 1)
        vals = get_values(entry, kind, ch, start, end, offset, max_limit).astype(np.float64)
        # 排除 NaN/Inf：否則會污染整段統計，且 json.dumps 產生的 NaN 前端 JSON.parse 解不開
        vals = vals[np.isfinite(vals)]
        if vals.size == 0:
            continue
        mn = min(mn, float(vals.min()))
        mx = max(mx, float(vals.max()))
        total += float(vals.sum())
        cnt += int(vals.size)
    return {
        "min": mn if cnt else None,
        "max": mx if cnt else None,
        "avg": (total / cnt) if cnt else None,
    }


# ---------------------------------------------------------------------------
# CSV 匯出：串流產生、逐塊 yield，永遠不在記憶體裡組出完整字串
# （前端舊版把整份 CSV 組成一個 JS 字串再包 Blob，千萬筆會直接撐爆分頁）。
# 格式與前端 L2776-2803 完全一致：三行前導 + 標題列，CRLF 行尾，UTF-8 無 BOM。
# ---------------------------------------------------------------------------

_CSV_ROWS_PER_CHUNK = 20000


def ctime_like(d) -> str:
    """對應前端 fmtCtimeDate()：月份與日期間兩個空白，日期不足兩位補空白而非補零。"""
    days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return "%s %s %2d %02d:%02d:%02d %d" % (
        days[(d.weekday() + 1) % 7], months[d.month - 1], d.day,
        d.hour, d.minute, d.second, d.year,
    )


def _js_number(x: float) -> str:
    """JS 的 String(number)：repr() 同樣是最短往返表示，兩者對這類數值一致。"""
    s = repr(float(x))
    return s[:-2] if s.endswith(".0") else s


def iter_csv(primary: dict, columns: list, idx_lo: int, idx_hi: int,
             offset: bool, max_limit: float, now):
    """
    columns：[{entry, kind, ch, label}, ...]，順序即輸出欄位順序。
    時間軸一律以 primary 的取樣格點為準；其他來源（比對檔）依同一時間點就近取樣，
    超出其記錄範圍的儲存格留空——與前端 L2782-2797 的行為一致。
    """
    tint = primary["meta"]["tint"]
    header = "\r\n".join([
        "14585A Exported Waveform",
        "Sampling Period:" + _js_number(tint),
        "Date:" + ctime_like(now),
        ",".join(["Time"] + [c["label"] for c in columns]),
    ])
    yield (header + "\r\n").encode("utf-8")

    for start in range(idx_lo, idx_hi + 1, _CSV_ROWS_PER_CHUNK):
        end = min(idx_hi, start + _CSV_ROWS_PER_CHUNK - 1)
        count = end - start + 1
        times = np.arange(start, end + 1, dtype=np.float64) * tint

        col_values = []
        for c in columns:
            entry = c["entry"]
            if c.get("missing"):
                col_values.append(np.full(count, np.nan))  # 未記錄的欄位：整欄留空
                continue
            if entry is primary:
                col_values.append(get_values(entry, c["kind"], c["ch"], start, end,
                                             offset, max_limit).astype(np.float64))
                continue
            # 其他來源：以主檔案的時間點就近取樣（round 到最近索引）
            other_tint = entry["meta"]["tint"]
            other_n = entry["meta"]["nRecords"]
            kk = np.rint(times / other_tint).astype(np.int64)
            valid = (kk >= 0) & (kk < other_n)
            vals = np.full(count, np.nan, dtype=np.float64)
            if valid.any():
                lo2, hi2 = int(kk[valid].min()), int(kk[valid].max())
                block = get_values(entry, c["kind"], c["ch"], lo2, hi2,
                                   offset, max_limit).astype(np.float64)
                vals[valid] = block[kk[valid] - lo2]
            col_values.append(vals)

        lines = []
        for r in range(count):
            cells = ["%.8f" % times[r]]
            for vals in col_values:
                v = vals[r]
                cells.append("" if np.isnan(v) else "%.9f" % v)
            lines.append(",".join(cells))
        yield ("\r\n".join(lines) + "\r\n").encode("utf-8")
