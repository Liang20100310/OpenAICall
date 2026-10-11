#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ASR（语音识别）模块：统一接口 + 两个可运行实现 + 离线模拟实现。

实现：
    * FasterWhisperASR      —— faster-whisper 本地推理（可选依赖，缺则报安装指引）；
    * OpenAICompatibleASR   —— OpenAI 兼容 /audio/transcriptions HTTP API
                              （纯标准库实现，兼容 Whisper.cpp server / Copilot /
                               OpenClaw 网关等任何兼容端点）；
    * SimulatedASR          —— 离线/测试用：从队列取文本，不依赖网络与模型。

统一接口：BaseASR.transcribe(pcm16, sample_rate) -> str
输入为 8 kHz / 16 bit 单声道 PCM（G.711 解码后）。
"""

from __future__ import annotations

import abc
import queue
from typing import Any, Dict, Optional

try:
    from ._http import HttpError, normalize_openai_base, post_multipart  # type: ignore
    from audio_bridge.audio_io import pcm16_to_wav  # type: ignore
except ImportError:  # 直接以脚本运行时的回退
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ai_pipeline._http import HttpError, normalize_openai_base, post_multipart  # type: ignore
    from audio_bridge.audio_io import pcm16_to_wav  # type: ignore

__all__ = ["BaseASR", "ASRResult", "FasterWhisperASR", "OpenAICompatibleASR",
           "SimulatedASR", "create_asr"]


class ASRResult:
    """识别结果。"""

    __slots__ = ("text", "language", "confidence")

    def __init__(self, text: str, language: str = "", confidence: float = 0.0) -> None:
        """text: 识别文本；language: 语种；confidence: 置信度（无则 0）。"""
        self.text = text
        self.language = language
        self.confidence = confidence

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ASRResult(text={self.text!r}, language={self.language!r}, confidence={self.confidence})"


class BaseASR(abc.ABC):
    """ASR 抽象基类。"""

    @abc.abstractmethod
    def transcribe(self, pcm16: bytes, sample_rate: int = 8000) -> str:
        """识别一段 16 bit 单声道 PCM，返回文本（可能为空串）。"""

    def transcribe_detailed(self, pcm16: bytes, sample_rate: int = 8000) -> ASRResult:
        """识别并返回详细结果（默认包装 transcribe）。"""
        return ASRResult(self.transcribe(pcm16, sample_rate))

    def close(self) -> None:
        """释放资源（默认无操作）。"""


class FasterWhisperASR(BaseASR):
    """faster-whisper 本地推理（CTranslate2 加速）。

    依赖：faster-whisper（含 numpy / ctranslate2）。缺失时给出明确安装指引。
    """

    def __init__(
        self,
        model_size: str = "small",
        device: str = "auto",
        compute_type: str = "default",
        language: Optional[str] = "zh",
        beam_size: int = 1,
    ) -> None:
        """model_size: tiny/base/small/medium/large-v3；language: 语种（None=自动）。"""
        try:
            import numpy  # noqa: F401  (faster-whisper 依赖)
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper 未安装，无法使用本地 ASR。"
                "请执行：pip install faster-whisper，"
                "或把 stt.type 改为 'openai_api' 使用 HTTP API。"
            ) from exc
        self._np = numpy
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self.language = language
        self.beam_size = beam_size

    def transcribe(self, pcm16: bytes, sample_rate: int = 8000) -> str:
        """PCM → float32 数组 → whisper 识别。"""
        if not pcm16:
            return ""
        samples = self._np.frombuffer(pcm16, dtype=self._np.int16).astype(self._np.float32)
        samples /= 32768.0
        segments, _info = self._model.transcribe(
            samples, language=self.language, beam_size=self.beam_size)
        return "".join(seg.text for seg in segments).strip()

    def transcribe_detailed(self, pcm16: bytes, sample_rate: int = 8000) -> ASRResult:
        """识别并返回文本/语种/平均置信度。"""
        if not pcm16:
            return ASRResult("")
        samples = self._np.frombuffer(pcm16, dtype=self._np.int16).astype(self._np.float32)
        samples /= 32768.0
        segments, info = self._model.transcribe(
            samples, language=self.language, beam_size=self.beam_size)
        segs = list(segments)
        text = "".join(s.text for s in segs).strip()
        if segs:
            conf = sum(getattr(s, "avg_logprob", 0.0) for s in segs) / len(segs)
        else:
            conf = 0.0
        return ASRResult(text, language=info.language or "", confidence=conf)


class OpenAICompatibleASR(BaseASR):
    """OpenAI 兼容 /audio/transcriptions HTTP API。

    base_url 例：https://api.openai.com/v1 或内网 Whisper 网关；
    api_key 通过 Authorization: Bearer 传递（可为空串）。
    """

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "whisper-1",
        language: Optional[str] = "zh",
        timeout: float = 60.0,
    ) -> None:
        """参数与 OpenAI audio transcriptions API 对齐。"""
        self.base = normalize_openai_base(base_url)
        self.api_key = api_key
        self.model = model
        self.language = language
        self.timeout = timeout

    def transcribe(self, pcm16: bytes, sample_rate: int = 8000) -> str:
        """上传 WAV，返回识别文本。"""
        if not pcm16:
            return ""
        wav = pcm16_to_wav(pcm16, sample_rate)
        fields = {"model": self.model}
        if self.language:
            fields["language"] = self.language
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            _status, body = post_multipart(
                f"{self.base}/audio/transcriptions",
                fields=fields,
                files={"file": ("audio.wav", wav, "audio/wav")},
                headers=headers,
                timeout=self.timeout,
            )
        except HttpError as exc:
            raise RuntimeError(f"ASR API 调用失败: {exc}") from exc
        import json
        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"ASR API 响应不是 JSON: {body[:200]!r}") from exc
        return str(data.get("text", "")).strip()


class SimulatedASR(BaseASR):
    """离线/测试用 ASR：从队列取预置文本（队列空返回空串）。

    用途：无网络/无模型环境下跑通 pipeline 全链路，或单元测试注入台词。
    """

    def __init__(self, default_text: str = "") -> None:
        """default_text: 队列空时返回的兜底文本。"""
        self._queue: "queue.Queue[str]" = queue.Queue()
        self.default_text = default_text

    def push(self, text: str) -> None:
        """注入一条"识别结果"（测试代码调用）。"""
        self._queue.put(text)

    def transcribe(self, pcm16: bytes, sample_rate: int = 8000) -> str:
        """返回下一条预置文本。"""
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return self.default_text


def create_asr(cfg: Dict[str, Any]) -> BaseASR:
    """按配置创建 ASR 实例（cfg 为 config.yaml 的 stt 段）。

    type: 'faster_whisper' | 'openai_api' | 'simulated'
    """
    kind = str(cfg.get("type", "faster_whisper")).lower()
    if kind in ("faster_whisper", "faster-whisper", "whisper_local"):
        return FasterWhisperASR(
            model_size=cfg.get("model_size", "small"),
            device=cfg.get("device", "auto"),
            compute_type=cfg.get("compute_type", "default"),
            language=cfg.get("language", "zh"),
            beam_size=int(cfg.get("beam_size", 1)),
        )
    if kind in ("openai_api", "openai", "http"):
        return OpenAICompatibleASR(
            base_url=cfg.get("base_url", "https://api.openai.com/v1"),
            api_key=cfg.get("api_key", ""),
            model=cfg.get("model", "whisper-1"),
            language=cfg.get("language", "zh"),
            timeout=float(cfg.get("timeout", 60.0)),
        )
    if kind == "simulated":
        return SimulatedASR(default_text=cfg.get("default_text", ""))
    raise ValueError(f"未知 stt.type: {kind!r}（可选 faster_whisper / openai_api / simulated）")
