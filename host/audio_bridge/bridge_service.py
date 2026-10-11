#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""串口音频收发服务：FPGA ↔ 上位机 的实时音频/事件桥。

契约：docs/INTERFACE_SPEC.md §2（UART 帧协议）、§3（寄存器）。

功能：
    * 接收 AUDIO_UL / DTMF_EVT / HOOK_EVT / REG_VAL / PING，向上层提供回调；
      PING 自动应答 PONG（协议要求立即回）；
    * 发送 AUDIO_DL（经下行抖动缓冲按 20 ms 节拍平滑发送）、RING_CMD、
      REG_WR / REG_RD（读操作同步等待 REG_VAL，带超时）、PING；
    * 内置抖动缓冲：每通道一个 JitterBuffer（20 ms 帧队列，深度可配），
      上行侧供 ASR 管道按需取帧，下行侧吸收 TTS 产生抖动；
    * 环回自测：REG LOOPBACK=0x01 时发送 AUDIO_DL 并校验 AUDIO_UL 回声。

依赖：pyserial（真实串口）。缺依赖时给出明确报错；
      自测模式使用内置 FpgaSimulator（软件模拟 FPGA 行为），无需硬件/依赖。

运行 `python3 bridge_service.py`（或 `python3 -m audio_bridge.bridge_service`）
执行无硬件自测：PING/寄存器/DTMF/摘挂机/环回全链路。
加 `--port /dev/ttyUSB0` 可对真实硬件跑环回自测。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# 支持直接以脚本运行：把 host/ 目录加入 sys.path
try:
    from .serial_framer import (
        AUDIO_SAMPLES_PER_FRAME, DTMF_CODE_TO_DIGIT, Frame, FrameDecoder, FrameEncoder,
        HOOK_OFF, LOOPBACK_CROSS, LOOPBACK_UART, MAX_CHANNELS, REG_LOOPBACK, REG_SCRATCH,
        TYPE_AUDIO_DL, TYPE_AUDIO_UL, TYPE_DTMF_EVT, TYPE_HOOK_EVT, TYPE_PING, TYPE_PONG,
        TYPE_REG_RD, TYPE_REG_VAL, TYPE_REG_WR, TYPE_RING_CMD,
        encode_frame, pack_audio, pack_dtmf_evt, pack_hook_evt, pack_reg_rd, pack_reg_wr,
        pack_ring_cmd, unpack_audio, unpack_dtmf_evt, unpack_hook_evt, unpack_reg_val,
    )
    from .audio_io import G711_FRAME_BYTES
except ImportError:  # 直接运行脚本时的导入回退
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from audio_bridge.serial_framer import (  # type: ignore
        AUDIO_SAMPLES_PER_FRAME, DTMF_CODE_TO_DIGIT, Frame, FrameDecoder, FrameEncoder,
        HOOK_OFF, LOOPBACK_CROSS, LOOPBACK_UART, MAX_CHANNELS, REG_LOOPBACK, REG_SCRATCH,
        TYPE_AUDIO_DL, TYPE_AUDIO_UL, TYPE_DTMF_EVT, TYPE_HOOK_EVT, TYPE_PING, TYPE_PONG,
        TYPE_REG_RD, TYPE_REG_VAL, TYPE_REG_WR, TYPE_RING_CMD,
        encode_frame, pack_audio, pack_dtmf_evt, pack_hook_evt, pack_reg_rd, pack_reg_wr,
        pack_ring_cmd, unpack_audio, unpack_dtmf_evt, unpack_hook_evt, unpack_reg_val,
    )
    from audio_bridge.audio_io import G711_FRAME_BYTES  # type: ignore

__all__ = [
    "BridgeConfig", "JitterBuffer", "BridgeService",
    "SerialTransport", "FakeSerialTransport", "FpgaSimulator",
]

