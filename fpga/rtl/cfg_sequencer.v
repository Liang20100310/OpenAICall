//======================================================================
// cfg_sequencer.v : 上电配置时序器（Codec I2C + SLIC SPI 寄存器初始化）
//----------------------------------------------------------------------
// 行为：
//   1. 复位释放后等待 WAIT_MS 毫秒（默认 20ms，等待外部电源/晶振稳定）
//   2. 按 ROM 顺序执行配置写：bus=0 -> I2C(Codec)，bus=1 -> SPI(SLIC)
//   3. 全部完成后置 done，期间可通过 stat_busy 观测
// ROM 内容（寄存器地址/值）必须按所选芯片数据手册填写，见每条注释。
//   * 例化参数 ROM_INIT 通过 init_rom 任务/initial 块写入（Gowin 支持）
//   * 也可通过 UART REG 0x30/0x31 手动读写外设（top 层做透传）
// 说明：本模块给出框架与槽位，寄存器数值以 Si32176 / TLV320AIC3104
//   数据手册为准，避免给出未经核实的"魔法数"。
//======================================================================
`timescale 1ns / 1ps
`default_nettype none

module cfg_sequencer #(
    parameter CLK_HZ    = 27_000_000,
    parameter WAIT_MS   = 20,
    parameter N_ENTRIES = 8
) (
    input  wire       clk,
    input  wire       rst_n,

    // SPI 主机接口
    output reg        spi_start,
    output reg [15:0] spi_tx,
    input  wire       spi_done,
    input  wire       spi_busy,

    // I2C 主机命令接口
    output reg        i2c_cmd_valid,
    output reg  [2:0] i2c_cmd,
    output reg  [7:0] i2c_tx,
    input  wire       i2c_done,
    input  wire       i2c_busy,
    input  wire       i2c_ack_err,

    output reg        done,
    output reg        err,
    output reg  [7:0] entry_idx
);

    // 命令编码（与 i2c_master 一致）
    localparam I2C_START = 3'd0, I2C_WRITE = 3'd1, I2C_STOP = 3'd4;

    localparam BUS_I2C = 1'b0, BUS_SPI = 1'b1;

    //------------------------------------------------------------------
    // 初始化 ROM：{bus, i2c_dev_addr/spi_addr[7:0], reg_addr[7:0], data[7:0]}
    // ⚠ 下列数值为"格式示例"，必须按实际芯片数据手册替换后再上板。
    //------------------------------------------------------------------
    function [31:0] rom_word;
        input [7:0] idx;
        begin
            case (idx)
                // --- Codec: TLV320AIC3104 (I2C 0x18) 示例：软复位 + 时钟配置 ---
                8'd0: rom_word = {BUS_I2C, 8'h18, 8'h01, 8'h80}; // 复位寄存器
                8'd1: rom_word = {BUS_I2C, 8'h18, 8'h03, 8'h10}; // 时钟接口控制(示例)
                8'd2: rom_word = {BUS_I2C, 8'h18, 8'h0B, 8'h00}; // 音频通路控制(示例)
                // --- SLIC: Si32176 (SPI) 示例：通道使能 ---
                8'd3: rom_word = {BUS_SPI, 8'h10, 8'h00, 8'h01}; // 线路通用控制(示例)
                8'd4: rom_word = {BUS_SPI, 8'h10, 8'h01, 8'h00};
                8'd5: rom_word = {BUS_SPI, 8'h11, 8'h00, 8'h01}; // 第二通道
                8'd6: rom_word = {BUS_SPI, 8'h11, 8'h01, 8'h00};
                default: rom_word = 32'h0000_0000;
            endcase
        end
    endfunction

    //------------------------------------------------------------------
    // 状态机
    //------------------------------------------------------------------
    localparam S_WAIT = 3'd0, S_FETCH = 3'd1, S_SPI = 3'd2,
               S_I2C0 = 3'd3, S_I2C1 = 3'd4, S_I2C2 = 3'd5,
               S_NEXT = 3'd6, S_DONE = 3'd7;

    reg [2:0]  state;
    reg [31:0] wait_cnt;
    reg [31:0] cur;
    reg [3:0]  step;          // I2C 单条命令内的小步（START+DEV+W...）
    wire [31:0] rom_cur = rom_word(entry_idx);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state       <= S_WAIT;
            wait_cnt    <= 32'd0;
            cur         <= 32'd0;
            step        <= 4'd0;
            spi_start   <= 1'b0;
            spi_tx      <= 16'h0000;
            i2c_cmd_valid <= 1'b0;
            i2c_cmd     <= 3'd0;
            i2c_tx      <= 8'h00;
            done        <= 1'b0;
            err         <= 1'b0;
            entry_idx   <= 8'd0;
        end else begin
            spi_start     <= 1'b0;
            i2c_cmd_valid <= 1'b0;

            case (state)
                S_WAIT: begin
                    // 上电延时
                    if (wait_cnt >= (CLK_HZ / 1000) * WAIT_MS) begin
                        state <= S_FETCH;
                    end else begin
                        wait_cnt <= wait_cnt + 32'd1;
                    end
                end

                S_FETCH: begin
                    cur <= rom_cur;
                    if (rom_cur == 32'h0000_0000) begin
                        state <= S_DONE;
                    end else if (rom_cur[24]) begin
                        // bus = SPI
                        spi_tx    <= {rom_cur[23:16], rom_cur[7:0]};
                        spi_start <= 1'b1;
                        state     <= S_SPI;
                    end else begin
                        step  <= 4'd0;
                        state <= S_I2C0;
                    end
                end

                S_SPI: begin
                    if (spi_done) state <= S_NEXT;
                end

                //------------------------------------------------------
                // I2C 序列：START -> DEV_ADDR(W) -> REG_ADDR -> DATA -> STOP
                //------------------------------------------------------
                S_I2C0: begin
                    if (!i2c_busy) begin
                        i2c_cmd_valid <= 1'b1;
                        if (step == 4'd0) begin
                            i2c_cmd <= I2C_START;
                            step    <= 4'd1;
                        end else if (step == 4'd1) begin
                            i2c_cmd <= I2C_WRITE;
                            i2c_tx  <= {cur[23:17], 1'b0};   // 7bit 地址 + W
                            step    <= 4'd2;
                        end else if (step == 4'd2) begin
                            i2c_cmd <= I2C_WRITE;
                            i2c_tx  <= cur[15:8];            // 寄存器地址
                            step    <= 4'd3;
                        end else if (step == 4'd3) begin
                            i2c_cmd <= I2C_WRITE;
                            i2c_tx  <= cur[7:0];             // 数据
                            step    <= 4'd4;
                        end else begin
                            i2c_cmd <= I2C_STOP;
                            step    <= 4'd5;
                        end
                        state <= S_I2C1;
                    end
                end

                S_I2C1: begin
                    if (i2c_done) begin
                        if (i2c_ack_err) err <= 1'b1;
                        if (step == 4'd5) state <= S_NEXT;
                        else              state <= S_I2C0;
                    end
                end

                S_NEXT: begin
                    entry_idx <= entry_idx + 8'd1;
                    state     <= S_FETCH;
                end

                S_DONE: begin
                    done <= 1'b1;
                end

                default: state <= S_DONE;
            endcase
        end
    end

endmodule

`default_nettype wire
