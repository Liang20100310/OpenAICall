#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP / MCP 接口：让 AI 在通话中触发电发脑任务。

HTTP 路由（FastAPI 与纯标准库 http.server 双实现，自动选择）：
    GET  /status   —— 查询桥服务 + 通话会话状态
    POST /exec     —— 执行白名单脚本（禁止任意命令执行）
    POST /tts      —— 向当前通话注入语音
    POST /hangup   —— 挂断当前通话
    POST /mcp      —— MCP JSON-RPC（initialize / tools/list / tools/call / ping）

MCP tools：
    execute_script  —— 执行白名单脚本 {name, params}
    query_status    —— 查询系统状态 {}
    send_message    —— 向通话注入语音 {text}

安全设计（脚本白名单）：
    * 只允许执行配置文件里显式登记的脚本（name → argv 模板），无登记即拒绝；
    * 参数值仅允许安全字符集、禁止以 '-' 开头（防止注入额外选项）；
    * subprocess 使用 argv 列表（shell=False），占位符 {param} 精确替换；
    * 每脚本独立超时，超时杀进程；可选 Bearer Token 鉴权。

依赖：FastAPI/uvicorn 可选；缺省自动退回纯标准库实现（零依赖可运行）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "ScriptSpec", "ScriptExecutor", "ApiCore",
    "create_app", "run_server", "build_core_from_config",
]

# 参数值安全字符集：字母/数字/中文/下划线/中横线/点/冒号/加号/斜杠/空格，1..128 字符
_PARAM_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff\-.=:+/ ]{1,128}$")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


# ---------------------------------------------------------------------------
# 脚本白名单执行器
# ---------------------------------------------------------------------------

@dataclass
class ScriptSpec:
    """一个白名单脚本的定义。"""
    name: str                        # 脚本名（/exec 的 name 字段）
    argv: List[str]                  # 命令模板（列表，支持 {param} 占位符）
    timeout_s: float = 30.0          # 执行超时（秒），超时杀进程
    description: str = ""            # 描述（供 AI/人查看）
    workdir: str = ""                # 工作目录（空 = 服务启动目录）


class ScriptExecutor:
    """白名单脚本执行器：只执行登记过的脚本，参数严格校验。"""

    def __init__(self, scripts: Dict[str, ScriptSpec]) -> None:
        """scripts: {脚本名: ScriptSpec}，来自配置文件的 mcp.scripts 段。"""
        self.scripts = dict(scripts)

    def list_scripts(self) -> List[Dict[str, Any]]:
        """列出全部白名单脚本（名称 + 描述 + 参数占位符）。"""
        out = []
        for spec in self.scripts.values():
            params = sorted({m for arg in spec.argv for m in _PLACEHOLDER_RE.findall(arg)})
            out.append({"name": spec.name, "description": spec.description,
                        "params": params, "timeout_s": spec.timeout_s})
        return out

    def execute(self, name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """执行白名单脚本，返回 {exit_code, stdout, stderr, duration_s}。

        非白名单 / 参数非法 / 超时 → 抛 PermissionError / ValueError / TimeoutError。
        """
        spec = self.scripts.get(name)
        if spec is None:
            raise PermissionError(f"脚本不在白名单中，拒绝执行: {name!r}")
        params = {str(k): str(v) for k, v in (params or {}).items()}

        # 收集占位符并校验参数
        needed = {m for arg in spec.argv for m in _PLACEHOLDER_RE.findall(arg)}
        unknown = set(params) - needed
        if unknown:
            raise ValueError(f"存在未声明的参数: {sorted(unknown)}（本脚本仅接受 {sorted(needed)}）")
        missing = needed - set(params)
        if missing:
            raise ValueError(f"缺少参数: {sorted(missing)}")
        for key, val in params.items():
            if not _PARAM_RE.match(val):
                raise ValueError(f"参数 {key!r} 的值含非法字符或过长: {val[:64]!r}")
            if val.startswith("-"):
                raise ValueError(f"参数 {key!r} 的值禁止以 '-' 开头: {val!r}")

        # 精确替换占位符（shell=False，无注入面）
        argv = []
        for arg in spec.argv:
            def _sub(m: re.Match) -> str:
                return params[m.group(1)]
            argv.append(_PLACEHOLDER_RE.sub(_sub, arg))

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=spec.timeout_s,
                cwd=spec.workdir or None,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"脚本可执行文件不存在: {argv[0]!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"脚本 {name!r} 执行超时（>{spec.timeout_s}s），已终止") from exc
        return {
            "name": name,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-8000:],     # 截断防止超大响应
            "stderr": proc.stderr[-4000:],
            "duration_s": round(time.monotonic() - t0, 3),
        }


