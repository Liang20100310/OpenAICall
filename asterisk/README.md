# Asterisk 配置说明 - FPGA 电话中继 + AI 语音指挥系统

本目录是 Asterisk(18/20 LTS)侧配置示例与 AGI 脚本,与 `../docs/INTERFACE_SPEC.md`
(§8 上位机 ↔ Asterisk)保持一致。目标:

- FPGA 中继设备(2 路 FXS 电话口,经**音频桥服务**)以 SIP 分机 **1001** 注册到本 PBX;
- 拨 **`00`** → 接 **AI 语音管道**(AGI/ARI 应答后接管放音/收号/挂断);
- 拨 **`01` + 2 位分机号** → 内网 SIP 分机(示例 `2001`/`2002`);
- 拨 **`02` + 外线号码** → SIP 中继出局(Twilio / Telnyx 模板);
- 全链路 **G.711 A-law 8 kHz**,与 FPGA 的 G.711 A-law 串口音频一致,端到端免转码。

## 文件清单

| 文件 | 作用 |
|---|---|
| `pjsip.conf` | 传输层、分机 1001/2001/2002、Twilio/Telnyx 中继模板、鉴权 |
| `extensions.conf` | 拨号规则:`00` / `01XX` / `02X.`、中继来电路由、匿名拒接兜底 |
| `modules.conf` | 最小化加载模块(PJSIP + AGI/ARI + G.711),示例 |
| `agi_ai_route.py` | `00` 的 AGI 脚本:应答 ↔ AI 管道(HTTP/Unix Socket)↔ 放音/收号/挂断 |

## 号码规划

| 拨号 | 路由 | 实现 |
|---|---|---|
| `00` | AI 语音管道 | `Answer()` → `AGI(agi_ai_route.py)` → 按 AI 动作放音/收号/转接/挂断 |
| `01XX`（如 `0101`） | 内网 SIP 分机 | MATH 线性映射：短号 `01`→`2001` … `10`→`2010`，`Dial(PJSIP/${EXT_TARGET},30,Tt)` |
| `02` + 号码 | 外线 | `Dial(PJSIP/${EXTEN:2}@twilio-trunk,60,Ttr)` |
| `2XXX` | 分机直拨 | `Dial(PJSIP/${EXTEN},30,Tt)` |
| 1002 | 回声测试 | `Echo()`，验证 RTP 双向质量 |

> 注：接口规范定义“`01` + 2 位分机号”。`extensions.conf` 的 `_01XX` 即 2 位短号，
> 用 `MATH(2000+N)` 线性映射到分机 `2001..2010`（扩容只改 `> 10` 处）；
> 每新增分机需在 `pjsip.conf` 补 endpoint/auth/aor 三段（模板见其 §2002 后注释）。
> **接口契约以 INTERFACE_SPEC §8 为准。**

## 部署步骤

```bash
# 1) 安装(Debian/Ubuntu 示例)
sudo apt install asterisk asterisk-core-sounds-en asterisk-core-sounds-zh  # 或源码装 20 LTS

# 2) 放置配置(先备份原配置)
sudo cp pjsip.conf extensions.conf modules.conf /etc/asterisk/
sudo cp agi_ai_route.py /var/lib/asterisk/agi-bin/
sudo chmod 755 /var/lib/asterisk/agi-bin/agi_ai_route.py
sudo mkdir -p /var/lib/asterisk/agi-cache /var/log/asterisk

# 3) 修改占位符
#    pjsip.conf:   <强密码...>、<account>.pstn.twilio.com、中继凭据
#    extensions.conf: OUTCALLERID、中继选择(Twilio/Telnyx)

# 4) 重载
sudo asterisk -rx "core restart now"
asterisk -rx "pjsip show endpoints"     # 应看到 1001/2001/2002
asterisk -rx "pjsip show registrations" # 应看到 twilio-trunk Registered
asterisk -rx "dialplan show internal"
```

## 与音频桥服务 / FPGA 的对接

```
FPGA(GW2AR-18) ──UART 921600·G.711A──▶ 音频桥服务(Python) ──SIP/RTP G.711A──▶ Asterisk
      │ SLIC×2 / Codec                        │ PJSUA2/aiosip 注册 1001        │
   FXS 电话口×2                          本地 socket/HTTP               AI 管道/分机/中继
```

