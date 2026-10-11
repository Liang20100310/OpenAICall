# FPGA 电话中继 + AI 语音指挥系统

普通座机拨号即可：**00** 转接电脑 AI 语音智能体（语音指挥干活）、**01** 转内网 SIP 分机、**02** 拨外线（PSTN）。

硬件：高云 GW2AR-18 FPGA（Tang Nano 20K / 立创逻辑派 G1）+ SLIC + Codec 模拟前端；
上位机：Asterisk/FreePBX + AI 语音管道（STT → LLM → TTS）+ MCP/HTTP 任务接口。

```
座机 ─ SLIC ─ Codec ─ FPGA ──UART(921600)── 上位机(Asterisk + AI Agent)
                          ├── 00 → AI 语音管道 (STT→LLM→TTS)
                          ├── 01 → 内网 SIP 分机
                          └── 02 → SIP 中继 → PSTN
```

---

## 1. 目录结构

| 目录 | 内容 |
|---|---|
| `docs/INTERFACE_SPEC.md` | **接口契约**（UART 帧协议、寄存器、TDM 时隙、I2S）——改动需升版本 |
| `docs/DEBUG_AND_TEST.md` | 调试与验证方案（仿真用例、环回、DTMF 误判排查、电气测试清单） |
| `docs/PRE_FAB_CHECKLIST.md` | 打板前检查清单（P0/P1/P2，含可运行附件） |
| `fpga/rtl/` | 全部可综合 Verilog（13 个模块，含可选 `slic_pcm.v`） |
| `fpga/tb/` | 8 个测试台 + `tools/gen_tables.py` 生成的参考表（`tb/gen/`） |
| `fpga/constr/tang_nano_20k.cst` | 高云 EDA 引脚约束模板（I2S / SPI / I2C / UART 分组标注） |
| `fpga/run_sim.sh` | 一键编译运行全部仿真 |
| `host/` | 上位机：串口音频桥、AI 语音管道、MCP/HTTP 服务、配置 |
| `asterisk/` | pjsip.conf / extensions.conf / AGI 路由脚本 |
| `pcb/PCB_DESIGN_SPEC.md` | PCB 设计规格书（框图、选型、布局布线、层叠、排针、测试点） |

## 2. FPGA 侧（Verilog，兼容高云 Gowin EDA）

| 模块 | 文件 | 说明 |
|---|---|---|
| DTMF 检测 | `dtmf_detector.v` | Goertzel 算法，8 频点（697/770/852/941 × 1209/1336/1477/1633），识别 0-9 * # 共 12 键；能量门限 + 主频占优 + twist 判据；按键去重/重按检测；单共享乘法器 |
| G.711 编解码 | `g711_codec.v` | 16bit 线性 PCM ↔ A-law / μ-law，与 Sun g711.c（Python audioop）**全码表逐位一致** |
| I2S 主控 | `i2s_controller.v` | Master：MCLK 12.288M / BCLK 512k / LRCLK 8k，32bit 槽 16bit 有效，收发 + 数字环回 |
| TDM 时隙交换 | `tdm_switch.v` | 32 时隙 × 8bit（G.711），TS1/2 上行、TS17/18 下行，映射寄存器可交叉（两机互打） |
| SPI 主机 | `spi_master.v` | Mode 0，≤20MHz（默认 ~1MHz），配置 SLIC（Si32176） |
| I2C 主机 | `i2c_master.v` | 100/400kHz，配置 Codec（TLV320AIC3104） |
| 配置时序器 | `cfg_sequencer.v` | 上电延时后按 ROM 写 Codec/SLIC 寄存器（ROM 数值需按数据手册填） |
| UART 收发 | `uart_tx.v` / `uart_rx.v` | 分数分频波特率（无累积误差），16x 过采样接收 |
| 帧协议桥 | `uart_bridge.v` | AA55 帧、CRC8、失步重同步、音频 FIFO、事件队列、寄存器/振铃接口 |
| FIFO | `fifo_sync.v` | 同步 FIFO（上行 512B/通道、下行 1KB/通道） |
| 顶层 | `top.v` | 例化互联、复位同步、跨时钟域、寄存器文件、2 通道 DTMF、事件仲裁 |
| 音频时钟 | `pll_audio_clk.v` | 27MHz→24.576MHz（上板用高云 rPLL；仿真透传） |
| *(可选变体)* | `slic_pcm.v` | Si32176 直出 PCM（256kHz×32bit 串行帧）→ TDM 的适配器，含摘挂机提取/去抖。**方案 A（无独立 Codec、SLIC 直接出 PCM）时使用**；本仓库默认方案为 Codec I2S（方案 B） |

### 仿真（已全部通过）

```bash
cd fpga
sh run_sim.sh        # 需要 iverilog（Icarus Verilog ≥ 10）
```