# 回调类型别名
AudioUlCallback = Callable[[int, bytes, float], None]        # (ch, g711[160], ts)
DtmfCallback = Callable[[int, str, int], None]               # (ch, digit, dur_ms)
HookCallback = Callable[[int, bool], None]                   # (ch, off_hook)
RegValCallback = Callable[[int, int], None]                  # (addr, val)
LogCallback = Callable[[str], None]                          # 日志行


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class BridgeConfig:
    """串口与桥服务参数。"""
    port: str = "/dev/ttyUSB0"       # 串口设备
    baudrate: int = 921600           # 协议默认 921600 8N1 无流控
    read_chunk: int = 4096           # 单次读串口字节数
    read_timeout: float = 0.05       # 串口读超时（s）
    ul_jitter_frames: int = 3        # 上行抖动缓冲深度（帧，3 帧 = 60 ms）
    dl_jitter_frames: int = 250      # 下行抖动缓冲上限（帧，250 帧 = 5 s）
    pace_dl: bool = True             # 下行按 20 ms 节拍发送（False = 背靠背直发）
    reg_timeout: float = 0.5         # REG_RD 等待 REG_VAL 超时（s）
    ping_timeout: float = 0.5        # PING 等待 PONG 超时（s）
    tx_queue_frames: int = 500       # 发送队列上限（帧）


# ---------------------------------------------------------------------------
# 抖动缓冲（每通道 20 ms 帧队列）
# ---------------------------------------------------------------------------

class JitterBuffer:
    """线程安全的定长帧队列（音频抖动缓冲）。

    * put()    入队一帧；队满按 overflow_policy 丢帧（计入 dropped）；
    * get()    出队一帧，队空可等待超时；
    * clear()  清空（打断/复位）。
    """

    def __init__(self, depth: int = 3, overflow_policy: str = "drop_oldest") -> None:
        """depth: 队列深度（帧）；overflow_policy: 'drop_oldest' 或 'drop_newest'。"""
        if depth < 1:
            raise ValueError("抖动缓冲深度必须 >= 1")
        if overflow_policy not in ("drop_oldest", "drop_newest"):
            raise ValueError(f"非法溢出策略: {overflow_policy}")
        self.depth = depth
        self.overflow_policy = overflow_policy
        self._q: List[bytes] = []
        self._cond = threading.Condition()
        self.pushed = 0        # 统计：入队帧数
        self.popped = 0        # 统计：出队帧数
        self.dropped = 0       # 统计：溢出丢弃帧数

    def put(self, frame: bytes) -> bool:
        """入队一帧。溢出时按策略丢帧。返回是否成功入队。"""
        with self._cond:
            self.pushed += 1
            if len(self._q) >= self.depth:
                self.dropped += 1
                if self.overflow_policy == "drop_oldest":
                    self._q.pop(0)
                else:
                    self._cond.notify()
                    return False
            self._q.append(bytes(frame))
            self._cond.notify()
            return True

    def get(self, timeout: float = 0.02) -> Optional[bytes]:
        """出队一帧；队空且超时返回 None。"""
        with self._cond:
            if not self._q:
                self._cond.wait(timeout)
            if not self._q:
                return None
            self.popped += 1
            return self._q.pop(0)

    def clear(self) -> int:
        """清空队列，返回丢弃帧数。"""
        with self._cond:
            n = len(self._q)
            self._q.clear()
            return n

    @property
    def size(self) -> int:
        """当前队列长度（帧）。"""
        with self._cond:
            return len(self._q)

    @property
    def depth_ms(self) -> float:
        """当前缓冲时长（ms），按 20 ms/帧计。"""
        return self.size * 20.0


# ---------------------------------------------------------------------------
# 串口传输层（真实 pyserial / 内置模拟器共用接口）
# ---------------------------------------------------------------------------

class SerialTransport:
    """pyserial 串口传输封装（真实硬件）。"""

    def __init__(self, port: str, baudrate: int = 921600, read_timeout: float = 0.05) -> None:
        try:
            import serial  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "缺少 pyserial 依赖，无法打开真实串口。"
                "请先安装：pip install pyserial（或使用模拟传输做自测）"
            ) from exc
        self._serial = serial.Serial(
            port=port, baudrate=baudrate, bytesize=8, parity="N", stopbits=1,
            timeout=read_timeout, write_timeout=1.0,
        )

    def read(self, max_bytes: int) -> bytes:
        """读串口（阻塞至超时），返回字节串。"""
        return self._serial.read(max_bytes)

    def write(self, data: bytes) -> None:
        """写串口。"""
        self._serial.write(data)

    @property
    def is_open(self) -> bool:
        """串口是否已打开。"""
        return bool(self._serial.is_open)

    def close(self) -> None:
        """关闭串口。"""
        self._serial.close()


