//======================================================================
// spi_master.v : SPI 主机（Mode 0: CPOL=0 CPHA=0，MSB 先发）
//----------------------------------------------------------------------
// 用途：配置 SLIC（如 Si32176，16bit 帧 = 8bit 地址 + 8bit 数据）
// 速率：sclk = clk / (2*CLK_DIV)；要求 ≤ 20MHz，建议 1MHz
// 接口：start 脉冲启动一帧，tx_data 位宽可参数化，done 脉冲返回 rx_data
//======================================================================
`timescale 1ns / 1ps
`default_nettype none

module spi_master #(
    parameter CLK_DIV = 14,        // 27MHz / (2*14) ≈ 964kHz
    parameter WIDTH   = 16         // 帧位宽
) (
    input  wire             clk,
    input  wire             rst_n,

    input  wire             start,       // 单拍：启动一帧
    input  wire [WIDTH-1:0] tx_data,
    output reg  [WIDTH-1:0] rx_data,
    output reg              busy,
    output reg              done,        // 单拍：帧结束

    output reg              sclk,
    output reg              cs_n,
    output reg              mosi,
    input  wire             miso
);

    reg [15:0] div_cnt;
    reg [7:0]  bit_cnt;
    reg [WIDTH-1:0] shifter;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            div_cnt <= 16'd0;
            bit_cnt <= 8'd0;
            shifter <= {WIDTH{1'b0}};
            rx_data <= {WIDTH{1'b0}};
            busy    <= 1'b0;
            done    <= 1'b0;
            sclk    <= 1'b0;
            cs_n    <= 1'b1;
            mosi    <= 1'b0;
        end else begin
            done <= 1'b0;

            if (!busy) begin
                sclk <= 1'b0;
                cs_n <= 1'b1;
                if (start) begin
                    busy    <= 1'b1;
                    cs_n    <= 1'b0;
                    shifter <= tx_data;
                    mosi    <= tx_data[WIDTH-1];
                    bit_cnt <= 8'd0;
                    div_cnt <= 16'd0;
                end
            end else begin
                if (div_cnt == CLK_DIV[15:0] - 16'd1) begin
                    div_cnt <= 16'd0;
                    sclk    <= ~sclk;
                    if (sclk == 1'b0) begin
                        // 上升沿：采样 MISO
                        shifter <= {shifter[WIDTH-2:0], miso};
                        rx_data <= {shifter[WIDTH-2:0], miso};
                    end else begin
                        // 下降沿：更新 MOSI / 计数
                        if (bit_cnt == WIDTH[7:0] - 8'd1) begin
                            busy <= 1'b0;
                            done <= 1'b1;
                            cs_n <= 1'b1;
                            sclk <= 1'b0;
                        end else begin
                            bit_cnt <= bit_cnt + 8'd1;
                            mosi    <= shifter[WIDTH-2];
                        end
                    end
                end else begin
                    div_cnt <= div_cnt + 16'd1;
                end
            end
        end
    end

endmodule

`default_nettype wire
