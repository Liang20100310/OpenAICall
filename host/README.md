# FPGA 电话中继 + AI 语音指挥系统 —— 上位机软件（host/）

Python 上位机：**串口音频桥 + AI 语音管道 + HTTP/MCP 电发脑接口**。
协议契约见 [`../docs/INTERFACE_SPEC.md`](../docs/INTERFACE_SPEC.md)（UART 帧、寄存器、DTMF 编码、音频格式）。

```
FPGA ──UART(921600)──▶ BridgeService ──▶ CallSession（VAD→ASR→LLM→TTS）
                         ▲   │                          │
                         │   └── AUDIO_DL ◀── DownlinkStream ◀── TTS PCM
              DTMF/HOOK ─┘                                  │
                                                            ▼
                     HTTP / MCP（exec / status / tts / hangup / tools）
```

## 目录结构

| 路径 | 作用 |
|---|---|
| `audio_bridge/serial_framer.py` | UART 帧编解码：组帧/解帧、CRC8(poly=0x07)、失步重同步（搜 0xAA 0x55）、SEQ 丢帧统计（纯标准库） |
| `audio_bridge/bridge_service.py` | 串口音频收发服务：AUDIO_UL/DTMF_EVT/HOOK_EVT 回调、AUDIO_DL/RING_CMD/REG_WR/RD 发送、每通道抖动缓冲、环回自测、FpgaSimulator 模拟器 |
| `audio_bridge/audio_io.py` | G.711 A-law ↔ 16bit PCM（audioop + 纯 Python 备用）、重采样、WAV、上/下行音频流抽象 |
| `ai_pipeline/vad.py` | 能量 VAD（自适应噪声底限 + 挂尾 + 毛刺保护） |
| `ai_pipeline/asr.py` | STT：faster-whisper 本地 / OpenAI 兼容 HTTP API / 离线模拟 |
| `ai_pipeline/llm.py` | LLM：OpenAI 兼容 Chat Completions（base_url 可配，多轮记忆，流式/非流式）/ 离线回声 |
| `ai_pipeline/tts.py` | TTS：edge-tts / OpenAI 兼容 /audio/speech / 离线提示音 |
| `ai_pipeline/pipeline.py` | 通话会话编排：barge-in 打断、静音超时挂断、最大通话时长、DTMF 路由 |
| `mcp_http/server.py` | HTTP/MCP 服务：/exec（白名单脚本）、/status、/tts、/hangup、MCP tools |
| `run_host.py` | 总入口：一键组装 桥 + 语音管道 + MCP |
| `config.example.yaml` | 配置示例（复制为 config.yaml） |
| `requirements.txt` | 依赖（核心仅 pyserial + PyYAML，其余可选） |

## 安装

```bash
cd host
pip install -r requirements.txt          # 核心：pyserial + PyYAML
# 可选增强（缺依赖时代码会给出明确提示，不会崩溃）：
pip install faster-whisper               # 本地 STT
pip install edge-tts                     # Edge TTS（另需系统安装 ffmpeg）
pip install fastapi uvicorn              # HTTP 服务框架（缺省自动回退标准库）
```

## 快速开始

### 1. 自测（全部无需硬件/网络）

```bash
python3 audio_bridge/serial_framer.py    # 帧协议：回环/CRC 注入/失步重同步/SEQ 统计
python3 audio_bridge/audio_io.py         # G.711 参考值 + audioop 穷举一致 + 流抽象
python3 audio_bridge/bridge_service.py   # 模拟 FPGA 全链路（含 REG LOOPBACK 环回）
python3 ai_pipeline/vad.py               # VAD 状态机
python3 ai_pipeline/llm.py               # 离线 LLM + base_url 归一化
python3 ai_pipeline/tts.py               # ToneTTS + 缺依赖报错检查
python3 ai_pipeline/pipeline.py          # 端到端：打断/超时/DTMF/摘挂机
```

全部输出 `== 结果: PASS ==`（退出码 0）即通过。语法总检：

```bash
python3 -m py_compile audio_bridge/*.py ai_pipeline/*.py mcp_http/*.py run_host.py
```

