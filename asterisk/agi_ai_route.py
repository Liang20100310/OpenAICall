#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agi_ai_route.py — AI 语音管道路由 AGI 脚本
==============================================================================
项目：FPGA 电话中继 + AI 语音指挥系统
契约：docs/INTERFACE_SPEC.md §8（拨 00 进入本脚本；音频 G.711 A-law 8 kHz）

流程：
    1) Answer            —— 应答，等待媒体稳定
    2) 与 AI 管道进程建会话（HTTP REST 或本地 Unix Socket，二选一）
    3) 主循环：从 AI 管道取"动作"并执行
         - play     : 播放提示音（支持本地文件 / HTTP TTS 缓存），放音期间收集 DTMF
         - gather   : 静音收号（GET DATA），超时/位数够即返回
         - say      : 请求管道 TTS 合成 → 落盘 → 播放
         - transfer : 转接内网分机/外线（置 AI_TRANSFER 变量交回 dialplan）
         - hook     : 软挂机/保持等控制
         - hangup   : 结束
       期间所有 DTMF 数字实时回传管道（事件流，对应 FPGA DTMF_EVT 语义）
    4) Hangup            —— 无论正常/异常都确保挂断

用法（extensions.conf）：
    same => n,AGI(agi_ai_route.py)
可选环境变量 / agi.conf：
    AI_PIPE_URL       http://127.0.0.1:8080        （HTTP 管道，优先）
    AI_PIPE_SOCKET    /run/ai-pipe/ai-pipe.sock    （Unix Socket 管道，HTTP 不可用时）
    AI_TTS_CACHE_DIR  /var/lib/asterisk/agi-cache  （TTS/远程音频缓存目录）
    AI_DTMF_DIGITS    32                           （gather 最大位数）
    AI_DTMF_TIMEOUT   5000                         （gather 首音超时 ms）
    AI_DTMF_TIMEOUT2  3000                         （gather 位间超时 ms）
    AI_LOG_FILE       /var/log/asterisk/agi_ai_route.log（可空=只打 AGI verbose）
