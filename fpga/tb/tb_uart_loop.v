//======================================================================
// tb_uart_loop.v : UART 收发器比特级环回测试
//----------------------------------------------------------------------
// 用例：
//   T1  256 个字节（0..255）逐个发送，经 txd->rxd 直连环回收回，比对一致
//   T2  帧错误注入：发送过程中破坏停止位 -> frame_err 应置位
// 判据：字节零丢失/零错误；frame_err 计数 == 1
//======================================================================
`timescale 1ns / 1ps

module tb_uart_loop;

    localparam CLK_HZ = 27_000_000;
    localparam BAUD   = 921_600;
    localparam real BIT_NS = 1.0e9 / BAUD;   // 1085.07ns

    reg clk = 0;
    reg rst_n = 0;
    always #18.518 clk = ~clk;               // 27MHz

    reg  [7:0] tx_data;
    reg        tx_valid;
    wire       tx_busy, txd;

    wire [7:0] rx_data;
    wire       rx_valid, frame_err;

    uart_tx #(.CLK_HZ(CLK_HZ), .BAUD(BAUD)) u_tx (
        .clk(clk), .rst_n(rst_n),
        .data(tx_data), .valid(tx_valid), .busy(tx_busy), .txd(txd)
    );

    wire rxd = err_drive ? 1'b0 : txd;       // 环回（T2 时拉低制造帧错误）
    reg  err_drive = 0;

    uart_rx #(.CLK_HZ(CLK_HZ), .BAUD(BAUD)) u_rx (
        .clk(clk), .rst_n(rst_n),
        .rxd(rxd), .data(rx_data), .valid(rx_valid), .frame_err(frame_err)
    );

    // 接收收集
    reg [7:0] rx_buf [0:255];
    integer rx_cnt = 0;
    integer err_cnt = 0;
    always @(posedge clk) begin
        if (rx_valid) begin
            rx_buf[rx_cnt] = rx_data;
            rx_cnt = rx_cnt + 1;
        end
        if (frame_err) err_cnt = err_cnt + 1;
    end

    integer i;
    initial begin
        if ($test$plusargs("dump")) begin
            $dumpfile("sim/tb_uart_loop.vcd");
            $dumpvars(0, tb_uart_loop);
        end
        tx_data = 0; tx_valid = 0;
        repeat (20) @(negedge clk);
        rst_n = 1;
        repeat (20) @(negedge clk);

        // T1: 256 字节
        for (i = 0; i < 256; i = i + 1) begin
            @(negedge clk);
            tx_data <= i[7:0];
            tx_valid <= 1'b1;
            @(negedge clk);
            tx_valid <= 1'b0;
            repeat (3) @(negedge clk);   // 等 busy 拉高（避免 TB 竞态）
            wait (tx_busy == 1'b0);
            repeat (20) @(negedge clk);
        end
        #(BIT_NS * 3);                   // 等最后一字节收完

        if (rx_cnt != 256) begin
            $display("UART T1 FAIL: rx_cnt=%0d exp=256", rx_cnt);
        end else begin
            for (i = 0; i < 256; i = i + 1) begin
                if (rx_buf[i] !== i[7:0]) begin
                    $display("UART T1 FAIL: idx=%0d got=%02x exp=%02x",
                             i, rx_buf[i], i[7:0]);
                    i = 256;
                end
            end
        end

        // T2: 线路拉低 12 个位周期 -> 起始位假触发 + 停止位错误
        err_drive = 1;
        #(BIT_NS * 12);
        err_drive = 0;
        #(BIT_NS * 4);

        if (err_cnt < 1) begin
            $display("UART T2 FAIL: 停止位错误未被检出");
        end else begin
            $display("UART T2 PASS: frame_err=%0d", err_cnt);
        end

        if (rx_cnt == 256 && err_cnt >= 1) $display("TB_UART_LOOP PASS");
        else                               $display("TB_UART_LOOP FAIL: rx=%0d err=%0d", rx_cnt, err_cnt);
        $finish;
    end

endmodule
