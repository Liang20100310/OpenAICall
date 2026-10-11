// =============================================================================
//  slic_pcm.v — Si32176 ×2 的 PCM/TDM 时隙收发器（方案 A 主语音通路）
// =============================================================================
//  所属项目：FPGA 电话中继 + AI 语音指挥系统（高云 GW2AR-18）
//  契约文档：docs/INTERFACE_SPEC.md §4（TDM 时隙）、§2.2（HOOK_EVT 0x21）
//
//  【帧格式】每条 SLIC 串行线（DR0/DX0、DR1/DX1 独立全双工）：
//      PCLK = 256 kHz（bit clock，FPGA 为 Master 输出，PCB §6 P12）
//      FS   = 8 kHz，高电平标记 bit0（帧首），PCB §6 P13
//      帧长 = 32 bit = 4 × 8 bit 时隙，MSB 先发
//      ┌────────┬────────┬────────┬────────┐
//      │ slot0  │ slot1  │ slot2  │ slot3  │
//      │ SYNC   │ 语音   │ 状态   │ 保留   │
//      │ 0x7E   │ G.711A │ bit0=  │ 0x00   │
//      │        │        │ 摘挂机 │        │
//      └────────┴────────┴────────┴────────┘
//
//  【与 tdm_switch / 内部 TDM 的映射】（INTERFACE_SPEC §4）：
//      DR0 slot1 → ul0 字节流 → TS1  (CH0 上行)     DX0 slot1 ← TS17 (CH0 下行)
//      DR1 slot1 → ul1 字节流 → TS2  (CH1 上行)     DX1 slot1 ← TS18 (CH1 下行)
//      slot2/slot3 不进 TDM，仅作状态/测试。
//
//  【摘挂机检测】两条路径，本模块实现 ①：
//      ① PCM 状态时隙提取（已实现）：slot2 bit0（1=摘机 off-hook，0=挂机）。
//         8 kHz 帧率采样，连续 HOOK_DEB_FRAMES=240 帧（30 ms）一致才翻转状态；
//         翻转时 hook*_evt 输出 1 个 clk 脉冲 → 由 uart_bridge 组帧
//         HOOK_EVT(0x21, CH, STATE)（STATE: 0=挂机，1=摘机，SPEC §2.2）。
//      ② SPI 轮询路径（在下一模块 spi_master/cfg_sequencer 实现）：
//         建议轮询周期 1 ms（1 kHz），读线路状态寄存器（地址为占位符，
//         需查 Si32176 datasheet「Register Map」/「Line Supervision」章节确认），
//         去抖同样 24~32 ms。若 Si32176 不在 PCM 时隙上报状态，改走 ②，
//         本模块 hook* 保持 0 即可。
//
//  【时序约定】（与 Si32176 真实边沿关系**需查数据手册确认**，必要时平移采样点）：
//      · DR 由 SLIC 在 PCLK 上升沿（位周期起点）驱动，本模块在位周期 3/4 点采样；
//      · DX 由本模块在位周期起点驱动，SLIC 在 PCLK 上升沿采样；
//      · 下行字节 dl*_valid 锁存后，最早在**下一帧** slot1 发出（1 帧流水）。
//
//  【时钟域】单时钟 clk = 24.576 MHz（CLK_AUDIO，PCLK_DIV=96 → 256 kHz）。
//      ul*/hook* 为本域脉冲；若 tdm_switch 工作在 27 MHz clk 域，
//      请经 fifo_sync/握手跨域（建议整个音频域统一用 clk_audio）。
// =============================================================================
`timescale 1ns / 1ps

module slic_pcm #(
    parameter integer PCLK_DIV        = 96,     // clk / PCLK_DIV = PCLK（24.576M/96=256k）
    parameter integer HOOK_DEB_FRAMES = 240,    // 摘挂机去抖帧数（240 帧 = 30 ms @8kHz）
    parameter [7:0]   SYNC_BYTE       = 8'h7E,  // slot0 内容（与 SPEC TS0 同步字一致）
    parameter [7:0]   IDLE_BYTE       = 8'hD5,  // 下行欠载填充（A-law 空闲码）
    parameter [7:0]   ST_TX_BYTE      = 8'h00   // 下行 slot2 发送值（bit0 备用）
)(
    input  wire       clk,          // 24.576 MHz
    input  wire       rst_n,        // 异步复位，同步释放由 top 保证

    // ---- SLIC 物理接口（PCB §6 P12-P15）----
    output reg        pclk,         // 256 kHz，Master 输出
    output reg        fs,           // 8 kHz，高 = bit0（帧首）
    input  wire       dr0,          // SLIC#0 → FPGA（CH0 上行串行）
    input  wire       dr1,          // SLIC#1 → FPGA（CH1 上行串行）
    output reg        dx0,          // FPGA → SLIC#0（CH0 下行串行）
    output reg        dx1,          // FPGA → SLIC#1（CH1 下行串行）

    // ---- 字节流接口（接 tdm_switch：ul*_data/ul*_wr、dl*_q/dl*_q_valid）----
    output reg  [7:0] ul0_byte,     // CH0 上行字节（G.711A）
    output reg        ul0_valid,    // 1 clk 脉冲，每帧 1 次
    output reg  [7:0] ul1_byte,
    output reg        ul1_valid,
    input  wire [7:0] dl0_byte,     // CH0 下行字节（来自 TS17）
    input  wire       dl0_valid,    // 字节有效（可与 tdm_switch dl0_q_valid 直连）
    input  wire [7:0] dl1_byte,
    input  wire       dl1_valid,

    // ---- 摘挂机（状态时隙提取 + 去抖）----
    output reg        hook0_state,  // 0=挂机 on-hook，1=摘机 off-hook
    output reg        hook1_state,
    output reg        hook0_evt,    // 状态翻转 1 clk 脉冲
    output reg        hook1_evt
);

    localparam integer FRAME_BITS = 32;                 // 4 时隙 × 8 bit
    localparam integer BIT_MID    = (PCLK_DIV * 3) / 4; // DR 采样点（3/4 位点）

    // ---- 内部寄存器 ----
    reg  [7:0]  dl0_tx, dl1_tx;     // 帧装载影子寄存器
    reg  [7:0]  dl0_hold, dl1_hold;
    reg         dl0_seen, dl1_seen;
    reg  [7:0]  rx0_sr, rx1_sr;     // 上行移位寄存器
    reg  [4:0]  bit_cnt;            // 0..31
    reg  [15:0] div_cnt;            // PCLK 分频计数
    reg         hook0_raw, hook1_raw;
    reg  [15:0] hook0_cnt, hook1_cnt;

    // 帧发送字：slot0=SYNC，slot1=下行语音，slot2=ST_TX_BYTE，slot3=0x00
    wire [FRAME_BITS-1:0] tx0_word = {SYNC_BYTE, dl0_tx, ST_TX_BYTE, 8'h00};
    wire [FRAME_BITS-1:0] tx1_word = {SYNC_BYTE, dl1_tx, ST_TX_BYTE, 8'h00};

    // 上行字节拼装（MSB 先入）
    wire [7:0] rx0_byte = {rx0_sr[6:0], dr0};
    wire [7:0] rx1_byte = {rx1_sr[6:0], dr1};

    wire        bit_tick  = (div_cnt == PCLK_DIV - 1);
    wire        frame_end = bit_tick && (bit_cnt == FRAME_BITS - 1);
    wire        tx_stb    = (div_cnt == 16'd0);          // 位周期起点（DX 驱动点）
    wire        rx_stb    = (div_cnt == BIT_MID);        // 3/4 位点（DR 采样点）
    wire [1:0]  slot_idx  = bit_cnt[4:3];                // 0..3

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            div_cnt     <= 16'd0;
            bit_cnt     <= 5'd0;
            pclk        <= 1'b0;
            fs          <= 1'b0;
            dx0         <= 1'b1;
            dx1         <= 1'b1;
            ul0_byte    <= 8'd0;
            ul0_valid   <= 1'b0;
            ul1_byte    <= 8'd0;
            ul1_valid   <= 1'b0;
            dl0_tx      <= IDLE_BYTE;
            dl1_tx      <= IDLE_BYTE;
            dl0_hold    <= 8'd0;
            dl1_hold    <= 8'd0;
            dl0_seen    <= 1'b0;
            dl1_seen    <= 1'b0;
            rx0_sr      <= 8'd0;
            rx1_sr      <= 8'd0;
            hook0_raw   <= 1'b0;
            hook1_raw   <= 1'b0;
            hook0_state <= 1'b0;
            hook1_state <= 1'b0;
            hook0_evt   <= 1'b0;
            hook1_evt   <= 1'b0;
            hook0_cnt   <= 16'd0;
            hook1_cnt   <= 16'd0;
        end else begin
            // ---- 单周期脉冲默认清零 ----
            ul0_valid <= 1'b0;
            ul1_valid <= 1'b0;
            hook0_evt <= 1'b0;
            hook1_evt <= 1'b0;

            // ---- PCLK 分频 / bit 计数 ----
            if (bit_tick) begin
                div_cnt <= 16'd0;
                bit_cnt <= (bit_cnt == FRAME_BITS - 1) ? 5'd0 : bit_cnt + 5'd1;
            end else begin
                div_cnt <= div_cnt + 16'd1;
            end
            pclk <= (div_cnt < PCLK_DIV / 2);
            fs   <= frame_end;          // 高电平覆盖 bit0 整个位周期

            // ---- 帧装载 + 摘挂机去抖（帧粒度，先于 dl 锁存，同拍时后者优先）----
            if (frame_end) begin
                dl0_tx   <= dl0_seen ? dl0_hold : IDLE_BYTE;
                dl1_tx   <= dl1_seen ? dl1_hold : IDLE_BYTE;
                dl0_seen <= 1'b0;
                dl1_seen <= 1'b0;

                if (hook0_raw == hook0_state) begin
                    hook0_cnt <= 16'd0;
                end else if (hook0_cnt == HOOK_DEB_FRAMES - 1) begin
                    hook0_state <= hook0_raw;
                    hook0_evt   <= 1'b1;
                    hook0_cnt   <= 16'd0;
                end else begin
                    hook0_cnt <= hook0_cnt + 16'd1;
                end

                if (hook1_raw == hook1_state) begin
                    hook1_cnt <= 16'd0;
                end else if (hook1_cnt == HOOK_DEB_FRAMES - 1) begin
                    hook1_state <= hook1_raw;
                    hook1_evt   <= 1'b1;
                    hook1_cnt   <= 16'd0;
                end else begin
                    hook1_cnt <= hook1_cnt + 16'd1;
                end
            end

            // ---- 下行字节锁存（可与帧装载同拍，seen 置 1 覆盖清 0）----
            if (dl0_valid) begin
                dl0_hold <= dl0_byte;
                dl0_seen <= 1'b1;
            end
            if (dl1_valid) begin
                dl1_hold <= dl1_byte;
                dl1_seen <= 1'b1;
            end

            // ---- DX 驱动（位周期起点，MSB 先发）----
            if (tx_stb) begin
                dx0 <= tx0_word[FRAME_BITS - 1 - bit_cnt];
                dx1 <= tx1_word[FRAME_BITS - 1 - bit_cnt];
            end

            // ---- DR 采样（3/4 位点）+ 字节/状态提取 ----
            if (rx_stb) begin
                rx0_sr <= rx0_byte;
                rx1_sr <= rx1_byte;
                if (bit_cnt[2:0] == 3'd7) begin
                    case (slot_idx)
                        2'd1: begin                       // 语音时隙
                            ul0_byte  <= rx0_byte;
                            ul0_valid <= 1'b1;
                            ul1_byte  <= rx1_byte;
                            ul1_valid <= 1'b1;
                        end
                        2'd2: begin                       // 状态时隙：bit0=摘挂机
                            hook0_raw <= rx0_byte[0];
                            hook1_raw <= rx1_byte[0];
                        end
                        default: ;                        // slot0/3 不处理
                    endcase
                end
            end
        end
    end

endmodule
