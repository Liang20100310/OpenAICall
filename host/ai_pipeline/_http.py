#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_pipeline 包内共用的极简 HTTP 客户端（纯标准库 urllib）。

提供：
    http_request()  —— 通用请求（JSON / 表单 / 原始字节），返回 (status, headers, body)
    post_json()     —— JSON POST
    HttpError       —— HTTP 非 2xx 异常（携带响应体片段，便于排错）

不引入 requests 等第三方依赖，保证缺依赖时 HTTP 型 ASR/LLM/TTS 仍可运行。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Optional, Tuple

__all__ = ["HttpError", "http_request", "post_json", "post_multipart", "normalize_openai_base"]


class HttpError(RuntimeError):
    """HTTP 请求失败（非 2xx 或网络错误）。"""

    def __init__(self, status: int, url: str, body: str) -> None:
        """status: HTTP 状态码（网络错误为 0）；url: 请求地址；body: 响应体片段。"""
        super().__init__(f"HTTP {status} @ {url}: {body[:500]}")
        self.status = status
        self.url = url
        self.body = body


def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = 30.0,
) -> Tuple[int, Dict[str, str], bytes]:
    """发起 HTTP 请求，返回 (status, headers, body_bytes)。

    非 2xx 抛 HttpError（body 片段附在异常信息中）。
    """
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code, url, exc.read().decode("utf-8", "replace")) from exc
    except urllib.error.URLError as exc:
        raise HttpError(0, url, f"网络错误: {exc.reason}") from exc


def post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """JSON POST 并解析 JSON 响应。"""
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    status, _, body = http_request(
        url, "POST", hdrs, json.dumps(payload).encode("utf-8"), timeout)
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HttpError(status, url, f"响应不是合法 JSON: {body[:200]!r}") from exc


def post_multipart(
    url: str,
    fields: Dict[str, str],
    files: Dict[str, Tuple[str, bytes, str]],
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 120.0,
) -> Tuple[int, bytes]:
    """multipart/form-data POST（ASR 上传音频用）。

    fields: 普通表单项；files: {字段名: (文件名, 字节, content_type)}。
    返回 (status, body_bytes)。
    """
    boundary = f"----openclaw{uuid.uuid4().hex}"
    parts = bytearray()
    for name, value in fields.items():
        parts += (f"--{boundary}\r\nContent-Disposition: form-data; "
                  f"name=\"{name}\"\r\n\r\n{value}\r\n").encode("utf-8")
    for name, (fname, data, ctype) in files.items():
        parts += (f"--{boundary}\r\nContent-Disposition: form-data; "
                  f"name=\"{name}\"; filename=\"{fname}\"\r\n"
                  f"Content-Type: {ctype}\r\n\r\n").encode("utf-8")
        parts += data + b"\r\n"
    parts += f"--{boundary}--\r\n".encode("utf-8")

    hdrs = {"Content-Type": f"multipart/form-data; boundary={boundary}", **(headers or {})}
    return http_request(url, "POST", hdrs, bytes(parts), timeout)


def normalize_openai_base(base_url: str) -> str:
    """归一化 OpenAI 兼容服务地址。

    兼容 "https://host" / "https://host/v1" / "https://host/v1/" 等写法，
    统一返回以 "/v1" 结尾（无斜杠）的前缀，便于拼接 /chat/completions 等路径。
    """
    base = base_url.strip().rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base
