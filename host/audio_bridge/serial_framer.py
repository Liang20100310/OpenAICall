#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UART 帧协议编解码（契约：docs/INTERFACE_SPEC.md §2）。

帧结构（小端）：
    偏移  长度  字段
    0     2    MAGIC = 0xAA 0x55
    2     1    TYPE
    3     1    SEQ（滚动序号 0..255）
    4     2    LEN（载荷长度 N，小端，0 <= N <= 512）
    6     N    PAYLOAD
    6+N   1    CRC8（CRC-8/ATM：poly=0x07, init=0x00, MSB first，覆盖 TYPE..PAYLOAD 末字节）

本模块为纯标准库实现，提供：
    * crc8()                 —— CRC-8/ATM 校验
    * encode_frame()         —— 组帧
    * FrameDecoder           —— 解帧（含失步重同步、CRC 校验、SEQ 丢帧统计）
    * FrameEncoder           —— 自动维护发送方 SEQ 的组帧器
    * 各帧类型的载荷打包/解包辅助函数
    * 寄存器地址 / DTMF 编码 / 摘挂机状态常量

运行 `python3 serial_framer.py`（或 `python3 -m audio_bridge.serial_framer`）
执行自带自测：组帧→解帧回环、CRC 错误注入、失步重同步、SEQ 丢帧统计。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

__all__ = [
    "MAGIC", "HEADER_LEN", "MAX_PAYLOAD",
    "TYPE_AUDIO_UL", "TYPE_AUDIO_DL", "TYPE_DTMF_EVT", "TYPE_HOOK_EVT",
    "TYPE_RING_CMD", "TYPE_REG_WR", "TYPE_REG_RD", "TYPE_REG_VAL",
    "TYPE_PING", "TYPE_PONG",
    "HOOK_ON", "HOOK_OFF",
    "REG_SCRATCH", "REG_CHAN_EN", "REG_LOOPBACK", "REG_DTMF_THR", "REG_DTMF_CFG",
    "REG_STATUS", "REG_SLOT_MAP0", "REG_SLOT_MAP1", "REG_VERSION",
    "DTMF_DIGIT_TO_CODE", "DTMF_CODE_TO_DIGIT",
    "Frame", "FrameDecoder", "DecoderStats", "FrameEncoder",
    "crc8", "encode_frame",
    "pack_audio", "unpack_audio", "pack_dtmf_evt", "unpack_dtmf_evt",
    "pack_hook_evt", "unpack_hook_evt", "pack_ring_cmd", "unpack_ring_cmd",
    "pack_reg_wr", "pack_reg_rd", "unpack_reg_val",
]

# ---------------------------------------------------------------------------
# 帧协议常量
# ---------------------------------------------------------------------------

MAGIC = b"\xAA\x55"          # 帧头魔数（失步重同步搜索依据）
HEADER_LEN = 6               # MAGIC(2) + TYPE(1) + SEQ(1) + LEN(2)
CRC_LEN = 1                  # 帧尾 CRC8 长度
MAX_PAYLOAD = 512            # 载荷长度上限（协议规定 0 <= N <= 512）

# 帧类型（§2.2）
TYPE_AUDIO_UL = 0x10         # FPGA→PC：CH(1B) + G711A[160]
TYPE_AUDIO_DL = 0x11         # PC→FPGA：CH(1B) + G711A[160]
TYPE_DTMF_EVT = 0x20         # FPGA→PC：CH(1B) + DIGIT(1B) + DUR_MS(2B LE)
TYPE_HOOK_EVT = 0x21         # FPGA→PC：CH(1B) + STATE(1B)
TYPE_RING_CMD = 0x22         # PC→FPGA：CH(1B) + ON(1B)
TYPE_REG_WR = 0x30           # PC→FPGA：ADDR(1B) + VAL(1B)
TYPE_REG_RD = 0x31           # PC→FPGA：ADDR(1B)
TYPE_REG_VAL = 0x32          # FPGA→PC：ADDR(1B) + VAL(1B)
TYPE_PING = 0x7E             # 双向，空载荷
TYPE_PONG = 0x7F             # 双向，空载荷

# 摘挂机状态（§2.2 HOOK_EVT STATE）
HOOK_ON = 0                  # 挂机 on-hook
HOOK_OFF = 1                 # 摘机 off-hook

