//======================================================================
// tb_dtmf_dbg.v : DTMF 检测器调试用（打印内部功率/判据）
//======================================================================
`timescale 1ns / 1ps

module tb_dtmf_dbg;

    reg clk = 0;
    reg rst_n = 0;
    always #5 clk = ~clk;

    reg signed [15:0] pcm_in;
    reg               pcm_valid;
    wire [3:0]        digit;
    wire              digit_valid;
    wire [15:0]       digit_dur;
    wire [51:0]       row_max, col_max;
    wire [7:0]        win_cnt;

    dtmf_detector #(.N_SAMPLES(256), .STABLE_N(2)) dut (
        .clk(clk), .rst_n(rst_n),
        .pcm_in(pcm_in), .pcm_valid(pcm_valid),
        .cfg_enable(1'b1), .cfg_threshold(8'h40),
        .digit(digit), .digit_valid(digit_valid), .digit_dur_ms(digit_dur),
        .dbg_row_max(row_max), .dbg_col_max(col_max), .dbg_win_cnt(win_cnt)
    );

    reg [15:0] sine [0:255];
    initial $readmemh("gen/sine_lut.hex", sine);

    reg [23:0] ph1, ph2;
    reg [31:0] inc1, inc2;

    function signed [15:0] tone_sample;
        input [23:0] p1;
        input [23:0] p2;
        reg signed [16:0] s;
        begin
            s = $signed(sine[p1[23:16]]) + $signed(sine[p2[23:16]]);
            tone_sample = s >>> 3;
        end
    endfunction

    integer k;
    initial begin
        pcm_in = 0; pcm_valid = 0;
        inc1 = (770.0 / 8000.0) * 16777216.0;
        inc2 = (1336.0 / 8000.0) * 16777216.0;
        ph1 = 0; ph2 = 0;
        repeat (10) @(negedge clk);
        rst_n = 1;
        repeat (10) @(negedge clk);

        // 发 800 个样点（'5' = 770 + 1336Hz）
        for (k = 0; k < 800; k = k + 1) begin
            @(negedge clk);
            pcm_in <= tone_sample(ph1, ph2);
            pcm_valid <= 1'b1;
            ph1 <= ph1 + inc1;
            ph2 <= ph2 + inc2;
            @(negedge clk);
            pcm_valid <= 1'b0;
            repeat (98) @(negedge clk);
        end

        #1000;
        $display("win_cnt=%0d", win_cnt);
        $display("pwr[0]=%0d pwr[1]=%0d pwr[2]=%0d pwr[3]=%0d",
                 dut.pwr[0], dut.pwr[1], dut.pwr[2], dut.pwr[3]);
        $display("pwr[4]=%0d pwr[5]=%0d pwr[6]=%0d pwr[7]=%0d",
                 dut.pwr[4], dut.pwr[5], dut.pwr[6], dut.pwr[7]);
        $display("row_max=%0d col_max=%0d cls_valid=%b cls_digit=%h",
                 row_max, col_max, dut.cls_valid, dut.cls_digit);
        $display("threshold=%0d", {24'd0, 8'h40, 20'd0});
        $display("same_cnt=%0d win_digit=%h rearm=%b last=%h",
                 dut.same_cnt, dut.win_digit, dut.rearm, dut.last_emitted);
        $display("v1[0]=%0d v2[0]=%0d", dut.v1[0], dut.v2[0]);
        $finish;
    end

endmodule
