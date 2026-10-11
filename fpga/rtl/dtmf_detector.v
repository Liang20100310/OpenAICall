//======================================================================
// dtmf_detector.v : DTMF 双音多频检测器（Goertzel 算法，全 12 键）
//----------------------------------------------------------------------
// 功能：
//   * 输入 8kHz 16bit 线性 PCM，滑动窗口 N=256 样点（32ms）做 Goertzel 频谱
//   * 检测 8 个标准频率：
//       行(低频): 697 / 770 / 852 / 941 Hz
//       列(高频): 1209 / 1336 / 1477 / 1633 Hz
//   * 识别 0-9 * # 共 12 键（1633Hz 列为 A-D 键，若其能量最强则判无效）
//   * 输出：digit(4bit 编码) + digit_valid(单脉冲) + digit_dur_ms
//     编码: 0x1..0x9='1'..'9', 0xA='0', 0xB='*', 0xC='#'
//   * 判据：绝对能量门限 + 主频占优(>=4x 次强) + twist(行/列功率比 ±6dB)
//   * 去重：同一按键只报一次；挂机/静音一窗(32ms)后再按同一键可重报；
//     连续不同键无需静音间隔。STABLE_N 个连续窗有效才触发（抗噪）。
// 资源：单共享乘法器 + FSM，8 路递推共用，适配高云 GW2AR-18。
// 时钟：任意 >= 2MHz 系统时钟（样点率 8kHz，处理一窗 < 100 拍）。
//======================================================================
`timescale 1ns / 1ps
`default_nettype none

