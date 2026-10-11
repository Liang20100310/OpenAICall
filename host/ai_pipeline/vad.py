#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VAD：能量静音检测（语音活动检测）。

电话场景的轻量 VAD：基于帧 RMS 能量 + 自适应噪声底限 + 挂尾（hangover）平滑，
纯标准库实现，输入为 20 ms / 8 kHz / 16 bit 单声道 PCM 帧（320 字节）。

输出事件（每帧一个）：
    SILENCE      —— 静音
    SPEECH_START —— 语音开始（由静音进入语音，只上报一次）
    SPEECH       —— 语音持续
    SPEECH_END   —— 语音结束（静音超过挂尾帧数后上报一次）

典型用法（pipeline 每收到一帧上行 PCM 调用一次）：
    vad = EnergyVAD(VADConfig())
    evt = vad.process(pcm_frame)
    if evt == VadEvent.SPEECH_START: ...
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

try:
    from .audio_io import frame_rms  # type: ignore
except ImportError:  # 直接以脚本运行时的回退
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from audio_bridge.audio_io import frame_rms  # type: ignore

__all__ = ["VadEvent", "VADConfig", "EnergyVAD"]


class VadEvent(Enum):
    """VAD 每帧输出的事件。"""
    SILENCE = "silence"
    SPEECH_START = "speech_start"
    SPEECH = "speech"
    SPEECH_END = "speech_end"


@dataclass
class VADConfig:
    """VAD 参数。"""
    sample_rate: int = 8000          # 采样率（Hz）
    frame_ms: int = 20               # 帧长（ms）
    threshold: float = 500.0         # 能量门限（RMS，16bit 域）；低于视为静音
    snr_ratio: float = 2.5           # 自适应模式下，语音需高于噪声底限 × snr_ratio
    adaptive: bool = True            # 是否自适应跟踪噪声底限
    noise_alpha: float = 0.95        # 噪声底限 EMA 系数（越大越慢）
    hangover_frames: int = 10        # 挂尾帧数（静音多久判定语音结束，10 帧 = 200 ms）
    min_speech_frames: int = 2       # 最少语音帧数（防止单帧毛刺触发）


class EnergyVAD:
    """能量 VAD：帧 RMS + 自适应噪声底限 + 挂尾状态机。"""

    def __init__(self, config: Optional[VADConfig] = None) -> None:
        """config: VADConfig；缺省用默认参数。"""
        self.cfg = config or VADConfig()
        self._in_speech = False          # 状态机是否处于语音段
        self._speech_frames = 0          # 当前连续语音帧数
        self._silence_frames = 0         # 当前连续静音帧数
        self._noise_floor = self.cfg.threshold / self.cfg.snr_ratio  # 噪声底限初值
        self._pending_start = False      # 已累计够 min_speech_frames，待上报 START
        self.last_rms = 0.0              # 最近一帧 RMS（调试/自适应展示用）
        self.speech_segments = 0         # 统计：累计语音段数

    # ---- 对外接口 -------------------------------------------------------

    def process(self, pcm_frame: bytes) -> VadEvent:
        """处理一帧 PCM，返回 VadEvent。"""
        rms = frame_rms(pcm_frame)
        self.last_rms = rms

        # 有效门限 = max(绝对门限, 噪声底限 × SNR 比)
        eff_thr = self.cfg.threshold
        if self.cfg.adaptive:
            eff_thr = max(eff_thr, self._noise_floor * self.cfg.snr_ratio)

        is_speech_frame = rms > eff_thr

        # 自适应噪声底限：仅在非语音帧时更新（EMA）
        if self.cfg.adaptive and not is_speech_frame:
            a = self.cfg.noise_alpha
            self._noise_floor = a * self._noise_floor + (1 - a) * rms

        if is_speech_frame:
            self._silence_frames = 0
            self._speech_frames += 1
            if not self._in_speech:
                if self._speech_frames >= self.cfg.min_speech_frames:
                    # 确认进入语音段：从第一帧起累计的都算这段语音
                    self._in_speech = True
                    self.speech_segments += 1
                    return VadEvent.SPEECH_START
                return VadEvent.SILENCE   # 毛刺保护：未达最少帧数
            return VadEvent.SPEECH

        # 静音帧
        self._speech_frames = 0
        if self._in_speech:
            self._silence_frames += 1
            if self._silence_frames >= self.cfg.hangover_frames:
                self._in_speech = False
                self._silence_frames = 0
                return VadEvent.SPEECH_END
            return VadEvent.SPEECH        # 挂尾期内仍视为语音延续（防截断）
        return VadEvent.SILENCE

    @property
    def in_speech(self) -> bool:
        """当前是否处于语音段（含挂尾期）。"""
        return self._in_speech

    @property
    def noise_floor(self) -> float:
        """当前自适应噪声底限（RMS）。"""
        return self._noise_floor

    def reset(self) -> None:
        """复位状态机（新通话开始时调用）。"""
        self._in_speech = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._noise_floor = self.cfg.threshold / self.cfg.snr_ratio
        self.last_rms = 0.0


# ---------------------------------------------------------------------------
# 自测（python3 ai_pipeline/vad.py）
# ---------------------------------------------------------------------------

def _self_test() -> bool:
    """构造"静音→语音→静音"序列验证状态机。"""
    import math

    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

    print("== vad 自测 ==")
    vad = EnergyVAD(VADConfig(threshold=500.0, hangover_frames=5, min_speech_frames=2))

    def frame(amp: float) -> bytes:
        """构造一帧 160 样点正弦 PCM（amp 为幅值）。"""
        samples = [int(amp * math.sin(i / 4.0)) for i in range(160)]
        return struct.pack("<160h", *samples)

    events: List[VadEvent] = []
    for _ in range(10):
        events.append(vad.process(frame(50)))        # 静音（低能量）
    events.append(vad.process(frame(8000)))          # 语音第 1 帧
    events.append(vad.process(frame(8000)))          # 语音第 2 帧（达 min_speech_frames → START）
    for _ in range(5):
        events.append(vad.process(frame(8000)))      # 语音持续
    for _ in range(4):
        events.append(vad.process(frame(50)))        # 挂尾期（仍算语音）
    events.append(vad.process(frame(50)))            # 第 5 个静音帧 → SPEECH_END
    events.append(vad.process(frame(50)))            # 静音

    check("初始静音事件", events[:10] == [VadEvent.SILENCE] * 10)
    check("SPEECH_START 恰好一次（毛刺保护）",
          events[10] == VadEvent.SILENCE and events[11] == VadEvent.SPEECH_START,
          str([e.value for e in events[9:13]]))
    check("语音持续事件", events[12:17] == [VadEvent.SPEECH] * 5)
    check("挂尾期仍视为语音", events[17:21] == [VadEvent.SPEECH] * 4)
    check("SPEECH_END 在挂尾后触发", events[21] == VadEvent.SPEECH_END)
    check("结束后回到静音", events[22] == VadEvent.SILENCE)
    check("语音段计数", vad.speech_segments == 1, str(vad.speech_segments))

    # 单帧毛刺不触发
    vad2 = EnergyVAD(VADConfig(threshold=500.0, hangover_frames=5, min_speech_frames=2))
    e1 = vad2.process(frame(8000))
    e2 = vad2.process(frame(50))
    check("单帧毛刺不触发", e1 == VadEvent.SILENCE and e2 == VadEvent.SILENCE)

    print("== 结果: %s ==" % ("PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_test() else 1)
