//======================================================================
// tb_tdm_switch.v : TDM 时隙交换测试
//----------------------------------------------------------------------
// 用例：
//   T1  写 TS1/TS2/TS17/TS18 -> 默认映射读出一致
//   T2  交叉映射（map_ul0=2, map_ul1=1）-> CH0/CH1 数据互换
//   T3  连续多帧：帧首锁存为上一帧内容（双缓冲语义）
//======================================================================
`timescale 1ns / 1ps

module tb_tdm_switch;

    reg clk = 0;
    reg rst_n = 0;
    always #5 clk = ~clk;

    reg        frame_sync;
    reg [7:0]  ul0_data, ul1_data, dl0_data, dl1_data;
    reg        ul0_wr, ul1_wr, dl0_wr, dl1_wr;
    reg [4:0]  map_ul0, map_ul1, map_dl0, map_dl1;
    wire [7:0] ul0_q, ul1_q, dl0_q, dl1_q;
    wire       ul0_qv, ul1_qv, dl0_qv, dl1_qv;
    wire [4:0] slot_cnt;
    wire       slot_stb;

    tdm_switch dut (
        .clk(clk), .rst_n(rst_n), .frame_sync(frame_sync),
        .ul0_data(ul0_data), .ul0_wr(ul0_wr),
        .ul1_data(ul1_data), .ul1_wr(ul1_wr),
        .dl0_data(dl0_data), .dl0_wr(dl0_wr),
        .dl1_data(dl1_data), .dl1_wr(dl1_wr),
        .map_ul0(map_ul0), .map_ul1(map_ul1),
        .map_dl0(map_dl0), .map_dl1(map_dl1),
        .ul0_q(ul0_q), .ul0_q_valid(ul0_qv),
        .ul1_q(ul1_q), .ul1_q_valid(ul1_qv),
        .dl0_q(dl0_q), .dl0_q_valid(dl0_qv),
        .dl1_q(dl1_q), .dl1_q_valid(dl1_qv),
        .slot_cnt(slot_cnt), .slot_stb(slot_stb)
    );

    integer errors = 0;

    task do_frame;
        input [7:0] v0, v1, v2, v3;
        input cross;
        begin
            @(negedge clk);
            frame_sync <= 1'b1;
            @(negedge clk);
            frame_sync <= 1'b0;
            // 帧内写入（模拟各生产者）
            ul0_data <= v0; ul0_wr <= 1'b1;
            ul1_data <= v1; ul1_wr <= 1'b1;
            dl0_data <= v2; dl0_wr <= 1'b1;
            dl1_data <= v3; dl1_wr <= 1'b1;
            @(negedge clk);
            ul0_wr <= 1'b0; ul1_wr <= 1'b0;
            dl0_wr <= 1'b0; dl1_wr <= 1'b0;
            map_ul0 <= cross ? 5'd2 : 5'd1;
            map_ul1 <= cross ? 5'd1 : 5'd2;
            map_dl0 <= cross ? 5'd18 : 5'd17;
            map_dl1 <= cross ? 5'd17 : 5'd18;
            repeat (10) @(negedge clk);
        end
    endtask

    initial begin
        if ($test$plusargs("dump")) begin
            $dumpfile("sim/tb_tdm.vcd");
            $dumpvars(0, tb_tdm_switch);
        end
        frame_sync = 0; ul0_wr = 0; ul1_wr = 0; dl0_wr = 0; dl1_wr = 0;
        ul0_data = 0; ul1_data = 0; dl0_data = 0; dl1_data = 0;
        map_ul0 = 5'd1; map_ul1 = 5'd2; map_dl0 = 5'd17; map_dl1 = 5'd18;
        repeat (5) @(negedge clk);
        rst_n = 1;

        // T1: 第一帧写入
        do_frame(8'h11, 8'h22, 8'h33, 8'h44, 1'b0);
        // 第二帧帧首锁存 -> 应看到第一帧写入值
        @(negedge clk);
        frame_sync <= 1'b1;
        @(negedge clk);
        frame_sync <= 1'b0;
        @(negedge clk);
        if (ul0_q !== 8'h11 || ul1_q !== 8'h22 ||
            dl0_q !== 8'h33 || dl1_q !== 8'h44) begin
            errors = errors + 1;
            $display("TDM T1 FAIL: got %h %h %h %h", ul0_q, ul1_q, dl0_q, dl1_q);
        end else
            $display("TDM T1 PASS: 默认映射读出正确");

        // T2: 交叉映射
        do_frame(8'h55, 8'h66, 8'h77, 8'h88, 1'b1);
        @(negedge clk);
        frame_sync <= 1'b1;
        @(negedge clk);
        frame_sync <= 1'b0;
        @(negedge clk);
        if (ul0_q !== 8'h66 || ul1_q !== 8'h55 ||
            dl0_q !== 8'h88 || dl1_q !== 8'h77) begin
            errors = errors + 1;
            $display("TDM T2 FAIL: got %h %h %h %h", ul0_q, ul1_q, dl0_q, dl1_q);
        end else
            $display("TDM T2 PASS: 交叉映射正确");

        // T3: 多帧稳定性
        do_frame(8'h01, 8'h02, 8'h03, 8'h04, 1'b0);
        do_frame(8'hA1, 8'hA2, 8'hA3, 8'hA4, 1'b0);
        @(negedge clk);
        frame_sync <= 1'b1;
        @(negedge clk);
        frame_sync <= 1'b0;
        @(negedge clk);
        if (ul0_q !== 8'hA1 || ul1_q !== 8'hA2) begin
            errors = errors + 1;
            $display("TDM T3 FAIL: got %h %h", ul0_q, ul1_q);
        end else
            $display("TDM T3 PASS: 多帧双缓冲正确");

        if (errors == 0) $display("TB_TDM PASS");
        else             $display("TB_TDM FAIL: %0d errors", errors);
        $finish;
    end

endmodule
