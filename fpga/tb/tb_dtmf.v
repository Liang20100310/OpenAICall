//======================================================================
// tb_dtmf.v : DTMF 检测器测试（Goertzel）
//----------------------------------------------------------------------
// 用例：
//   T1  12 个标准双音逐个激励（1..9,*,0,#），要求按键序号完全正确
//   T2  纯静音 -> 不得报键
//   T3  单音（只有行频）-> 不得报键
//   T4  低幅伪随机噪声 -> 不得报键
//   T5  同一键快速重按（中间 330 样点静音）-> 应报两次
// 激励：正弦 LUT + 相位累加器，fs=8kHz，幅度 4000/音（约 -8dBFS/音）
// 判据：digit_valid 序列与期望 digit 序列逐个一致
//======================================================================
`timescale 1ns / 1ps

module tb_dtmf;

    reg clk = 0;
    reg rst_n = 0;
    always #5 clk = ~clk;      // 100MHz 虚拟时钟（样点间隔 100 拍）

    reg signed [15:0] pcm_in;
    reg               pcm_valid;
    wire [3:0]        digit;
    wire              digit_valid;
    wire [15:0]       digit_dur;

    dtmf_detector #(.N_SAMPLES(256), .STABLE_N(2)) dut (
        .clk(clk), .rst_n(rst_n),
        .pcm_in(pcm_in), .pcm_valid(pcm_valid),
        .cfg_enable(1'b1), .cfg_threshold(8'h40),
        .digit(digit), .digit_valid(digit_valid), .digit_dur_ms(digit_dur),
        .dbg_row_max(), .dbg_col_max(), .dbg_win_cnt()
    );

    // 正弦 LUT（Q1.15）
    reg [15:0] sine [0:255];
    initial $readmemh("gen/sine_lut.hex", sine);

    // 双音发生：phase_inc = f * 2^24 / 8000
    reg [23:0] ph1, ph2;
    reg [31:0] inc1, inc2;

    function signed [15:0] tone_sample;
        input [23:0] p1;
        input [23:0] p2;
        reg signed [16:0] s;
        begin
            s = $signed(sine[p1[23:16]]) + $signed(sine[p2[23:16]]);
            // 两个 Q1.15 正弦相加后取约 4000 幅度：s/8
            tone_sample = s >>> 3;
        end
    endfunction

    // 期望结果
    reg [3:0] expected [0:15];
    integer   exp_cnt;
    reg [3:0] got [0:15];
    integer   got_cnt;

    always @(posedge clk) begin
        if (digit_valid) begin
            got[got_cnt] = digit;
            got_cnt = got_cnt + 1;
            $display("  [DTMF] digit=%0d dur=%0dms", digit, digit_dur);
        end
    end

    // 发 N 个样点（每样点 100 拍）
    task send_samples;
        input integer n;
        input use_tone;       // 1=双音 0=静音/噪声
        input noise;          // 1=噪声
        integer k;
        begin
            for (k = 0; k < n; k = k + 1) begin
                @(negedge clk);
                if (use_tone) begin
                    pcm_in <= tone_sample(ph1, ph2);
                end else if (noise) begin
                    pcm_in <= $random % 200;   // 低幅噪声
                end else begin
                    pcm_in <= 16'sd0;
                end
                pcm_valid <= 1'b1;
                ph1 <= ph1 + inc1;
                ph2 <= ph2 + inc2;
                @(negedge clk);
                pcm_valid <= 1'b0;
                repeat (98) @(negedge clk);
            end
        end
    endtask

    task set_freq;
        input [31:0] f1;
        input [31:0] f2;
        real r1, r2;
        begin
            r1 = f1;
            r2 = f2;
            inc1 = (r1 / 8000.0) * 16777216.0;   // 避免 32bit 乘法溢出
            inc2 = (r2 / 8000.0) * 16777216.0;
            ph1 = 24'd0;
            ph2 = 24'd0;
        end
    endtask

    // 12 键频率表
    task press;
        input integer row;     // 0..3
        input integer col;     // 0..2
        input [3:0] code;
        begin
            case (row)
                0: set_freq(32'd697, (col == 0) ? 32'd1209 : (col == 1) ? 32'd1336 : 32'd1477);
                1: set_freq(32'd770, (col == 0) ? 32'd1209 : (col == 1) ? 32'd1336 : 32'd1477);
                2: set_freq(32'd852, (col == 0) ? 32'd1209 : (col == 1) ? 32'd1336 : 32'd1477);
                default: set_freq(32'd941, (col == 0) ? 32'd1209 : (col == 1) ? 32'd1336 : 32'd1477);
            endcase
            expected[exp_cnt] = code;
            exp_cnt = exp_cnt + 1;
            send_samples(800, 1'b1, 1'b0);   // 3 个窗口以上
            send_samples(330, 1'b0, 1'b0);   // 静音（重武装）
        end
    endtask

    integer i;
    integer bad;

    initial begin
        if ($test$plusargs("dump")) begin
            $dumpfile("sim/tb_dtmf.vcd");
            $dumpvars(0, tb_dtmf);
        end
        pcm_in = 0; pcm_valid = 0; ph1 = 0; ph2 = 0;
        got_cnt = 0; exp_cnt = 0;
        repeat (10) @(negedge clk);
        rst_n = 1;
        repeat (10) @(negedge clk);

        //---- T1: 12 键逐个 ----
        $display("T1: 12 个标准双音逐个激励");
        press(0, 0, 4'h1);   // 1
        press(0, 1, 4'h2);   // 2
        press(0, 2, 4'h3);   // 3
        press(1, 0, 4'h4);   // 4
        press(1, 1, 4'h5);   // 5
        press(1, 2, 4'h6);   // 6
        press(2, 0, 4'h7);   // 7
        press(2, 1, 4'h8);   // 8
        press(2, 2, 4'h9);   // 9
        press(3, 0, 4'hB);   // *
        press(3, 1, 4'hA);   // 0
        press(3, 2, 4'hC);   // #

        //---- T2: 纯静音 ----
        $display("T2: 纯静音（期望无按键）");
        send_samples(2000, 1'b0, 1'b0);

        //---- T3: 单音（仅行频 770Hz）----
        $display("T3: 单音 770Hz（期望无按键）");
        set_freq(32'd770, 32'd770);
        send_samples(1000, 1'b1, 1'b0);
        send_samples(330, 1'b0, 1'b0);

        //---- T4: 低幅噪声 ----
        $display("T4: 低幅噪声（期望无按键）");
        send_samples(2000, 1'b0, 1'b1);

        //---- T5: 同键重按 ----
        $display("T5: '5' 重按两次（期望 2 次按键）");
        press(1, 1, 4'h5);
        press(1, 1, 4'h5);

        //---- 判定 ----
        $display("---- 结果 ----");
        bad = 0;
        if (got_cnt != exp_cnt) begin
            bad = 1;
            $display("TB_DTMF FAIL: 按键数不符 got=%0d exp=%0d", got_cnt, exp_cnt);
        end else begin
            for (i = 0; i < exp_cnt; i = i + 1) begin
                if (got[i] !== expected[i]) begin
                    bad = 1;
                    $display("TB_DTMF FAIL: 第 %0d 键 got=%0d exp=%0d",
                             i, got[i], expected[i]);
                end
            end
        end
        if (!bad)
            $display("TB_DTMF PASS: 全部 %0d 次按键识别正确（含 12 键 + 重按）", got_cnt);
        $finish;
    end

endmodule
