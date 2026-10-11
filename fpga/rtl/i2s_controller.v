//======================================================================
// i2s_controller.v : I2S 主控制器（Master，兼容高云 GW2AR / 通用 FPGA）
//----------------------------------------------------------------------
// 功能：
//   * 产生 MCLK / BCLK / LRCLK，作为 Master 与音频 Codec（TLV320AIC3104 等）
//     进行 16bit PCM 收发
//   * 标准 I2S 时序：MSB 相对 LRCLK 边沿延迟 1 个 BCLK，数据在 BCLK
//     上升沿采样、下降沿附近更新
//   * 时序（fs=8kHz）：MCLK=12.288MHz(1536*fs)，BCLK=512kHz(64*fs)，
//     LRCLK=8kHz，每帧 64 BCLK = 2 槽 x 32bit，有效数据 16bit 左对齐
//   * 全部逻辑同步于 clk_audio（24.576MHz），BCLK 用计数器使能产生，
//     避免门控时钟（利于时序收敛）
//   * loopback_en = 1 时收发数字环回（自测用）
//======================================================================
`timescale 1ns / 1ps
`default_nettype none

module i2s_controller #(
    parameter SLOT_WIDTH = 32,          // 槽宽（bit），32 => 2*32 = 64 BCLK/帧
    parameter DATA_WIDTH = 16           // 有效数据位宽
) (
    input  wire               clk_audio,    // 24.576MHz（必须 = 48 * BCLK）
    input  wire               rst_n,

    // 并行音频接口（帧首采样 tx_*，帧末输出 rx_*）
    input  wire [15:0]        tx_left,      // CH0
    input  wire [15:0]        tx_right,     // CH1
    output reg  [15:0]        rx_left,      // CH0
    output reg  [15:0]        rx_right,     // CH1
    output reg                rx_frame_valid,  // 每 8kHz 帧一次

    // I2S 物理接口
    output reg                mclk,
    output reg                bclk,
    output reg                lrclk,
    output wire               sdout,
    input  wire               sdin,

    // 控制 / 调试
    input  wire               loopback_en,
    output wire [5:0]         bit_cnt_dbg
);

    //------------------------------------------------------------------
    // 分频计数：clk_audio / 48 = BCLK；高 24 拍、低 24 拍
    //------------------------------------------------------------------
    reg [5:0] div_cnt;
    reg [5:0] bit_cnt;          // 0..63，一帧 64 BCLK

    // 采样/更新节拍（都在 BCLK 高/低相位中部，避免边沿竞争）
    wire tick_sample = (div_cnt == 6'd12);   // BCLK 高相位中部：采样 SDIN
    wire tick_update = (div_cnt == 6'd36);   // BCLK 低相位中部：更新 SDOUT/计数

    assign bit_cnt_dbg = bit_cnt;

    //------------------------------------------------------------------
    // 发送移位寄存器（16bit 有效数据左对齐于 32bit 槽，I2S 延迟 1 拍）
    //------------------------------------------------------------------
    reg [31:0] tx_shift;
    reg [15:0] rx_shift;
    reg [15:0] tx_r_hold;      // 右声道帧首锁存（保证左右声道同帧）

    wire [15:0] tx_l_src = loopback_en ? rx_left  : tx_left;
    wire [15:0] tx_r_src = loopback_en ? rx_right : tx_right;

    assign sdout = tx_shift[31];

    always @(posedge clk_audio or negedge rst_n) begin
        if (!rst_n) begin
            div_cnt        <= 6'd0;
            bit_cnt        <= 6'd0;
            mclk           <= 1'b0;
            bclk           <= 1'b0;
            lrclk          <= 1'b0;
            tx_shift       <= 32'h0000_0000;
            rx_shift       <= 16'h0000;
            tx_r_hold      <= 16'h0000;
            rx_left        <= 16'h0000;
            rx_right       <= 16'h0000;
            rx_frame_valid <= 1'b0;
        end else begin
            // MCLK = clk_audio / 2
            mclk <= ~mclk;

            // BCLK 波形
            if (div_cnt == 6'd23) begin
                bclk   <= 1'b0;
                div_cnt <= div_cnt + 6'd1;
            end else if (div_cnt == 6'd47) begin
                bclk    <= 1'b1;
                div_cnt <= 6'd0;
            end else begin
                div_cnt <= div_cnt + 6'd1;
            end

            rx_frame_valid <= 1'b0;

            //----------------------------------------------------------
            // 输出更新（BCLK 下降沿位置）
            //----------------------------------------------------------
            if (tick_update) begin
                if (bit_cnt == 6'd63) begin
                    bit_cnt <= 6'd0;
                    lrclk   <= 1'b0;                       // 左声道开始
                    tx_shift <= {1'b0, tx_l_src, 15'h0};   // I2S: 延迟 1 拍
                    tx_r_hold <= tx_r_src;                 // 右声道同帧锁存
                    rx_frame_valid <= 1'b1;
                end else begin
                    bit_cnt <= bit_cnt + 6'd1;
                    if (bit_cnt == 6'd31) lrclk <= 1'b1;   // 右声道开始
                    if (bit_cnt + 6'd1 == 6'd32)
                        tx_shift <= {1'b0, tx_r_hold, 15'h0};
                    else
                        tx_shift <= {tx_shift[30:0], 1'b0};
                end
            end

            //----------------------------------------------------------
            // 输入采样（BCLK 上升沿位置）
            // 数据位位于每槽 bit1..bit16（I2S 延迟 1 拍）
            //----------------------------------------------------------
            if (tick_sample) begin
                rx_shift <= {rx_shift[14:0], sdin};
                if (bit_cnt[4:0] == 5'd16) begin
                    if (bit_cnt[5] == 1'b0) rx_left  <= {rx_shift[14:0], sdin};
                    else                    rx_right <= {rx_shift[14:0], sdin};
                end
            end
        end
    end

endmodule

`default_nettype wire
