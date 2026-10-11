#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AGI 测试脚本（源代码）：00 路由 → AI 语音管道 的联调替身（mock）。

用途：在上位机 ARI 媒体接管完成之前，用它验证 00 路由的拨号计划、
     摘机应答、放音、DTMF 收号、超时挂断五个环节。

部署：放入 Asterisk AGI 目录（默认 /var/lib/asterisk/agi-bin/），加执行权限：
    chmod +x ai_relay_agi_mock.py
    asterisk -rx "dialplan reload"

AGI 协议：stdin 读环境变量与响应，stdout 写命令（本脚本已实现最小协议）。
正式版对接：把本脚本替换为 ARI externalMedia（把 RTP 媒体接到
host/ai_pipeline 的 CallSession），语音编码 G.711 A-law（免转码）。
"""

from __future__ import annotations

import sys
import time
from typing import Dict, Optional


def _read_env() -> Dict[str, str]:
    """读取 AGI 环境变量块（空行结束）。"""
    env: Dict[str, str] = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        if ":" in line:
            key, _, val = line.partition(":")
            env[key.strip()] = val.strip()
    return env


def _cmd(cmd: str) -> str:
    """发送 AGI 命令并返回响应行（'200 result=...'）。"""
    sys.stdout.write(cmd + "\n")
    sys.stdout.flush()
    return sys.stdin.readline().strip()


def main() -> int:
    """主流程：应答 → 放音 → 收号 → 记录 → 退出。"""
    env = _read_env()
    cid = env.get("agi_callerid", "?")
    unique = env.get("agi_uniqueid", "?")
    print(f"[agi-mock] call from {cid} unique={unique}", file=sys.stderr)

    _cmd("ANSWER")
    _cmd("VERBOSE \"AGI mock: AI pipeline route answered\" 3")

    # 1) 提示音（用系统自带音源；正式版替换为 TTS 下发音）
    _cmd("STREAM FILE tt-monkeys \"\" 0")

    # 2) 收号：等待按键（DTMF，对应上位机 DTMF 路由；10 s 超时，# 结束）
    resp = _cmd("GET DATA tt-monkeys 10000 4")
    # 响应形如 200 result=123（数字串）
    digits = ""
    if "result=" in resp:
        digits = resp.split("result=", 1)[1].split()[0]
        if digits == "-1":
            digits = ""
    print(f"[agi-mock] DTMF received: {digits!r}", file=sys.stderr)
    _cmd(f"VERBOSE \"AGI mock: DTMF={digits}\" 3")

    # 3) 回放确认（验证放音双向链路）
    if digits:
        _cmd("STREAM FILE auth-thankyou \"\" 0")
    else:
        _cmd("STREAM FILE vm-goodbye \"\" 0")

    # 4) 记录到 CDR 变量（便于测试矩阵 A5/CDR 核对）
    _cmd("SET VARIABLE AGI_MOCK_DTMF \"{}\"".format(digits or "none"))
    _cmd("SET VARIABLE AGI_MOCK_TS \"{}\"".format(int(time.time())))
    _cmd("HANGUP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
