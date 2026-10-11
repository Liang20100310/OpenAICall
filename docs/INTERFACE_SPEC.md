# 接口与协议规范（FPGA ↔ 上位机 / 内部总线）

版本：v1.0 ｜ 适用项目：FPGA 电话中继 + AI 语音指挥系统

本文档是 **RTL、上位机软件、PCB、测试** 四方共同遵守的唯一接口契约。任何修改需同步更新本文档并升版本号。

---

## 1. 全局参数

| 参数 | 值 | 说明 |
|---|---|---|
| 语音采样率 fs | 8000 Hz | 电话带宽 300–3400 Hz |
| 采样格式（线上/串口） | G.711 A-law 8 bit | 节省 UART 带宽；FPGA 内部为 16 bit 线性 PCM |
| 采样格式（FPGA 内部） | 16 bit 有符号，二进制补码 | I2S/Codec 侧 |
| 每帧语音时长 | 20 ms = 160 样点/通道 | 串口上一帧音频 = 160 字节/通道 |
| 语音通道数（首版） | 2（CH0、CH1） | 预留扩到 4 |
| UART | 921600 baud, 8N1, 无流控 | 参数化，可提升至 2 Mbaud |
| I2S | 标准 I2S（MSB 延迟 1 BCLK），32 bit 槽宽，16 bit 有效数据 | BCLK = 512 kHz，LRCLK = 8 kHz，MCLK = 12.288 MHz |
| 内部 TDM 总线 | 32 时隙 × 8 bit，时隙率 256 kHz，帧率 8 kHz | 见 §4 |

带宽核算：2 通道 × 160 B / 20 ms = 16 kB/s ≈ 160 kbps（含帧头 ≈ 190 kbps），
921600 baud 有效载荷约 92 kB/s，余量充足，可扩到 4 通道。

---

## 2. UART 帧协议（二进制，小端）

### 2.1 帧结构

```
偏移  长度  字段      说明
0     2    MAGIC     固定 0xAA 0x55（用于失步重同步）
2     1    TYPE      帧类型，见 2.2
3     1    SEQ       滚动序号 0..255，收方用于检测丢帧
4     2    LEN       载荷长度 N（小端），0 ≤ N ≤ 512
6     N    PAYLOAD   载荷
6+N   1    CRC8      CRC-8/ATM：poly=0x07, init=0x00, MSB first，覆盖 TYPE..PAYLOAD 末字节
```

解析规则：
- 收方在失步时逐字节搜索 `AA 55` 重新对齐；
- CRC 校验失败或 LEN 超界 → 丢弃该帧并计入错误计数（错误计数可在状态寄存器读取）；
- 发送方对 `AUDIO_DL` 连续发送时允许背靠背帧（无帧间空闲要求）。

### 2.2 帧类型

| TYPE | 方向 | 名称 | PAYLOAD |
|---|---|---|---|
| 0x10 | FPGA→PC | AUDIO_UL | `CH(1B)` + `G711A[160]`（20 ms 上行语音） |
| 0x11 | PC→FPGA | AUDIO_DL | `CH(1B)` + `G711A[160]`（20 ms 下行语音） |
| 0x20 | FPGA→PC | DTMF_EVT | `CH(1B)` + `DIGIT(1B)` + `DUR_MS(2B LE)` |
| 0x21 | FPGA→PC | HOOK_EVT | `CH(1B)` + `STATE(1B)`：0=挂机 on-hook，1=摘机 off-hook |
| 0x22 | PC→FPGA | RING_CMD | `CH(1B)` + `ON(1B)`：1=开始振铃，0=停止振铃 |
| 0x30 | PC→FPGA | REG_WR | `ADDR(1B)` + `VAL(1B)` |
| 0x31 | PC→FPGA | REG_RD | `ADDR(1B)` |
| 0x32 | FPGA→PC | REG_VAL | `ADDR(1B)` + `VAL(1B)` |
| 0x7E | 双向 | PING | 空 |
| 0x7F | 双向 | PONG | 空（收到 PING 立即回） |

### 2.3 DTMF 数字编码

| DIGIT | 含义 | DIGIT | 含义 |
|---|---|---|---|
| 0x01..0x09 | '1'..'9' | 0x0A | '0' |
| 0x0B | '*' | 0x0C | '#' |
| 0x00 / 0x0D..0xFF | 保留 | | |

DTMF 事件为**沿触发**：每个按键只上报一次，`DUR_MS` 为有效时长（典型 80–200 ms）。

---

## 3. FPGA 配置寄存器（REG_WR / REG_RD 地址空间）