class FakeSerialTransport:
    """内存模拟串口：write() 直通 FpgaSimulator，read() 取模拟器响应。

    用途：无硬件自测 / CI 回归。接口与 SerialTransport 一致。
    """

    def __init__(self, simulator: "FpgaSimulator", read_timeout: float = 0.05) -> None:
        self._sim = simulator
        self._rx = bytearray()
        self._cond = threading.Condition()
        self._open = True
        self._read_timeout = read_timeout

    def read(self, max_bytes: int) -> bytes:
        with self._cond:
            if not self._rx:
                self._cond.wait(self._read_timeout)
            out = bytes(self._rx[:max_bytes])
            del self._rx[:max_bytes]
            return out

    def write(self, data: bytes) -> None:
        resp = self._sim.feed(bytes(data))
        if resp:
            with self._cond:
                self._rx.extend(resp)
                self._cond.notify()

    def inject(self, data: bytes) -> None:
        """模拟 FPGA 主动上报（DTMF/HOOK 事件等）。"""
        with self._cond:
            self._rx.extend(data)
            self._cond.notify()

    @property
    def is_open(self) -> bool:
        return self._open

    def close(self) -> None:
        self._open = False
        with self._cond:
            self._cond.notify_all()


class FpgaSimulator:
    """FPGA 协议行为模拟器（自测用），实现协议 §2/§3 语义：

    * PING → PONG；
    * REG_WR → 写寄存器；REG_RD → 回 REG_VAL；
    * AUDIO_DL → LOOPBACK bit0/bit1 置位时按原样（bit2 置位则换通道）回 AUDIO_UL；
    * 支持注入 DTMF_EVT / HOOK_EVT 模拟用户动作。
    """

    def __init__(self, channels: int = 2) -> None:
        self.channels = channels
        self.regs: Dict[int, int] = {
            0x00: 0x00, 0x01: 0x03, 0x02: 0x00, 0x03: 0x40,
            0x04: 0x03, 0x05: 0x00, 0x06: 0x12, 0x07: 0x13, 0x08: 0x10,
        }
        self._dec = FrameDecoder()
        self._enc = FrameEncoder()
        self.dl_received = 0     # 统计：收到的 AUDIO_DL 帧数

    def feed(self, data: bytes) -> bytes:
        """喂入上位机字节，返回应答字节（可能为空）。"""
        out = bytearray()
        for frame in self._dec.feed(data):
            if frame.type == TYPE_PING:
                out += self._enc.build(TYPE_PONG)
            elif frame.type == TYPE_REG_WR:
                if len(frame.payload) == 2:
                    addr, val = frame.payload[0], frame.payload[1]
                    self.regs[addr] = val
            elif frame.type == TYPE_REG_RD:
                if len(frame.payload) == 1:
                    addr = frame.payload[0]
                    val = self.regs.get(addr, 0x00)
                    out += self._enc.build(TYPE_REG_VAL, bytes((addr, val)))
            elif frame.type == TYPE_AUDIO_DL:
                self.dl_received += 1
                try:
                    ch, g711 = unpack_audio(frame.payload)
                except ValueError:
                    continue
                loopback = self.regs.get(REG_LOOPBACK, 0x00)
                if loopback & (LOOPBACK_UART | 0x02):
                    if loopback & LOOPBACK_CROSS:
                        ch ^= 1
                    out += self._enc.build(TYPE_AUDIO_UL, pack_audio(TYPE_AUDIO_UL, ch, g711))
        return bytes(out)

    def inject_dtmf(self, channel: int, digit: str, dur_ms: int = 120) -> bytes:
        """构造 DTMF_EVT 上报字节（模拟用户按键）。"""
        return self._enc.build(TYPE_DTMF_EVT, pack_dtmf_evt(channel, digit, dur_ms))

    def inject_hook(self, channel: int, off_hook: bool) -> bytes:
        """构造 HOOK_EVT 上报字节（模拟摘/挂机）。"""
        return self._enc.build(TYPE_HOOK_EVT, pack_hook_evt(channel, off_hook))