# ---------------------------------------------------------------------------
# API 核心（HTTP 框架无关，FastAPI / stdlib 双适配共用）
# ---------------------------------------------------------------------------

class ApiCore:
    """HTTP/MCP 业务核心。

    status_fn() -> dict                       —— 系统状态（桥服务 + 通话会话）
    tts_fn(text) -> dict                      —— 向通话注入语音
    hangup_fn(reason) -> dict                 —— 挂断通话
    auth_token: 非空则要求 Authorization: Bearer <token>
    """

    def __init__(
        self,
        executor: ScriptExecutor,
        status_fn: Callable[[], Dict[str, Any]],
        tts_fn: Callable[[str], Dict[str, Any]],
        hangup_fn: Callable[[str], Dict[str, Any]],
        auth_token: str = "",
    ) -> None:
        """见类文档。"""
        self.executor = executor
        self.status_fn = status_fn
        self.tts_fn = tts_fn
        self.hangup_fn = hangup_fn
        self.auth_token = auth_token

    # ---- 鉴权 -----------------------------------------------------------

    def check_auth(self, headers: Dict[str, str]) -> bool:
        """校验 Bearer Token（未配置 token 则放行）。"""
        if not self.auth_token:
            return True
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        return auth == f"Bearer {self.auth_token}"

    # ---- HTTP 业务 ------------------------------------------------------

    def api_status(self) -> Dict[str, Any]:
        """GET /status。"""
        return {"ok": True, "time": time.time(), "status": self.status_fn()}

    def api_exec(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /exec {name, params}。"""
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            return 400, {"ok": False, "error": "缺少脚本名 name"}
        try:
            result = self.executor.execute(name, payload.get("params") or {})
            return 200, {"ok": result["exit_code"] == 0, "result": result}
        except PermissionError as exc:
            return 403, {"ok": False, "error": str(exc)}
        except (ValueError, TimeoutError, RuntimeError) as exc:
            return 400, {"ok": False, "error": str(exc)}

    def api_tts(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /tts {text}。"""
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return 400, {"ok": False, "error": "缺少文本 text"}
        try:
            return 200, {"ok": True, "result": self.tts_fn(text)}
        except Exception as exc:
            return 409, {"ok": False, "error": str(exc)}

    def api_hangup(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /hangup {reason?}。"""
        reason = str(payload.get("reason") or "remote_api")
        try:
            return 200, {"ok": True, "result": self.hangup_fn(reason)}
        except Exception as exc:
            return 409, {"ok": False, "error": str(exc)}

    # ---- MCP JSON-RPC ---------------------------------------------------

    def api_mcp(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """MCP JSON-RPC 入口（POST /mcp）。"""
        rpc_id = payload.get("id")
        method = payload.get("method", "")
        params = payload.get("params") or {}

        def ok_result(result: Any) -> Dict[str, Any]:
            return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

        def err(code: int, message: str) -> Dict[str, Any]:
            return {"jsonrpc": "2.0", "id": rpc_id,
                    "error": {"code": code, "message": message}}

        if method == "initialize":
            return ok_result({
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fpga-phone-relay-mcp", "version": "1.0"},
            })
        if method in ("notifications/initialized", "notifications/cancelled"):
            return {"jsonrpc": "2.0", "id": None, "result": {}}   # 通知类，无实质响应
        if method == "ping":
            return ok_result({})
        if method == "tools/list":
            return ok_result({"tools": self._mcp_tools()})
        if method == "tools/call":
            return self._mcp_call_tool(params, ok_result, err)
        return err(-32601, f"未知方法: {method}")

    def _mcp_tools(self) -> List[Dict[str, Any]]:
        """MCP tools 定义。"""
        return [
            {
                "name": "execute_script",
                "description": ("执行电发脑白名单脚本（仅限管理员登记的脚本，"
                                "禁止任意命令）。可用脚本列表见 query_status 的 scripts 字段。"),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "白名单脚本名"},
                        "params": {"type": "object",
                                   "description": "脚本参数（键值均为字符串）"},
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "query_status",
                "description": "查询系统状态：桥服务统计、通话状态、可用脚本列表。",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "send_message",
                "description": "向当前通话注入一段语音（TTS 播报给电话对方）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要播报的文本"},
                    },
                    "required": ["text"],
                },
            },
        ]

    def _mcp_call_tool(self, params: Dict[str, Any], ok_result, err) -> Dict[str, Any]:
        """tools/call 分派。"""
        name = params.get("name")
        args = params.get("arguments") or {}

        def text_result(data: Any) -> Dict[str, Any]:
            return ok_result({
                "content": [{"type": "text",
                             "text": json.dumps(data, ensure_ascii=False)}],
                "isError": False,
            })

        if name == "execute_script":
            code, resp = self.api_exec(args)
            if code != 200:
                return err(-32000, resp.get("error", "执行失败"))
            return text_result(resp)
        if name == "query_status":
            return text_result(self.api_status())
        if name == "send_message":
            code, resp = self.api_tts(args)
            if code != 200:
                return err(-32000, resp.get("error", "注入失败"))
            return text_result(resp)
        return err(-32602, f"未知工具: {name}")


# ---------------------------------------------------------------------------
# 适配层 1：FastAPI（可选，装了 fastapi/uvicorn 时优先）
# ---------------------------------------------------------------------------

def create_app(core: ApiCore):
    """创建 FastAPI 应用（需要 fastapi；缺失时抛 RuntimeError）。"""
    try:
        from fastapi import Body, FastAPI, Header, HTTPException, Request  # type: ignore
        from fastapi.responses import JSONResponse  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "未安装 fastapi，无法创建 FastAPI 应用。"
            "请 pip install fastapi uvicorn，或直接用内置标准库 HTTP 服务（自动回退）。"
        ) from exc

    app = FastAPI(title="FPGA 电话中继 · 电发脑接口", version="1.0")

    def auth(authorization: Optional[str]) -> None:
        if not core.check_auth({"Authorization": authorization or ""}):
            raise HTTPException(status_code=401, detail="未授权（Bearer Token 校验失败）")

    @app.get("/status")
    def get_status(authorization: Optional[str] = Header(None)):
        auth(authorization)
        return core.api_status()

    @app.post("/exec")
    def post_exec(payload: Dict[str, Any] = Body(...),
                  authorization: Optional[str] = Header(None)):
        auth(authorization)
        code, resp = core.api_exec(payload)
        return JSONResponse(status_code=code, content=resp)

    @app.post("/tts")
    def post_tts(payload: Dict[str, Any] = Body(...),
                 authorization: Optional[str] = Header(None)):
        auth(authorization)
        code, resp = core.api_tts(payload)
        return JSONResponse(status_code=code, content=resp)

    @app.post("/hangup")
    def post_hangup(payload: Dict[str, Any] = Body(default={}),
                    authorization: Optional[str] = Header(None)):
        auth(authorization)
        code, resp = core.api_hangup(payload or {})
        return JSONResponse(status_code=code, content=resp)

    @app.post("/mcp")
    async def post_mcp(request: Request, authorization: Optional[str] = Header(None)):
        auth(authorization)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(status_code=400,
                                content={"error": "请求体不是合法 JSON"})
        return core.api_mcp(payload)

    return app


# ---------------------------------------------------------------------------
# 适配层 2：纯标准库 http.server（零依赖回退）
# ---------------------------------------------------------------------------

def _make_stdlib_handler(core: ApiCore):
    """构造绑定 ApiCore 的 BaseHTTPRequestHandler 子类。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "PhoneRelayMCP/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
            print(f"[mcp-http] {self.address_string()} {fmt % args}", file=sys.stderr)

        def _send(self, code: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth_ok(self) -> bool:
            return core.check_auth(dict(self.headers.items()))

        def _read_json(self) -> Optional[Dict[str, Any]]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None

        def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
            if not self._auth_ok():
                self._send(401, {"ok": False, "error": "未授权"})
                return
            if self.path == "/status":
                self._send(200, core.api_status())
            else:
                self._send(404, {"ok": False, "error": f"未知路径 {self.path}"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._auth_ok():
                self._send(401, {"ok": False, "error": "未授权"})
                return
            payload = self._read_json()
            if payload is None:
                self._send(400, {"ok": False, "error": "请求体不是合法 JSON 对象"})
                return
            if self.path == "/exec":
                self._send(*core.api_exec(payload))
            elif self.path == "/tts":
                self._send(*core.api_tts(payload))
            elif self.path == "/hangup":
                self._send(*core.api_hangup(payload))
            elif self.path == "/mcp":
                self._send(200, core.api_mcp(payload))
            else:
                self._send(404, {"ok": False, "error": f"未知路径 {self.path}"})

    return Handler


def run_server(core: ApiCore, host: str = "127.0.0.1", port: int = 8765,
               prefer: str = "auto") -> None:
    """启动 HTTP/MCP 服务（阻塞）。

    prefer: 'auto'（优先 FastAPI/uvicorn，缺则标准库）| 'fastapi' | 'stdlib'
    """
    use_fastapi = prefer == "fastapi" or (prefer == "auto")
    if use_fastapi:
        try:
            import uvicorn  # type: ignore
            app = create_app(core)
            print(f"[mcp-http] FastAPI 服务启动 http://{host}:{port}", file=sys.stderr)
            uvicorn.run(app, host=host, port=port, log_level="info")
            return
        except RuntimeError as exc:
            if prefer == "fastapi":
                raise
            print(f"[mcp-http] FastAPI 不可用（{exc}），回退标准库实现", file=sys.stderr)
        except ImportError:
            if prefer == "fastapi":
                raise RuntimeError("未安装 uvicorn，请 pip install uvicorn 或改用 stdlib 模式")
            print("[mcp-http] uvicorn 不可用，回退标准库实现", file=sys.stderr)

    handler = _make_stdlib_handler(core)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"[mcp-http] 标准库服务启动 http://{host}:{port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# ---------------------------------------------------------------------------
# 从配置构建
# ---------------------------------------------------------------------------

def build_core_from_config(cfg: Dict[str, Any],
                           bridge: Any = None,
                           session: Any = None) -> ApiCore:
    """根据 config.yaml 构建 ApiCore。

    bridge: BridgeService 实例（可空）；session: CallSession 实例（可空）。
    未接真实对象时对应接口返回明确的"未接线"错误，不崩溃。
    """
    mcp_cfg = cfg.get("mcp", {}) if isinstance(cfg, dict) else {}
    scripts: Dict[str, ScriptSpec] = {}
    for name, spec in (mcp_cfg.get("scripts") or {}).items():
        if not isinstance(spec, dict) or "argv" not in spec:
            raise ValueError(f"脚本 {name!r} 配置非法：必须包含 argv 列表")
        argv = spec["argv"]
        if isinstance(argv, str):
            argv = shlex.split(argv)      # 允许写成字符串，自动拆分
        scripts[name] = ScriptSpec(
            name=name,
            argv=[str(a) for a in argv],
            timeout_s=float(spec.get("timeout_s", 30.0)),
            description=str(spec.get("description", "")),
            workdir=str(spec.get("workdir", "")),
        )
    executor = ScriptExecutor(scripts)

    def status_fn() -> Dict[str, Any]:
        out: Dict[str, Any] = {"scripts": executor.list_scripts()}
        if bridge is not None:
            out["bridge"] = bridge.stats()
        else:
            out["bridge"] = {"connected": False, "note": "未接串口桥服务"}
        if session is not None:
            out["call"] = session.status()
        else:
            out["call"] = {"state": "none", "note": "未接通话会话"}
        return out

    def tts_fn(text: str) -> Dict[str, Any]:
        if session is None:
            raise RuntimeError("未接通话会话，无法注入语音")
        session.inject_tts(text)
        return {"queued": True, "text": text}

    def hangup_fn(reason: str) -> Dict[str, Any]:
        if session is None:
            raise RuntimeError("未接通话会话，无法挂断")
        session.hangup(reason)
        return {"hangup": True, "reason": reason}

    return ApiCore(
        executor=executor,
        status_fn=status_fn,
        tts_fn=tts_fn,
        hangup_fn=hangup_fn,
        auth_token=str(mcp_cfg.get("auth_token", "")),
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _demo_core() -> ApiCore:
    """演示用 ApiCore（无硬件/无会话，tts/hangup 打日志模拟）。"""
    scripts = {
        "echo_hello": ScriptSpec(
            name="echo_hello",
            argv=[sys.executable, "-c", "print('hello from whitelist script')"],
            timeout_s=5.0,
            description="演示脚本：打印问候（证明白名单可执行）",
        ),
        "device_reboot": ScriptSpec(
            name="device_reboot",
            argv=[sys.executable, "-c",
                  "import sys; print('reboot device', sys.argv[1])", "{device_id}"],
            timeout_s=10.0,
            description="演示脚本：模拟设备重启（带 device_id 参数）",
        ),
    }
    executor = ScriptExecutor(scripts)
    demo_log: List[str] = []

    return ApiCore(
        executor=executor,
        status_fn=lambda: {"mode": "demo", "scripts": executor.list_scripts(),
                           "call": {"state": "active"}, "log": demo_log[-10:]},
        tts_fn=lambda text: (demo_log.append(f"tts:{text}"), {"queued": True, "text": text})[1],
        hangup_fn=lambda reason: (demo_log.append(f"hangup:{reason}"),
                                  {"hangup": True, "reason": reason})[1],
    )


def main() -> int:
    """命令行入口：python3 mcp_http/server.py [--config config.yaml] [--demo]"""
    ap = argparse.ArgumentParser(description="HTTP / MCP 电发脑接口服务")
    ap.add_argument("--config", default=None, help="config.yaml 路径")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    ap.add_argument("--demo", action="store_true", help="演示模式（无硬件/无会话）")
    ap.add_argument("--prefer", default="auto", choices=["auto", "fastapi", "stdlib"],
                    help="HTTP 实现选择（默认 auto）")
    args = ap.parse_args()

    if args.demo or not args.config:
        if not args.demo:
            print("[mcp-http] 未提供 --config，进入演示模式", file=sys.stderr)
        core = _demo_core()
    else:
        try:
            import yaml  # type: ignore
        except ImportError:
            print("缺少 PyYAML：pip install PyYAML（或用 --demo 模式）", file=sys.stderr)
            return 2
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        core = build_core_from_config(cfg)

    run_server(core, host=args.host, port=args.port, prefer=args.prefer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
