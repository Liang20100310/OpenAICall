#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 语音管道：通话会话编排（VAD → STT → LLM → TTS → 播放）。

一次 CallSession 的完整流程：
    1. 上行 PCM（20 ms 帧）持续喂给 VAD；
    2. 检测到语音 → 累积话段（含 100 ms 预缓冲）；语音结束 → 送 ASR；
    3. ASR 文本送 LLM 生成回复；回复送 TTS 合成 PCM；
    4. TTS PCM 进 DownlinkStream 切 20 ms G.711 帧，由 bridge 按节拍下发。

必须支持的通话控制：
    * 打断（barge-in）：TTS 播放期间检测到用户说话 → 立即停播并转入听音；
    * 静音超时挂断：可配，默认 15 s（无语音/无按键/无播放即计时）；
    * 最大通话时长：可配，默认 300 s；
    * 按键打断（DTMF 路由）：'*' 停播、'#' 挂断，可配置任意数字 → 处理函数。

线程模型：
    * 事件入口（on_uplink_pcm / on_dtmf / on_hook）由 bridge 回调线程调用，必须轻量；
    * ASR/LLM/TTS 在独立工作线程串行执行，合成结果经 generation 计数器
      防止"打断后旧语音继续播出"。

运行 `python3 ai_pipeline/pipeline.py` 执行离线端到端自测
（SimulatedASR + OfflineEchoLLM + ToneTTS，无网络/无硬件）。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional

try:
    from .asr import BaseASR, SimulatedASR
    from .llm import BaseLLM, OfflineEchoLLM
    from .tts import BaseTTS, ToneTTS
    from .vad import EnergyVAD, VadEvent, VADConfig
    from audio_bridge.audio_io import FRAME_MS, SAMPLE_RATE, alaw_to_linear
    from audio_bridge.audio_io import DownlinkStream
except ImportError:  # 直接以脚本运行时的回退
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ai_pipeline.asr import BaseASR, SimulatedASR  # type: ignore
    from ai_pipeline.llm import BaseLLM, OfflineEchoLLM  # type: ignore
    from ai_pipeline.tts import BaseTTS, ToneTTS  # type: ignore
    from ai_pipeline.vad import EnergyVAD, VadEvent, VADConfig  # type: ignore
    from audio_bridge.audio_io import FRAME_MS, SAMPLE_RATE, alaw_to_linear  # type: ignore
    from audio_bridge.audio_io import DownlinkStream  # type: ignore

__all__ = ["CallState", "CallConfig", "CallSession"]


class CallState(Enum):
    """通话状态机。"""
    IDLE = "idle"          # 空闲（未摘机）
    RINGING = "ringing"    # 振铃中（呼入）
    ACTIVE = "active"      # 通话中
    ENDED = "ended"        # 已挂断


@dataclass
class CallConfig:
    """通话控制参数。"""
    silence_timeout_s: float = 15.0    # 静音超时挂断（默认 15 s）
    max_call_s: float = 300.0          # 最大通话时长（默认 300 s）
    barge_in: bool = True              # 是否允许语音打断 TTS 播放
    greeting: str = ""                 # 应答后播报的问候语（空 = 不播）
    dtmf_hangup_digit: str = "#"       # 该按键挂断
    dtmf_interrupt_digit: str = "*"    # 该按键打断当前播报
    min_utterance_ms: int = 300        # 最短话段（低于则当噪声丢弃）
    preroll_frames: int = 5            # 话段预缓冲帧数（5 帧 = 100 ms）
    auto_answer: bool = True           # 摘机事件自动应答
    vad: VADConfig = field(default_factory=VADConfig)


