#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 对话模块：OpenAI 兼容 Chat Completions API。

特性：
    * base_url 可配，兼容 OpenAI / Copilot / OpenClaw 网关等任何兼容端点
      （"https://host"、"https://host/v1"、"https://host/v1/" 均可）；
    * 多轮对话记忆（可配置轮数上限）、system prompt、温度/最大长度可配；
    * chat() 非流式 / chat_stream() 流式（SSE）两种调用；
    * 纯标准库 urllib 实现；HTTP 失败抛 LLMError（含响应体片段）；
    * OfflineEchoLLM：离线/测试用确定性回复，不依赖网络。

第三方依赖：无（纯标准库）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional

try:
    from ._http import HttpError, http_request, normalize_openai_base, post_json  # type: ignore
except ImportError:  # 直接以脚本运行时的回退
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ai_pipeline._http import HttpError, http_request, normalize_openai_base, post_json  # type: ignore

__all__ = ["LLMError", "ChatMessage", "LLMConfig", "OpenAIChatLLM", "OfflineEchoLLM",
           "BaseLLM", "create_llm"]


class LLMError(RuntimeError):
    """LLM 调用失败。"""


@dataclass
class ChatMessage:
    """一条对话消息。"""
    role: str            # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMConfig:
    """LLM 参数。"""
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    system_prompt: str = "你是一个电话语音助理，回答简洁口语化，适合语音播报。"
    temperature: float = 0.7
    max_tokens: int = 512
    timeout: float = 60.0
    max_history: int = 20          # 保留的对话消息条数（不含 system）


class BaseLLM:
    """LLM 抽象接口。"""

    def chat(self, user_text: str) -> str:
        """单轮对话：送入用户文本，返回助手文本。"""
        raise NotImplementedError

    def reset(self) -> None:
        """清空对话记忆（新通话开始）。"""
        raise NotImplementedError