# ---------------------------------------------------------------------------
# 桥服务
# ---------------------------------------------------------------------------

class BridgeService:
    """串口音频桥服务。

    线程模型：
        * reader 线程：串口读 → FrameDecoder → 分发回调 / 入上行抖动缓冲；
        * dl_pacer 线程：从下行抖动缓冲按 20 ms 节拍取帧发串口（pace_dl=True 时）。

    上层用法：
        bridge = BridgeService(cfg, on_audio_ul=..., on_dtmf=..., on_hook=...)
        bridge.start()
        bridge.send_audio_dl(0, g711_frame)
        bridge.reg_write(REG_LOOPBACK, 0x00)
        bridge.stop()
    """

    def __init__(
        self,
        config: Optional[BridgeConfig] = None,
        transport: Optional[object] = None,     # SerialTransport / FakeSerialTransport
        on_audio_ul: Optional[AudioUlCallback] = None,
        on_dtmf: Optional[DtmfCallback] = None,
        on_hook: Optional[HookCallback] = None,
        on_reg_val: Optional[RegValCallback] = None,
        on_log: Optional[LogCallback] = None,
    ) -> None:
        self.cfg = config or BridgeConfig()
        self._transport = transport
        self.on_audio_ul = on_audio_ul
        self.on_dtmf = on_dtmf
        self.on_hook = on_hook
        self.on_reg_val = on_reg_val
        self.on_log = on_log

        self._dec = FrameDecoder()
        self._enc = FrameEncoder()
        self._tx_lock = threading.Lock()        # 组帧 + 写串口的原子性
        self._running = False
        self._reader_thread: Optional[threading.Thread] = None
        self._dl_thread: Optional[threading.Thread] = None

        # 每通道上行抖动缓冲（协议首版 2 通道，按 MAX_CHANNELS 预留）
        self.ul_jitter: List[JitterBuffer] = [
            JitterBuffer(depth=self.cfg.ul_jitter_frames, overflow_policy="drop_oldest")
            for _ in range(MAX_CHANNELS)
        ]
        # 下行帧队列（发送线程消费）；用 JitterBuffer 承载，深度可配
        self._dl_queue = JitterBuffer(depth=self.cfg.tx_queue_frames, overflow_policy="drop_oldest")

        # REG_RD 同步等待
        self._reg_events: Dict[int, threading.Event] = {}
        self._reg_values: Dict[int, int] = {}
        self._reg_lock = threading.Lock()
        # PONG 同步等待
        self._pong_event = threading.Event()
        self._last_ping_ts = 0.0

        # 统计
        self.tx_frames = 0
        self.rx_frames = 0
        self.parse_errors_before = 0

    # ---- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        """打开传输并启动收发线程。"""
        if self._running:
            return
        if self._transport is None:
            self._transport = SerialTransport(
                self.cfg.port, self.cfg.baudrate, self.cfg.read_timeout)
        self._running = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="bridge-reader", daemon=True)
        self._reader_thread.start()
        if self.cfg.pace_dl:
            self._dl_thread = threading.Thread(
                target=self._dl_pacer_loop, name="bridge-dl-pacer", daemon=True)
            self._dl_thread.start()
        self._log(f"桥服务已启动 port={self.cfg.port} baud={self.cfg.baudrate} "
                  f"pace_dl={self.cfg.pace_dl}")

    def stop(self) -> None:
        """停止线程并关闭传输。"""
        self._running = False
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
        for t in (self._reader_thread, self._dl_thread):
            if t is not None and t.is_alive():
                t.join(timeout=1.0)
        self._log("桥服务已停止")

    @property
    def running(self) -> bool:
        """服务是否运行中。"""
        return self._running

    # ---- 接收 -----------------------------------------------------------

    def _reader_loop(self) -> None:
        """串口接收线程：读字节 → 解帧 → 分发。"""
        while self._running:
            try:
                data = self._transport.read(self.cfg.read_chunk)
            except Exception as exc:
                if self._running:
                    self._log(f"串口读异常: {exc}")
                    time.sleep(0.1)
                    continue
                break
            if not data:
                continue
            for frame in self._dec.feed(data):
                self._dispatch(frame)

    def _dispatch(self, frame: Frame) -> None:
        """把一个已校验的帧分发到回调 / 缓冲 / 同步等待者。"""
        self.rx_frames += 1
        try:
            if frame.type == TYPE_AUDIO_UL:
                ch, g711 = unpack_audio(frame.payload)
                ts = time.monotonic()
                if 0 <= ch < MAX_CHANNELS:
                    self.ul_jitter[ch].put(g711)
                if self.on_audio_ul:
                    self.on_audio_ul(ch, g711, ts)
            elif frame.type == TYPE_DTMF_EVT:
                ch, digit, dur = unpack_dtmf_evt(frame.payload)
                if self.on_dtmf:
                    self.on_dtmf(ch, digit, dur)
            elif frame.type == TYPE_HOOK_EVT:
                ch, off_hook = unpack_hook_evt(frame.payload)
                if self.on_hook:
                    self.on_hook(ch, off_hook)
            elif frame.type == TYPE_REG_VAL:
                addr, val = unpack_reg_val(frame.payload)
                with self._reg_lock:
                    self._reg_values[addr] = val
                    ev = self._reg_events.get(addr)
                    if ev:
                        ev.set()
                if self.on_reg_val:
                    self.on_reg_val(addr, val)
            elif frame.type == TYPE_PING:
                # 协议要求收到 PING 立即回 PONG
                self._send_frame(TYPE_PONG)
            elif frame.type == TYPE_PONG:
                self._pong_event.set()
        except ValueError as exc:
            self._log(f"载荷解析失败 {frame.type_name}: {exc}")

    # ---- 发送 -----------------------------------------------------------

    def _send_frame(self, ftype: int, payload: bytes = b"") -> None:
        """组帧并写串口（线程安全）。"""
        raw = self._enc.build(ftype, payload)
        with self._tx_lock:
            self._transport.write(raw)
        self.tx_frames += 1

    def send_audio_dl(self, channel: int, g711: bytes) -> None:
        """发送一帧下行音频（160 字节 G.711）。

        pace_dl=True 时进入下行队列由发送线程按 20 ms 节拍发出；
        False 时背靠背立即发送（协议允许）。
        """
        payload = pack_audio(TYPE_AUDIO_DL, channel, g711)
        if self.cfg.pace_dl:
            self._dl_queue.put(payload)  # 只缓存载荷，发送线程组帧保序
        else:
            self._send_frame(TYPE_AUDIO_DL, payload)

    def _dl_pacer_loop(self) -> None:
        """下行发送线程：按 20 ms 节拍从队列取载荷组帧发送。"""
        interval = AUDIO_SAMPLES_PER_FRAME / 8000.0  # 20 ms
        next_t = time.monotonic()
        while self._running:
            payload = self._dl_queue.get(timeout=interval)
            if payload is not None:
                self._send_frame(TYPE_AUDIO_DL, payload)
            next_t += interval
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()  # 落后太多则重新对齐

    def send_ring(self, channel: int, on: bool) -> None:
        """振铃控制：on=True 开始振铃，False 停止。"""
        self._send_frame(TYPE_RING_CMD, pack_ring_cmd(channel, on))

    def reg_write(self, addr: int, val: int) -> None:
        """写配置寄存器（§3）。"""
        self._send_frame(TYPE_REG_WR, pack_reg_wr(addr, val))

    def reg_read(self, addr: int, timeout: Optional[float] = None) -> int:
        """读配置寄存器：发送 REG_RD 并同步等待 REG_VAL。

        超时抛 TimeoutError；等待期间同一地址的并发读用内部锁串行化。
        """
        timeout = self.cfg.reg_timeout if timeout is None else timeout
        with self._reg_lock:
            ev = threading.Event()
            self._reg_events[addr] = ev
        try:
            self._send_frame(TYPE_REG_RD, pack_reg_rd(addr))
            if not ev.wait(timeout):
                raise TimeoutError(f"REG_RD 0x{addr:02X} 等待 REG_VAL 超时（{timeout}s）")
            with self._reg_lock:
                return self._reg_values.get(addr, 0)
        finally:
            with self._reg_lock:
                self._reg_events.pop(addr, None)

    def ping(self, timeout: Optional[float] = None) -> float:
        """发送 PING 并等待 PONG，返回往返时延（ms）。"""
        timeout = self.cfg.ping_timeout if timeout is None else timeout
        self._pong_event.clear()
        self._last_ping_ts = time.monotonic()
        self._send_frame(TYPE_PING)
        if not self._pong_event.wait(timeout):
            raise TimeoutError(f"PING 等待 PONG 超时（{timeout}s）")
        return (time.monotonic() - self._last_ping_ts) * 1000.0

    # ---- 上行取帧（供 ASR 管道拉取） ------------------------------------

    def read_ul_g711(self, channel: int, timeout: float = 0.02) -> Optional[bytes]:
        """从通道上行抖动缓冲取一帧 G.711（160 字节），空则返回 None。"""
        if not (0 <= channel < MAX_CHANNELS):
            raise ValueError(f"通道号越界: {channel}")
        return self.ul_jitter[channel].get(timeout)

    # ---- 环回自测 -------------------------------------------------------

    def run_loopback_test(self, channel: int = 0, frames: int = 20,
                          timeout: float = 2.0) -> Dict[str, object]:
        """环回自测（协议 §3 REG_LOOPBACK bit0：UART 音频环回 DL→UL）。

        流程：REG_WR LOOPBACK=0x01 → 经下行队列发送递增图案的 AUDIO_DL 帧 →
        校验 AUDIO_UL 回声（通道号 + 载荷完全一致）→ 恢复 LOOPBACK=0x00。
        测试期间临时加深上行抖动缓冲，避免收集期间溢出丢帧。

        返回 {'passed': bool, 'sent': n, 'received': n, 'matched': n, 'detail': str}
        """
        expected = [
            bytes(((i * 7 + k) & 0xFF) for k in range(G711_FRAME_BYTES))
            for i in range(frames)
        ]
        # 临时替换深缓冲，测试结束后恢复
        orig_jb = self.ul_jitter[channel]
        self.ul_jitter[channel] = JitterBuffer(depth=frames + 16, overflow_policy="drop_oldest")
        received: List[bytes] = []
        try:
            self.reg_write(REG_LOOPBACK, LOOPBACK_UART)
            time.sleep(0.02)  # 等配置生效
            self.ul_jitter[channel].clear()
            # 经下行发送链路（pace_dl 时 20 ms 节拍，避免瞬时淹没接收缓冲）
            for g711 in expected:
                self.send_audio_dl(channel, g711)
            deadline = time.monotonic() + timeout
            while len(received) < frames and time.monotonic() < deadline:
                frame = self.ul_jitter[channel].get(timeout=0.05)
                if frame is not None:
                    received.append(frame)
        finally:
            self.reg_write(REG_LOOPBACK, 0x00)
            self.ul_jitter[channel] = orig_jb

        # 顺序对齐比较：回声序列应与发送图案完全一致
        matched = 0
        j = 0
        for g711 in expected:
            while j < len(received) and received[j] != g711:
                j += 1
            if j < len(received) and received[j] == g711:
                matched += 1
                j += 1
        passed = matched == frames and len(received) == frames
        detail = (f"sent={frames} received={len(received)} matched={matched}"
                  + ("" if passed else "（环回数据不匹配或有丢帧）"))
        return {"passed": passed, "sent": frames, "received": len(received),
                "matched": matched, "detail": detail}

    # ---- 统计 / 日志 ----------------------------------------------------

    def stats(self) -> Dict[str, object]:
        """聚合统计：帧计数、解码器错误计数、抖动缓冲状态。"""
        return {
            "tx_frames": self.tx_frames,
            "rx_frames": self.rx_frames,
            "decoder": self._dec.stats.snapshot(),
            "ul_jitter": [
                {"size": jb.size, "pushed": jb.pushed, "popped": jb.popped,
                 "dropped": jb.dropped}
                for jb in self.ul_jitter
            ],
            "dl_queue": {"size": self._dl_queue.size, "dropped": self._dl_queue.dropped},
        }

    def _log(self, msg: str) -> None:
        """日志输出（默认打到 stderr，可挂接上层日志）。"""
        if self.on_log:
            self.on_log(msg)
        else:
            print(f"[bridge] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 自测（python3 bridge_service.py [--port ...]）
# ---------------------------------------------------------------------------

def _self_test(port: Optional[str] = None) -> bool:
    """无硬件自测（FpgaSimulator）或真实硬件环回自测。"""
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

    print("== bridge_service 自测 ==")

    dtmf_seen: List[tuple] = []
    hook_seen: List[tuple] = []

    if port:
        print(f"  模式: 真实硬件 {port}")
        transport = SerialTransport(port, 921600)
    else:
        print("  模式: FpgaSimulator 软件模拟（无需硬件）")
        sim = FpgaSimulator()
        transport = FakeSerialTransport(sim)

    cfg = BridgeConfig(port=port or "sim", pace_dl=True)
    bridge = BridgeService(
        cfg, transport=transport,
        on_dtmf=lambda ch, d, ms: dtmf_seen.append((ch, d, ms)),
        on_hook=lambda ch, off: hook_seen.append((ch, off)),
    )
    bridge.start()
    time.sleep(0.1)

    # 1) PING/PONG 往返
    try:
        rtt = bridge.ping(timeout=1.0)
        check("PING/PONG 往返", rtt >= 0, f"rtt={rtt:.2f}ms")
    except TimeoutError as exc:
        check("PING/PONG 往返", False, str(exc))

    # 2) 寄存器读写（SCRATCH）
    try:
        bridge.reg_write(REG_SCRATCH, 0x5A)
        val = bridge.reg_read(REG_SCRATCH, timeout=1.0)
        check("REG 写读 SCRATCH == 0x5A", val == 0x5A, f"读回 0x{val:02X}")
        ver = bridge.reg_read(0x08, timeout=1.0)
        check("REG 读 VERSION", isinstance(ver, int), f"0x{ver:02X}")
    except TimeoutError as exc:
        check("REG 写读 SCRATCH == 0x5A", False, str(exc))

    # 3) 环回自测（REG LOOPBACK=0x01 时验证 DL→UL）
    result = bridge.run_loopback_test(channel=0, frames=20, timeout=2.0)
    check("环回自测（LOOPBACK=0x01）", bool(result["passed"]), str(result["detail"]))

    # 4) DTMF / 摘挂机事件上报（模拟器注入）
    if not port:
        transport.inject(sim.inject_dtmf(0, "5", 120))
        transport.inject(sim.inject_dtmf(1, "#", 90))
        transport.inject(sim.inject_hook(0, True))
        time.sleep(0.2)
        check("DTMF 事件回调", dtmf_seen == [(0, "5", 120), (1, "#", 90)], str(dtmf_seen))
        check("HOOK 事件回调", hook_seen == [(0, True)], str(hook_seen))

    # 5) 统计
    st = bridge.stats()
    check("统计可读", st["rx_frames"] > 0 and "decoder" in st, str(st["decoder"]))

    bridge.stop()
    print("== 结果: %s ==" % ("PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="串口音频桥服务自测")
    ap.add_argument("--port", default=None, help="真实串口（如 /dev/ttyUSB0）；缺省用模拟器")
    args = ap.parse_args()
    sys.exit(0 if _self_test(args.port) else 1)