### 2. 无硬件联调（软件模拟 FPGA）

```bash
python3 run_host.py --config config.example.yaml --sim
# 自动：PING 探测 → 环回自测(20/20) → 模拟摘机 → 通话建立
# 另开终端体验 MCP：
curl -s http://127.0.0.1:8765/status
curl -s -X POST http://127.0.0.1:8765/tts -d '{"text":"测试注入"}' -H 'Content-Type: application/json'
curl -s -X POST http://127.0.0.1:8765/hangup -d '{}' -H 'Content-Type: application/json'
```

### 3. 真实硬件

```bash
cp config.example.yaml config.yaml
# 修改：serial.port（如 /dev/ttyUSB0）、stt/llm 的 base_url 与 api_key、tts.type
python3 run_host.py --config config.yaml
# 串口链路体检（真实 FPGA，需开 REG LOOPBACK 支持）：
python3 audio_bridge/bridge_service.py --port /dev/ttyUSB0
```

### 4. 仅 MCP 演示服务

```bash
python3 mcp_http/server.py --demo --port 8765
```

## 配置说明（config.example.yaml 全量注释）

| 段 | 关键项 | 默认 | 说明 |
|---|---|---|---|
| `serial` | `port` / `baudrate` | `/dev/ttyUSB0` / `921600` | 串口设备与速率（协议 8N1 无流控） |
| `bridge` | `ul_jitter_frames` | `3` | 上行抖动缓冲深度（帧，20 ms/帧） |
| | `pace_dl` | `true` | 下行 20 ms 节拍发送；`false` 背靠背直发 |
| `stt` | `type` | `openai_api` | `faster_whisper` / `openai_api` / `simulated` |
| | `base_url` `api_key` `model` | — | openai_api 三件套（兼容 Copilot/OpenClaw 网关） |
| `llm` | `base_url` `api_key` `model` | — | 同上；`system_prompt` 可定制人设 |
| `tts` | `type` | `edge` | `edge` / `openai_api` / `tone` |
| `call` | `silence_timeout_s` | `15` | 静音超时挂断 |
| | `max_call_s` | `300` | 最大通话时长 |
| | `barge_in` | `true` | 用户开口立即停播 TTS |
| | `dtmf_hangup_digit` / `dtmf_interrupt_digit` | `#` / `*` | 按键挂断 / 打断 |
| `vad` | `threshold` / `hangover_frames` | `500` / `10` | 能量门限 / 话段结束判定 |
| `mcp` | `host` / `port` / `auth_token` | `127.0.0.1` / `8765` / 空 | HTTP/MCP 监听与鉴权 |
| | `scripts` | — | **脚本白名单**（见下） |

## HTTP / MCP 接口

| 接口 | 方法 | 请求体 | 作用 |
|---|---|---|---|
| `/status` | GET | — | 桥统计 + 通话状态 + 可用脚本 |
| `/exec` | POST | `{"name":"device_reboot","params":{"device_id":"7"}}` | 执行白名单脚本 |
| `/tts` | POST | `{"text":"..."}` | 向当前通话注入语音 |
| `/hangup` | POST | `{"reason":"..."}` | 挂断当前通话 |
| `/mcp` | POST | JSON-RPC 2.0 | MCP：`initialize` / `tools/list` / `tools/call` / `ping` |

MCP tools：`execute_script{name,params}`、`query_status{}`、`send_message{text}`。
配置 `mcp.auth_token` 后所有接口需 `Authorization: Bearer <token>`。

### 脚本白名单（安全模型）

```yaml
mcp:
  scripts:
    device_reboot:
      argv: ["/usr/bin/python3", "/opt/relay/scripts/device_reboot.py", "--device", "{device_id}"]
      timeout_s: 30
```

- **只有登记过的脚本可执行**，未登记名直接 403；
- `argv` 为参数向量（`shell=False`），`{param}` 占位符精确替换；
- 参数值白名单字符集（字母/数字/中文/`._-:=+/ `），且禁止以 `-` 开头（防注入额外选项）；
- 多参数/缺参数/超时均显式报错；每脚本独立超时并杀进程。

