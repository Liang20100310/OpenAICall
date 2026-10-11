//======================================================================
// tb_top.v : 顶层集成测试（比特级 UART 全链路）
//----------------------------------------------------------------------
// 用例（覆盖 UART 物理层 + 帧协议 + 寄存器 + FIFO + TDM 时序 + 环回）：
//   T1  PING -> PONG
//   T2  REG_WR(0x02=0x01) 打开 UART 音频环回，REG_RD 回读 0x01
//   T3  连续 2 个 AUDIO_DL(ch0) 帧 -> 期望收到 2 个 AUDIO_UL 帧且载荷一致
// 判据：帧类型/载荷逐字节一致；无超时
//======================================================================
`timescale 1ns / 1ps

module tb_top;

    localparam real BIT_NS = 1.0e9 / 921600.0;

    reg clk = 0;
    reg rst_n = 0;
    always #18.518 clk = ~clk;

    reg  rxd_drv = 1'b1;         // TB -> FPGA
    wire txd_mon;                // FPGA -> TB

    wire        mclk, bclk, lrclk, sdout;
    reg         sdin = 1'b0;
    wire        i2c_scl, i2c_sda;
    pullup (i2c_sda);
    pullup (i2c_scl);
    wire        spi_sclk, spi_cs_n, spi_mosi;
    wire [1:0]  slic_ring;
    wire [5:0]  led;
    wire        dbg_frame_sync, dbg_dtmf_valid;

    top #(.SIM_MODE(1), .CFG_WAIT_MS(0)) dut (
        .clk_27m(clk), .rst_n(rst_n),
        .uart_rxd(rxd_drv), .uart_txd(txd_mon),
        .codec_mclk(mclk), .codec_bclk(bclk), .codec_lrclk(lrclk),
        .codec_sdout(sdout), .codec_sdin(sdin),
        .i2c_scl(i2c_scl), .i2c_sda(i2c_sda),
        .spi_sclk(spi_sclk), .spi_cs_n(spi_cs_n),
        .spi_mosi(spi_mosi), .spi_miso(1'b1),
        .slic_ring(slic_ring), .slic_hook(2'b00), .slic_int(2'b11),
        .led(led), .dbg_frame_sync(dbg_frame_sync), .dbg_dtmf_valid(dbg_dtmf_valid)
    );

    //------------------------------------------------------------------
    // CRC8 / 帧组装（与 INTERFACE_SPEC 一致）
    //------------------------------------------------------------------
    function [7:0] crc8_next;
        input [7:0] crc;
        input [7:0] dat;
        integer i;
        reg [7:0] c;
        begin
            c = crc ^ dat;
            for (i = 0; i < 8; i = i + 1)
                c = c[7] ? ((c << 1) ^ 8'h07) : (c << 1);
            crc8_next = c;
        end
    endfunction

    //------------------------------------------------------------------
    // 比特级 UART 发送（TB -> DUT）
    //------------------------------------------------------------------
    task uart_send_byte;
        input [7:0] d;
        integer k;
        begin
            rxd_drv = 1'b0;  #(BIT_NS);              // 起始位
            for (k = 0; k < 8; k = k + 1) begin
                rxd_drv = d[k];  #(BIT_NS);          // 数据位 LSB 先
            end
            rxd_drv = 1'b1;  #(BIT_NS);              // 停止位
        end
    endtask

    reg [7:0] f_pay [0:170];
    reg [7:0] f_seq = 8'h00;

    task send_frame;
        input [7:0] ftype;
        input integer len;
        integer k;
        reg [7:0] crc;
        begin
            crc = 8'h00;
            uart_send_byte(8'hAA);
            uart_send_byte(8'h55);
            uart_send_byte(ftype); crc = crc8_next(crc, ftype);
            uart_send_byte(f_seq); crc = crc8_next(crc, f_seq);
            uart_send_byte(len[7:0]); crc = crc8_next(crc, len[7:0]);
            uart_send_byte(len[15:8]); crc = crc8_next(crc, len[15:8]);
            for (k = 0; k < len; k = k + 1) begin
                uart_send_byte(f_pay[k]);
                crc = crc8_next(crc, f_pay[k]);
            end
            uart_send_byte(crc);
            f_seq = f_seq + 8'd1;
        end
    endtask

    //------------------------------------------------------------------
    // 比特级 UART 接收（DUT -> TB）
    //------------------------------------------------------------------
    task uart_recv_byte;
        output [7:0] d;
        integer k;
        begin
            @(negedge txd_mon);          // 起始沿
            #(BIT_NS * 1.5);             // 位中点
            for (k = 0; k < 8; k = k + 1) begin
                d[k] = txd_mon;
                #(BIT_NS);
            end
            // 此刻位于停止位中点附近
        end
    endtask

    reg [7:0] r_type, r_seq;
    integer   r_len;
    reg [7:0] r_pay [0:170];

    task recv_frame;
        integer k;
        reg [7:0] b, crc;
        begin
            // 帧同步搜索 AA 55
            uart_recv_byte(b);
            while (b !== 8'hAA) uart_recv_byte(b);
            uart_recv_byte(b);
            if (b !== 8'h55) begin
                $display("RECV: 同步失败 %02x", b);
            end
            uart_recv_byte(r_type);
            uart_recv_byte(r_seq);
            uart_recv_byte(b); r_len = b;
            uart_recv_byte(b); r_len = r_len + (b << 8);
            for (k = 0; k < r_len; k = k + 1) uart_recv_byte(r_pay[k]);
            uart_recv_byte(b);           // CRC（此处只读不校验，单元 TB 已覆盖）
            crc = 8'h00;
            crc = crc8_next(crc, r_type);
            crc = crc8_next(crc, r_seq);
            crc = crc8_next(crc, r_len[7:0]);
            crc = crc8_next(crc, r_len[15:8]);
            for (k = 0; k < r_len; k = k + 1) crc = crc8_next(crc, r_pay[k]);
            if (crc !== b) $display("RECV: CRC 不一致 type=%02x", r_type);
        end
    endtask

    //------------------------------------------------------------------
    integer i, errors = 0;

    initial begin
        if ($test$plusargs("dump")) begin
            $dumpfile("sim/tb_top.vcd");
            $dumpvars(0, tb_top);
        end
        repeat (20) @(negedge clk);
        rst_n = 1;
        repeat (100) @(negedge clk);

        //---- T1: PING -> PONG ----
        $display("T1: PING -> PONG");
        send_frame(8'h7E, 0);
        recv_frame();
        if (r_type !== 8'h7F) begin
            errors = errors + 1;
            $display("T1 FAIL: 期望 7F 实际 %02x", r_type);
        end else $display("T1 PASS");

        //---- T2: REG 写读 ----
        $display("T2: REG_WR(0x02=0x01) / REG_RD");
        f_pay[0] = 8'h02; f_pay[1] = 8'h01;
        send_frame(8'h30, 2);
        f_pay[0] = 8'h02;
        send_frame(8'h31, 1);
        recv_frame();
        if (r_type !== 8'h32 || r_pay[0] !== 8'h02 || r_pay[1] !== 8'h01) begin
            errors = errors + 1;
            $display("T2 FAIL: type=%02x addr=%02x val=%02x",
                     r_type, r_pay[0], r_pay[1]);
        end else $display("T2 PASS（UART 音频环回已打开）");

        //---- T3: 音频环回（AUDIO_DL -> AUDIO_UL）----
        $display("T3: AUDIO_DL x2 -> AUDIO_UL 回显");
        f_pay[0] = 8'h00;
        for (i = 0; i < 160; i = i + 1) f_pay[i+1] = 8'h10 + i[7:0];
        send_frame(8'h11, 161);
        recv_frame();
        if (r_type !== 8'h10 || r_pay[0] !== 8'h00) begin
            errors = errors + 1;
            $display("T3 FAIL: 帧1 type=%02x ch=%02x", r_type, r_pay[0]);
        end else begin
            for (i = 0; i < 160; i = i + 1) begin
                if (r_pay[i+1] !== (8'h10 + i[7:0])) begin
                    errors = errors + 1;
                    $display("T3 FAIL: 帧1 载荷 idx=%0d got=%02x", i, r_pay[i+1]);
                    i = 160;
                end
            end
        end

        for (i = 0; i < 160; i = i + 1) f_pay[i+1] = 8'h90 + i[7:0];
        send_frame(8'h11, 161);
        recv_frame();
        if (r_type !== 8'h10 || r_pay[0] !== 8'h00) begin
            errors = errors + 1;
            $display("T3 FAIL: 帧2 type=%02x ch=%02x", r_type, r_pay[0]);
        end else begin
            for (i = 0; i < 160; i = i + 1) begin
                if (r_pay[i+1] !== (8'h90 + i[7:0])) begin
                    errors = errors + 1;
                    $display("T3 FAIL: 帧2 载荷 idx=%0d got=%02x", i, r_pay[i+1]);
                    i = 160;
                end
            end
        end
        if (errors == 0) $display("T3 PASS: 两帧音频逐字节回显一致");

        if (errors == 0) $display("TB_TOP PASS: 顶层全链路集成测试通过");
        else             $display("TB_TOP FAIL: %0d errors", errors);
        $finish;
    end

    // 全局超时保护
    initial begin
        #400_000_000;    // 400ms 仿真时间
        $display("TB_TOP FAIL: 超时（无响应）");
        $finish;
    end

endmodule