- 桥服务必须以 **G.711 A-law 8 kHz** 建立 RTP(`pjsip.conf` 各端点 `allow=alaw` 已强约束);
- DTMF 建议 **RFC2833/4733 带外**(`dtmf_mode=rfc4733`):FPGA 检出的 DTMF(`DTMF_EVT`)
  由桥服务转成 SIP INFO/RFC4733 上送,避免二次音频检测;
- 振铃方向:Asterisk → 桥服务(呼叫 1001)→ FPGA `RING_CMD(0x22)` → SLIC 振铃 90 Vrms/20 Hz;
  摘挂机(`HOOK_EVT 0x21`)由桥服务映射为 SIP 呼叫应答/挂断。

## AI 管道接口(`agi_ai_route.py` 依赖)

管道进程需实现以下 HTTP REST(或 Unix Socket 等价 JSON-lines):

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/sessions` | 开会话,入参 `{session_id, channel, caller, codec, sample_rate}`,返回 `{session_id}` |
| GET | `/v1/sessions/{id}/next_action` | 阻塞取下一动作(见下) |
| POST | `/v1/sessions/{id}/events` | DTMF/转接/挂断事件回传 |
| POST | `/v1/sessions/{id}/tts` | 文本合成,返回 `{file}` 本地音频路径 |
| DELETE | `/v1/sessions/{id}` | 关会话 |

动作格式:`{"action":"play","file":"..."}`、`{"action":"say","text":"..."}`、
`{"action":"gather","timeout_ms":5000,"max_digits":4}`、
`{"action":"transfer","target":"2001"}`、`{"action":"hangup"}`。

脚本行为:`ANSWER` → 开会话 → 循环执行动作(放音时同步收号并回传事件)
→ `transfer` 时置 `AI_TRANSFER` 变量交回 dialplan 转接 → `hangup` 或异常都确保挂断。
日志写 `/var/log/asterisk/agi_ai_route.log`(`AI_LOG_FILE` 可改)。

若改用 **ARI** 直接接管媒体(录音/旁路注入),在 `modules.conf` 已加载
`res_ari.so`/`res_stasis*.so`,用 `Stasis()` 替换 `AGI()` 行即可,媒体操作走
`res_stasis_snoop`(旁听)与 playback/recording 资源。

## 安全建议(必做)

1. **强密码**:所有 endpoint/auth 密码 ≥ 16 位随机字符,各不相同;中继密码与分机密码隔离;
   定期轮换。禁止使用 `1234`/`password` 等弱口令(`sipvicious`/`sngrep` 扫描器专扫)。
2. **禁止匿名呼叫**:
   - 不定义无 auth 的 endpoint(PJSIP 默认拒绝未鉴权注册/呼叫);
   - `[public]` 上下文对一切呼叫 `Hangup()`;
   - `allow_guest=no`(`pjsip.conf` 默认即否,勿开启);
   - `asterisk -rx "pjsip show endpoints" | grep -v OK` 定期审计在线注册。
3. **fail2ban**(封锁暴力破解/扫描):

   ```ini
   # /etc/fail2ban/jail.d/asterisk.conf
   [asterisk]
   enabled  = true
   port     = 5060,5061
   protocol = udp,tcp
   filter   = asterisk
   logpath  = /var/log/asterisk/security.log   ; 或 full.log
   maxretry = 5
   findtime = 600
   bantime  = 3600
   ```
   并在 `logger.conf` 开启 `security` 日志通道;`iptables`/`nftables` 对 5060 限速
   (如 `-m limit --limit 20/min`)。
4. **网络边界**:PBX 不直接暴露公网 5060;如需外线,中继侧用 **IP ACL**
   (`[twilio-identify]` 的 `match` 官方网段)+ 注册鉴权双保险;防火墙限制
   RTP 端口段(`rtp.conf` `rtpstart/rtpend`)只对中继 IP 开放。
5. **传输加密(推荐)**:内网可用 UDP;跨公网建议 TLS 信令(`[transport-tls]`,
   已留模板)+ SRTP(端点 `media_encryption=sdes`)。
6. **权限最小化**:`modules.conf` 只加载必需模块;Asterisk 以专用低权限用户运行;
   `agi_ai_route.py` 目录 755、属主 root,AGI 可写目录仅 `agi-cache`。
7. **审计**:开 `cdr_manager`/`cel_manager`;录音告知合规(涉及通话录音需用户同意)。

## 快速验证

```bash
asterisk -rx "pjsip set logger on"          # 信令抓取
asterisk -rx "core set verbose 5"
# 1001 → 00:应听到 AI 提示音,日志出现 "AI session opened"
# 1001 → 012001(或 0120):2001 振铃
# 1001 → 021380013800:中继出局,主叫为 <中继主叫号码>
# 1001 → 1002:回声测试
sngrep          # 另一终端观察 SIP 信令
tcpdump -i any -n portrange 10000-20000      # RTP 确认 alaw(payload type 8)
```

## 用 `asterisk -rx` 测试拨号路由

所有命令在服务器 shell 直接执行（等效于进 Asterisk CLI 敲命令）。建议先开信令日志：

```bash
asterisk -rx "core set verbose 5"
asterisk -rx "pjsip set logger on"
```

**1) 配置就绪自检**

```bash
asterisk -rx "dialplan show internal"          # 应看到 00 / _01XX / 1002 / 2XXX 等规则
asterisk -rx "dialplan show ext-local"         # 确认 _01XX 映射段已加载
asterisk -rx "pjsip show endpoints"            # 1001/2001/2002 三段齐全，状态 Unreachable/Idle 均正常
asterisk -rx "pjsip show endpoint 1001"        # 逐项看 auth/aor/contact 绑定
asterisk -rx "pjsip show registrations"        # twilio-trunk 应为 Registered
```

**2) 不接真机，用 Originate 模拟拨号（验证路由走向）**

```bash
# 拨 00 → AI 管道：应执行 Answer+AGI，AGI 日志出现 "AI route start"
asterisk -rx "channel originate Local/00@internal application Echo"

