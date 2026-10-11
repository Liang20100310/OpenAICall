#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audio_bridge 包：UART 帧协议 + 串口音频桥 + 音频 I/O 抽象。

    serial_framer.py —— UART 帧编解码（CRC8 / 失步重同步 / SEQ 统计）
    bridge_service.py —— 串口音频收发服务（回调 / 抖动缓冲 / 环回自测）
    audio_io.py      —— G.711 A-law ↔ PCM、重采样、上/下行音频流
"""

__all__ = ["serial_framer", "bridge_service", "audio_io"]