# 配置寄存器地址（§3）
REG_SCRATCH = 0x00           # 读写测试寄存器
REG_CHAN_EN = 0x01           # bit0/1 = CH0/CH1 音频上行使能，复位 0x03
REG_LOOPBACK = 0x02          # bit0=UART 音频环回，bit1=I2S 数字环回，bit2=CH0<->CH1 交叉
REG_DTMF_THR = 0x03          # DTMF 检测门限，复位 0x40
REG_DTMF_CFG = 0x04          # bit0=DTMF 使能，bit1=事件去重使能，复位 0x03
REG_STATUS = 0x05            # 只读：bit0=UART_RX_ERR, bit1=FIFO_OVF, bit2/3=CH0/CH1 摘机
REG_SLOT_MAP0 = 0x06         # CH0 接收时隙映射，复位 0x12
REG_SLOT_MAP1 = 0x07         # CH1 接收时隙映射，复位 0x13
REG_VERSION = 0x08           # 版本号，复位 0x10

# LOOPBACK 寄存器位掩码
LOOPBACK_UART = 0x01         # UART 音频环回（DL -> UL 直通）
LOOPBACK_I2S = 0x02          # I2S 数字环回
LOOPBACK_CROSS = 0x04        # CH0 <-> CH1 交叉

# 音频帧载荷长度：CH(1B) + 160 字节 G.711 A-law（20 ms @ 8 kHz）
AUDIO_PAYLOAD_LEN = 1 + 160
AUDIO_SAMPLES_PER_FRAME = 160

# DTMF 数字编码（§2.3）
DTMF_DIGIT_TO_CODE: Dict[str, int] = {
    "1": 0x01, "2": 0x02, "3": 0x03,
    "4": 0x04, "5": 0x05, "6": 0x06,
    "7": 0x07, "8": 0x08, "9": 0x09,
    "0": 0x0A, "*": 0x0B, "#": 0x0C,
}
DTMF_CODE_TO_DIGIT: Dict[int, str] = {v: k for k, v in DTMF_DIGIT_TO_CODE.items()}

# 合法通道号（首版 2 通道，预留 4）
NUM_CHANNELS = 2
MAX_CHANNELS = 4


# ---------------------------------------------------------------------------
# CRC-8/ATM：poly=0x07, init=0x00, MSB first, xorout=0x00
# ---------------------------------------------------------------------------

def _build_crc8_table() -> List[int]:
    """预计算 CRC8 查表（256 项），加速组帧/解帧。"""
    table: List[int] = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) & 0xFF) ^ 0x07
            else:
                crc = (crc << 1) & 0xFF
        table.append(crc)
    return table


_CRC8_TABLE = _build_crc8_table()


def crc8(data: Union[bytes, bytearray, memoryview]) -> int:
    """计算 CRC-8/ATM 校验值（覆盖 TYPE..PAYLOAD 的全部字节）。"""
    crc = 0x00
    for byte in data:
        crc = _CRC8_TABLE[crc ^ byte]
    return crc


# ---------------------------------------------------------------------------
# 组帧
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    """一个已解析 / 待发送的 UART 帧。"""
    type: int                  # 帧类型（TYPE_* 常量）
    seq: int                   # 滚动序号 0..255
    payload: bytes = b""       # 载荷

    @property
    def type_name(self) -> str:
        """帧类型的可读名称（便于日志）。"""
        return _TYPE_NAMES.get(self.type, f"UNKNOWN(0x{self.type:02X})")


_TYPE_NAMES = {
    TYPE_AUDIO_UL: "AUDIO_UL",
    TYPE_AUDIO_DL: "AUDIO_DL",
    TYPE_DTMF_EVT: "DTMF_EVT",
    TYPE_HOOK_EVT: "HOOK_EVT",
    TYPE_RING_CMD: "RING_CMD",
    TYPE_REG_WR: "REG_WR",
    TYPE_REG_RD: "REG_RD",
    TYPE_REG_VAL: "REG_VAL",
    TYPE_PING: "PING",
    TYPE_PONG: "PONG",
}


def encode_frame(ftype: int, seq: int, payload: Union[bytes, bytearray] = b"") -> bytes:
    """按协议组帧：MAGIC + TYPE + SEQ + LEN + PAYLOAD + CRC8。

    参数：
        ftype:   帧类型（0..255）
        seq:     滚动序号（会按 0..255 取模）
        payload: 载荷字节（0 <= len <= 512）
    返回：完整帧字节串。
    """
    if not (0 <= ftype <= 0xFF):
        raise ValueError(f"帧类型越界: {ftype}")
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"载荷超长: {len(payload)} > {MAX_PAYLOAD}")
    seq &= 0xFF
    body = bytes((ftype, seq)) + len(payload).to_bytes(2, "little") + payload
    return MAGIC + body + bytes((crc8(body),))


class FrameEncoder:
    """发送侧组帧器：自动维护并递增 SEQ（0..255 循环）。"""

    def __init__(self, start_seq: int = 0) -> None:
        """start_seq: 初始序号（默认 0）。"""
        self._seq = start_seq & 0xFF
        self.sent_frames = 0  # 统计：累计组帧数

    @property
    def seq(self) -> int:
        """下一个将要使用的序号。"""
        return self._seq

    def build(self, ftype: int, payload: Union[bytes, bytearray] = b"") -> bytes:
        """组一帧并自动递增 SEQ。"""
        frame = encode_frame(ftype, self._seq, payload)
        self._seq = (self._seq + 1) & 0xFF
        self.sent_frames += 1
        return frame


# ---------------------------------------------------------------------------
# 解帧（含失步重同步 / 错误统计）
# ---------------------------------------------------------------------------

@dataclass
class DecoderStats:
    """解帧器统计信息（可通过状态上报，协议 §2.1 错误计数）。"""
    frames_ok: int = 0          # 成功解析的帧数
    crc_errors: int = 0         # CRC 校验失败帧数
    len_errors: int = 0         # LEN 超界 / 载荷长度非法次数
    resyncs: int = 0            # 失步重同步次数（丢弃字节后重新对齐）
    bytes_discarded: int = 0    # 重同步累计丢弃字节数
    seq_lost: int = 0           # 按 SEQ 推算的丢帧总数
    seq_discontinuities: int = 0  # SEQ 不连续次数
    unknown_types: int = 0      # 未识别帧类型数（不丢弃，照常上交）

    def snapshot(self) -> Dict[str, int]:
        """返回统计信息的字典副本。"""
        return {
            "frames_ok": self.frames_ok,
            "crc_errors": self.crc_errors,
            "len_errors": self.len_errors,
            "resyncs": self.resyncs,
            "bytes_discarded": self.bytes_discarded,
            "seq_lost": self.seq_lost,
            "seq_discontinuities": self.seq_discontinuities,
            "unknown_types": self.unknown_types,
        }