| ADDR | 名称 | 复位值 | 说明 |
|---|---|---|---|
| 0x00 | SCRATCH | 0x00 | 读写测试寄存器（验证串口通路） |
| 0x01 | CHAN_EN | 0x03 | bit0/1 = CH0/CH1 音频上行使能 |
| 0x02 | LOOPBACK | 0x00 | bit0=UART 音频环回（DL→UL 直通），bit1=I2S 数字环回，bit2=CH0↔CH1 交叉 |
| 0x03 | DTMF_THR | 0x40 | DTMF 检测门限（归一化能量，8 bit） |
| 0x04 | DTMF_CFG | 0x03 | bit0=DTMF 使能，bit1=事件去重使能 |
| 0x05 | STATUS | RO | bit0=UART_RX_ERR，bit1=FIFO_OVF，bit2=CH0 摘机，bit3=CH1 摘机 |
| 0x06 | SLOT_MAP0 | 0x11 | CH0 下行读时隙映射（17 = TS17） |
| 0x07 | SLOT_MAP1 | 0x12 | CH1 下行读时隙映射（18 = TS18） |
| 0x08 | VERSION | 0x10 | 版本号 |

---

## 4. FPGA 内部 TDM 总线与时隙分配

- 信号：`ts_stb`（256 kHz 时隙脉冲）、`ts_frame`（8 kHz 帧脉冲）、`ts_tx[7:0]`、`ts_rx[7:0]`、`ts_slot[4:0]`（0..31）。
- 每帧 32 个时隙 × 8 bit = 256 bit @ 8 kHz = 2.048 Mbps。

| 时隙 | 用途 |
|---|---|
| TS0 | 空闲/同步（发 0x7E 标识，可选校验） |
| TS1 | CH0 上行（Codec → 交换 → UART） |
| TS2 | CH1 上行（Codec → 交换 → UART） |
| TS17 | CH0 下行（UART → 交换 → Codec） |
| TS18 | CH1 下行（UART → 交换 → Codec） |
| TS3..TS16, TS19..TS31 | 预留（第二片 Codec / 扩展通道） |

`tdm_switch` 通过 `SLOT_MAP` 寄存器配置"时隙 → 通道"的交叉连接，默认 TS1→CH0、TS2→CH1；
置 LOOPBACK[2] 时 TS1↔TS2 交叉，实现两部话机互打（数字交叉，不经上位机）。

---

## 5. I2S / Codec 接口

| 信号 | 方向（FPGA 视角） | 说明 |
|---|---|---|
| MCLK | 输出 | 12.288 MHz（1536 × fs），由音频 PLL 产生 |
| BCLK | 输出 | 512 kHz（64 × fs），Master |
| LRCLK/WS | 输出 | 8 kHz，低电平 = 左声道（CH0），高 = 右声道（CH1） |
| SDIN | 输入 | Codec → FPGA（上行 PCM，16 bit 左对齐于 32 bit 槽） |
| SDOUT | 输出 | FPGA → Codec（下行 PCM，16 bit 左对齐于 32 bit 槽） |

时序：标准 I2S，数据相对 LRCLK 翻转沿延迟 1 个 BCLK；MSB 先发；16 bit 有效数据后补 16 bit 0。
（若 Codec 配置为左对齐/DSP 模式，通过 `I2S_CFG` 参数切换，无需改接口。）

## 6. SPI / I2C 配置接口

| 接口 | 用途 | 速率 | 模式 |
|---|---|---|---|
| I2C | Codec（如 TLV320AIC3104）寄存器配置 | 100 kHz（400 kHz 可选） | Master，7-bit 地址 |
| SPI | SLIC（如 Si32176）寄存器配置 | ≤ 20 MHz（建议 1 MHz） | Mode 0，16-bit 帧（8 addr + 8 data） |

配置时序：复位释放后 20 ms，`cfg_sequencer` 从初始化 ROM 依次写寄存器；上位机也可通过 REG_WR 空间间接改写（首版可只支持 ROM + 串口透传调试）。

## 7. 时钟与复位

| 时钟 | 频率 | 来源 |
|---|---|---|
| CLK_SYS | 27 MHz | 开发板晶振（UART、DTMF、TDM、SPI/I2C） |
| CLK_AUDIO | 24.576 MHz | Gowin rPLL 由 27 MHz 倍频/分频 |
| MCLK/BCLK/LRCLK | 12.288M / 512k / 8k | CLK_AUDIO 分频 |

- 全局复位 `rst_n`（外部按键）+ 上电复位 + 异步复位同步释放（两级触发器）。
- I2S 与 TDM 之间用 8 kHz 帧脉冲做跨时钟域握手；音频 FIFO 深度 32 样点（4 ms）吸收抖动。

## 8. 上位机 ↔ Asterisk

- 音频桥服务（Python）通过 **本地 SIP UA**（PJSUA2 / aiosip）注册为分机 `1001`；
- Asterisk `pjsip.conf` 中 `1001` 为本地端点；Dialplan：
  - `00` → AI 语音管道（AGI/ARI 应答后接管媒体）
  - `01` + 分机号 → 内网 SIP 分机（`Dial(PJSIP/...)`）
  - `02` + 外线号 → SIP 中继（Twilio/Telnyx）
- 桥服务 ↔ Asterisk 走 G.711 A-law（与 FPGA 侧一致，免转码）。
