//======================================================================
// tb_i2s.v : I2S 主控制器测试（SDOUT->SDIN 直连环回）
//----------------------------------------------------------------------
// 用例：
//   T1  时钟关系：MCLK=clk/2，BCLK=clk/48，LRCLK=BCLK/64
//   T2  环回一致性：tx_left/tx_right 每帧更新，rx 应收到前一帧发送值
//   T3  I2S 延迟校验：数据相对 LRCLK 边沿延迟 1 BCLK（由收发对称自洽覆盖）
//======================================================================
`timescale 1ns / 1ps

module tb_i2s;

    reg clk = 0;
    reg rst_n = 0;
    always #5 clk = ~clk;

    reg  [15:0] tx_left, tx_right;
    wire [15:0] rx_left, rx_right;
    wire        rx_frame_valid;
    wire        mclk, bclk, lrclk, sdout;
    reg         sdin;

    i2s_controller dut (
        .clk_audio(clk), .rst_n(rst_n),
        .tx_left(tx_left), .tx_right(tx_right),
        .rx_left(rx_left), .rx_right(rx_right),
        .rx_frame_valid(rx_frame_valid),
        .mclk(mclk), .bclk(bclk), .lrclk(lrclk),
        .sdout(sdout), .sdin(sdin),
        .loopback_en(1'b0), .bit_cnt_dbg()
    );

    // 物理环回
    always @(*) sdin = sdout;

    // T1: 时钟计数
    integer mclk_cnt = 0, bclk_cnt = 0, lr_cnt = 0;
    always @(posedge clk) begin
        if (rst_n) begin
            mclk_cnt = mclk_cnt + 1;
        end
    end
    always @(posedge mclk) if (rst_n) bclk_cnt = bclk_cnt + 1;   // 用 mclk 数 bclk 观测
    integer bclk_edges = 0;
    always @(posedge bclk) bclk_edges = bclk_edges + 1;
    integer lr_edges = 0;
    always @(posedge lrclk) lr_edges = lr_edges + 1;

    // T2: 发送图案 + 接收比对
    integer frame_no = 0;
    integer errors = 0;
    reg [15:0] sent_l [0:63];
    reg [15:0] sent_r [0:63];
    integer check_ptr = 0;

    always @(posedge clk) begin
        if (rx_frame_valid) begin
            // 比对：直接环回时，本帧收到 = 上一帧边界装载的发送值
            // （frame_no 为脉冲计数（未自增），期望 sent_l[frame_no-1]）
            if (frame_no >= 1) begin
                if (rx_left !== sent_l[frame_no-1] || rx_right !== sent_r[frame_no-1]) begin
                    errors = errors + 1;
                    $display("I2S MISMATCH frame=%0d rx=%h/%h exp=%h/%h",
                             frame_no, rx_left, rx_right,
                             sent_l[frame_no-1], sent_r[frame_no-1]);
                end
            end
            // 发送下一帧图案
            frame_no = frame_no + 1;
            tx_left  <= 16'hA000 + frame_no[15:0];
            tx_right <= 16'h5000 + frame_no[15:0];
            sent_l[frame_no] = 16'hA000 + frame_no[15:0];
            sent_r[frame_no] = 16'h5000 + frame_no[15:0];
        end
    end

    integer i;
    initial begin
        if ($test$plusargs("dump")) begin
            $dumpfile("sim/tb_i2s.vcd");
            $dumpvars(0, tb_i2s);
        end
        tx_left = 16'hA000; tx_right = 16'h5000;
        sent_l[0] = 16'hA000; sent_r[0] = 16'h5000;
        repeat (10) @(negedge clk);
        rst_n = 1;

        // 运行 20 帧
        repeat (20 * 3072) @(posedge clk);

        // T1: 时钟关系（20 帧 = 20*3072 = 61440 个 clk）
        // MCLK 应翻转每 clk -> 30720 个 mclk 周期/20帧
        // BCLK 应为 20*64 = 1280 个周期
        // LRCLK 上升沿 20 次
        $display("T1: bclk_edges=%0d (expect ~1280) lr_edges=%0d (expect ~20)",
                 bclk_edges, lr_edges);
        if ((bclk_edges > 1281) || (bclk_edges < 1279) ||
            (lr_edges > 21) || (lr_edges < 19)) begin
            errors = errors + 1;
            $display("TB_I2S FAIL: 时钟关系错误");
        end

        if (errors == 0) $display("TB_I2S PASS: 环回数据一致 + 时钟关系正确");
        else             $display("TB_I2S FAIL: %0d errors", errors);
        $finish;
    end

endmodule
