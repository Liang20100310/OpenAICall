//======================================================================
// g711_codec.v : G.711 A-law / mu-law 编解码（16bit 线性 PCM <-> 8bit 码字）
//----------------------------------------------------------------------
// 设计说明：
//   * 算法与 ITU-T G.711 / Sun g711.c（Python audioop 同源）逐位一致，
//     仿真测试台 tb_g711.v 用全码表（256 + 65536 项）逐点比对通过。
//   * 纯组合函数 + 一级输出寄存器（1 拍延迟），可综合，无厂商原语。
//   * A-law 量化精度 13bit（大信号段步长 1024 LSB@16bit，往返最大误差 512），
//     mu-law 精度 14bit（往返最大误差 256）。
// 端口约定：
//   enc: pcm_in(16bit 有符号) -> code_out(8bit)
//   dec: code_in(8bit) -> pcm_out(16bit 有符号)
//   valid 信号为单周期脉冲，输出 valid 延迟 1 拍。
//======================================================================
`timescale 1ns / 1ps
`default_nettype none

module g711_codec #(
    parameter LAW = 0           // 0 = A-law, 1 = mu-law
) (
    input  wire        clk,
    input  wire        rst_n,

    // 编码通道：线性 PCM -> G.711
    input  wire signed [15:0] enc_pcm_in,
    input  wire               enc_valid,
    output reg  [7:0]         enc_code_out,
    output reg                enc_code_valid,

    // 解码通道：G.711 -> 线性 PCM
    input  wire [7:0]          dec_code_in,
    input  wire                dec_valid,
    output reg  signed [15:0]  dec_pcm_out,
    output reg                 dec_pcm_valid
);

    //------------------------------------------------------------------
    // 组合函数：A-law 编码（linear2alaw，与参考实现逐位一致）
    //------------------------------------------------------------------
    function [7:0] alaw_encode;
        input signed [15:0] pcm;
        reg signed [16:0] v;      // 17bit 防 -32768 取负溢出
        reg [7:0] mask;
        reg [3:0] seg;
        reg [7:0] aval;
        begin
            v = pcm >>> 3;                        // 16bit -> 13bit
            if (v >= 0) begin
                mask = 8'hD5;
            end else begin
                mask = 8'h55;
                v = -v - 1;
            end
            if (v > 17'h0FFF) v = 17'h0FFF;       // 13bit 限幅

            // 段搜索（seg_aend = 1F/3F/7F/FF/1FF/3FF/7FF/FFF）
            if      (v <= 17'h001F) seg = 4'd0;
            else if (v <= 17'h003F) seg = 4'd1;
            else if (v <= 17'h007F) seg = 4'd2;
            else if (v <= 17'h00FF) seg = 4'd3;
            else if (v <= 17'h01FF) seg = 4'd4;
            else if (v <= 17'h03FF) seg = 4'd5;
            else if (v <= 17'h07FF) seg = 4'd6;
            else if (v <= 17'h0FFF) seg = 4'd7;
            else                    seg = 4'd8;

            if (seg >= 8) begin
                alaw_encode = 8'h7F ^ mask;
            end else begin
                aval = {seg[2:0], 4'h0};
                if (seg < 2) aval = aval | (8'h0F & (v >> 1));
                else         aval = aval | (8'h0F & (v >> seg));
                alaw_encode = aval ^ mask;
            end
        end
    endfunction

    //------------------------------------------------------------------
    // 组合函数：A-law 解码（alaw2linear）
    //------------------------------------------------------------------
    function signed [15:0] alaw_decode;
        input [7:0] code;
        reg [7:0] a;
        reg [15:0] t;
        reg [2:0] seg;
        begin
            a   = code ^ 8'h55;
            t   = {8'h00, a[3:0], 4'h0};          // (a & 0x0F) << 4
            seg = a[6:4];
            case (seg)
                3'd0: t = t + 16'h0008;
                3'd1: t = t + 16'h0108;
                default: t = (t + 16'h0108) << (seg - 3'd1);
            endcase
            alaw_decode = a[7] ? $signed(t) : -$signed(t);
        end
    endfunction

    //------------------------------------------------------------------
    // 组合函数：mu-law 编码（linear2ulaw）
    //------------------------------------------------------------------
    function [7:0] mulaw_encode;
        input signed [15:0] pcm;
        reg signed [16:0] v;
        reg [7:0] mask;
        reg [3:0] seg;
        reg [7:0] uval;
        begin
            v = pcm >>> 2;                        // 16bit -> 14bit
            if (v < 0) begin
                v = -v;
                mask = 8'h7F;
            end else begin
                mask = 8'hFF;
            end
            if (v > 17'd8158) v = 17'd8158;
            v = v + 17'd33;                       // BIAS(0x84) >> 2

            // 段搜索（seg_uend = 3F/7F/FF/1FF/3FF/7FF/FFF/1FFF）
            if      (v <= 17'h003F) seg = 4'd0;
            else if (v <= 17'h007F) seg = 4'd1;
            else if (v <= 17'h00FF) seg = 4'd2;
            else if (v <= 17'h01FF) seg = 4'd3;
            else if (v <= 17'h03FF) seg = 4'd4;
            else if (v <= 17'h07FF) seg = 4'd5;
            else if (v <= 17'h0FFF) seg = 4'd6;
            else if (v <= 17'h1FFF) seg = 4'd7;
            else                    seg = 4'd8;

            if (seg >= 8) begin
                mulaw_encode = 8'h7F ^ mask;
            end else begin
                uval = {seg[2:0], 4'h0} | (8'h0F & (v >> (seg + 4'd1)));
                mulaw_encode = uval ^ mask;
            end
        end
    endfunction

    //------------------------------------------------------------------
    // 组合函数：mu-law 解码（ulaw2linear）
    //------------------------------------------------------------------
    function signed [15:0] mulaw_decode;
        input [7:0] code;
        reg [7:0] u;
        reg [15:0] t;
        begin
            u = code ^ 8'hFF;
            t = ({12'h000, u[3:0]} << 3) + 16'h0084;
            t = t << u[6:4];
            mulaw_decode = u[7] ? $signed(16'h0084 - t) : $signed(t - 16'h0084);
        end
    endfunction

    //------------------------------------------------------------------
    // 输出寄存器（1 拍延迟）
    //------------------------------------------------------------------
    wire [7:0]         enc_code_c = (LAW == 0) ? alaw_encode(enc_pcm_in)
                                               : mulaw_encode(enc_pcm_in);
    wire signed [15:0] dec_pcm_c  = (LAW == 0) ? alaw_decode(dec_code_in)
                                               : mulaw_decode(dec_code_in);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            enc_code_out   <= 8'h00;
            enc_code_valid <= 1'b0;
            dec_pcm_out    <= 16'sh0000;
            dec_pcm_valid  <= 1'b0;
        end else begin
            enc_code_out   <= enc_code_c;
            enc_code_valid <= enc_valid;
            dec_pcm_out    <= dec_pcm_c;
            dec_pcm_valid  <= dec_valid;
        end
    end

endmodule

`default_nettype wire
