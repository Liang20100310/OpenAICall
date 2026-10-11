#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_pipeline 包：VAD → STT → LLM → TTS 语音管道。

各环节独立模块：
    vad.py  —— 能量静音检测（VAD）
    asr.py  —— 语音识别（faster-whisper 本地 / OpenAI 兼容 HTTP API）
    llm.py  —— 大模型对话（OpenAI 兼容 Chat Completions API）
    tts.py  —— 语音合成（edge-tts / OpenAI 兼容 TTS API）
    pipeline.py —— 通话会话编排（打断、静音超时、DTMF 路由）

第三方依赖全部可选导入，缺失时抛出带安装指引的 RuntimeError。
"""

__all__ = ["vad", "asr", "llm", "tts", "pipeline"]
