//======================================================================
// fifo_sync.v : 通用同步 FIFO（2 的幂深度，FWFT 语义：rd_data 常显队首）
//----------------------------------------------------------------======
// 用途：UART 音频收发缓冲、抖动吸收
//======================================================================
`timescale 1ns / 1ps
`default_nettype none

module fifo_sync #(
    parameter WIDTH = 8,
    parameter AW    = 9                 // 深度 = 2^AW
) (
    input  wire             clk,
    input  wire             rst_n,

    input  wire             wr_en,
    input  wire [WIDTH-1:0] wr_data,
    output wire             full,

    input  wire             rd_en,
    output wire [WIDTH-1:0] rd_data,
    output wire             empty,

    output wire [AW:0]      level       // 当前数据量
);

    localparam DEPTH = (1 << AW);

    reg [WIDTH-1:0] mem [0:DEPTH-1];
    reg [AW:0] wr_ptr, rd_ptr;

    assign level = wr_ptr - rd_ptr;
    assign empty = (wr_ptr == rd_ptr);
    assign full  = (level == DEPTH[AW:0]);
    assign rd_data = mem[rd_ptr[AW-1:0]];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_ptr <= {(AW+1){1'b0}};
            rd_ptr <= {(AW+1){1'b0}};
        end else begin
            if (wr_en && !full) begin
                mem[wr_ptr[AW-1:0]] <= wr_data;
                wr_ptr <= wr_ptr + 1'b1;
            end
            if (rd_en && !empty) begin
                rd_ptr <= rd_ptr + 1'b1;
            end
        end
    end

endmodule

`default_nettype wire
