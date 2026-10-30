"""合成 .dlog 產生器（串流寫入，不在記憶體組整檔）。header 格式同 parity_tools 舊版 make_synth.py。

trailer=True 時在主資料之後追加 5 筆「pre-trigger trailer」（主資料為正、trailer 為負），
解析端會把 trailer 排到主資料之前，因此 record 0..4 的 V1 為負、第 5 筆起為正。
"""
from pathlib import Path

import numpy as np

TRAILER_RECORDS = 5
CHUNK = 1 << 20  # records per write


def _header(channels: int) -> bytes:
    chans = "".join(
        '<channel id="%d"><sense_volt>1</sense_volt><sense_curr>1</sense_curr>'
        "<model>TEST%d</model></channel>\n" % (ch, ch)
        for ch in range(1, channels + 1)
    )
    return ("<dlog>\n<tint>0.001</tint><time>1.0</time>\n" + chans + "</dlog>\n").encode("utf-8")


def make_big(path: Path, n_records: int, channels: int = 4, trailer: bool = False) -> Path:
    path = Path(path)
    n_fields = channels * 2  # V, I per channel
    field_offset = np.arange(n_fields, dtype=np.float64) * 0.1
    with open(path, "wb") as f:
        f.write(_header(channels))
        f.write(int(n_records).to_bytes(8, "big"))
        for start in range(0, n_records, CHUNK):
            n = min(CHUNK, n_records - start)
            r = np.arange(start, start + n, dtype=np.float64)
            base = 1.0 + (r % 1000) * 0.001
            f.write((base[:, None] + field_offset[None, :]).astype(">f4").tobytes())
        if trailer:
            j = np.arange(TRAILER_RECORDS, dtype=np.float64)
            base = -(1.0 + j * 0.001)
            f.write((base[:, None] - field_offset[None, :]).astype(">f4").tobytes())
    return path