class CallSession:
    """一次通话的语音管道会话。

    与 bridge_service 的接线（典型用法）：
        session = CallSession(cfg, asr, llm, tts, downlink, on_hangup=...)
        bridge = BridgeService(
            cfg_bridge,
            on_audio_ul=lambda ch, g711, ts: session.on_uplink_g711(g711) if ch == 0 else None,
            on_dtmf=lambda ch, d, ms: session.on_dtmf(d, ms) if ch == 0 else None,
            on_hook=lambda ch, off: session.on_hook(off) if ch == 0 else None,
        )
        下行：bridge 的发送线程循环调用 downlink.next_g711_frame() →
              bridge.send_audio_dl(ch, frame)。
    """

    def __init__(
        self,
        config: Optional[CallConfig],
        asr: BaseASR,
        llm: BaseLLM,
        tts: BaseTTS,
        downlink: DownlinkStream,
        channel: int = 0,
        on_hangup: Optional[Callable[[str, "CallSession"], None]] = None,
        on_state: Optional[Callable[[CallState, "CallSession"], None]] = None,
        on_asr: Optional[Callable[[str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        """config: 通话参数；asr/llm/tts: 管道三要素；downlink: 下行音频流（bridge 消费）。

        on_hangup(reason, session): 挂断回调（reason: silence_timeout / max_duration /
            on_hook / dtmf_hangup / local ...）；on_state(state, session): 状态变迁回调；
        on_asr(text): 每次识别到用户文本回调（日志/弹屏用）。
        """
        self.cfg = config or CallConfig()
        self.asr = asr
        self.llm = llm
        self.tts = tts
        self.downlink = downlink
        self.channel = channel
        self.on_hangup = on_hangup
        self.on_state = on_state
        self.on_asr = on_asr
        self.on_log = on_log

        self.state = CallState.IDLE
        self.vad = EnergyVAD(self.cfg.vad)
        self.dtmf_log: List[str] = []          # 本通按键记录
        self.dtmf_routes: Dict[str, Callable[[str], None]] = {}  # 自定义按键处理

        self._utt_buf = bytearray()            # 当前话段累积
        self._prebuf: Deque[bytes] = deque(maxlen=max(1, self.cfg.preroll_frames))
        self._play_gen = 0                     # 播放代号（打断时 +1，旧合成作废）
        self._synth_busy = False               # 工作线程是否正在合成/播报
        self._call_start = 0.0
        self._last_activity = 0.0
        self._hangup_reason: Optional[str] = None

        self._jobs: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop,
                                        name="call-pipeline-worker", daemon=True)
        self._worker.start()

        # 统计
        self.stat_utterances = 0
        self.stat_asr_texts = 0
        self.stat_barge_ins = 0
        self.stat_speak_jobs = 0

    # ------------------------------------------------------------------
    # 事件入口（bridge 回调线程调用，保持轻量）
    # ------------------------------------------------------------------

    def on_uplink_g711(self, g711: bytes) -> None:
        """上行 G.711 帧入口（160 字节），解码后走 on_uplink_pcm。"""
        self.on_uplink_pcm(alaw_to_linear(g711))

    def on_uplink_pcm(self, pcm: bytes, ts: Optional[float] = None) -> None:
        """上行 PCM 帧入口（320 字节 = 20 ms @ 8 kHz）。

        职责：超时检查 → VAD → 话段累积 → 话段结束提交工作线程。
        """
        if self.state != CallState.ACTIVE:
            return
        now = time.monotonic()

        # 最大通话时长
        if now - self._call_start >= self.cfg.max_call_s:
            self.hangup("max_duration")
            return
        # 静音超时（无语音/按键/播放 才计时；播放中不挂断）
        if not self._is_playing() and now - self._last_activity >= self.cfg.silence_timeout_s:
            self.hangup("silence_timeout")
            return

        evt = self.vad.process(pcm)
        self._prebuf.append(bytes(pcm))

        if evt == VadEvent.SPEECH_START:
            self._last_activity = now
            # 打断：用户开口立即停播 TTS
            if self.cfg.barge_in and self._is_playing():
                self._interrupt_playback("barge_in")
                self.stat_barge_ins += 1
            # 话段从预缓冲（含开口前 100 ms）开始
            self._utt_buf.clear()
            for frame in self._prebuf:
                self._utt_buf.extend(frame)
        elif evt == VadEvent.SPEECH:
            self._last_activity = now
            self._utt_buf.extend(pcm)
        elif evt == VadEvent.SPEECH_END:
            self._last_activity = now
            self._finish_utterance()

    def on_dtmf(self, digit: str, dur_ms: int = 0) -> None:
        """DTMF 按键事件（沿触发，每键一次）。"""
        if self.state != CallState.ACTIVE:
            return
        self._last_activity = time.monotonic()
        self.dtmf_log.append(digit)
        self._log(f"DTMF: '{digit}' ({dur_ms}ms)")
        if digit == self.cfg.dtmf_hangup_digit:
            self.hangup("dtmf_hangup")
            return
        if digit == self.cfg.dtmf_interrupt_digit:
            if self._is_playing():
                self._interrupt_playback("dtmf_interrupt")
            return
        handler = self.dtmf_routes.get(digit)
        if handler is not None:
            try:
                handler(digit)
            except Exception as exc:
                self._log(f"DTMF 处理函数异常: {exc}")

    def on_hook(self, off_hook: bool) -> None:
        """摘挂机事件：摘机→应答，挂机→挂断。"""
        if off_hook:
            if self.state in (CallState.IDLE, CallState.RINGING) and self.cfg.auto_answer:
                self.answer()
        else:
            if self.state == CallState.ACTIVE:
                self.hangup("on_hook")

    # ------------------------------------------------------------------
    # 通话控制
    # ------------------------------------------------------------------

    def ring(self, on: bool = True) -> None:
        """呼入振铃状态控制（由 bridge 的 RING_CMD 呼叫流程调用）。"""
        if on and self.state == CallState.IDLE:
            self._set_state(CallState.RINGING)
        elif not on and self.state == CallState.RINGING:
            self._set_state(CallState.IDLE)

    def answer(self) -> None:
        """应答通话：复位计时/VAD，可选播报问候语。"""
        if self.state == CallState.ACTIVE:
            return
        self._hangup_reason = None
        self._call_start = time.monotonic()
        self._last_activity = self._call_start
        self.vad.reset()
        self._utt_buf.clear()
        self._prebuf.clear()
        self.dtmf_log.clear()
        self._set_state(CallState.ACTIVE)
        self._log(f"通话应答（channel={self.channel}）")
        if self.cfg.greeting:
            self._submit_speak(self.cfg.greeting)

    def inject_tts(self, text: str) -> None:
        """向当前通话注入语音（MCP /tts 用）。空文本忽略。"""
        if self.state != CallState.ACTIVE:
            raise RuntimeError(f"通话未接通（state={self.state.value}），无法注入语音")
        if not text.strip():
            return
        self._submit_speak(text)

    def hangup(self, reason: str = "local") -> None:
        """挂断通话：停播、通知回调、置 ENDED。"""
        if self.state == CallState.ENDED:
            return
        self._hangup_reason = reason
        self._interrupt_playback(f"hangup:{reason}")
        self._set_state(CallState.ENDED)
        self._log(f"通话挂断：{reason}")
        if self.on_hangup:
            try:
                self.on_hangup(reason, self)
            except Exception as exc:
                self._log(f"on_hangup 回调异常: {exc}")

    def close(self) -> None:
        """释放工作线程（会话彻底结束时调用）。"""
        self.hangup("close")
        self._jobs.put(None)
        self._worker.join(timeout=2.0)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """会话状态字典（供 MCP /status 上报）。"""
        now = time.monotonic()
        return {
            "state": self.state.value,
            "channel": self.channel,
            "call_duration_s": round(now - self._call_start, 1) if self._call_start else 0.0,
            "last_activity_age_s": round(now - self._last_activity, 1) if self._last_activity else 0.0,
            "playing": self._is_playing(),
            "pending_downlink_frames": self.downlink.pending_frames,
            "hangup_reason": self._hangup_reason,
            "dtmf_log": list(self.dtmf_log),
            "vad_in_speech": self.vad.in_speech,
            "stats": {
                "utterances": self.stat_utterances,
                "asr_texts": self.stat_asr_texts,
                "barge_ins": self.stat_barge_ins,
                "speak_jobs": self.stat_speak_jobs,
            },
        }

    @property
    def active(self) -> bool:
        """通话是否进行中。"""
        return self.state == CallState.ACTIVE

    # ------------------------------------------------------------------
    # 内部：话段 / 播放 / 工作线程
    # ------------------------------------------------------------------

    def _finish_utterance(self) -> None:
        """一段用户说话结束：提交 ASR→LLM→TTS 任务。"""
        utt = bytes(self._utt_buf)
        self._utt_buf.clear()
        if len(utt) < self.cfg.min_utterance_ms * SAMPLE_RATE * 2 // 1000:
            return  # 过短，当噪声丢弃
        self.stat_utterances += 1
        self._jobs.put(("utterance", self._play_gen, utt))

    def _submit_speak(self, text: str) -> None:
        """提交一条"合成并播报"任务。"""
        self.stat_speak_jobs += 1
        self._jobs.put(("speak", self._play_gen, text))

    def _is_playing(self) -> bool:
        """是否有语音在播（合成中或下行队列非空）。"""
        return self._synth_busy or self.downlink.playing

    def _interrupt_playback(self, why: str) -> None:
        """打断播放：作废旧合成任务 + 清空下行缓冲。"""
        self._play_gen += 1
        cleared = self.downlink.clear()
        self._log(f"打断播放（{why}），清除 {cleared} 帧")

    def _set_state(self, new: CallState) -> None:
        """状态迁移并回调。"""
        if new == self.state:
            return
        old = self.state
        self.state = new
        self._log(f"状态: {old.value} -> {new.value}")
        if self.on_state:
            try:
                self.on_state(new, self)
            except Exception as exc:
                self._log(f"on_state 回调异常: {exc}")

    def _worker_loop(self) -> None:
        """工作线程：串行执行 ASR / LLM / TTS（长任务不阻塞 bridge 回调）。"""
        while True:
            job = self._jobs.get()
            if job is None:
                break
            kind, gen, data = job
            # 被打断的任务直接丢弃
            if gen != self._play_gen:
                continue
            try:
                self._synth_busy = True
                if kind == "utterance":
                    self._handle_utterance(gen, data)
                elif kind == "speak":
                    self._handle_speak(gen, data)
            except Exception as exc:
                self._log(f"工作线程任务异常: {exc}")
            finally:
                self._synth_busy = False

    def _handle_utterance(self, gen: int, pcm: bytes) -> None:
        """话段 → ASR → LLM → TTS 播放。"""
        text = self.asr.transcribe(pcm, SAMPLE_RATE).strip()
        if not text:
            self._log("ASR 识别为空，跳过")
            return
        self.stat_asr_texts += 1
        self._log(f"ASR: {text}")
        if self.on_asr:
            try:
                self.on_asr(text)
            except Exception as exc:
                self._log(f"on_asr 回调异常: {exc}")
        if self.state != CallState.ACTIVE or gen != self._play_gen:
            return  # 识别期间通话已结束/被打断
        reply = self.llm.chat(text).strip()
        self._log(f"LLM: {reply}")
        if self.state != CallState.ACTIVE or gen != self._play_gen:
            return
        self._handle_speak(gen, reply)

    def _handle_speak(self, gen: int, text: str) -> None:
        """文本 → TTS → 下行队列（若未被打断）。"""
        chunk = self.tts.synthesize(text)
        if gen != self._play_gen or self.state != CallState.ACTIVE:
            return  # 合成期间被打断，作废
        if chunk.pcm:
            self.downlink.submit_pcm(chunk.pcm, chunk.sample_rate)
            self.downlink.flush_tail()

    def _log(self, msg: str) -> None:
        """日志输出（可挂接上层）。"""
        if self.on_log:
            self.on_log(f"[call ch{self.channel}] {msg}")
        else:
            print(f"[call ch{self.channel}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 离线端到端自测（python3 ai_pipeline/pipeline.py）
# ---------------------------------------------------------------------------

def _self_test() -> bool:
    """SimulatedASR + OfflineEchoLLM + ToneTTS 全链路（含打断/超时/DTMF）。"""
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

    print("== pipeline 自测（离线） ==")

    import math
    import struct

    def speech_frame(amp: int = 8000) -> bytes:
        """构造一帧语音（正弦）PCM。"""
        return struct.pack("<160h", *[int(amp * math.sin(i / 4.0)) for i in range(160)])

    def silence_frame() -> bytes:
        """构造一帧静音 PCM。"""
        return b"\x00" * 320

    def new_session(**cfg_kw):
        """构造一套离线管道会话。"""
        asr = SimulatedASR()
        llm = OfflineEchoLLM()
        tts = ToneTTS()
        dl = DownlinkStream()
        cfg = CallConfig(**cfg_kw)
        session = CallSession(cfg, asr, llm, tts, dl)
        return session, asr, dl

    def wait_synth_done(dl, session, timeout: float = 2.0) -> bool:
        """等待：合成任务已产出音频（pending>0）且完整收尾（不再忙碌）。"""
        deadline = time.monotonic() + timeout
        while dl.pending_frames == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        while session._synth_busy and time.monotonic() < deadline:
            time.sleep(0.01)
        return dl.pending_frames > 0 and not session._synth_busy

    # ---- 场景 1：完整往返（语音 → ASR → LLM → TTS → 下行帧） ----
    session, asr, dl = new_session(greeting="你好", silence_timeout_s=30.0)
    asr.push("打开一号设备")
    session.answer()
    ok_greet = wait_synth_done(dl, session)
    check("应答后问候语入下行", ok_greet, f"pending={dl.pending_frames}")
    dl.clear()

    for _ in range(30):
        session.on_uplink_pcm(speech_frame())       # 600 ms 语音
    for _ in range(15):
        session.on_uplink_pcm(silence_frame())      # 300 ms 静音 → SPEECH_END
    ok_reply = wait_synth_done(dl, session)
    check("语音→ASR→LLM→TTS→下行 全链路", ok_reply,
          f"pending={dl.pending_frames} asr_texts={session.stat_asr_texts}")
    check("ASR 文本回调", session.stat_asr_texts == 1, str(session.stat_asr_texts))

    # ---- 场景 2：barge-in 打断 ----
    dl.clear()                                      # 清掉场景 1 残留，保证等待条件可靠
    session.inject_tts("这是一段很长的播报内容用于测试打断行为一二三四五")
    ok_play = wait_synth_done(dl, session)
    playing_before = dl.pending_frames
    for _ in range(10):
        session.on_uplink_pcm(speech_frame())       # 用户开口
    check("barge-in 立即停播", ok_play and dl.pending_frames == 0
          and session.stat_barge_ins == 1,
          f"before={playing_before} after={dl.pending_frames} barge_ins={session.stat_barge_ins}")

    # ---- 场景 3：DTMF 打断 / 挂断 ----
    dl.clear()
    session.inject_tts("第二次播报")
    wait_synth_done(dl, session)
    session.on_dtmf("*", 100)                        # '*' 打断
    check("DTMF '*' 打断播报", not session._is_playing())
    session.on_dtmf("1", 80)
    check("自定义 DTMF 记录", session.dtmf_log[-1] == "1", str(session.dtmf_log))
    session.on_dtmf("#", 80)                         # '#' 挂断
    check("DTMF '#' 挂断", session.state == CallState.ENDED
          and session.status()["hangup_reason"] == "dtmf_hangup")
    session.close()

    # ---- 场景 4：静音超时挂断 ----
    session2, _asr2, _dl2 = new_session(silence_timeout_s=0.4, max_call_s=300.0)
    session2.answer()
    t0 = time.monotonic()
    while session2.state == CallState.ACTIVE and time.monotonic() - t0 < 2.0:
        session2.on_uplink_pcm(silence_frame())
        time.sleep(0.02)                             # 按真实 20 ms 节拍推进计时
    check("静音超时挂断（默认 15s，测试配 0.4s）",
          session2.state == CallState.ENDED
          and session2.status()["hangup_reason"] == "silence_timeout",
          session2.status()["hangup_reason"] or session2.state.value)
    session2.close()

    # ---- 场景 5：最大通话时长 ----
    session3, _asr3, _dl3 = new_session(silence_timeout_s=300.0, max_call_s=0.3)
    session3.answer()
    t0 = time.monotonic()
    while session3.state == CallState.ACTIVE and time.monotonic() - t0 < 2.0:
        session3.on_uplink_pcm(speech_frame())
        time.sleep(0.02)
    check("最大通话时长挂断（默认 300s，测试配 0.3s）",
          session3.state == CallState.ENDED
          and session3.status()["hangup_reason"] == "max_duration",
          session3.status()["hangup_reason"] or session3.state.value)
    session3.close()

    # ---- 场景 6：摘挂机事件 ----
    session4, _a4, _d4 = new_session(silence_timeout_s=300.0)
    session4.on_hook(True)                           # 摘机 → 自动应答
    check("摘机自动应答", session4.state == CallState.ACTIVE)
    session4.on_hook(False)                          # 挂机
    check("挂机结束通话", session4.state == CallState.ENDED
          and session4.status()["hangup_reason"] == "on_hook")
    session4.close()

    print("== 结果: %s ==" % ("PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    sys.exit(0 if _self_test() else 1)