class OpenAIChatLLM(BaseLLM):
    """OpenAI 兼容 Chat Completions 客户端。"""

    def __init__(self, config: Optional[LLMConfig] = None) -> None:
        """config: LLMConfig；缺省用默认参数。"""
        self.cfg = config or LLMConfig()
        self.base = normalize_openai_base(self.cfg.base_url)
        self.history: List[ChatMessage] = []

    # ---- 对外接口 -------------------------------------------------------

    def chat(self, user_text: str) -> str:
        """送入用户文本（自动维护多轮记忆），返回助手回复文本。"""
        self.history.append(ChatMessage("user", user_text))
        messages = self._build_messages()
        payload = {
            "model": self.cfg.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }
        try:
            data = post_json(f"{self.base}/chat/completions", payload,
                             headers=self._auth_headers(), timeout=self.cfg.timeout)
        except HttpError as exc:
            self.history.pop()   # 调用失败回滚这轮 user 消息
            raise LLMError(f"LLM 调用失败: {exc}") from exc
        try:
            reply = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            self.history.pop()
            raise LLMError(f"LLM 响应结构异常: {json.dumps(data, ensure_ascii=False)[:300]}") from exc
        self.history.append(ChatMessage("assistant", reply))
        self._trim_history()
        return reply

    def chat_stream(self, user_text: str) -> Generator[str, None, None]:
        """流式对话：逐段 yield 助手文本增量；结束后写入记忆。"""
        self.history.append(ChatMessage("user", user_text))
        messages = self._build_messages()
        payload = {
            "model": self.cfg.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": True,
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", **self._auth_headers()}
        chunks: List[str] = []
        try:
            _status, _hdrs, raw = http_request(
                f"{self.base}/chat/completions", "POST", headers, body,
                timeout=self.cfg.timeout)
        except HttpError as exc:
            self.history.pop()
            raise LLMError(f"LLM 流式调用失败: {exc}") from exc
        # 解析 SSE：data: {...}\\n\\n 行
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                delta = json.loads(data_str)["choices"][0].get("delta", {})
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            piece = delta.get("content") or ""
            if piece:
                chunks.append(piece)
                yield piece
        reply = "".join(chunks)
        self.history.append(ChatMessage("assistant", reply))
        self._trim_history()

    def reset(self) -> None:
        """清空对话记忆。"""
        self.history.clear()

    # ---- 内部 -----------------------------------------------------------

    def _build_messages(self) -> List[ChatMessage]:
        """组装发送给 API 的消息列表（system + 记忆）。"""
        msgs: List[ChatMessage] = []
        if self.cfg.system_prompt:
            msgs.append(ChatMessage("system", self.cfg.system_prompt))
        msgs.extend(self.history)
        return msgs

    def _trim_history(self) -> None:
        """按 max_history 裁剪记忆（system 不计入）。"""
        if self.cfg.max_history > 0 and len(self.history) > self.cfg.max_history:
            self.history = self.history[-self.cfg.max_history:]

    def _auth_headers(self) -> Dict[str, str]:
        """认证头（api_key 为空则不带）。"""
        return {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}


class OfflineEchoLLM(BaseLLM):
    """离线/测试用 LLM：确定性回声回复，无网络依赖。

    回复规则："收到：<用户文本>"；文本以"指令"开头时回复"执行：<内容>"。
    也可通过 queue 注入预设回复。
    """

    def __init__(self, echo_prefix: str = "收到：") -> None:
        """echo_prefix: 回声前缀。"""
        self.echo_prefix = echo_prefix
        self.turns = 0

    def chat(self, user_text: str) -> str:
        """返回确定性回复。"""
        self.turns += 1
        if user_text.startswith("指令"):
            return f"执行：{user_text[2:].strip() or '空指令'}"
        return f"{self.echo_prefix}{user_text}"

    def reset(self) -> None:
        """复位轮次计数。"""
        self.turns = 0


def create_llm(cfg: Dict[str, Any]) -> BaseLLM:
    """按配置创建 LLM（cfg 为 config.yaml 的 llm 段）。

    type: 'openai_api'（默认） | 'offline'
    """
    kind = str(cfg.get("type", "openai_api")).lower()
    if kind in ("offline", "echo", "simulated"):
        return OfflineEchoLLM(echo_prefix=cfg.get("echo_prefix", "收到："))
    if kind in ("openai_api", "openai", "http"):
        return OpenAIChatLLM(LLMConfig(
            base_url=cfg.get("base_url", "https://api.openai.com/v1"),
            api_key=cfg.get("api_key", ""),
            model=cfg.get("model", "gpt-4o-mini"),
            system_prompt=cfg.get("system_prompt", LLMConfig.system_prompt),
            temperature=float(cfg.get("temperature", 0.7)),
            max_tokens=int(cfg.get("max_tokens", 512)),
            timeout=float(cfg.get("timeout", 60.0)),
            max_history=int(cfg.get("max_history", 20)),
        ))
    raise ValueError(f"未知 llm.type: {kind!r}（可选 openai_api / offline）")


# ---------------------------------------------------------------------------
# 自测（python3 ai_pipeline/llm.py）—— 离线部分
# ---------------------------------------------------------------------------

def _self_test() -> bool:
    """OfflineEchoLLM 行为校验（API 调用需真实端点，不在自测范围）。"""
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

    print("== llm 自测（离线） ==")
    llm = OfflineEchoLLM()
    check("回声回复", llm.chat("打开一号设备") == "收到：打开一号设备")
    check("指令回复", llm.chat("指令启动备用电源") == "执行：启动备用电源")
    llm.reset()
    check("reset 复位", llm.turns == 0)

    # 地址归一化
    check("base_url 归一化", normalize_openai_base("https://x.com") == "https://x.com/v1"
          and normalize_openai_base("https://x.com/v1/") == "https://x.com/v1")

    print("== 结果: %s ==" % ("PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_test() else 1)
