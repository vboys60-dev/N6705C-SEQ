"""合成一份帶 pre-trigger trailer 的 .dlog，專門測試 trailer 排序邏輯（真實檔案都沒有這個分支）。"""
import struct

header = (
    "<dlog>\n"
    "<tint>0.001</tint><time>1.0</time>\n"
    '<channel id="1"><sense_volt>1</sense_volt><sense_curr>1</sense_curr><model>TESTA</model></channel>\n'
    '<channel id="2"><sense_volt>1</sense_volt><sense_curr>1</sense_curr><model>TESTB</model></channel>\n'
    "</dlog>\n"
).encode("utf-8")

N_MAIN = 20
N_TRAILER = 5
bytes_per_record = 2 * 8  # 2 channels * (V,I) * 4 bytes

def record_bytes(base):
    # ch1 V, ch1 I, ch2 V, ch2 I —— 用可辨識的遞增數值方便肉眼核對排序
    return struct.pack(">ffff", base + 0.1, base + 0.2, base + 0.3, base + 0.4)

main_data = b"".join(record_bytes(1000.0 + i * 10) for i in range(N_MAIN))
trailer_data = b"".join(record_bytes(-1000.0 - i * 10) for i in range(N_TRAILER))

body = header + struct.pack(">Q", N_MAIN) + main_data + trailer_data

with open("synth_trailer.dlog", "wb") as f:
    f.write(body)

print("written synth_trailer.dlog, size=", len(body),
      "expected total records =", N_MAIN + N_TRAILER)
