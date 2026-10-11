//======================================================================
// i2c_master.v : I2C 主机（7bit 地址，100/400kHz，字节级命令接口）
//----------------------------------------------------------------------
// 用途：配置 Codec（如 TLV320AIC3104）
// 命令接口（cmd_valid 单拍，busy 期间不接受新命令）：
//   CMD_START      : 产生 START 条件
//   CMD_WRITE      : 发送 tx_byte，rx_ack=0 表示从机应答
//   CMD_READ_ACK   : 读 1 字节并回 ACK
//   CMD_READ_NACK  : 读 1 字节并回 NACK（最后一字节）
//   CMD_STOP       : 产生 STOP 条件
// done 单拍表示命令完成，rx_byte 为读数据，ack_err 置位表示无应答
// 速率：scl ≈ clk / (4*CLK_DIV)（27MHz, CLK_DIV=17 -> ~397kHz）
//======================================================================
`timescale 1ns / 1ps
`default_nettype none

module i2c_master #(
    parameter CLK_DIV = 68         // 27MHz/(4*68) ≈ 99kHz
) (
    input  wire       clk,
    input  wire       rst_n,

    input  wire       cmd_valid,
    input  wire [2:0] cmd,
    input  wire [7:0] tx_byte,
    output reg  [7:0] rx_byte,
    output reg        busy,
    output reg        done,
    output reg        ack_err,

    output reg        scl_oen,     // 0 = 驱动低，1 = 释放（开漏）
    output reg        sda_oen,
    input  wire       sda_in       // 外部上拉后的实际 SDA 电平
);

    localparam CMD_START     = 3'd0,
               CMD_WRITE     = 3'd1,
               CMD_READ_ACK  = 3'd2,
               CMD_READ_NACK = 3'd3,
               CMD_STOP      = 3'd4;

    localparam ST_IDLE  = 4'd0,
               ST_START = 4'd1,
               ST_WRITE = 4'd2,
               ST_WACK  = 4'd3,
               ST_READ  = 4'd4,
               ST_RACK  = 4'd5,
               ST_STOP  = 4'd6,
               ST_DONE  = 4'd7;

    reg [3:0]  state;
    reg [15:0] div_cnt;
    reg [2:0]  phase;        // 0..3 每个 SCL 周期 4 相
    reg [2:0]  bit_cnt;
    reg [7:0]  shifter;
    reg        rd_nack;      // 读字节后回 NACK

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state    <= ST_IDLE;
            div_cnt  <= 16'd0;
            phase    <= 3'd0;
            bit_cnt  <= 3'd0;
            shifter  <= 8'h00;
            rx_byte  <= 8'h00;
            busy     <= 1'b0;
            done     <= 1'b0;
            ack_err  <= 1'b0;
            scl_oen  <= 1'b1;
            sda_oen  <= 1'b1;
            rd_nack  <= 1'b0;
        end else begin
            done <= 1'b0;

            if (state == ST_IDLE) begin
                scl_oen <= 1'b1;
                sda_oen <= 1'b1;
                if (cmd_valid) begin
                    busy  <= 1'b1;
                    phase <= 3'd0;
                    div_cnt <= 16'd0;
                    ack_err <= 1'b0;
                    case (cmd)
                        CMD_START:     state <= ST_START;
                        CMD_WRITE:     begin
                            shifter <= tx_byte;
                            bit_cnt <= 3'd0;
                            state   <= ST_WRITE;
                        end
                        CMD_READ_ACK:  begin
                            rd_nack <= 1'b0;
                            bit_cnt <= 3'd0;
                            state   <= ST_READ;
                        end
                        CMD_READ_NACK: begin
                            rd_nack <= 1'b1;
                            bit_cnt <= 3'd0;
                            state   <= ST_READ;
                        end
                        default:       state <= ST_STOP;
                    endcase
                end
            end else if (div_cnt == CLK_DIV[15:0] - 16'd1) begin
                div_cnt <= 16'd0;
                phase   <= (phase == 3'd3) ? 3'd0 : phase + 3'd1;

                case (state)
                    //--------------------------------------------------
                    ST_START: begin
                        // SDA 高->低 期间 SCL 高
                        case (phase)
                            3'd0: begin sda_oen <= 1'b1; scl_oen <= 1'b1; end
                            3'd1: begin sda_oen <= 1'b0; scl_oen <= 1'b1; end
                            3'd2: begin sda_oen <= 1'b0; scl_oen <= 1'b0; end
                            default: begin
                                state <= ST_DONE;
                            end
                        endcase
                    end
                    //--------------------------------------------------
                    ST_WRITE: begin
                        case (phase)
                            3'd0: begin scl_oen <= 1'b0; sda_oen <= shifter[7]; end
                            3'd1: begin scl_oen <= 1'b1; end
                            3'd2: begin scl_oen <= 1'b1; end
                            default: begin
                                scl_oen <= 1'b0;
                                shifter <= {shifter[6:0], 1'b0};
                                if (bit_cnt == 3'd7) state <= ST_WACK;
                                else bit_cnt <= bit_cnt + 3'd1;
                            end
                        endcase
                    end
                    ST_WACK: begin
                        case (phase)
                            3'd0: begin scl_oen <= 1'b0; sda_oen <= 1'b1; end  // 释放 SDA
                            3'd1: begin scl_oen <= 1'b1; end
                            3'd2: begin scl_oen <= 1'b1; if (sda_in) ack_err <= 1'b1; end
                            default: begin
                                scl_oen <= 1'b0;
                                state   <= ST_DONE;
                            end
                        endcase
                    end
                    //--------------------------------------------------
                    ST_READ: begin
                        case (phase)
                            3'd0: begin scl_oen <= 1'b0; sda_oen <= 1'b1; end
                            3'd1: begin scl_oen <= 1'b1; end
                            3'd2: begin
                                scl_oen <= 1'b1;
                                shifter <= {shifter[6:0], sda_in};
                            end
                            default: begin
                                scl_oen <= 1'b0;
                                if (bit_cnt == 3'd7) begin
                                    rx_byte <= {shifter[6:0], sda_in};
                                    state   <= ST_RACK;
                                end else bit_cnt <= bit_cnt + 3'd1;
                            end
                        endcase
                    end
                    ST_RACK: begin
                        case (phase)
                            3'd0: begin scl_oen <= 1'b0; sda_oen <= rd_nack; end
                            3'd1: begin scl_oen <= 1'b1; end
                            3'd2: begin scl_oen <= 1'b1; end
                            default: begin
                                scl_oen <= 1'b0;
                                sda_oen <= 1'b1;
                                state   <= ST_DONE;
                            end
                        endcase
                    end
                    //--------------------------------------------------
                    ST_STOP: begin
                        case (phase)
                            3'd0: begin scl_oen <= 1'b0; sda_oen <= 1'b0; end
                            3'd1: begin scl_oen <= 1'b1; sda_oen <= 1'b0; end
                            3'd2: begin scl_oen <= 1'b1; sda_oen <= 1'b1; end
                            default: state <= ST_DONE;
                        endcase
                    end
                    //--------------------------------------------------
                    ST_DONE: begin
                        done  <= 1'b1;
                        busy  <= 1'b0;
                        state <= ST_IDLE;
                    end
                    default: state <= ST_IDLE;
                endcase
            end else begin
                div_cnt <= div_cnt + 16'd1;
            end
        end
    end

endmodule

`default_nettype wire