# 拨 0101（短号 01 → 2001）：应看到 Dial(PJSIP/2001) 尝试
asterisk -rx "channel originate Local/0101@internal application Monitor"

# 拨 02+外线：观察是否向 twilio-trunk 发 INVITE（配合 sngrep 看信令）
asterisk -rx "channel originate Local/021380013800@internal application Monitor"

# 边界用例：非法短号 0199（>10）应拒绝并播 pbx-invalid
asterisk -rx "channel originate Local/0199@internal application Monitor"
```

**3) 接真机/软电话端到端验证**

```bash
# 1001 摘机拨 00：听 AI 提示音；同时另一终端看 RTP 与 AGI 日志
asterisk -rx "core show channels"               # 通话中应有 PJSIP/1001-... 与 AGI 通道
tail -f /var/log/asterisk/agi_ai_route.log      # 见 "AI session opened"

# 1001 拨 0101：2001 振铃；1001 拨 0102：2002 振铃
# 1001 拨 1002：回声测试（验证 RTP 双向）
# 1001 拨 02+号码：中继出局，主叫为 <中继主叫号码>

# 挂断后核对 CDR
asterisk -rx "cdr show status"
tail /var/log/asterisk/cdr-csv/Master.csv
```

**4) 路由走向速查**

| 拨号 | 预期 CLI 现象（`core set verbose 5` 下） |
|---|---|
| `00` | `-- Exec [internal/00] Answer()` → `AGI(agi_ai_route.py)` → AGI 日志 |
| `0101` | `-- Exec [ext-local/01XX] Set(EXT_TARGET=2001)` → `Dial(PJSIP/2001,...)` |
| `0199` | `GotoIf(...> 10...)` → `Playback(pbx-invalid)` → Hangup |
| `021380013800` | `GoTo(external-out,1380013800)` → `Dial(PJSIP/1380013800@twilio-trunk,...)` |

常见问题：

| 现象 | 排查 |
|---|---|
| 1001 注册后立即掉线 | `max_contacts=1` 与重复注册冲突;看 `pjsip set logger on` 的 401/403 |
| 拨 00 无提示音 | `agi_ai_route.py` 权限/`AI_PIPE_URL` 不通;看 AGI 日志 |
| 双方听不到声音 | 直通媒体被 NAT 阻断--确认 `direct_media=no`、`rtp_symmetric=yes`、外部 RTP 地址配置 |
| 中继单通 | 运营商要求固定主叫/支持 `send_pai`;核对 `from_user`/`from_domain` |
| 出现转码 | `allow` 被改动;`rtp set debug on` 看 payload type,应恒为 8(PCMA) |
