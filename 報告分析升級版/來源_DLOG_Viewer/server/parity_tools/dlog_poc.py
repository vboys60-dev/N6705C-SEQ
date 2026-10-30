#!/usr/bin/env python3
"""POC：用 numpy 重現 parseDlogWorker() + buildLod() 的邏輯，供與 JS Worker 原班程式碼對拍。"""
import re
import struct
import sys

import numpy as np

LOD_F0 = 64
LOD_F1 = 4096


def parse_dlog(buf: bytes):
    end_marker = b"</dlog>\n"
    idx = buf.find(end_marker)
    if idx == -1:
        raise ValueError("找不到 </dlog> 結束標記，檔案格式不符")
    header_end = idx + len(end_marker)
    header_text = buf[:header_end].decode("utf-8")

    tint = float(re.search(r"<tint>([^<]+)</tint>", header_text).group(1))
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
        raise ValueError("檔案長度不足，缺少記錄筆數欄位")
    n_records = struct.unpack_from(">Q", buf, header_end)[0]
    records_start = header_end + 8
    bytes_per_record = len(fields) * per_field * 4
    main_bytes = n_records * bytes_per_record
    if records_start + main_bytes > len(buf):
        raise ValueError(
            "資料長度不符：期望 %d bytes，實際剩餘 %d bytes"
            % (main_bytes, len(buf) - records_start)
        )

    remain_after_main = len(buf) - (records_start + main_bytes)
    trailer_records = remain_after_main // bytes_per_record
    total_records = trailer_records + n_records

    V = {ch: np.zeros(total_records, dtype=np.float32) for ch in range(1, 5)}
    I = {ch: np.zeros(total_records, dtype=np.float32) for ch in range(1, 5)}

    def read_block(start_off, count, dest_offset):
        if count == 0:
            return
        # 每筆記錄裡的欄位順序＝fields 順序；perField=3 時每個欄位在記錄內佔 3*4 bytes，
        # 只取每組的第一個 float32（min/max 模式下的「主值」）。
        raw = np.frombuffer(
            buf, dtype=">f4", count=count * len(fields) * per_field, offset=start_off
        ).reshape(count, len(fields), per_field)
        vals = raw[:, :, 0]  # (count, len(fields))
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


def build_lod(V, I, n, active_channels):
    lod = {}
    group_size = LOD_F1 // LOD_F0
    for ch in active_channels:
        Vc, Ic = V[ch], I[ch]
        len0 = max(1, -(-n // LOD_F0))  # ceil

        def minmax_pass(arr_v, arr_i, factor, length, src_len):
            vmin = np.empty(length, dtype=np.float32)
            vmax = np.empty(length, dtype=np.float32)
            imin = np.empty(length, dtype=np.float32)
            imax = np.empty(length, dtype=np.float32)
            pmin = np.empty(length, dtype=np.float32)
            pmax = np.empty(length, dtype=np.float32)
            for b in range(length):
                start = b * factor
                end = min(src_len - 1, start + factor - 1)
                vseg = arr_v[start:end + 1]
                iseg = arr_i[start:end + 1]
                pseg = vseg.astype(np.float64) * iseg.astype(np.float64)
                pseg = pseg.astype(np.float32)
                vmin[b] = vseg.min(); vmax[b] = vseg.max()
                imin[b] = iseg.min(); imax[b] = iseg.max()
                pmin[b] = pseg.min(); pmax[b] = pseg.max()
            return vmin, vmax, imin, imax, pmin, pmax

        vMin0, vMax0, iMin0, iMax0, pMin0, pMax0 = minmax_pass(Vc, Ic, LOD_F0, len0, n)

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


def dump(prefix, name, arr):
    arr.astype(np.float32).tofile(prefix + "." + name + ".f32")


def main():
    file_path, out_prefix = sys.argv[1], sys.argv[2]
    with open(file_path, "rb") as f:
        buf = f.read()
    parsed = parse_dlog(buf)
    lod = build_lod(parsed["V"], parsed["I"], parsed["nRecords"], parsed["activeChannels"])

    print({
        "tint": parsed["tint"],
        "declaredTime": parsed["declaredTime"],
        "nRecords": parsed["nRecords"],
        "channelModels": parsed["channelModels"],
        "activeChannels": parsed["activeChannels"],
        "fields": parsed["fields"],
        "minmax": parsed["minmax"],
    })

    for ch in parsed["activeChannels"]:
        dump(out_prefix, "V%d" % ch, parsed["V"][ch])
        dump(out_prefix, "I%d" % ch, parsed["I"][ch])
        for kind in ("V", "I", "P"):
            l0min, l0max = lod["%s%d" % (kind, ch)]["level0"]
            l1min, l1max = lod["%s%d" % (kind, ch)]["level1"]
            dump(out_prefix, "%s%d_l0min" % (kind, ch), l0min)
            dump(out_prefix, "%s%d_l0max" % (kind, ch), l0max)
            dump(out_prefix, "%s%d_l1min" % (kind, ch), l1min)
            dump(out_prefix, "%s%d_l1max" % (kind, ch), l1max)


if __name__ == "__main__":
    main()
