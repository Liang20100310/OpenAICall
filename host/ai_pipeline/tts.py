#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TTS 语音合成模块：统一接口 + 两个可运行实现 + 离线测试实现。

实现：
    * EdgeTTS         —— 微软 Edge 免费 TTS（可选依赖 edge-tts；输出 mp3，
                         需本机 ffmpeg 解码为 PCM，缺失时给出明确指引）；
    * OpenAICompatTTS —— OpenAI 兼容 /audio/speech HTTP API（请求 WAV 输出，
                         纯标准库解析，自动重采样到 8 kHz）；
    * ToneTTS         —— 离线/测试用：生成固定时长提示音（无依赖、可跑通全链路）。

统一接口：BaseTTS.synthesize(text) -> TTSChunk(pcm16, sample_rate)
输出统一为 8 kHz / 16 bit 单声道 PCM（可直接切 20 ms 帧下发）。
"""

from __future__ import annotations

import abc
import math
import struct
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional

try:
    from ._http import HttpError, normalize_openai_base, http_request  # type: ignore
    from audio_bridge.audio_io import SAMPLE_RATE, resample_pcm16, wav_to_pcm16  # type: ignore
except ImportError:  # 直接以脚本运行时的回退
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ai_pipeline._http import HttpError, normalize_openai_base, http_request  # type: ignore
    from audio_bridge.audio_io import SAMPLE_RATE, resample_pcm16, wav_to_pcm16  # type: ignore

__all__ = ["TTSChunk", "BaseTTS", "EdgeTTS", "OpenAICompatTTS", "ToneTTS", "create_tts"]


@dataclass
class TTSChunk:
    """一段合成音频。"""
    pcm: bytes              # 16 bit LE 单声道 PCM
    sample_rate: int        # 采样率（Hz），统一为 8000
    text: str = ""          # 原始文本（日志用）


class BaseTTS(abc.ABC):
    """TTS 抽象基类。"""

    @abc.abstractmethod
    def synthesize(self, text: str) -> TTSChunk:
        """合成一段语音，返回 8 kHz PCM。"""

    def close(self) -> None:
        """释放资源（默认无操作）。"""


class EdgeTTS(BaseTTS):
    """微软 Edge 在线 TTS（edge-tts）。

    流程：edge-tts 合成 mp3 字节 → ffmpeg 解码为 8 kHz/16bit/单声道 PCM。
    依赖：edge-tts（Python 包）+ ffmpeg（系统可执行文件），均缺时报明确指引。
    """

    def __init__(
        self,
        voice: str = "zh-CN-XiaoxiaoNeural",
        rate: str = "+0%",
        volume: str = "+0%",
        ffmpeg_bin: str = "ffmpeg",
    ) -> None:
        """voice: edge-tts 音色名；rate/volume: 语速/音量调节（如 "+10%"）。"""
        try:
            import edge_tts  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "edge-tts 未安装，无法使用 EdgeTTS。请执行：pip install edge-tts，"
                "并确保系统已安装 ffmpeg；或把 tts.type 改为 'openai_api' / 'tone'。"
            ) from exc
        self.voice = voice
        self.rate = rate
        self.volume = volume
        self.ffmpeg_bin = ffmpeg_bin

    def synthesize(self, text: str) -> TTSChunk:
        """文本 → mp3 → ffmpeg 解码 → 8 kHz PCM。"""
        if not text.strip():
            return TTSChunk(pcm=b"", sample_rate=SAMPLE_RATE, text=text)
        mp3 = self._synthesize_mp3(text)
        pcm = self._mp3_to_pcm(mp3)
        return TTSChunk(pcm=pcm, sample_rate=SAMPLE_RATE, text=text)

    def _synthesize_mp3(self, text: str) -> bytes:
        """调用 edge-tts（异步库）收集 mp3 字节。"""
        import asyncio

        import edge_tts  # type: ignore

        async def _run() -> bytes:
            communicate = edge_tts.Communicate(text, self.voice,
                                               rate=self.rate, volume=self.volume)
            buf = bytearray()
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 已有事件循环（如嵌在 async 框架）：用独立线程跑，避免死锁
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, _run()).result()
            return loop.run_until_complete(_run())
        except RuntimeError:
            return asyncio.run(_run())

    def _mp3_to_pcm(self, mp3: bytes) -> bytes:
        """ffmpeg 解码 mp3 → 8 kHz/16bit/单声道 PCM。"""
        if not mp3:
            return b""
        cmd = [
            self.ffmpeg_bin, "-hide_banner", "-loglevel", "error",
            "-f", "mp3", "-i", "pipe:0",
            "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
            "pipe:1",
        ]
        try:
            proc = subprocess.run(cmd, input=mp3, capture_output=True, timeout=30)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"未找到 ffmpeg（{self.ffmpeg_bin}），EdgeTTS 无法解码 mp3。"
                "请安装 ffmpeg 或改用 tts.type='openai_api'。"
            ) from exc
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg 解码失败: {proc.stderr.decode('utf-8', 'replace')[:300]}")
        return proc.stdout


class OpenAICompatTTS(BaseTTS):
    """OpenAI 兼容 /audio/speech HTTP API（WAV 输出）。

    兼容 OpenAI / 各类网关；请求 response_format="wav"，标准库解析后
    自动重采样到 8 kHz 单声道 PCM。
    """

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "tts-1",
        voice: str = "alloy",
        speed: float = 1.0,
        timeout: float = 60.0,
    ) -> None:
        """参数与 OpenAI audio speech API 对齐。"""
        self.base = normalize_openai_base(base_url)
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.speed = speed
        self.timeout = timeout

    def synthesize(self, text: str) -> TTSChunk:
        """文本 → WAV → 解析 → 8 kHz PCM。"""
        if not text.strip():
            return TTSChunk(pcm=b"", sample_rate=SAMPLE_RATE, text=text)
        import json
        payload = {
            "model": self.model,
            "voice": self.voice,
            "input": text,
            "response_format": "wav",
            "speed": self.speed,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            _status, _hdrs, body = http_request(
                f"{self.base}/audio/speech", "POST", headers,
                json.dumps(payload).encode("utf-8"), timeout=self.timeout)
        except HttpError as exc:
            raise RuntimeError(f"TTS API 调用失败: {exc}") from exc
        try:
            pcm, rate, _nch = wav_to_pcm16(body)
        except Exception as exc:
            raise RuntimeError(f"TTS 响应不是合法 WAV: {exc}") from exc
        if rate != SAMPLE_RATE:
            pcm = resample_pcm16(pcm, rate, SAMPLE_RATE)
        return TTSChunk(pcm=pcm, sample_rate=SAMPLE_RATE, text=text)


class ToneTTS(BaseTTS):
    """离线/测试用 TTS：按文本长度生成 440 Hz 提示音（8 kHz PCM）。

    无任何依赖，用于无网络环境下跑通 pipeline / 验证下行链路。
    """

    def __init__(self, freq: float = 440.0, amplitude: float = 0.3) -> None:
        """freq: 音调（Hz）；amplitude: 幅值（0..1）。"""
        self.freq = freq
        self.amplitude = amplitude

    def synthesize(self, text: str) -> TTSChunk:
        """按文本长度合成提示音：时长 = 0.15 + 0.03 × 字符数（上限 3 s）。"""
        if not text.strip():
            return TTSChunk(pcm=b"", sample_rate=SAMPLE_RATE, text=text)
        duration = min(3.0, 0.15 + 0.03 * max(len(text), 1))
        n = int(duration * SAMPLE_RATE)
        amp = int(32767 * self.amplitude)
        samples = [int(amp * math.sin(2 * math.pi * self.freq * i / SAMPLE_RATE))
                   for i in range(n)]
        return TTSChunk(pcm=struct.pack(f"<{n}h", *samples), sample_rate=SAMPLE_RATE,
                        text=text)


def create_tts(cfg: Dict[str, Any]) -> BaseTTS:
    """按配置创建 TTS（cfg 为 config.yaml 的 tts 段）。

    type: 'edge' | 'openai_api' | 'tone'
    """
    kind = str(cfg.get("type", "edge")).lower()
    if kind in ("edge", "edge_tts", "edge-tts"):
        return EdgeTTS(
            voice=cfg.get("voice", "zh-CN-XiaoxiaoNeural"),
            rate=cfg.get("rate", "+0%"),
            volume=cfg.get("volume", "+0%"),
            ffmpeg_bin=cfg.get("ffmpeg_bin", "ffmpeg"),
        )
    if kind in ("openai_api", "openai", "http"):
        return OpenAICompatTTS(
            base_url=cfg.get("base_url", "https://api.openai.com/v1"),
            api_key=cfg.get("api_key", ""),
            model=cfg.get("model", "tts-1"),
            voice=cfg.get("voice", "alloy"),
            speed=float(cfg.get("speed", 1.0)),
            timeout=float(cfg.get("timeout", 60.0)),
        )
    if kind in ("tone", "simulated", "offline"):
        return ToneTTS(
            freq=float(cfg.get("freq", 440.0)),
            amplitude=float(cfg.get("amplitude", 0.3)),
        )
    raise ValueError(f"未知 tts.type: {kind!r}（可选 edge / openai_api / tone）")


# ---------------------------------------------------------------------------
# 自测（python3 ai_pipeline/tts.py）—— 离线部分
# ---------------------------------------------------------------------------

def _self_test() -> bool:
    """ToneTTS / 依赖缺失报错提示 校验。"""
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

    print("== tts 自测（离线） ==")
    tone = ToneTTS()
    chunk = tone.synthesize("测试")
    n_samples = len(chunk.pcm) // 2
    check("ToneTTS 输出 8kHz PCM",
          chunk.sample_rate == 8000 and len(chunk.pcm) % 2 == 0 and n_samples > 0,
          f"{n_samples} 样点")
    check("ToneTTS 时长随文本增长",
          len(tone.synthesize("这是一段比较长的测试文本").pcm) > len(chunk.pcm))
    check("空文本返回空", tone.synthesize("").pcm == b"")

    # create_tts 分派
    t = create_tts({"type": "tone"})
    check("create_tts('tone')", isinstance(t, ToneTTS))

    # 缺依赖时报错清晰（不崩溃）
    import importlib.util
    if importlib.util.find_spec("edge_tts") is None:
        try:
            create_tts({"type": "edge"})
            check("缺 edge-tts 时明确报错", False, "未抛异常")
        except RuntimeError as exc:
            check("缺 edge-tts 时明确报错", "pip install edge-tts" in str(exc), str(exc)[:60])
    else:
        print("  [SKIP] edge-tts 已安装，跳过缺依赖报错检查")

    print("== 结果: %s ==" % ("PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_test() else 1)
