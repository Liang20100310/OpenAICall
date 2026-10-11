//======================================================================
// tb_uart_bridge.v : UART 帧协议桥测试（字节级）
//----------------------------------------------------------------------
// 用例：
//   T1  PING -> PONG 帧
//   T2  REG_WR/REG_RD -> REG_VAL 帧回读一致
//   T3  AUDIO_DL(ch0, 160B) -> 下行 FIFO 弹出 160B 逐字节一致
//   T4  上行：喂 160B -> AUDIO_UL 帧载荷一致
//   T5  事件：DTMF/HOOK -> 事件帧载荷一致
//   T6  容错：CRC 错帧被丢弃且 stat_crc_err 计数；失步后重同步
// 判据：帧解析结果与期望完全一致
//======================================================================
`timescale 1ns / 1ps

module tb_uart_bridge;

    reg clk = 0;
    reg rst_n = 0;
    always #18.518 clk = ~clk;

    // 字节级 UART 接口（tx_busy 恒 0：立即接收）
    reg  [7:0] rx_data;  reg rx_valid;
    wire [7:0] tx_data;  wire tx_valid;

    // 音频
    reg  [7:0] ul0_byte, ul1_byte;  reg ul0_valid, ul1_valid;
    wire [7:0] dl0_byte, dl1_byte;
    wire       dl0_empty, dl1_empty;
    reg        dl0_pop, dl1_pop;

    // 事件
    reg        ev_valid;
    reg [7:0]  ev_type, ev_ch, ev_code;
    reg [15:0] ev_dur;

    // 寄存器模型
    wire       reg_we, reg_re;
    wire [7:0] reg_waddr, reg_wdata, reg_raddr;
    reg  [7:0] reg_rdata;
    reg  [7:0] regs [0:255];
    always @(posedge clk) begin
        if (reg_we) regs[reg_waddr] <= reg_wdata;
        if (reg_re) reg_rdata <= regs[reg_raddr];
    end

    wire        ring_valid, ring_on;
    wire [7:0]  ring_ch;
    wire [15:0] stat_crc_err, stat_frame_err, stat_seq_gap;
    wire        ev_overflow;

    uart_bridge dut (
        .clk(clk), .rst_n(rst_n),
        .rx_data(rx_data), .rx_valid(rx_valid),
        .tx_data(tx_data), .tx_valid(tx_valid), .tx_busy(1'b0),
        .ul0_byte(ul0_byte), .ul0_valid(ul0_valid),
        .ul1_byte(ul1_byte), .ul1_valid(ul1_valid),
        .dl0_byte(dl0_byte), .dl0_empty(dl0_empty), .dl0_pop(dl0_pop),
        .dl1_byte(dl1_byte), .dl1_empty(dl1_empty), .dl1_pop(dl1_pop),
        .ev_valid(ev_valid), .ev_type(ev_type), .ev_ch(ev_ch),
        .ev_code(ev_code), .ev_dur_ms(ev_dur),
        .reg_we(reg_we), .reg_waddr(reg_waddr), .reg_wdata(reg_wdata),
        .reg_re(reg_re), .reg_raddr(reg_raddr), .reg_rdata(reg_rdata),
        .ring_valid(ring_valid), .ring_ch(ring_ch), .ring_on(ring_on),
        .stat_crc_err(stat_crc_err), .stat_frame_err(stat_frame_err),
        .stat_seq_gap(stat_seq_gap), .ev_overflow(ev_overflow)
    );

    //------------------------------------------------------------------
    // CRC8（与 RTL 同算法）
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
    // 发送帧任务（驱动 rx_*）
    //------------------------------------------------------------------
    reg [7:0] pay [0:170];
    reg [7:0] seq_no = 8'h00;

    task send_frame;
        input [7:0] ftype;
        input [15:0] len;
        input        corrupt_crc;
        integer k;
        reg [7:0] crc;
        begin
            crc = 8'h00;
            @(negedge clk);
            rx_data <= 8'hAA; rx_valid <= 1'b1; @(negedge clk);
            rx_data <= 8'h55; @(negedge clk);
            rx_data <= ftype; crc = crc8_next(crc, ftype); @(negedge clk);
            rx_data <= seq_no; crc = crc8_next(crc, seq_no); @(negedge clk);
            rx_data <= len[7:0]; crc = crc8_next(crc, len[7:0]); @(negedge clk);
            rx_data <= len[15:8]; crc = crc8_next(crc, len[15:8]); @(negedge clk);
            for (k = 0; k < len; k = k + 1) begin
                rx_data <= pay[k];
                crc = crc8_next(crc, pay[k]);
                @(negedge clk);
            end
            rx_data <= corrupt_crc ? ~crc : crc;
            @(negedge clk);
            rx_valid <= 1'b0;
            seq_no = seq_no + 8'd1;
            repeat (20) @(negedge clk);
        end
    endtask

    //------------------------------------------------------------------
    // TX 流收集 + 解析
    //------------------------------------------------------------------
    reg [7:0] tx_buf [0:2047];
    integer   tx_cnt = 0;
    always @(posedge clk) begin
        if (tx_valid) begin
            tx_buf[tx_cnt] = tx_data;
            tx_cnt = tx_cnt + 1;
        end
    end

    // 从 tx_buf[idx] 起解析一帧
    integer parse_idx = 0;
    reg [7:0] p_type, p_seq;
    integer   p_len;
    reg [7:0] p_pay [0:170];
    integer   errors = 0;

    task parse_frame;
        integer k;
        reg [7:0] crc;
        begin
            // 搜索 AA 55
            while (!(tx_buf[parse_idx] == 8'hAA && tx_buf[parse_idx+1] == 8'h55))
                parse_idx = parse_idx + 1;
            parse_idx = parse_idx + 2;
            p_type = tx_buf[parse_idx]; parse_idx = parse_idx + 1;
            p_seq  = tx_buf[parse_idx]; parse_idx = parse_idx + 1;
            p_len  = tx_buf[parse_idx]; parse_idx = parse_idx + 1;
            p_len  = p_len + (tx_buf[parse_idx] << 8); parse_idx = parse_idx + 1;
            for (k = 0; k < p_len; k = k + 1) begin
                p_pay[k] = tx_buf[parse_idx];
                parse_idx = parse_idx + 1;
            end
            // CRC 校验
            crc = 8'h00;
            crc = crc8_next(crc, p_type);
            crc = crc8_next(crc, p_seq);
            crc = crc8_next(crc, p_len[7:0]);
            crc = crc8_next(crc, p_len[15:8]);
            for (k = 0; k < p_len; k = k + 1)
                crc = crc8_next(crc, p_pay[k]);
            if (crc !== tx_buf[parse_idx]) begin
                errors = errors + 1;
                $display("PARSE FAIL: CRC 不一致 type=%02x", p_type);
            end
            parse_idx = parse_idx + 1;
        end
    endtask

    integer i, t0;

    initial begin
        if ($test$plusargs("dump")) begin
            $dumpfile("sim/tb_uart_bridge.vcd");
            $dumpvars(0, tb_uart_bridge);
        end
        rx_data = 0; rx_valid = 0;
        ul0_byte = 0; ul1_byte = 0; ul0_valid = 0; ul1_valid = 0;
        dl0_pop = 0; dl1_pop = 0;
        ev_valid = 0; ev_type = 0; ev_ch = 0; ev_code = 0; ev_dur = 0;
        for (i = 0; i < 256; i = i + 1) regs[i] = 0;
        repeat (10) @(negedge clk);
        rst_n = 1;
        repeat (10) @(negedge clk);

        //---- T1: PING -> PONG ----
        pay[0] = 0;
        send_frame(8'h7E, 16'd0, 1'b0);
        wait (tx_cnt >= 7);          // PONG 帧 = 7 字节
        parse_frame();
        if (p_type !== 8'h7F) begin
            errors = errors + 1;
            $display("T1 FAIL: 期望 PONG(7F) 实际 %02x", p_type);
        end else $display("T1 PASS: PING->PONG");

        //---- T2: REG_WR / REG_RD ----
        pay[0] = 8'h00; pay[1] = 8'hA5;
        send_frame(8'h30, 16'd2, 1'b0);      // REG_WR 0x00 = 0xA5
        pay[0] = 8'h00;
        send_frame(8'h31, 16'd1, 1'b0);      // REG_RD 0x00
        wait (tx_cnt >= 16);         // 累计 7 + 9 = 16 字节
        parse_frame();
        if (p_type !== 8'h32 || p_pay[0] !== 8'h00 || p_pay[1] !== 8'hA5) begin
            errors = errors + 1;
            $display("T2 FAIL: REG_VAL type=%02x addr=%02x val=%02x",
                     p_type, p_pay[0], p_pay[1]);
        end else $display("T2 PASS: REG_WR/REG_RD 回读一致");

        //---- T3: AUDIO_DL(ch0) -> 下行 FIFO ----
        pay[0] = 8'h00;
        for (i = 0; i < 160; i = i + 1) pay[i+1] = 8'h30 + i[7:0];
        send_frame(8'h11, 16'd161, 1'b0);
        // 弹出并比对
        for (i = 0; i < 160; i = i + 1) begin
            wait (dl0_empty == 1'b0);
            @(negedge clk);
            if (dl0_byte !== (8'h30 + i[7:0])) begin
                errors = errors + 1;
                $display("T3 FAIL: idx=%0d got=%02x", i, dl0_byte);
            end
            dl0_pop <= 1'b1;
            @(negedge clk);
            dl0_pop <= 1'b0;
        end
        $display("T3 PASS: AUDIO_DL 160B 一致");

        //---- T4: 上行 AUDIO_UL ----
        for (i = 0; i < 160; i = i + 1) begin
            @(negedge clk);
            ul0_byte <= 8'h80 + i[7:0];
            ul0_valid <= 1'b1;
            @(negedge clk);
            ul0_valid <= 1'b0;
        end
        wait (tx_cnt >= 184);        // 累计 16 + 168 = 184 字节
        parse_frame();
        if (p_type !== 8'h10 || p_pay[0] !== 8'h00 || p_len !== 161) begin
            errors = errors + 1;
            $display("T4 FAIL: type=%02x len=%0d ch=%02x", p_type, p_len, p_pay[0]);
        end else begin
            for (i = 0; i < 160; i = i + 1) begin
                if (p_pay[i+1] !== (8'h80 + i[7:0])) begin
                    errors = errors + 1;
                    $display("T4 FAIL: 载荷 idx=%0d got=%02x", i, p_pay[i+1]);
                end
            end
            $display("T4 PASS: AUDIO_UL 160B 一致");
        end

        //---- T5: 事件帧 ----
        @(negedge clk);
        ev_type <= 8'h20; ev_ch <= 8'h01; ev_code <= 8'h05; ev_dur <= 16'd120;
        ev_valid <= 1'b1;
        @(negedge clk);
        ev_valid <= 1'b0;
        wait (tx_cnt >= 195);        // 累计 184 + 11 = 195 字节
        parse_frame();
        if (p_type !== 8'h20 || p_pay[0] !== 8'h01 || p_pay[1] !== 8'h05 ||
            p_pay[2] !== 8'd120 || p_pay[3] !== 8'd0) begin
            errors = errors + 1;
            $display("T5 FAIL: 事件帧 type=%02x %02x %02x %02x %02x",
                     p_type, p_pay[0], p_pay[1], p_pay[2], p_pay[3]);
        end else $display("T5 PASS: DTMF 事件帧一致");

        //---- T6: CRC 错帧 + 重同步 ----
        t0 = stat_crc_err;
        pay[0] = 8'h12; pay[1] = 8'h34;
        send_frame(8'h30, 16'd2, 1'b1);      // 坏 CRC
        if (stat_crc_err !== t0 + 16'd1) begin
            errors = errors + 1;
            $display("T6 FAIL: CRC 错未计数 stat=%0d", stat_crc_err);
        end
        // 之后发一个好帧验证恢复
        pay[0] = 8'h00; pay[1] = 8'h5A;
        send_frame(8'h30, 16'd2, 1'b0);
        if (regs[0] !== 8'h5A) begin
            errors = errors + 1;
            $display("T6 FAIL: 坏帧后未恢复 regs[0]=%02x", regs[0]);
        end else $display("T6 PASS: CRC 错帧丢弃 + 重同步恢复");

        if (errors == 0) $display("TB_UART_BRIDGE PASS");
        else             $display("TB_UART_BRIDGE FAIL: %0d errors", errors);
        $finish;
    end

endmodule