class FrameDecoder:
    """UART 帧解码器。

    用法：
        dec = FrameDecoder()
        for frame in dec.feed(serial_bytes):
            handle(frame)

    特性：
        * 失步时逐字节搜索 0xAA 0x55 重新对齐（统计 resyncs / bytes_discarded）；
        * LEN 超界（>512）→ 丢弃并计数，跳过当前魔数继续搜索；
        * CRC 校验失败 → 计数并跳过当前魔数、重新搜索 0xAA 0x55 对齐
          （相比整帧丢弃，能避免截断/假帧吞掉紧随其后的有效帧）；
        * SEQ 丢帧统计：按 (last_seq+1)&0xFF 与实际 SEQ 的差累计 seq_lost。
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.stats = DecoderStats()
        self._last_seq: Optional[int] = None

    def reset(self) -> None:
        """清空缓冲与统计（不重置 SEQ 基准以外的状态）。"""
        self._buf.clear()
        self.stats = DecoderStats()
        self._last_seq = None

    def feed(self, data: Union[bytes, bytearray, memoryview]) -> List[Frame]:
        """喂入串口原始字节，返回本次解析出的完整帧列表。"""
        if data:
            self._buf.extend(data)
        out: List[Frame] = []
        buf = self._buf

        while True:
            # 1) 搜索帧头魔数，丢弃魔数之前的垃圾字节（失步重同步）
            idx = buf.find(MAGIC)
            if idx < 0:
                # 没有完整魔数：若末字节恰为 0xAA 则保留（可能是半个魔数），其余丢弃
                keep = 1 if (len(buf) > 0 and buf[-1] == MAGIC[0]) else 0
                drop = len(buf) - keep
                if drop > 0:
                    self.stats.bytes_discarded += drop
                    self.stats.resyncs += 1
                    del buf[:drop]
                break
            if idx > 0:
                self.stats.bytes_discarded += idx
                self.stats.resyncs += 1
                del buf[:idx]

            # 2) 等待完整帧头
            if len(buf) < HEADER_LEN:
                break

            ftype = buf[2]
            seq = buf[3]
            plen = buf[4] | (buf[5] << 8)

            # 3) LEN 越界检查（协议：0 <= N <= 512）
            if plen > MAX_PAYLOAD:
                self.stats.len_errors += 1
                # 无法确定帧边界，跳过当前魔数继续搜索
                del buf[:2]
                continue

            total = HEADER_LEN + plen + CRC_LEN
            if len(buf) < total:
                break  # 帧未收全，等待更多字节

            # 4) CRC 校验（覆盖 TYPE..PAYLOAD 末字节，即 buf[2 : 6+plen]）
            calc = crc8(buf[2:HEADER_LEN + plen])
            if calc != buf[HEADER_LEN + plen]:
                self.stats.crc_errors += 1
                # 丢弃该帧：只跳过 2 字节魔数并重新搜索对齐，
                # 保证后续真实帧不会被本候选帧吞掉
                del buf[:2]
                continue

            # 5) 帧有效：SEQ 丢帧统计
            if self._last_seq is not None:
                expected = (self._last_seq + 1) & 0xFF
                if seq != expected:
                    gap = (seq - expected) & 0xFF
                    self.stats.seq_lost += gap
                    self.stats.seq_discontinuities += 1
            self._last_seq = seq

            frame = Frame(type=ftype, seq=seq, payload=bytes(buf[HEADER_LEN:HEADER_LEN + plen]))
            if ftype not in _TYPE_NAMES:
                self.stats.unknown_types += 1
            self.stats.frames_ok += 1
            out.append(frame)
            del buf[:total]

        return out


# ---------------------------------------------------------------------------
# 各帧类型载荷的打包 / 解包辅助
# ---------------------------------------------------------------------------

def pack_audio(ftype: int, channel: int, g711: bytes) -> bytes:
    """打包 AUDIO_UL / AUDIO_DL 载荷：CH(1B) + G711A[160]。

    仅组载荷，不含帧头；配合 encode_frame / FrameEncoder.build 使用。
    """
    if ftype not in (TYPE_AUDIO_UL, TYPE_AUDIO_DL):
        raise ValueError(f"非音频帧类型: 0x{ftype:02X}")
    if not (0 <= channel < MAX_CHANNELS):
        raise ValueError(f"通道号越界: {channel}")
    g711 = bytes(g711)
    if len(g711) != AUDIO_SAMPLES_PER_FRAME:
        raise ValueError(f"音频载荷必须为 160 字节，实际 {len(g711)}")
    return bytes((channel,)) + g711


def unpack_audio(payload: bytes) -> Tuple[int, bytes]:
    """解包音频载荷，返回 (channel, g711[160])。"""
    payload = bytes(payload)
    if len(payload) != AUDIO_PAYLOAD_LEN:
        raise ValueError(f"音频载荷长度非法: {len(payload)} != {AUDIO_PAYLOAD_LEN}")
    return payload[0], payload[1:]


def pack_dtmf_evt(channel: int, digit: str, dur_ms: int) -> bytes:
    """打包 DTMF_EVT 载荷：CH(1B) + DIGIT(1B) + DUR_MS(2B LE)。"""
    if digit not in DTMF_DIGIT_TO_CODE:
        raise ValueError(f"非法 DTMF 数字: {digit!r}")
    if not (0 <= channel < MAX_CHANNELS):
        raise ValueError(f"通道号越界: {channel}")
    if not (0 <= dur_ms <= 0xFFFF):
        raise ValueError(f"时长越界: {dur_ms}")
    return bytes((channel, DTMF_DIGIT_TO_CODE[digit])) + dur_ms.to_bytes(2, "little")


def unpack_dtmf_evt(payload: bytes) -> Tuple[int, str, int]:
    """解包 DTMF_EVT 载荷，返回 (channel, digit, dur_ms)。"""
    payload = bytes(payload)
    if len(payload) != 4:
        raise ValueError(f"DTMF_EVT 载荷长度非法: {len(payload)} != 4")
    digit = DTMF_CODE_TO_DIGIT.get(payload[1])
    if digit is None:
        raise ValueError(f"保留 DTMF 编码: 0x{payload[1]:02X}")
    dur = payload[2] | (payload[3] << 8)
    return payload[0], digit, dur


def pack_hook_evt(channel: int, off_hook: bool) -> bytes:
    """打包 HOOK_EVT 载荷：CH(1B) + STATE(1B)（1=摘机，0=挂机）。"""
    if not (0 <= channel < MAX_CHANNELS):
        raise ValueError(f"通道号越界: {channel}")
    return bytes((channel, HOOK_OFF if off_hook else HOOK_ON))


def unpack_hook_evt(payload: bytes) -> Tuple[int, bool]:
    """解包 HOOK_EVT 载荷，返回 (channel, off_hook)。"""
    payload = bytes(payload)
    if len(payload) != 2:
        raise ValueError(f"HOOK_EVT 载荷长度非法: {len(payload)} != 2")
    if payload[1] not in (HOOK_ON, HOOK_OFF):
        raise ValueError(f"非法摘挂机状态: {payload[1]}")
    return payload[0], payload[1] == HOOK_OFF


def pack_ring_cmd(channel: int, on: bool) -> bytes:
    """打包 RING_CMD 载荷：CH(1B) + ON(1B)（1=开始振铃，0=停止）。"""
    if not (0 <= channel < MAX_CHANNELS):
        raise ValueError(f"通道号越界: {channel}")
    return bytes((channel, 1 if on else 0))


def unpack_ring_cmd(payload: bytes) -> Tuple[int, bool]:
    """解包 RING_CMD 载荷，返回 (channel, on)。"""
    payload = bytes(payload)
    if len(payload) != 2:
        raise ValueError(f"RING_CMD 载荷长度非法: {len(payload)} != 2")
    return payload[0], bool(payload[1])


def pack_reg_wr(addr: int, val: int) -> bytes:
    """打包 REG_WR 载荷：ADDR(1B) + VAL(1B)。"""
    if not (0 <= addr <= 0xFF and 0 <= val <= 0xFF):
        raise ValueError(f"寄存器地址/值越界: addr={addr} val={val}")
    return bytes((addr, val))


def pack_reg_rd(addr: int) -> bytes:
    """打包 REG_RD 载荷：ADDR(1B)。"""
    if not (0 <= addr <= 0xFF):
        raise ValueError(f"寄存器地址越界: {addr}")
    return bytes((addr,))


def unpack_reg_val(payload: bytes) -> Tuple[int, int]:
    """解包 REG_VAL 载荷，返回 (addr, val)。"""
    payload = bytes(payload)
    if len(payload) != 2:
        raise ValueError(f"REG_VAL 载荷长度非法: {len(payload)} != 2")
    return payload[0], payload[1]


# ---------------------------------------------------------------------------
# 自测（python3 serial_framer.py）
# ---------------------------------------------------------------------------

def _self_test() -> bool:
    """组帧→解帧回环、CRC 错误注入、失步重同步、SEQ 丢帧统计。"""
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))

    print("== serial_framer 自测 ==")

    # 0) CRC8 参考值（CRC-8/ATM：poly=0x07, init=0, MSB first）
    #    "123456789" 的标准校验值为 0xF4；crc8(b"\\x01") = 0x07
    check("crc8(b'123456789') == 0xF4", crc8(b"123456789") == 0xF4,
          f"实际 0x{crc8(b'123456789'):02X}")
    check("crc8(b'\\x01') == 0x07", crc8(b"\x01") == 0x07,
          f"实际 0x{crc8(bytes([0x01])):02X}")
    check("crc8(b'') == 0x00", crc8(b"") == 0x00)

    # 1) 组帧 → 解帧回环（多种载荷长度，含空载荷与 512 字节上限）
    dec = FrameDecoder()
    enc = FrameEncoder()
    payloads = [b"", b"\x01", b"\x00\x01\x02", bytes(range(256)), b"\xA5" * 512]
    expect: List[Frame] = []
    stream = bytearray()
    types_cycle = [TYPE_PING, TYPE_AUDIO_UL, TYPE_DTMF_EVT, TYPE_REG_VAL, TYPE_PONG]
    for i, pl in enumerate(payloads):
        ftype = types_cycle[i % len(types_cycle)]
        raw = enc.build(ftype, pl)
        stream.extend(raw)
        expect.append(Frame(type=ftype, seq=(enc.seq - 1) & 0xFF, payload=pl))

    # 分块喂入（模拟串口任意分片到达）
    got: List[Frame] = []
    step = 7
    for off in range(0, len(stream), step):
        got.extend(dec.feed(bytes(stream[off:off + step])))

    check("回环帧数一致", len(got) == len(expect), f"{len(got)} vs {len(expect)}")
    check("回环内容一致",
          all(g.type == e.type and g.seq == e.seq and g.payload == e.payload
              for g, e in zip(got, expect)))
    check("回环无错误计数",
          dec.stats.crc_errors == 0 and dec.stats.len_errors == 0
          and dec.stats.resyncs == 0 and dec.stats.seq_lost == 0,
          str(dec.stats.snapshot()))

    # 2) CRC 错误注入：翻转载荷 1 bit → 应被丢弃并计数
    dec2 = FrameDecoder()
    bad = bytearray(encode_frame(TYPE_AUDIO_DL, 0, b"\x11" * 161))
    bad[10] ^= 0x01                       # 破坏载荷字节
    frames = dec2.feed(bytes(bad))
    check("CRC 错误帧被丢弃", len(frames) == 0)
    check("CRC 错误计数 == 1", dec2.stats.crc_errors == 1, str(dec2.stats.snapshot()))
    # 紧随其后的好帧仍可正常解析
    good = encode_frame(TYPE_AUDIO_DL, 1, b"\x22" * 161)
    frames = dec2.feed(good)
    check("CRC 错误后恢复正常解析", len(frames) == 1 and frames[0].seq == 1)

    # 3) 失步重同步：垃圾字节 + 被截断的假帧 + 有效帧
    dec3 = FrameDecoder()
    truncated = encode_frame(TYPE_REG_WR, 3, b"\x01\x02\x03\x04")[:9]  # 只有前 9 字节
    valid = encode_frame(TYPE_PONG, 4)
    garbage = b"\x00\x11\x22\xAA"  # 末字节 0xAA 是"半个魔数"，应被保留再与后续拼接
    frames = dec3.feed(garbage + truncated + valid)
    check("失步后重同步恢复有效帧", len(frames) == 1 and frames[0].type == TYPE_PONG,
          f"got={[(f.type_name, f.seq) for f in frames]}")
    check("重同步有丢弃统计", dec3.stats.bytes_discarded > 0, str(dec3.stats.snapshot()))

    # 3b) 帧头 0xAA 0x55 被拆到两次 feed
    dec3b = FrameDecoder()
    f = encode_frame(TYPE_PING, 9)
    out = dec3b.feed(f[:1])
    out += dec3b.feed(f[1:])
    check("跨 feed 半魔数拼接", len(out) == 1 and out[0].seq == 9)

    # 4) SEQ 丢帧统计：发送 0,1,5 → 应统计丢 3 帧（2,3,4）
    dec4 = FrameDecoder()
    for s in (0, 1, 5):
        dec4.feed(encode_frame(TYPE_PING, s))
    check("SEQ 丢帧数 == 3", dec4.stats.seq_lost == 3, str(dec4.stats.snapshot()))
    check("SEQ 不连续次数 == 1", dec4.stats.seq_discontinuities == 1)

    # 5) LEN 超界检测：手工构造 LEN=600 的坏帧头
    dec5 = FrameDecoder()
    bad_hdr = MAGIC + bytes((TYPE_PING, 0)) + (600).to_bytes(2, "little") + b"\x00" * 8
    frames = dec5.feed(bad_hdr + encode_frame(TYPE_PING, 1))
    check("LEN 超界被计数且后续帧可恢复",
          dec5.stats.len_errors == 1 and len(frames) == 1,
          str(dec5.stats.snapshot()))

    # 6) 载荷打包/解包一致性
    ch, g711 = unpack_audio(pack_audio(TYPE_AUDIO_UL, 1, b"\xD5" * 160))
    check("音频载荷打包/解包", ch == 1 and g711 == b"\xD5" * 160)
    ch2, digit, dur = unpack_dtmf_evt(pack_dtmf_evt(0, "#", 120))
    check("DTMF 载荷打包/解包", ch2 == 0 and digit == "#" and dur == 120)
    ch3, off = unpack_hook_evt(pack_hook_evt(1, True))
    check("HOOK 载荷打包/解包", ch3 == 1 and off is True)

    print("== 结果: %s ==" % ("PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