| 测试台 | 覆盖 | 结果 |
|---|---|---|
| `tb_g711` | A-law/μ-law 全码表（65536+256 项 ×2）与 audioop 参考实现比对 | ✅ 逐位一致 |
| `tb_dtmf` | 12 键双音逐个 + 重按 + 静音/单音/噪声拒绝 | ✅ 14/14 |
| `tb_i2s` | 收发环回一致性 + MCLK/BCLK/LRCLK 分频关系 | ✅ |
| `tb_tdm_switch` | 默认映射 / 交叉映射 / 多帧双缓冲 | ✅ |
| `tb_uart_loop` | 比特级 256 字节收发 + 停止位错误检测 | ✅ |
| `tb_uart_bridge` | PING/PONG、REG 读写、音频上下行 160B、事件帧、CRC 容错重同步 | ✅ 6/6 |
| `tb_top` | **全链路集成**（比特级 UART + 帧协议 + 寄存器 + FIFO + 环回） | ✅ |

调试用测试台：`tb_dtmf_dbg.v`、`tb_uart_dbg.v`（打印内部状态）。

### 上板（Gowin EDA）

1. 新建工程 → 器件选 **GW2AR-LV18QN88C8/I7**（Tang Nano 20K）或 G1 对应型号；
2. 加入 `fpga/rtl/*.v`（含 `top.v`，顶层设为 `top`；`slic_pcm.v` 仅方案 A 需要）；
3. 导入 `constr/tang_nano_20k.cst`，**按原理图核对全部引脚号**（文件内已标注示例/待改）；
4. IP Core Generator 生成 **rPLL**：27MHz → 24.576MHz，替换 `pll_audio_clk.v` 中的兜底直通；
5. SDC 时钟约束见 `.cst` 文件尾部说明；综合 → 布线 → 下载。

## 3. 上位机（Python 3.10+）

```bash
cd host
pip install -r requirements.txt     # 核心仅 pyserial + PyYAML，其余可选
cp config.example.yaml config.yaml  # 填串口号 / API key / 脚本白名单
python3 run_host.py --sim           # 无硬件自测（内置 FPGA 模拟器）
python3 run_host.py                 # 接实际 FPGA
```

- `audio_bridge/`：UART 帧编解码（CRC8/重同步/丢帧统计）、串口音频服务、G.711 转换（audioop + 纯 Python 双实现）、抖动缓冲；
- `ai_pipeline/`：VAD → STT（faster-whisper / OpenAI 兼容）→ LLM（OpenAI 兼容 Chat，兼容 Copilot/OpenClaw 网关）→ TTS（edge-tts / OpenAI 兼容），支持**打断（barge-in）**、静音超时挂断（15s）、最大通话时长（300s）、DTMF 路由（`*` 打断 / `#` 挂断）；
- `mcp_http/`：`/status` `/exec`（白名单脚本）`/tts` `/hangup` `/mcp`（MCP tools：execute_script / query_status / send_message），Bearer 鉴权；
- 自测：`serial_framer` 17 项、`audio_io` 24 项（含 65536 穷举）、`bridge_service` 7 项、`pipeline` 9 项 —— 全部实际运行通过。

## 4. Asterisk / PBX

见 `asterisk/README.md`：分机 `1001`（FPGA 中继）、拨号计划 `00/01XX/02X.`、Twilio/Telnyx 中继模板、AGI 脚本 `agi_ai_route.py`、安全加固（fail2ban / TLS/SRTP / 禁匿名）。
全链路统一 **G.711 A-law**，与 FPGA 串口格式一致，免转码。

## 5. PCB

见 `pcb/PCB_DESIGN_SPEC.md`：系统框图、器件三选一（SLIC：Si32176 / Le79R70 / Le88506；Codec：TLV320AIC3104 / Le58Q021 / Si3241）、电源树（-48V 馈电 + 90Vrms 振铃）、高压安全间距（IPC-2221B，工程取 ≥2.0mm）、AGND/DGND 分割、4 层叠层、2×20 排针分配、23 个测试点。

**推荐组合**：SLIC 选 **Si32176 ×2**（SPI + 内置振铃发生器，省外部高压振铃电源），Codec 选 **TLV320AIC3104**（I2C + I2S 16bit，3.3V 同域）。

## 6. 已知限制 / 待确认

1. `cfg_sequencer.v` 的初始化 ROM 数值是**格式示例**，必须按所选 SLIC/Codec 数据手册替换后才能上板；
2. `pll_audio_clk.v` 上板需替换为高云 rPLL IP（27MHz 无法整数分频出 24.576MHz）；
3. DTMF 最短可测按键时长 = 2 × 32ms = 64ms（`STABLE_N=2`）；40ms 短按需设 `STABLE_N=1`；
4. `.cst` 引脚号为模板示例，须按实际 PCB 原理图核对；
5. 两路以上扩展：TDM 已预留 TS3-16 / TS19-31，加 `slic_pcm` 或第二 Codec 即可；
6. 电话侧电气指标（回波损耗、侧音、振铃波形）需电话分析仪实测定稿。
