#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上位机总入口：串口音频桥 + AI 语音管道 + HTTP/MCP 接口 一键启动。

用法：
    python3 run_host.py --config config.yaml            # 真实串口
    python3 run_host.py --config config.yaml --sim      # 软件模拟 FPGA（无硬件自验）
    python3 run_host.py --demo                          # 仅启动 MCP 演示服务

接线关系：
    FPGA --UART--> BridgeService --on_audio_ul--> CallSession(VAD→ASR→LLM→TTS)
    CallSession --DownlinkStream--> 下行发送线程 --send_audio_dl--> BridgeService --> FPGA
    DTMF/HOOK 事件 --> CallSession（打断/挂断/路由）
    HTTP/MCP   --> ApiCore（状态查询 / 注入语音 / 挂断 / 白名单脚本）
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio_bridge.audio_io import DownlinkStream                       # noqa: E402
from audio_bridge.bridge_service import (                              # noqa: E402
    BridgeConfig, BridgeService, FpgaSimulator, FakeSerialTransport,
)
from ai_pipeline.asr import create_asr                                 # noqa: E402
from ai_pipeline.llm import create_llm                                 # noqa: E402
from ai_pipeline.tts import create_tts                                 # noqa: E402
from ai_pipeline.pipeline import CallConfig, CallSession, CallState     # noqa: E402
from ai_pipeline.vad import VADConfig                                  # noqa: E402
from mcp_http.server import build_core_from_config, run_server          # noqa: E402


def load_config(path: str) -> dict:
    """加载 YAML 配置（缺 PyYAML 时给出明确报错）。"""
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少 PyYAML：pip install PyYAML") from exc
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class SessionProxy:
    """通话会话动态代理：会话在摘机时才创建，MCP 侧始终拿当前会话。

    实现 build_core_from_config 所需的 status/inject_tts/hangup 三个方法。
    """

    def __init__(self, runtime: "HostRuntime") -> None:
        self._rt = runtime

    def _current(self) -> CallSession:
        s = self._rt.session
        if s is None or s.state == CallState.ENDED:
            raise RuntimeError("当前无进行中的通话")
        return s

    def status(self) -> dict:
        """当前会话状态（无会话时返回说明性字典）。"""
        s = self._rt.session
        return s.status() if s is not None else {"state": "none", "note": "尚无通话"}

    def inject_tts(self, text: str) -> None:
        """向当前通话注入语音。"""
        self._current().inject_tts(text)

    def hangup(self, reason: str) -> None:
        """挂断当前通话。"""
        s = self._rt.session
        if s is None:
            raise RuntimeError("当前无通话")
        s.hangup(reason)


