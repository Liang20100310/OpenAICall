//======================================================================
// uart_tx.v : UART 发送器（8N1，分数分频波特率发生器）
//----------------------------------------------------------------------
// 平均波特率 = BAUD（无累积误差），适配 27MHz 等非整除时钟
//======================================================================
`timescale 1ns / 1ps
`default_nettype none

module uart_tx #(
    parameter CLK_HZ = 27_000_000,
    parameter BAUD   = 921_600
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] data,
    input  wire       valid,       // 单拍：发送 data
    output reg        busy,
    output reg        txd
);

    // 分数分频：每拍累加 BAUD，累加到 CLK_HZ 输出一个位节拍
    reg [31:0] acc;
    wire       bit_tick = (acc + BAUD >= CLK_HZ);

    reg [3:0]  bit_idx;      // 0=起始位, 1..8=数据, 9=停止位
    reg [9:0]  shifter;      // {停止位, 8bit 数据, 起始位}

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            acc     <= 32'd0;
            txd     <= 1'b1;
            busy    <= 1'b0;
            bit_idx <= 4'd0;
            shifter <= 10'h3FF;
        end else begin
            if (bit_tick) begin
                if (acc + BAUD >= CLK_HZ) acc <= acc + BAUD - CLK_HZ;
                else                      acc <= acc + BAUD;

                if (busy) begin
                    txd <= shifter[0];
                    shifter <= {1'b1, shifter[9:1]};
                    if (bit_idx == 4'd9) begin
                        busy    <= 1'b0;
                        bit_idx <= 4'd0;
                        txd     <= 1'b1;
                    end else begin
                        bit_idx <= bit_idx + 4'd1;
                    end
                end
            end else begin
                acc <= acc + BAUD;
            end

            if (valid && !busy) begin
                busy    <= 1'b1;
                shifter <= {1'b1, data, 1'b0};   // 停止, 数据(LSB先), 起始
                bit_idx <= 4'd0;
            end
        end
    end

endmodule

`default_nettype wire
