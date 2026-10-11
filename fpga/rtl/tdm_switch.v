//======================================================================
// tdm_switch.v : TDM 时隙交换（2 路语音通道，可扩展 32 时隙）
//----------------------------------------------------------------------
// 总线定义（见 docs/INTERFACE_SPEC.md §4）：
//   * 每个音频帧（8kHz）内执行 32 个时隙（TS0..TS31），每时隙 8bit
//     （G.711 码字），帧脉冲 frame_sync 标记帧首
//   * 时隙分配：TS0 空闲/同步；TS1/TS2 = CH0/CH1 上行（Codec->UART）；
//     TS17/TS18 = CH0/CH1 下行（UART->Codec）；其余预留
//   * 交换矩阵：生产者把样点写入指定时隙（写口），消费者按映射寄存器
//     从任意时隙读出（读口）—— 即"时隙交换"
//   * 读数据在帧首锁存（双缓冲语义，1 帧延迟，音频无感）
//   * LOOPBACK[2] 置位时 CH0/CH1 映射互换，实现两部话机数字互打
// 资源：32x8bit 寄存器阵列，无厂商原语
//======================================================================
`timescale 1ns / 1ps
`default_nettype none

module tdm_switch #(
    parameter N_SLOTS     = 32,
    parameter SLOT_UL0    = 5'd1,    // CH0 上行写时隙
    parameter SLOT_UL1    = 5'd2,    // CH1 上行写时隙
    parameter SLOT_DL0    = 5'd17,   // CH0 下行写时隙
    parameter SLOT_DL1    = 5'd18    // CH1 下行写时隙
) (
    input  wire       clk,
    input  wire       rst_n,

    // 帧同步（8kHz，样点到齐一帧后由上游给出）
    input  wire       frame_sync,

    // 写口（生产者，wr_valid 单拍脉冲）
    input  wire [7:0] ul0_data,  input wire ul0_wr,   // 写 TS1
    input  wire [7:0] ul1_data,  input wire ul1_wr,   // 写 TS2
    input  wire [7:0] dl0_data,  input wire dl0_wr,   // 写 TS17
    input  wire [7:0] dl1_data,  input wire dl1_wr,   // 写 TS18

    // 映射配置（消费者读哪个时隙）
    input  wire [4:0] map_ul0,        // CH0 上行读时隙，默认 TS1
    input  wire [4:0] map_ul1,        // CH1 上行读时隙，默认 TS2
    input  wire [4:0] map_dl0,        // CH0 下行读时隙，默认 TS17
    input  wire [4:0] map_dl1,        // CH1 下行读时隙，默认 TS18

    // 读口（帧首锁存输出）
    output reg  [7:0] ul0_q, output reg ul0_q_valid,
    output reg  [7:0] ul1_q, output reg ul1_q_valid,
    output reg  [7:0] dl0_q, output reg dl0_q_valid,
    output reg  [7:0] dl1_q, output reg dl1_q_valid,

    // 调试：时隙计数 / 时隙选通
    output reg  [4:0] slot_cnt,
    output reg        slot_stb        // 每时隙一拍（供观测/扩展用）
);

    //------------------------------------------------------------------
    // 时隙存储（32 x 8bit）
    //------------------------------------------------------------------
    reg [7:0] slot_mem [0:31];
    integer i;

    //------------------------------------------------------------------
    // 写端口（互斥写，按优先级）
    //------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (i = 0; i < N_SLOTS; i = i + 1)
                slot_mem[i] <= 8'h00;
            slot_cnt <= 5'd0;
            slot_stb <= 1'b0;
        end else begin
            // 时隙计数器：帧同步后 32 拍走完一帧（可观测时序）
            slot_stb <= 1'b1;
            if (frame_sync) slot_cnt <= 5'd0;
            else            slot_cnt <= slot_cnt + 5'd1;

            if (ul0_wr) slot_mem[SLOT_UL0] <= ul0_data;
            if (ul1_wr) slot_mem[SLOT_UL1] <= ul1_data;
            if (dl0_wr) slot_mem[SLOT_DL0] <= dl0_data;
            if (dl1_wr) slot_mem[SLOT_DL1] <= dl1_data;
        end
    end

    //------------------------------------------------------------------
    // 读端口：帧首按映射锁存（读到的是上一帧内容，双缓冲）
    //------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ul0_q <= 8'h00; ul0_q_valid <= 1'b0;
            ul1_q <= 8'h00; ul1_q_valid <= 1'b0;
            dl0_q <= 8'h00; dl0_q_valid <= 1'b0;
            dl1_q <= 8'h00; dl1_q_valid <= 1'b0;
        end else begin
            ul0_q_valid <= 1'b0;
            ul1_q_valid <= 1'b0;
            dl0_q_valid <= 1'b0;
            dl1_q_valid <= 1'b0;
            if (frame_sync) begin
                ul0_q <= slot_mem[map_ul0];  ul0_q_valid <= 1'b1;
                ul1_q <= slot_mem[map_ul1];  ul1_q_valid <= 1'b1;
                dl0_q <= slot_mem[map_dl0];  dl0_q_valid <= 1'b1;
                dl1_q <= slot_mem[map_dl1];  dl1_q_valid <= 1'b1;
            end
        end
    end

endmodule

`default_nettype wire