class HostRuntime:
    """把 bridge / 通话会话 / MCP 组装成完整运行时。"""

    def __init__(self, cfg: dict, use_sim: bool) -> None:
        self.cfg = cfg
        self.use_sim = use_sim
        self.channel = int(cfg.get("call", {}).get("channel", 0))
        self.downlink = DownlinkStream()
        self.session: CallSession | None = None
        self._stop = threading.Event()

        # ---- 串口桥 ----
        bcfg_raw = dict(cfg.get("bridge", {}))
        scfg = dict(cfg.get("serial", {}))
        self.bridge_cfg = BridgeConfig(
            port=str(scfg.get("port", "/dev/ttyUSB0")),
            baudrate=int(scfg.get("baudrate", 921600)),
            read_chunk=int(bcfg_raw.get("read_chunk", 4096)),
            read_timeout=float(bcfg_raw.get("read_timeout", 0.05)),
            ul_jitter_frames=int(bcfg_raw.get("ul_jitter_frames", 3)),
            dl_jitter_frames=int(bcfg_raw.get("dl_jitter_frames", 250)),
            pace_dl=bool(bcfg_raw.get("pace_dl", True)),
            reg_timeout=float(bcfg_raw.get("reg_timeout", 0.5)),
            ping_timeout=float(bcfg_raw.get("ping_timeout", 0.5)),
        )
        self.sim: FpgaSimulator | None = None
        transport = None
        if use_sim:
            self.sim = FpgaSimulator()
            transport = FakeSerialTransport(self.sim)
        self._transport = transport

        self.bridge = BridgeService(
            self.bridge_cfg,
            transport=transport,
            on_audio_ul=self._on_audio_ul,
            on_dtmf=self._on_dtmf,
            on_hook=self._on_hook,
        )

        # ---- AI 管道组件（按配置创建；模拟模式自动降级为离线组件）----
        if use_sim:
            from ai_pipeline.asr import SimulatedASR
            from ai_pipeline.llm import OfflineEchoLLM
            from ai_pipeline.tts import ToneTTS
            self.asr = SimulatedASR()
            self.llm = OfflineEchoLLM()
            self.tts = ToneTTS()
        else:
            self.asr = create_asr(cfg.get("stt", {"type": "openai_api"}))
            self.llm = create_llm(cfg.get("llm", {"type": "openai_api"}))
            self.tts = create_tts(cfg.get("tts", {"type": "tone"}))

    # ---- bridge 回调 ----------------------------------------------------

    def _on_audio_ul(self, ch: int, g711: bytes, ts: float) -> None:
        """上行音频 → 当前通话会话。"""
        if ch == self.channel and self.session is not None:
            self.session.on_uplink_g711(g711)

    def _on_dtmf(self, ch: int, digit: str, dur_ms: int) -> None:
        """DTMF 事件 → 当前通话会话。"""
        if ch == self.channel and self.session is not None:
            self.session.on_dtmf(digit, dur_ms)

    def _on_hook(self, ch: int, off_hook: bool) -> None:
        """摘挂机 → 新建/结束通话会话。"""
        if ch != self.channel:
            return
        if off_hook:
            if self.session is None or self.session.state == CallState.ENDED:
                self._open_session()
                self.session.answer()
        else:
            if self.session is not None:
                self.session.hangup("on_hook")

    def _open_session(self) -> None:
        """创建一次新通话会话。"""
        ccfg_raw = dict(self.cfg.get("call", {}))
        vad_raw = dict(self.cfg.get("vad", {}))
        vad = VADConfig(
            threshold=float(vad_raw.get("threshold", 500.0)),
            snr_ratio=float(vad_raw.get("snr_ratio", 2.5)),
            adaptive=bool(vad_raw.get("adaptive", True)),
            noise_alpha=float(vad_raw.get("noise_alpha", 0.95)),
            hangover_frames=int(vad_raw.get("hangover_frames", 10)),
            min_speech_frames=int(vad_raw.get("min_speech_frames", 2)),
        )
        ccfg = CallConfig(
            silence_timeout_s=float(ccfg_raw.get("silence_timeout_s", 15.0)),
            max_call_s=float(ccfg_raw.get("max_call_s", 300.0)),
            barge_in=bool(ccfg_raw.get("barge_in", True)),
            greeting=str(ccfg_raw.get("greeting", "")),
            dtmf_hangup_digit=str(ccfg_raw.get("dtmf_hangup_digit", "#")),
            dtmf_interrupt_digit=str(ccfg_raw.get("dtmf_interrupt_digit", "*")),
            min_utterance_ms=int(ccfg_raw.get("min_utterance_ms", 300)),
            preroll_frames=int(ccfg_raw.get("preroll_frames", 5)),
            auto_answer=bool(ccfg_raw.get("auto_answer", True)),
            vad=vad,
        )
        self.session = CallSession(
            ccfg, self.asr, self.llm, self.tts, self.downlink,
            channel=self.channel,
            on_hangup=lambda reason, s: print(f"[host] 通话结束：{reason}", file=sys.stderr),
        )

    # ---- 下行发送线程 ----------------------------------------------------

    def _dl_sender_loop(self) -> None:
        """把 DownlinkStream 的 20 ms G.711 帧持续送入 bridge 下行链路。"""
        while not self._stop.is_set():
            frame = self.downlink.next_g711_frame(timeout=0.05)
            if frame is not None:
                self.bridge.send_audio_dl(self.channel, frame)

    # ---- 生命周期 --------------------------------------------------------

    def start(self) -> None:
        """启动桥服务与下行发送线程；模拟模式跑环回自测。"""
        self.bridge.start()
        threading.Thread(target=self._dl_sender_loop, name="dl-sender",
                         daemon=True).start()
        try:
            rtt = self.bridge.ping(timeout=1.0)
            print(f"[host] 链路探测 PING rtt={rtt:.2f}ms", file=sys.stderr)
        except TimeoutError:
            print("[host] 警告：PING 无响应，请检查串口/波特率", file=sys.stderr)
        if self.use_sim:
            result = self.bridge.run_loopback_test(channel=self.channel, frames=20)
            print(f"[host] 模拟链路环回自测: {result['detail']}", file=sys.stderr)
            if self.sim is not None and self._transport is not None:
                # 模拟一次摘机，方便无硬件体验完整通话流程
                self._transport.inject(self.sim.inject_hook(self.channel, True))
                time.sleep(0.1)
                if self.session is not None:
                    self.session.inject_tts("模拟链路已接通，请挂机或通过 MCP 接口交互")

    def stop(self) -> None:
        """停止全部组件。"""
        self._stop.set()
        if self.session is not None:
            self.session.close()
        self.bridge.stop()


def main() -> int:
    """命令行入口。"""
    ap = argparse.ArgumentParser(description="FPGA 电话中继上位机总入口")
    ap.add_argument("--config", default=None, help="config.yaml 路径")
    ap.add_argument("--sim", action="store_true", help="软件模拟 FPGA（无硬件）")
    ap.add_argument("--demo", action="store_true", help="仅启动 MCP 演示服务")
    ap.add_argument("--host", default=None, help="MCP 监听地址（覆盖配置）")
    ap.add_argument("--port", type=int, default=None, help="MCP 监听端口（覆盖配置）")
    args = ap.parse_args()

    if args.demo or (not args.config and not args.sim):
        # MCP 演示模式（无串口/无会话）
        from mcp_http.server import _demo_core
        print("[host] MCP 演示模式（无串口/无会话）", file=sys.stderr)
        run_server(_demo_core(), host=args.host or "127.0.0.1",
                   port=args.port or 8765)
        return 0

    if not args.config:
        print("请提供 --config config.yaml（或用 --demo）", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    runtime = HostRuntime(cfg, use_sim=args.sim)
    runtime.start()
    mcp_cfg = cfg.get("mcp", {})
    host = args.host or str(mcp_cfg.get("host", "127.0.0.1"))
    port = args.port or int(mcp_cfg.get("port", 8765))
    core = build_core_from_config(cfg, bridge=runtime.bridge, session=SessionProxy(runtime))
    print(f"[host] 服务就绪：串口桥 + 语音管道 + MCP http://{host}:{port}", file=sys.stderr)
    try:
        run_server(core, host=host, port=port)
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
