#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音频 I/O 抽象：G.711 A-law ↔ 线性 PCM、重采样、上/下行音频流。

契约（docs/INTERFACE_SPEC.md §1）：
    * 采样率 8000 Hz，串口上 G.711 A-law 8 bit，FPGA 内部 16 bit 线性 PCM；
    * 一帧语音 20 ms = 160 样点/通道 = 160 字节 G.711 = 320 字节 PCM(16bit LE)。

模块内容：
    * alaw_to_linear() / linear_to_alaw() —— G.711 A-law ↔ 16bit LE 线性 PCM；
      优先用标准库 audioop（Python <=3.12），缺 audioop（3.13+）时自动切换
      纯 Python 实现（与 audioop 数值完全一致）；
    * resample_pcm16() —— 线性插值重采样（TTS 输出 16/22.05/24 kHz → 8 kHz）；
    * UplinkStream   —— 上行 G.711 帧 → PCM，广播给 ASR/VAD 等订阅者；
    * DownlinkStream —— 接收 TTS 的任意采样率 PCM，切 20ms G.711 帧供串口发送，
      支持 clear() 供"打断(barge-in)"立即停播。

运行 `python3 audio_io.py` 执行自测：参考值校验（0xD5↔+8、0x55↔-8）、
全 256 码字回环、与 audioop 一致性对比（若可用）、重采样与下行分帧检查。
"""

from __future__ import annotations

import struct
import sys
import threading
import time
from collections import deque
from typing import Callable, Deque, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 音频格式常量（§1）
# ---------------------------------------------------------------------------

SAMPLE_RATE = 8000                 # 电话带宽采样率
FRAME_MS = 20                      # 每帧语音时长
SAMPLES_PER_FRAME = 160            # 20 ms @ 8 kHz = 160 样点
G711_FRAME_BYTES = 160             # 每通道每帧 G.711 字节数
PCM_FRAME_BYTES = SAMPLES_PER_FRAME * 2   # 每帧线性 PCM 字节数（16 bit）

__all__ = [
    "SAMPLE_RATE", "FRAME_MS", "SAMPLES_PER_FRAME", "G711_FRAME_BYTES", "PCM_FRAME_BYTES",
    "alaw_to_linear", "linear_to_alaw", "alaw_sample_to_linear", "linear_sample_to_alaw",
    "resample_pcm16", "pcm16_to_floats", "floats_to_pcm16", "wav_to_pcm16", "pcm16_to_wav",
    "frame_rms", "UplinkStream", "DownlinkStream", "PcmCallback",
]


# ---------------------------------------------------------------------------
# G.711 A-law ↔ 线性 PCM
# ---------------------------------------------------------------------------

# ---- 纯 Python 实现（兼容 Python 3.13+ 无 audioop 的环境）----

# A-law 解码：与 CPython audioop.alaw2lin(x, 2) 数值完全一致
def alaw_sample_to_linear(a: int) -> int:
    """单字节 A-law 码字 → 16 bit 线性样点（与 audioop.alaw2lin 一致）。"""
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t = (t + 0x108) << (seg - 1)
    return t if (a & 0x80) else -t


# A-law 编码：分段按 2 的幂划分（与 CPython audioop.lin2alaw 全域一致，
# 已对全部 65536 个 int16 输入穷举比对）
def linear_sample_to_alaw(pcm: int) -> int:
    """单个 16 bit 线性样点 → A-law 码字（与 audioop.lin2alaw 全域一致）。

    分段规则：正数幅值 m = x；负数幅值 m = |x| - 1（向零截断）；
    seg 由 m 所在的 2 的幂区间决定（seg0: m<256，seg s: 2^(s+7) <= m < 2^(s+8)）；
    量化位 q = (m >> 4) & 0xF（seg<2）或 (m >> (seg+3)) & 0xF（seg>=2）。
    """
    if pcm >= 0:
        mask = 0xD5           # 正数：符号位 = 1
        m = pcm
    else:
        mask = 0x55           # 负数：符号位 = 0
        m = -pcm - 1          # 向零截断，保证 -8 ↔ 0x55、-1 ↔ 0x55
    if m < 256:
        seg = 0
    else:
        seg = min(7, m.bit_length() - 8)
    if seg < 2:
        q = (m >> 4) & 0x0F
    else:
        q = (m >> (seg + 3)) & 0x0F
    return ((seg << 4) | q) ^ mask


# 预计算 256 项解码表，加速批量转换
_ALAW_TO_LINEAR: Tuple[int, ...] = tuple(alaw_sample_to_linear(a) for a in range(256))

# 优先使用标准库 audioop（Python <= 3.12，C 实现快）；3.13+ 自动退回纯 Python
try:  # pragma: no cover - 依赖解释器版本
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # audioop 在 3.12 已弃用
        import audioop as _audioop  # type: ignore
    _HAVE_AUDIOOP = True
except ImportError:            # Python 3.13+ 已移除 audioop
    _audioop = None
    _HAVE_AUDIOOP = False


def alaw_to_linear(data: bytes) -> bytes:
    """G.711 A-law 字节串 → 16 bit 小端线性 PCM 字节串。

    等价于 audioop.alaw2lin(data, 2)；纯 Python 备用实现数值与之完全一致。
    """
    data = bytes(data)
    if _HAVE_AUDIOOP:
        return _audioop.alaw2lin(data, 2)
    # 纯 Python 路径：查表 + struct 打包
    samples = [_ALAW_TO_LINEAR[b] for b in data]
    return struct.pack(f"<{len(samples)}h", *samples) if samples else b""


def linear_to_alaw(data: bytes) -> bytes:
    """16 bit 小端线性 PCM 字节串 → G.711 A-law 字节串。

    等价于 audioop.lin2alaw(data, 2)；输入长度必须为偶数。
    """
    data = bytes(data)
    if len(data) % 2 != 0:
        raise ValueError(f"PCM 字节数必须为偶数，实际 {len(data)}")
    if _HAVE_AUDIOOP:
        return _audioop.lin2alaw(data, 2)
    if not data:
        return b""
    samples = struct.unpack(f"<{len(data) // 2}h", data)
    return bytes(linear_sample_to_alaw(s) for s in samples)


# ---------------------------------------------------------------------------
# 重采样 / PCM 工具
# ---------------------------------------------------------------------------

def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int = SAMPLE_RATE) -> bytes:
    """单声道 16 bit PCM 线性插值重采样（如 TTS 24 kHz → 8 kHz）。"""
    pcm = bytes(pcm)
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("采样率必须为正数")
    if len(pcm) % 2 != 0:
        raise ValueError(f"PCM 字节数必须为偶数，实际 {len(pcm)}")
    if src_rate == dst_rate or len(pcm) < 4:
        return pcm
    n_src = len(pcm) // 2
    samples = struct.unpack(f"<{n_src}h", pcm)
    n_dst = max(1, round(n_src * dst_rate / src_rate))
    out: List[int] = []
    step = (n_src - 1) / (n_dst - 1) if n_dst > 1 else 0.0
    for i in range(n_dst):
        pos = i * step
        i0 = int(pos)
        if i0 >= n_src - 1:
            out.append(samples[-1])
        else:
            frac = pos - i0
            val = samples[i0] + (samples[i0 + 1] - samples[i0]) * frac
            out.append(int(round(max(-32768, min(32767, val)))))
    return struct.pack(f"<{n_dst}h", *out)


def pcm16_to_floats(pcm: bytes) -> List[float]:
    """16 bit PCM → [-1.0, 1.0] 浮点列表（供 numpy/ASR 互转）。"""
    pcm = bytes(pcm)
    if len(pcm) % 2 != 0:
        raise ValueError("PCM 字节数必须为偶数")
    if not pcm:
        return []
    return [s / 32768.0 for s in struct.unpack(f"<{len(pcm) // 2}h", pcm)]


def floats_to_pcm16(samples: List[float]) -> bytes:
    """[-1.0, 1.0] 浮点列表 → 16 bit PCM。"""
    out = [int(round(max(-1.0, min(1.0, s)) * 32767)) for s in samples]
    return struct.pack(f"<{len(out)}h", *out) if out else b""


def wav_to_pcm16(wav: bytes) -> Tuple[bytes, int, int]:
    """解析 WAV 字节串，返回 (单声道 16bit PCM, 采样率, 原声道数)。

    多声道自动下混为单声道；8 bit / 24 bit 等非 16 bit 格式在此转换为 16 bit。
    """
    import io
    import wave

    with wave.open(io.BytesIO(wav), "rb") as wf:
        n_ch = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    if width == 2:
        if n_ch == 1:
            return raw, rate, n_ch
        # 多声道下混：取各声道均值
        n_frames = len(raw) // (2 * n_ch)
        vals = struct.unpack(f"<{n_frames * n_ch}h", raw[: n_frames * 2 * n_ch])
        mono = [sum(vals[i * n_ch:(i + 1) * n_ch]) // n_ch for i in range(n_frames)]
        return struct.pack(f"<{n_frames}h", *mono), rate, n_ch
    if width == 1:  # 8 bit 无符号
        n_frames = len(raw) // n_ch
        mono = [(sum(raw[i * n_ch:(i + 1) * n_ch]) // n_ch - 128) << 8 for i in range(n_frames)]
        return struct.pack(f"<{n_frames}h", *mono), rate, n_ch
    if width == 3:  # 24 bit
        n_frames = len(raw) // (3 * n_ch)
        mono = []
        for i in range(n_frames):
            acc = 0
            for c in range(n_ch):
                off = (i * n_ch + c) * 3
                v = int.from_bytes(raw[off:off + 3], "little", signed=True)
                acc += v >> 8
            mono.append(acc // n_ch)
        return struct.pack(f"<{n_frames}h", *mono), rate, n_ch
    raise ValueError(f"不支持的 WAV 采样宽度: {width * 8} bit")


def pcm16_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """16 bit 单声道 PCM → WAV 字节串（供 ASR 上传 / 调试落盘）。"""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(pcm))
    return buf.getvalue()


def frame_rms(pcm: bytes) -> float:
    """计算一帧 16 bit PCM 的均方根幅度（VAD 能量检测用）。"""
    pcm = bytes(pcm)
    if len(pcm) < 2:
        return 0.0
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    acc = 0
    for s in samples:
        acc += s * s
    return (acc / n) ** 0.5


# ---------------------------------------------------------------------------
# 上行流：G.711 帧 → PCM → 广播订阅者（喂给 ASR/VAD）
# ---------------------------------------------------------------------------

PcmCallback = Callable[[bytes, float], None]
"""订阅者回调签名：(pcm16_bytes, timestamp_monotonic)。"""


class UplinkStream:
    """上行音频流：接收 bridge 的 G.711 A-law 帧，转 PCM 后广播给订阅者。

    线程安全：可在串口接收线程中调用 push_g711()，订阅者在同一线程被回调
    （订阅者应自行把耗时处理转投工作线程）。
    """

    def __init__(self) -> None:
        self._subs: List[PcmCallback] = []
        self._lock = threading.Lock()
        self.frames_in = 0          # 统计：收到的 G.711 帧数
        self.bytes_out = 0          # 统计：输出的 PCM 字节数

    def subscribe(self, callback: PcmCallback) -> None:
        """注册订阅者；每收到一帧上行音频即以 (pcm, ts) 回调。"""
        with self._lock:
            self._subs.append(callback)

    def unsubscribe(self, callback: PcmCallback) -> None:
        """注销订阅者。"""
        with self._lock:
            if callback in self._subs:
                self._subs.remove(callback)

    def push_g711(self, g711: bytes, ts: Optional[float] = None) -> bytes:
        """推送一帧 G.711（160 字节），转换为 PCM 并广播。返回 PCM 字节。"""
        if len(g711) != G711_FRAME_BYTES:
            raise ValueError(f"上行 G.711 帧必须为 {G711_FRAME_BYTES} 字节，实际 {len(g711)}")
        pcm = alaw_to_linear(g711)
        if ts is None:
            ts = time.monotonic()
        self.frames_in += 1
        self.bytes_out += len(pcm)
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            cb(pcm, ts)
        return pcm


# ---------------------------------------------------------------------------
# 下行流：TTS PCM → 20ms G.711 帧队列（供 bridge 发送）
# ---------------------------------------------------------------------------

class DownlinkStream:
    """下行音频流：接收任意采样率 PCM（TTS 输出），切分成 20 ms G.711 帧。

    * submit_pcm()   —— 提交 PCM（自动重采样到 8 kHz 并缓冲成帧）；
    * next_g711_frame() —— 供 bridge 发送线程按 20 ms 节拍取帧；
    * clear()        —— 立即清空待发帧（打断 barge-in / 停播）。

    线程安全：TTS 工作线程 submit，串口发送线程取帧，pipeline 主动 clear。
    """

    def __init__(self, max_frames: int = 250) -> None:
        """max_frames: 队列上限（帧数，250 帧 = 5 s），溢出丢弃最旧帧。"""
        self._queue: Deque[bytes] = deque()
        self._cond = threading.Condition()
        self._max_frames = max_frames
        self._partial = bytearray()   # 不足一帧的 PCM 尾巴
        self.frames_queued = 0        # 统计：累计入队帧数
        self.frames_sent = 0          # 统计：累计出队帧数
        self.frames_dropped = 0       # 统计：溢出丢弃帧数

    def submit_pcm(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> int:
        """提交 PCM 字节（16 bit LE 单声道），返回本次新入队的 G.711 帧数。"""
        pcm = bytes(pcm)
        if sample_rate != SAMPLE_RATE:
            pcm = resample_pcm16(pcm, sample_rate, SAMPLE_RATE)
        with self._cond:
            self._partial.extend(pcm)
            added = 0
            while len(self._partial) >= PCM_FRAME_BYTES:
                chunk = bytes(self._partial[:PCM_FRAME_BYTES])
                del self._partial[:PCM_FRAME_BYTES]
                self._queue.append(linear_to_alaw(chunk))
                self.frames_queued += 1
                added += 1
                if len(self._queue) > self._max_frames:
                    self._queue.popleft()
                    self.frames_dropped += 1
            if added:
                self._cond.notify_all()
            return added

    def next_g711_frame(self, timeout: float = FRAME_MS / 1000.0) -> Optional[bytes]:
        """取一帧待发 G.711（160 字节）；队列空且超时返回 None。"""
        with self._cond:
            if not self._queue:
                self._cond.wait(timeout)
            if not self._queue:
                return None
            frame = self._queue.popleft()
            self.frames_sent += 1
            return frame

    def clear(self) -> int:
        """清空待发帧（打断播放），返回被清掉的帧数。"""
        with self._cond:
            n = len(self._queue)
            self._queue.clear()
            self._partial.clear()
            self._cond.notify_all()
            return n

    @property
    def pending_frames(self) -> int:
        """当前待发帧数。"""
        with self._cond:
            return len(self._queue)

    @property
    def playing(self) -> bool:
        """是否仍有音频在播放（pending > 0）。"""
        return self.pending_frames > 0

    def flush_tail(self) -> int:
        """把不足 20 ms 的 PCM 尾巴补零后入队（一句话结束时调用）。"""
        with self._cond:
            tail = bytes(self._partial)
            self._partial.clear()
        if not tail:
            return 0
        pad = (-len(tail)) % PCM_FRAME_BYTES
        return self.submit_pcm(tail + b"\x00" * pad)


# ---------------------------------------------------------------------------
# 自测（python3 audio_io.py）
# ---------------------------------------------------------------------------

def _self_test() -> bool:
    """G.711 参考值校验 + 回环 + audioop 一致性 + 重采样/分帧检查。"""
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

    print("== audio_io 自测 ==")
    print(f"  音频后端: {'audioop' if _HAVE_AUDIOOP else '纯 Python（audioop 不可用）'}")

    # 1) ITU-T G.711 参考值：A-law 0xD5 ↔ +8，0x55 ↔ -8
    check("alaw_to_linear(0xD5) == +8", alaw_to_linear(b"\xd5") == struct.pack("<h", 8),
          f"实际 {struct.unpack('<h', alaw_to_linear(bytes([0xD5])))[0]}")
    check("alaw_to_linear(0x55) == -8", alaw_to_linear(b"\x55") == struct.pack("<h", -8),
          f"实际 {struct.unpack('<h', alaw_to_linear(bytes([0x55])))[0]}")
    check("linear_to_alaw(+8) == 0xD5", linear_to_alaw(struct.pack("<h", 8)) == b"\xd5",
          f"实际 0x{linear_to_alaw(struct.pack('<h', 8))[0]:02X}")
    check("linear_to_alaw(-8) == 0x55", linear_to_alaw(struct.pack("<h", -8)) == b"\x55",
          f"实际 0x{linear_to_alaw(struct.pack('<h', -8))[0]:02X}")
    check("alaw_to_linear(0xAA) == +32256（最大正幅值）",
          alaw_to_linear(b"\xaa") == struct.pack("<h", 32256),
          f"实际 {struct.unpack('<h', alaw_to_linear(bytes([0xAA])))[0]}")
    check("alaw_to_linear(0x2A) == -32256（最大负幅值）",
          alaw_to_linear(b"\x2a") == struct.pack("<h", -32256),
          f"实际 {struct.unpack('<h', alaw_to_linear(bytes([0x2A])))[0]}")

    # 2) 与 audioop 全码字一致性（audioop 可用时）+ 纯 Python 备用路径校验
    all_codes = bytes(range(256))
    test_pcm = struct.pack("<256h", *[i * 127 - 16256 for i in range(256)])
    if _HAVE_AUDIOOP:
        check("全 256 码字解码与 audioop 一致",
              alaw_to_linear(all_codes) == _audioop.alaw2lin(all_codes, 2))
        check("线性→A-law 与 audioop 一致",
              linear_to_alaw(test_pcm) == _audioop.lin2alaw(test_pcm, 2))
    else:
        print("  [SKIP] audioop 不可用，跳过一致性对比（纯 Python 实现已用参考值校验）")
    # 纯 Python 备用路径（3.13+ 无 audioop 时使用）必须与主路径数值一致
    pure_dec = struct.pack("<256h", *[alaw_sample_to_linear(a) for a in range(256)])
    check("纯 Python 解码与主路径一致（3.13+ 备用）", pure_dec == alaw_to_linear(all_codes))
    pure_enc = bytes(linear_sample_to_alaw(s) for s in struct.unpack("<256h", test_pcm))
    check("纯 Python 编码与主路径一致（3.13+ 备用）", pure_enc == linear_to_alaw(test_pcm))
    # 穷举全部 65536 个 int16 样点，逐点编码/解码与主路径一致（含 audioop 对照）
    import array
    all_pcm = array.array("h", range(-32768, 32768)).tobytes()
    enc_main = linear_to_alaw(all_pcm)
    enc_pure = bytes(linear_sample_to_alaw(s) for s in range(-32768, 32768))
    check("穷举 65536 样点编码一致", enc_pure == enc_main)
    dec_pure = struct.pack("<65536h", *[_ALAW_TO_LINEAR[b] for b in enc_pure])
    check("穷举 65536 样点编码→解码可回放", dec_pure == alaw_to_linear(enc_pure))

    # 3) 编码→解码回环：量化误差应小于该码字的量化步长（A-law 13 bit 段内）
    pcm_in = struct.pack("<300h", *[int(20000 * __import__("math").sin(i / 7.0)) for i in range(300)])
    pcm_out = alaw_to_linear(linear_to_alaw(pcm_in))
    n = 300
    a = struct.unpack(f"<{n}h", pcm_in)
    b = struct.unpack(f"<{n}h", pcm_out)
    max_err = max(abs(x - y) for x, y in zip(a, b))
    # A-law 大信号段（seg7）量化步长 1024，允许半步长 + 8 的偏移裕量
    check("A-law 回环量化误差有界", max_err <= 520, f"max_err={max_err}")

    # 4) 空输入 / 非法输入
    check("空输入返回空", alaw_to_linear(b"") == b"" and linear_to_alaw(b"") == b"")
    try:
        linear_to_alaw(b"\x00")
        check("奇数长度 PCM 报错", False)
    except ValueError:
        check("奇数长度 PCM 报错", True)

    # 5) 重采样：24 kHz → 8 kHz，长度按比例
    src = struct.pack("<240h", *([1000, -1000] * 120))
    dst = resample_pcm16(src, 24000, 8000)
    check("24k→8k 重采样长度", len(dst) // 2 == 80, f"{len(dst) // 2} 样点")
    same = resample_pcm16(src, 8000, 8000)
    check("同采样率直通", same == src)

    # 6) WAV 往返
    wav = pcm16_to_wav(pcm_in, 8000)
    back, rate, nch = wav_to_pcm16(wav)
    check("WAV 往返一致", back == pcm_in and rate == 8000 and nch == 1)

    # 7) UplinkStream 广播
    got: List[bytes] = []
    ul = UplinkStream()
    ul.subscribe(lambda pcm, ts: got.append(pcm))
    g711_frame = b"\xd5" * 160
    ul.push_g711(g711_frame)
    check("UplinkStream 广播", len(got) == 1 and got[0] == struct.pack("<160h", *([8] * 160)))
    try:
        ul.push_g711(b"\x00" * 10)
        check("UplinkStream 长度校验", False)
    except ValueError:
        check("UplinkStream 长度校验", True)

    # 8) DownlinkStream 分帧 / clear
    dl = DownlinkStream()
    added = dl.submit_pcm(struct.pack("<320h", *([8] * 320)), 8000)  # 40 ms = 2 帧
    f1 = dl.next_g711_frame(0.1)
    f2 = dl.next_g711_frame(0.1)
    check("DownlinkStream 分帧", added == 2 and f1 == b"\xd5" * 160 and f2 == b"\xd5" * 160)
    dl.submit_pcm(struct.pack("<320h", *([0] * 320)))
    cleared = dl.clear()
    check("DownlinkStream clear（打断停播）", cleared == 2 and dl.next_g711_frame(0.05) is None)
    # 尾巴补零
    dl2 = DownlinkStream()
    dl2.submit_pcm(struct.pack("<100h", *([8] * 100)))   # 100 样点 < 160，不入队
    check("不足一帧先缓冲", dl2.pending_frames == 0)
    dl2.flush_tail()
    check("flush_tail 补零成帧", dl2.pending_frames == 1)

    print("== 结果: %s ==" % ("PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
