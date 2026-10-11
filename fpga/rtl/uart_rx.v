//======================================================================
// uart_rx.v : UART 接收器（8N1，16x 过采样，分数分频波特率）
//----------------------------------------------------------------======
// 起始位中点判误码，逐位中点采样，抗 ~2% 波特率偏差
//======================================================================
`timescale 1ns / 1ps
`default_nettype none

module uart_rx #(
    parameter CLK_HZ = 27_000_000,
    parameter BAUD   = 921_600
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       rxd,
    output reg  [7:0] data,
    output reg        valid,       // 单拍：收到 1 字节
    output reg        frame_err    // 停止位错误（计数用）
);

    // 16x 过采样节拍
    localparam ACC_INC = BAUD * 16;

    reg [31:0] acc;
    wire       tick16 = (acc + ACC_INC >= CLK_HZ);

    // 输入同步 + 起始沿检测
    reg rxd_meta, rxd_sync, rxd_prev;

    reg [3:0]  os_cnt;       // 16x 计数
    reg [3:0]  bit_idx;      // 0=起始, 1..8=数据, 9=停止
    reg [7:0]  shifter;
    reg        receiving;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            acc       <= 32'd0;
            rxd_meta  <= 1'b1;
            rxd_sync  <= 1'b1;
            rxd_prev  <= 1'b1;
            os_cnt    <= 4'd0;
            bit_idx   <= 4'd0;
            shifter   <= 8'h00;
            receiving <= 1'b0;
            data      <= 8'h00;
            valid     <= 1'b0;
            frame_err <= 1'b0;
        end else begin
            // 双触发器同步
            rxd_meta <= rxd;
            rxd_sync <= rxd_meta;
            rxd_prev <= rxd_sync;

            valid     <= 1'b0;
            frame_err <= 1'b0;

            if (tick16) begin
                if (acc + ACC_INC >= CLK_HZ) acc <= acc + ACC_INC - CLK_HZ;
                else                         acc <= acc + ACC_INC;
            end else begin
                acc <= acc + ACC_INC;
            end

            if (!receiving) begin
                // 起始沿检测：每拍检测（不能被过采样节拍门控，否则漏检）
                if (rxd_prev && !rxd_sync) begin
                    receiving <= 1'b1;
                    os_cnt    <= 4'd0;
                    bit_idx   <= 4'd0;
                end
            end else if (tick16) begin
                os_cnt <= os_cnt + 4'd1;
                if (os_cnt == 4'd7) begin
                    // 位中点采样（每 16 个过采样节拍一次，os_cnt 自由循环）
                    if (bit_idx == 4'd0) begin
                        if (rxd_sync != 1'b0) begin
                            receiving <= 1'b0;      // 假起始，放弃
                        end
                    end else if (bit_idx <= 4'd8) begin
                        shifter <= {rxd_sync, shifter[7:1]};   // LSB 先
                    end else begin
                        // 停止位
                        receiving <= 1'b0;
                        if (rxd_sync == 1'b1) begin
                            data  <= shifter;
                            valid <= 1'b1;
                        end else begin
                            frame_err <= 1'b1;
                        end
                    end
                    bit_idx <= bit_idx + 4'd1;
                end
            end
        end
    end

endmodule

`default_nettype wire