==============================================================================
"""

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

# ----------------------------------------------------------------------------
# AGI 协议封装（Asterisk FastAGI/AGI over stdin/stdout）
# ----------------------------------------------------------------------------
class AGIError(Exception):
    pass


class AGI:
    """最小 AGI 协议实现：命令写 stdout，响应读 stdin（200 result=x (end)）。"""

    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.env: Dict[str, str] = {}
        self._read_env()

    def _read_env(self):
        """读取 AGI 环境变量块：`agi_request: ...` 行，空行结束。"""
        while True:
            line = self.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                break
            if line.startswith("agi_") and ":" in line:
                key, _, val = line.partition(":")
                self.env[key.strip()] = val.strip()

    def _cmd(self, cmd: str, *args) -> Dict[str, Any]:
        # AGI 命令参数须转义空格/引号/反斜杠
        def esc(s: str) -> str:
            return str(s).replace("\\", "\\\\").replace(" ", "\\ ").replace('"', '\\"')

        line = cmd + "".join(" " + esc(a) for a in args)
        self.stdout.write(line + "\n")
        self.stdout.flush()
        resp = self.stdin.readline()
        if not resp:
            raise AGIError("AGI channel closed")
        resp = resp.strip()
        if not resp.startswith("200"):
            raise AGIError(f"AGI cmd failed: {line} -> {resp}")
        # 200 result=1 (hangup)  或  200 result=-1
        out: Dict[str, Any] = {"raw": resp}
        try:
            mid = resp.split("result=", 1)[1]
            val = mid.split(" ", 1)[0].rstrip(")")
            out["result"] = int(val)
        except (IndexError, ValueError):
            out["result"] = -1
        if "(" in resp and resp.endswith(")"):
            out["data"] = resp[resp.rfind("(") + 1:-1]
        return out

    # ---- 常用命令 ----
    def answer(self) -> int:
        return self._cmd("ANSWER")["result"]

    def hangup(self, channel: str = "") -> int:
        return self._cmd("HANGUP", channel)["result"]

    def verbose(self, msg: str, level: int = 3) -> int:
        return self._cmd("VERBOSE", msg, str(level))["result"]

    def set_variable(self, name: str, value: str) -> int:
        return self._cmd("SET VARIABLE", name, value)["result"]

    def get_variable(self, name: str) -> str:
        r = self._cmd("GET VARIABLE", name)
        return r.get("data", "")

    def stream_file(self, filename: str, escape: str = "0123456789*#") -> int:
        """放音并监听按键。返回按下的数字 ASCII（0=超时/放完，-1=错误/挂断）。"""
        return self._cmd("STREAM FILE", filename, escape)["result"]

    def get_data(self, filename: str, timeout_ms: int = 5000, max_digits: int = 32) -> str:
        """放提示音并收号，返回收到的数字串（可能为空）。"""
        r = self._cmd("GET DATA", filename, str(timeout_ms), str(max_digits))
        return r.get("data", "")

    def get_option(self, filename: str, escape: str, timeout_ms: int = 5000) -> int:
        return self._cmd("GET OPTION", filename, escape, str(timeout_ms))["result"]

    def exec_app(self, app: str, args: str = "") -> int:
        return self._cmd("EXEC", app, args)["result"]

    def streamfile_or_silence(self, filename: str, escape: str = "0123456789*#") -> int:
        """文件不存在时静默 200 ms，避免报错打断流程。"""
        try:
            return self.stream_file(filename, escape)
        except AGIError:
            time.sleep(0.2)
            return 0


# ----------------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------------
LOG_FILE = os.environ.get("AI_LOG_FILE", "/var/log/asterisk/agi_ai_route.log")
_log_fp = None


def log(msg: str, agi: Optional[AGI] = None):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    if agi is not None:
        try:
            agi.verbose(msg)
        except Exception:
            pass
    global _log_fp
    if LOG_FILE:
        try:
            if _log_fp is None:
                _log_fp = open(LOG_FILE, "a", encoding="utf-8")
            _log_fp.write(line + "\n")
            _log_fp.flush()
        except OSError:
            pass


# ----------------------------------------------------------------------------
# AI 管道客户端：HTTP REST 优先，Unix Socket (JSON lines) 兜底
# ----------------------------------------------------------------------------
class PipelineClient:
    """与 AI 语音管道进程交互。统一接口：
        open_session(meta) -> session_id
        next_action(session_id) -> dict  动作描述
        send_event(session_id, event)    DTMF/状态事件
        close_session(session_id)
    动作协议（管道侧需实现）：
        {"action": "play",   "file": "/path/xx.wav"}          # 本地或缓存文件
        {"action": "say",    "text": "……", "voice": "zh"}     # TTS，返回 file
        {"action": "gather", "timeout_ms": 5000, "max_digits": 4}
        {"action": "transfer", "target": "2001"}              # 内网分机或外线
        {"action": "hangup"}
    """

    def __init__(self, base_url: str, sock_path: str):
        self.base_url = base_url.rstrip("/")
        self.sock_path = sock_path
        self.mode = "http" if base_url else "socket"

    # ---- HTTP ----
    def _http(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        url = self.base_url + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}

    # ---- Unix Socket (每请求一连接，JSON lines) ----
    def _sock(self, payload: dict) -> dict:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        try:
            s.connect(self.sock_path)
            s.sendall((json.dumps(payload) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode()) if buf else {}
        finally:
            s.close()

    def _call(self, method: str, path: str, payload: dict) -> dict:
        if self.mode == "http":
            try:
                return self._http(method, path, payload)
            except (urllib.error.URLError, OSError):
                if not self.sock_path:
                    raise
                self.mode = "socket"
        return self._sock({"method": method, "path": path, "body": payload})

    # ---- 接口 ----
    def open_session(self, meta: dict) -> str:
        r = self._call("POST", "/v1/sessions", meta)
        return str(r.get("session_id", ""))

    def next_action(self, sid: str) -> dict:
        return self._call("GET", f"/v1/sessions/{sid}/next_action", {})

    def send_event(self, sid: str, event: dict):
        self._call("POST", f"/v1/sessions/{sid}/events", event)

    def synthesize(self, sid: str, text: str, voice: str = "zh") -> str:
        """请求管道 TTS，返回本地可播放文件路径。"""
        r = self._call("POST", f"/v1/sessions/{sid}/tts",
                       {"text": text, "voice": voice})
        return str(r.get("file", ""))

    def close_session(self, sid: str):
        try:
            self._call("DELETE", f"/v1/sessions/{sid}", {})
        except Exception:
            pass


# ----------------------------------------------------------------------------
# 音频工具：HTTP 音频落盘缓存（Asterisk 只能播本地文件）
# ----------------------------------------------------------------------------
CACHE_DIR = os.environ.get("AI_TTS_CACHE_DIR", "/var/lib/asterisk/agi-cache")


def ensure_playable(path_or_url: str, sid: str) -> str:
    """本地路径直接返回；http(s) 下载到缓存目录并返回本地路径。"""
    if not path_or_url.startswith(("http://", "https://")):
        return path_or_url
    os.makedirs(CACHE_DIR, exist_ok=True)
    local = os.path.join(CACHE_DIR, f"{sid}_{abs(hash(path_or_url)) & 0xffffffff:08x}.wav")
    if not os.path.exists(local):
        with urllib.request.urlopen(path_or_url, timeout=15) as r, open(local, "wb") as f:
            f.write(r.read())
    return local


def agi_base_name(path: str) -> str:
    """STREAM FILE 用不含扩展名的 basename（Asterisk 按 format 查找）。"""
    return os.path.splitext(os.path.basename(path))[0]


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def run() -> int:
    agi = AGI()
    cid = agi.env.get("agi_callerid", "unknown")
    chan = agi.env.get("agi_channel", "unknown")
    uniqueid = agi.env.get("agi_uniqueid", str(int(time.time())))
    log(f"AI route start: chan={chan} caller={cid} uid={uniqueid}", agi)

    base_url = os.environ.get("AI_PIPE_URL", "http://127.0.0.1:8080")
    sock_path = os.environ.get("AI_PIPE_SOCKET", "/run/ai-pipe/ai-pipe.sock")
    gather_timeout = int(os.environ.get("AI_DTMF_TIMEOUT", "5000"))
    gather_timeout2 = int(os.environ.get("AI_DTMF_TIMEOUT2", "3000"))
    gather_max = int(os.environ.get("AI_DTMF_DIGITS", "32"))

    client = PipelineClient(base_url, sock_path)
    sid = ""

    try:
        # 1) 应答
        res = agi.answer()
        log(f"ANSWER result={res}", agi)
        if res < 0:
            return 1
        time.sleep(0.2)          # 等 RTP 建立（与 dialplan Wait(0.3) 互补）

        # 2) 建 AI 会话
        sid = client.open_session({
            "session_id": uniqueid,
            "channel": chan,
            "caller": cid,
            "codec": "G.711 A-law",
            "sample_rate": 8000,
        })
        if not sid:
            log("pipeline open_session failed", agi)
            agi.streamfile_or_silence("custom/ai-unavailable")
            agi.hangup()
            return 1
        agi.set_variable("AI_SESSION_ID", sid)
        log(f"AI session opened: {sid}", agi)

        # 3) 动作主循环
        pending_digits = ""
        while True:
            action = client.next_action(sid)
            kind = str(action.get("action", "hangup")).lower()
            log(f"action: {kind} {action}", agi)

            if kind == "play":
                f = ensure_playable(str(action.get("file", "")), sid)
                d = stream_play(agi, f)
                if d:
                    pending_digits += d
                    client.send_event(sid, {"type": "dtmf", "digit": d, "src": "playback"})

            elif kind == "say":
                f = client.synthesize(sid, str(action.get("text", "")),
                                      str(action.get("voice", "zh")))
                f = ensure_playable(f, sid)
                d = stream_play(agi, f)
                if d:
                    pending_digits += d
                    client.send_event(sid, {"type": "dtmf", "digit": d, "src": "tts"})

            elif kind == "gather":
                prompt = action.get("file")
                if prompt:
                    f = ensure_playable(str(prompt), sid)
                    digits = agi.get_data(f,
                                          int(action.get("timeout_ms", gather_timeout)),
                                          int(action.get("max_digits", gather_max)))
                else:
                    digits = agi.get_data("silence/1",
                                          int(action.get("timeout_ms", gather_timeout)),
                                          int(action.get("max_digits", gather_max)))
                digits = pending_digits + digits
                pending_digits = ""
                client.send_event(sid, {"type": "dtmf_string", "digits": digits,
                                        "timeout2_ms": gather_timeout2})
                log(f"gather -> '{digits}'", agi)

            elif kind == "transfer":
                target = str(action.get("target", "")).strip()
                if target:
                    agi.set_variable("AI_TRANSFER", target)
                    client.send_event(sid, {"type": "transfer", "target": target})
                    log(f"transfer -> {target}", agi)
                break

            elif kind == "hook":
                # 保持/恢复等软控制：留给扩展（EXEC）
                app = str(action.get("app", ""))
                arg = str(action.get("args", ""))
                if app:
                    agi.exec_app(app, arg)

            elif kind == "hangup":
                client.send_event(sid, {"type": "hangup"})
                break

            else:
                log(f"unknown action: {action}", agi)
                break

        # 4) 正常收尾
        client.close_session(sid)
        sid = ""
        agi.hangup()
        log("AI route normal end", agi)
        return 0

    except Exception as e:                                   # noqa: BLE001
        log(f"AI route exception: {e!r}", agi)
        try:
            if sid:
                client.close_session(sid)
        except Exception:
            pass

        try:
            agi.streamfile_or_silence("custom/ai-unavailable")
        except Exception:
            pass
        try:
            agi.hangup()
        except Exception:
            pass
        return 2


def stream_play(agi: AGI, path: str) -> str:
    """放音并捕获一个按键，返回数字字符或 ''。"""
    r = agi.stream_file(agi_base_name(path))
    # STREAM FILE result = 按键 ASCII 码，0 = 放完无按键，-1 = 错误/挂断
    if r > 0x20 and r < 0x7f:
        return chr(r)
    return ""


if __name__ == "__main__":
    sys.exit(run())