## 与 bridge/pipeline 的代码接线（自定义集成时参考）

```python
from audio_bridge.bridge_service import BridgeService, BridgeConfig
from audio_bridge.audio_io import DownlinkStream
from ai_pipeline.pipeline import CallSession, CallConfig
from ai_pipeline.asr import create_asr
from ai_pipeline.llm import create_llm
from ai_pipeline.tts import create_tts

dl = DownlinkStream()
session = CallSession(CallConfig(), create_asr(stt_cfg), create_llm(llm_cfg),
                      create_tts(tts_cfg), dl)

bridge = BridgeService(
    BridgeConfig(port="/dev/ttyUSB0"),
    on_audio_ul=lambda ch, g711, ts: ch == 0 and session.on_uplink_g711(g711),
    on_dtmf=lambda ch, d, ms: ch == 0 and session.on_dtmf(d, ms),
    on_hook=lambda ch, off: ch == 0 and session.on_hook(off),
)
bridge.start()
# 下行发送线程：
while True:
    frame = dl.next_g711_frame(0.05)
    if frame:
        bridge.send_audio_dl(0, frame)
```

或直接用现成入口：`python3 run_host.py --config config.yaml`（内部即上述接线 + MCP 服务）。

## 协议要点速查（详见 INTERFACE_SPEC.md）

- 帧：`AA 55 | TYPE | SEQ | LEN(2B LE) | PAYLOAD | CRC8`，CRC8 poly=0x07/init=0x00/MSB-first，覆盖 TYPE..PAYLOAD；
- 音频：20 ms/160 样点，G.711 A-law 8 bit，`AUDIO_UL/DL = CH(1B)+G711[160]`；
- DTMF 编码：`0x01..09='1'..'9'`、`0x0A='0'`、`0x0B='*'`、`0x0C='#'`，沿触发含 DUR_MS；
- 寄存器：`0x00 SCRATCH`、`0x02 LOOPBACK`（bit0=UART 环回）、`0x05 STATUS`（RO）、`0x08 VERSION`；
- `PING(0x7E) → 立即回 PONG(0x7F)`。

## 已知限制

1. **Asterisk/SIP 对接未包含**（INTERFACE_SPEC.md §8 的 PJSUA2/aiosip 分机注册），当前以"串口桥 + AI 管道 + MCP"为主干；对接时把 SIP 的 G.711 帧直接喂 `UplinkStream/DownlinkStream` 即可（免转码）。
2. **G.711 A-law 量化误差**：回环最大误差约 ±512（16bit 域），电话音质可接受；纯 Python 编解码与 audioop 已穷举一致（Python 3.13+ 无 audioop 亦可运行）。
3. **barge-in 采用"开口即断"策略**：受 VAD min_speech_frames（默认 2 帧 = 40 ms）影响，打断响应约 40–60 ms。
4. **TTS 打断粒度**：合成（TTS API）已发出的请求无法撤回，打断只保证"作废已合成音频 + 清空播放队列"。
5. **时长限制按通道计**：双通道并发通话时各会话独立计时（每通道一个 CallSession）。
6. **fastapi/uvicorn 缺省不装**：HTTP 服务自动回退标准库 `http.server`，功能一致（含 MCP）。
7. **`audioop` 在 Python 3.12 为弃用状态**：代码优先使用它并压制弃用告警，3.13+ 自动切换纯 Python 实现。

## 需要用户配置的项

- `serial.port`：串口设备名（Linux `/dev/ttyUSB0`，Windows `COM3`）；
- `stt.api_key` / `llm.api_key` / `tts.api_key`：对应服务的密钥（用本地 faster-whisper/离线模式可不配）；
- `stt.base_url` / `llm.base_url`：OpenAI 兼容端点（可指向 Copilot / OpenClaw 网关）；
- `mcp.scripts`：电发脑脚本白名单（按实际脚本路径登记）；
- `mcp.auth_token`：远程访问时务必配置。
