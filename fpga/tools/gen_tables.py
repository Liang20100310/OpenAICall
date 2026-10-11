#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成仿真/实现所需的参考表与常数：
1. G.711 A-law / mu-law 全码表（参考实现 = Python audioop（源自 Sun g711.c））
   - alaw_dec_ref.hex : 256 项，A-law 码字 -> 16bit 线性 PCM
   - alaw_enc_ref.hex : 65536 项，16bit 线性 PCM -> A-law 码字（TB 逐点比对）
   - mulaw_dec_ref.hex / mulaw_enc_ref.hex 同理
2. DTMF Goertzel 系数（Q2.14，fs=8kHz）打印，供 RTL localparam 使用
3. tb 用正弦查找表 sine_lut.hex（256 点，Q1.15）
用法: python3 gen_tables.py   (输出到 ../tb/gen/)
"""
import math
import os
import struct

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tb", "gen")
os.makedirs(OUT, exist_ok=True)

try:
    import audioop  # Python<=3.12 标准库，Sun g711.c 移植，作为参考实现
    HAVE_AUDIOOP = True
except Exception:
    HAVE_AUDIOOP = False


def alaw_enc_ref(x: int) -> int:
    """参考 A-law 编码（与 ITU-T G.711 表一致，Sun g711.c linear2alaw）"""
    if HAVE_AUDIOOP:
        return audioop.lin2alaw(struct.pack("<h", x), 2)[0]
    # 纯 Python 后备实现（segment 搜索法）
    mask = 0xD5 if x >= 0 else 0x55
    if x < 0:
        x = -x - 8
    if x > 32635:
        x = 32635
    seg = 0
    if x < 256:
        pass  # seg = 0
    else:
        seg = 7
        for s in range(1, 8):
            if x < (256 << s):
                seg = s
                break
        x >>= (seg + 3) if seg else 4  # seg>0: 归一化到 4bit 量化
    aval = (seg << 4) | ((x >> 4) & 0x0F)
    return (aval ^ mask) & 0xFF


def alaw_dec_ref(a: int) -> int:
    if HAVE_AUDIOOP:
        return struct.unpack("<h", audioop.alaw2lin(bytes([a]), 2))[0]
    t = (a ^ 0x55) & 0xFF
    t = (t & 0x0F) << 4
    seg = (t & 0x70) >> 4  # 注意：此处 t 已左移，正确做法见下
    # 重新按标准公式
    v = (a ^ 0x55) & 0xFF
    seg = (v & 0x70) >> 4
    t = (v & 0x0F) << 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return t if (v & 0x80) else -t


def mulaw_enc_ref(x: int) -> int:
    if HAVE_AUDIOOP:
        return audioop.lin2ulaw(struct.pack("<h", x), 2)[0]
    BIAS = 0x84
    CLIP = 32635
    sign = 0x80 if x < 0 else 0
    if x < 0:
        x = -x
    if x > CLIP:
        x = CLIP
    x += BIAS
    seg = 7
    for s in range(8):
        if x < (0x100 << s):
            seg = s
            break
    uval = (seg << 4) | ((x >> (seg + 3)) & 0x0F)
    return (uval ^ (sign | 0x7F)) & 0xFF  # 注意与标准 u 律取反约定，由 audioop 校验


def mulaw_dec_ref(u: int) -> int:
    if HAVE_AUDIOOP:
        return struct.unpack("<h", audioop.ulaw2lin(bytes([u]), 2))[0]
    u = (~u) & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t <<= (u & 0x70) >> 4
    return (0x84 - t) if (u & 0x80) else (t - 0x84)


def write_hex(path, values, width_bits):
    """按 16 进制小写写表，每行一个值"""
    with open(path, "w") as f:
        for v in values:
            f.write("%0*x\n" % (width_bits // 4, v & ((1 << width_bits) - 1)))
    print("written", path, len(values), "entries")


def main():
    print("audioop available:", HAVE_AUDIOOP)

    # ---- G.711 参考表 ----
    alaw_dec = [alaw_dec_ref(a) & 0xFFFF for a in range(256)]
    mulaw_dec = [mulaw_dec_ref(u) & 0xFFFF for u in range(256)]
    alaw_enc = [alaw_enc_ref(x - 32768) for x in range(65536)]
    mulaw_enc = [mulaw_enc_ref(x - 32768) for x in range(65536)]
    write_hex(os.path.join(OUT, "alaw_dec_ref.hex"), alaw_dec, 16)
    write_hex(os.path.join(OUT, "mulaw_dec_ref.hex"), mulaw_dec, 16)
    write_hex(os.path.join(OUT, "alaw_enc_ref.hex"), alaw_enc, 8)
    write_hex(os.path.join(OUT, "mulaw_enc_ref.hex"), mulaw_enc, 8)

    # 自检：编解码往返误差 ≤ 量化步长
    worst = 0
    for x in range(-32768, 32768, 7):
        a = alaw_enc_ref(x)
        y = alaw_dec_ref(a)
        worst = max(worst, abs(y - x))
    print("A-law round-trip worst error:", worst, "(应 <= ~256 for small values, 一般 < 量化步长)")
    print("A-law 校验点: enc(+8)=%02x enc(-8)=%02x dec(0xD5)=%d dec(0x55)=%d"
          % (alaw_enc_ref(8), alaw_enc_ref(-8), alaw_dec_ref(0xD5), alaw_dec_ref(0x55)))
    print("mu-law 校验点: enc(0)=%02x enc(-1)=%02x" % (mulaw_enc_ref(0), mulaw_enc_ref(-1)))

    # ---- DTMF Goertzel 系数 (fs=8kHz, Q2.14) ----
    fs = 8000.0
    freqs = [697, 770, 852, 941, 1209, 1336, 1477, 1633]
    print("\n-- Goertzel coefficients Q2.14 (fs=8000) --")
    for f in freqs:
        c = 2.0 * math.cos(2.0 * math.pi * f / fs)
        q = int(round(c * 16384))
        print("f=%4d Hz : coef_f = %+.6f  ->  Q14 %6d  (0x%04X)" % (f, c, q, q & 0xFFFF))

    # ---- 正弦 LUT (256 点, Q1.15) ----
    sine = [int(round(32767 * math.sin(2 * math.pi * i / 256))) & 0xFFFF for i in range(256)]
    write_hex(os.path.join(OUT, "sine_lut.hex"), sine, 16)

    # ---- DTMF 双音激励采样数据（TB 用，验证用例）----
    # 生成 '5' (770+1336Hz)、'0' (941+1336Hz)、'#'(941+1477Hz) 各 512 样点
    amp = 4000
    with open(os.path.join(OUT, "dtmf_tone5.hex"), "w") as f5, \
         open(os.path.join(OUT, "dtmf_tone0.hex"), "w") as f0, \
         open(os.path.join(OUT, "dtmf_toneh.hex"), "w") as fh:
        for n in range(512):
            s5 = amp * math.sin(2 * math.pi * 770 * n / fs) + amp * math.sin(2 * math.pi * 1336 * n / fs)
            s0 = amp * math.sin(2 * math.pi * 941 * n / fs) + amp * math.sin(2 * math.pi * 1336 * n / fs)
            sh = amp * math.sin(2 * math.pi * 941 * n / fs) + amp * math.sin(2 * math.pi * 1477 * n / fs)
            f5.write("%04x\n" % (int(s5) & 0xFFFF))
            f0.write("%04x\n" % (int(s0) & 0xFFFF))
            fh.write("%04x\n" % (int(sh) & 0xFFFF))
    print("written dtmf tone vectors in", OUT)


if __name__ == "__main__":
    main()
