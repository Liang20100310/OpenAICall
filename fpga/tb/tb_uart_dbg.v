//======================================================================
// tb_uart_dbg.v : UART 收发调试（打印比特时序）
//======================================================================
`timescale 1ns / 1ps

module tb_uart_dbg;

    localparam CLK_HZ = 27_000_000;
    localparam BAUD   = 921_600;

    reg clk = 0;
    reg rst_n = 0;
    always #18.518 clk = ~clk;

    reg  [7:0] tx_data; reg tx_valid; wire tx_busy, txd;
    wire [7:0] rx_data; wire rx_valid, frame_err;

    uart_tx #(.CLK_HZ(CLK_HZ), .BAUD(BAUD)) u_tx (
        .clk(clk), .rst_n(rst_n),
        .data(tx_data), .valid(tx_valid), .busy(tx_busy), .txd(txd));

    wire rxd = txd;
    uart_rx #(.CLK_HZ(CLK_HZ), .BAUD(BAUD)) u_rx (
        .clk(clk), .rst_n(rst_n),
        .rxd(rxd), .data(rx_data), .valid(rx_valid), .frame_err(frame_err));

    // 发 10 个字节，记录每字节收发情况
    integer sent_n = 0, got_n = 0, err_n = 0;
    initial begin
        $monitoroff;
        tx_data = 0; tx_valid = 0;
        repeat (20) @(negedge clk);
        rst_n = 1;
        repeat (20) @(negedge clk);

        for (sent_n = 0; sent_n < 10; sent_n = sent_n + 1) begin
            $display("[TX] send #%0d data=%02x at %0.0fns", sent_n, 8'h40 + sent_n, $realtime);
            send(8'h40 + sent_n[7:0]);
        end
        repeat (400) @(negedge clk);
        $display("summary: sent=10 rx_ok=%0d frame_err=%0d", got_n, err_n);
        $finish;
    end

    always @(posedge clk) begin
        if (rx_valid)  begin got_n = got_n + 1; $display("[RX] ok data=%02x at %0.0fns", rx_data, $realtime); end
        if (frame_err) begin err_n = err_n + 1; $display("[RX] frame_err at %0.0fns", $realtime); end
    end

    task send;
        input [7:0] d;
        begin
            @(negedge clk);
            tx_data <= d; tx_valid <= 1'b1;
            @(negedge clk);
            tx_valid <= 1'b0;
            repeat (3) @(negedge clk);
            wait (tx_busy == 1'b0);
            repeat (60) @(negedge clk);
        end
    endtask

endmodule