module dtmf_detector #(
    parameter N_SAMPLES     = 256,      // 窗口样点数（2 的幂次）
    parameter STABLE_N      = 2,        // 连续有效窗数触发（2 => 最短可测 64ms）
    parameter COEF_SHIFT    = 14        // Goertzel 系数小数位（Q2.14）
) (
    input  wire               clk,
    input  wire               rst_n,

    // 音频输入（8kHz 样点流）
    input  wire signed [15:0] pcm_in,
    input  wire               pcm_valid,

    // 配置
    input  wire               cfg_enable,
    input  wire [7:0]         cfg_threshold,   // 有效能量门限 = cfg << 20

    // 检测结果
    output reg  [3:0]         digit,           // 0x1..0xC，见上
    output reg                digit_valid,     // 单周期脉冲
    output reg  [15:0]        digit_dur_ms,    // 本次按键有效时长（32ms/窗）

    // 调试观测
    output wire [51:0]        dbg_row_max,
    output wire [51:0]        dbg_col_max,
    output reg  [7:0]         dbg_win_cnt
);

    //------------------------------------------------------------------
    // Goertzel 系数：c = 2*cos(2*pi*f/fs), fs = 8000Hz, Q2.14
    //------------------------------------------------------------------
    function signed [15:0] coef_of;
        input [2:0] idx;
        begin
            case (idx)
                3'd0: coef_of = 16'sd27980;   // 697Hz
                3'd1: coef_of = 16'sd26956;   // 770Hz
                3'd2: coef_of = 16'sd25701;   // 852Hz
                3'd3: coef_of = 16'sd24219;   // 941Hz
                3'd4: coef_of = 16'sd19073;   // 1209Hz
                3'd5: coef_of = 16'sd16325;   // 1336Hz
                3'd6: coef_of = 16'sd13085;   // 1477Hz
                3'd7: coef_of = 16'sd9315;    // 1633Hz
            endcase
        end
    endfunction

    //------------------------------------------------------------------
    // 内部寄存器
    //------------------------------------------------------------------
    localparam S_IDLE = 3'd0, S_UPD = 3'd1, S_UPD2 = 3'd2,
               S_POW0 = 3'd3, S_POW1 = 3'd4, S_POW2 = 3'd5,
               S_POW3 = 3'd6, S_CLS = 3'd7;

    reg signed [25:0] v1 [0:7];      // Goertzel 递推寄存器
    reg signed [25:0] v2 [0:7];
    reg signed [51:0] pwr [0:7];     // 各频点功率（窗末）

    reg  [2:0]  state;
    reg  [2:0]  idx;
    reg  [8:0]  samp_cnt;             // 窗内样点计数 0..N_SAMPLES-1
    reg  signed [15:0] x_lat;         // 锁存的当前样点

    // 共享乘法器
    reg signed [25:0] ma, mb;
    wire signed [51:0] prod = ma * mb;

    // 功率计算中间量
    reg signed [51:0] t_sq1, t_sq2;

    // 分类结果（组合）
    reg [2:0]  row_idx, col_idx;      // 最强行/列索引
    reg [3:0]  cls_digit;             // 0 = 无效
    reg        cls_valid;

    integer k;

    //------------------------------------------------------------------
    // 分类：从 8 个功率中找出最强行/列并做有效性判断
    //------------------------------------------------------------------
    reg [51:0] row_max, row_2nd, col_max, col_2nd;
    reg [2:0]  row_arg, col_arg;
    reg [51:0] pv;

    always @(*) begin
        // ---- 行（pwr[0..3]）----
        row_max = 52'd0; row_2nd = 52'd0; row_arg = 3'd0;
        for (k = 0; k < 4; k = k + 1) begin
            pv = pwr[k];
            if (pv[51]) pv = 52'd0;              // 负值按 0 处理
            if (pv > row_max) begin
                row_2nd = row_max;
                row_max = pv;
                row_arg = k;
            end else if (pv > row_2nd) begin
                row_2nd = pv;
            end
        end
        // ---- 列（pwr[4..6] 为 1209/1336/1477，pwr[7]=1633 用于排除）----
        col_max = 52'd0; col_2nd = 52'd0; col_arg = 3'd0;
        for (k = 4; k < 7; k = k + 1) begin
            pv = pwr[k];
            if (pv[51]) pv = 52'd0;
            if (pv > col_max) begin
                col_2nd = col_max;
                col_max = pv;
                col_arg = k - 4;
            end else if (pv > col_2nd) begin
                col_2nd = pv;
            end
        end

        // ---- 有效性判据 ----
        // 1) 行/列最大功率均超过绝对门限
        // 2) 主频占优：最强 >= 4 * 次强（防止边带/谐波误判）
        // 3) 1633Hz(A-D 列) 不得占优：p7 <= col_max/4（防止 A-D 键误报 12 键）
        // 4) twist：行/列功率比在 1/4 .. 4 之间（±6dB）
        pv = pwr[7];
        if (pv[51]) pv = 52'd0;
        cls_valid = 1'b0;
        cls_digit = 4'd0;
        if ((row_max > {24'd0, cfg_threshold, 20'd0}) &&
            (col_max > {24'd0, cfg_threshold, 20'd0}) &&
            (pv <= (col_max >> 2)) &&
            (row_max >= (row_2nd << 2)) &&
            (col_max >= (col_2nd << 2)) &&
            (row_max <= (col_max << 2)) &&
            (col_max <= (row_max << 2))) begin
            cls_valid = 1'b1;
            case ({row_arg, col_arg})
                6'b000_000: cls_digit = 4'h1;
                6'b000_001: cls_digit = 4'h2;
                6'b000_010: cls_digit = 4'h3;
                6'b001_000: cls_digit = 4'h4;
                6'b001_001: cls_digit = 4'h5;
                6'b001_010: cls_digit = 4'h6;
                6'b010_000: cls_digit = 4'h7;
                6'b010_001: cls_digit = 4'h8;
                6'b010_010: cls_digit = 4'h9;
                6'b011_000: cls_digit = 4'hB;   // '*'
                6'b011_001: cls_digit = 4'hA;   // '0'
                6'b011_010: cls_digit = 4'hC;   // '#'
                default:    cls_digit = 4'h0;
            endcase
            if (cls_digit == 4'h0) cls_valid = 1'b0;
        end
    end

    assign dbg_row_max = row_max;
    assign dbg_col_max = col_max;

    //------------------------------------------------------------------
    // 去重 / 按键事件生成
    //------------------------------------------------------------------
    reg [3:0]  win_digit;      // 上一窗识别结果
    reg [7:0]  same_cnt;       // 连续同键窗计数
    reg        rearm;          // 允许再次上报同一键
    reg [3:0]  last_emitted;

    wire [7:0] same_cnt_next = same_cnt + 8'd1;

    //------------------------------------------------------------------
    // 主 FSM
    //------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state        <= S_IDLE;
            idx          <= 3'd0;
            samp_cnt     <= 9'd0;
            x_lat        <= 16'sd0;
            ma           <= 26'sd0;
            mb           <= 26'sd0;
            t_sq1        <= 52'sd0;
            t_sq2        <= 52'sd0;
            digit        <= 4'd0;
            digit_valid  <= 1'b0;
            digit_dur_ms <= 16'd0;
            win_digit    <= 4'd0;
            same_cnt     <= 8'd0;
            rearm        <= 1'b1;
            last_emitted <= 4'd0;
            dbg_win_cnt  <= 8'd0;
            for (k = 0; k < 8; k = k + 1) begin
                v1[k]  <= 26'sd0;
                v2[k]  <= 26'sd0;
                pwr[k] <= 52'sd0;
            end
        end else begin
            digit_valid <= 1'b0;      // 默认单脉冲

            case (state)
                //----------------------------------------------------------
                S_IDLE: begin
                    if (pcm_valid && cfg_enable) begin
                        x_lat    <= pcm_in;
                        idx      <= 3'd0;
                        ma       <= coef_of(3'd0);
                        mb       <= v1[0];
                        state    <= S_UPD;
                    end
                end

                //----------------------------------------------------------
                // 递推更新：v1' = c*v1/2^14 - v2 + x, v2' = v1（每频点 2 拍）
                S_UPD: begin
                    // prod = coef(idx) * v1(idx) 已就绪
                    v1[idx] <= (prod >>> COEF_SHIFT) - v2[idx] + x_lat;
                    v2[idx] <= v1[idx];
                    state   <= S_UPD2;
                end

                S_UPD2: begin
                    if (idx == 3'd7) begin
                        idx <= 3'd0;
                        if (samp_cnt == (N_SAMPLES - 1)) begin
                            samp_cnt <= 9'd0;
                            dbg_win_cnt <= dbg_win_cnt + 8'd1;
                            // 窗末：开始求功率
                            ma    <= v1[0];
                            mb    <= v1[0];
                            state <= S_POW0;
                        end else begin
                            samp_cnt <= samp_cnt + 9'd1;
                            state    <= S_IDLE;
                        end
                    end else begin
                        idx <= idx + 3'd1;
                        ma  <= coef_of(idx + 3'd1);
                        mb  <= v1[idx + 3'd1];
                        state <= S_UPD;
                    end
                end

                //----------------------------------------------------------
                // 功率：p = v1^2 + v2^2 - (c*v1/2^14)*v2，每频点 5 拍
                S_POW0: begin               // prod = v1^2
                    t_sq1 <= prod;
                    ma    <= v2[idx];
                    mb    <= v2[idx];
                    state <= S_POW1;
                end
                S_POW1: begin               // prod = v2^2
                    t_sq2 <= prod;
                    ma    <= coef_of(idx);
                    mb    <= v1[idx];
                    state <= S_POW2;
                end
                S_POW2: begin               // prod = c*v1
                    ma    <= prod >>> COEF_SHIFT;
                    mb    <= v2[idx];
                    state <= S_POW3;
                end
                S_POW3: begin               // prod = (c*v1/2^14)*v2
                    pwr[idx] <= t_sq1 + t_sq2 - prod;
                    if (idx == 3'd7) begin
                        idx   <= 3'd0;
                        state <= S_CLS;
                    end else begin
                        idx   <= idx + 3'd1;
                        ma    <= v1[idx + 3'd1];
                        mb    <= v1[idx + 3'd1];
                        state <= S_POW0;
                    end
                end

                //----------------------------------------------------------
                // 分类 + 去重 + 事件输出
                S_CLS: begin
                    if (!cfg_enable) begin
                        win_digit <= 4'd0;
                        same_cnt  <= 8'd0;
                    end else if (cls_valid) begin
                        if (cls_digit == win_digit)
                            same_cnt <= same_cnt_next;
                        else
                            same_cnt <= 8'd1;
                        win_digit <= cls_digit;

                        if ((same_cnt_next >= STABLE_N) &&
                            ((cls_digit != last_emitted) || rearm)) begin
                            digit        <= cls_digit;
                            digit_valid  <= 1'b1;
                            digit_dur_ms <= {3'd0, same_cnt_next, 5'd0};
                            // dur = same_cnt_next * 32ms（左移 5 位）
                            last_emitted <= cls_digit;
                            rearm        <= 1'b0;
                        end
                    end else begin
                        // 静音/无效窗：重武装，允许同键重报
                        win_digit <= 4'd0;
                        same_cnt  <= 8'd0;
                        rearm     <= 1'b1;
                    end

                    // 窗与窗之间清零递推寄存器，避免残留能量
                    for (k = 0; k < 8; k = k + 1) begin
                        v1[k]  <= 26'sd0;
                        v2[k]  <= 26'sd0;
                    end
                    state <= S_IDLE;
                end

                default: state <= S_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire
