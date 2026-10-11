//======================================================================
// uart_bridge.v : UART 音频/事件/寄存器 帧协议桥（见 docs/INTERFACE_SPEC.md §2）
//----------------------------------------------------------------------
// 帧: AA 55 | TYPE | SEQ | LEN(2,小端) | PAYLOAD | CRC8(poly=0x07, init=0)
//     CRC 覆盖 TYPE..PAYLOAD 末字节
//
// 上行（FPGA->PC）：
//   0x10 AUDIO_UL : CH + G711A[160]（上行 FIFO 攒满 160B 组帧）
//   0x20 DTMF_EVT : CH + DIGIT + DUR_MS(2)
//   0x21 HOOK_EVT : CH + STATE
//   0x32 REG_VAL  : ADDR + VAL
//   0x7F PONG
// 下行（PC->FPGA）：
//   0x11 AUDIO_DL : CH + G711A[160] -> 暂存 -> CRC 通过后提交下行 FIFO
//   0x22 RING_CMD : CH + ON
//   0x30 REG_WR   : ADDR + VAL
//   0x31 REG_RD   : ADDR（自动回 0x32 REG_VAL）
//   0x7E PING（自动回 0x7F PONG）
//
// 设计要点：
//   * 解帧失步时搜索 AA 55 重同步；CRC 错/长度非法丢弃并计错
//   * 上行 FIFO 每通道 512B（约 3 帧余量），下行 FIFO 每通道 1KB
//   * 事件队列深度 4，满则丢弃并置溢出标志
//   * 全同步单时钟设计，无跨时钟域问题
//======================================================================
`timescale 1ns / 1ps
`default_nettype none

module uart_bridge (
    input  wire        clk,
    input  wire        rst_n,

    // ---- UART 字节接口 ----
    input  wire [7:0]  rx_data,
    input  wire        rx_valid,
    output wire [7:0]  tx_data,
    output wire        tx_valid,
    input  wire        tx_busy,

    // ---- 上行音频（G.711 码字流，8kHz）----
    input  wire [7:0]  ul0_byte,  input wire ul0_valid,
    input  wire [7:0]  ul1_byte,  input wire ul1_valid,

    // ---- 下行音频（读出侧）----
    output wire [7:0]  dl0_byte,  output wire dl0_empty,
    input  wire        dl0_pop,
    output wire [7:0]  dl1_byte,  output wire dl1_empty,
    input  wire        dl1_pop,

    // ---- 事件输入（DTMF / 摘挂机）----
    input  wire        ev_valid,
    input  wire [7:0]  ev_type,      // 0x20 DTMF / 0x21 HOOK
    input  wire [7:0]  ev_ch,
    input  wire [7:0]  ev_code,      // DTMF: digit; HOOK: state
    input  wire [15:0] ev_dur_ms,

    // ---- 寄存器接口 ----
    output reg         reg_we,
    output reg  [7:0]  reg_waddr,
    output reg  [7:0]  reg_wdata,
    output reg         reg_re,
    output reg  [7:0]  reg_raddr,
    input  wire [7:0]  reg_rdata,    // reg_re 后 1 拍有效

    // ---- 振铃控制 ----
    output reg         ring_valid,
    output reg  [7:0]  ring_ch,
    output reg         ring_on,

    // ---- 状态 ----
    output reg  [15:0] stat_crc_err,
    output reg  [15:0] stat_frame_err,
    output reg  [15:0] stat_seq_gap,
    output reg         ev_overflow
);

    //------------------------------------------------------------------
    // CRC8 (poly=0x07, init=0x00, MSB first)
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

    //==================================================================
    // 1) 上行音频 FIFO（每通道 512B）
    //==================================================================
    wire        fifo0_full, fifo1_full;
    wire [9:0]  fifo0_level, fifo1_level;
    wire [7:0]  fifo0_dout, fifo1_dout;
    reg         fifo0_pop, fifo1_pop;

    fifo_sync #(.WIDTH(8), .AW(9)) u_fifo_ul0 (
        .clk(clk), .rst_n(rst_n),
        .wr_en(ul0_valid), .wr_data(ul0_byte), .full(fifo0_full),
        .rd_en(fifo0_pop), .rd_data(fifo0_dout), .empty(),
        .level(fifo0_level)
    );
    fifo_sync #(.WIDTH(8), .AW(9)) u_fifo_ul1 (
        .clk(clk), .rst_n(rst_n),
        .wr_en(ul1_valid), .wr_data(ul1_byte), .full(fifo1_full),
        .rd_en(fifo1_pop), .rd_data(fifo1_dout), .empty(),
        .level(fifo1_level)
    );

    //==================================================================
    // 2) 下行音频 FIFO（1KB/通道）
    //==================================================================
    reg        dl0_wr, dl1_wr;
    reg [7:0]  dl0_wdata, dl1_wdata;

    fifo_sync #(.WIDTH(8), .AW(10)) u_fifo_dl0 (
        .clk(clk), .rst_n(rst_n),
        .wr_en(dl0_wr), .wr_data(dl0_wdata), .full(),
        .rd_en(dl0_pop), .rd_data(dl0_byte), .empty(dl0_empty),
        .level()
    );
    fifo_sync #(.WIDTH(8), .AW(10)) u_fifo_dl1 (
        .clk(clk), .rst_n(rst_n),
        .wr_en(dl1_wr), .wr_data(dl1_wdata), .full(),
        .rd_en(dl1_pop), .rd_data(dl1_byte), .empty(dl1_empty),
        .level()
    );

    //==================================================================
    // 3) 接收解帧
    //==================================================================
    localparam R_MAGIC0 = 4'd0, R_MAGIC1 = 4'd1, R_TYPE = 4'd2, R_SEQ = 4'd3,
               R_LENL   = 4'd4, R_LENH   = 4'd5, R_PAY  = 4'd6, R_CRC  = 4'd7;

    reg [3:0]  rstate;
    reg [7:0]  r_type, r_seq;
    reg [15:0] r_len, r_cnt;
    reg [7:0]  r_crc;
    reg [7:0]  r_small [0:7];          // 短载荷（REG/RING/PING）
    reg [7:0]  r_stage [0:160];        // AUDIO_DL 载荷暂存
    reg        r_is_audio;
    reg [7:0]  r_audio_ch;
    reg [7:0]  last_seq;
    reg        seq_first;

    // 分发脉冲
    reg        p_audio_cmt;            // 音频帧 CRC 通过
    reg        p_pong;                 // 请求回 PONG
    reg [7:0]  rv_addr;

    integer i;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rstate <= R_MAGIC0;
            r_type <= 8'h00; r_seq <= 8'h00;
            r_len <= 16'd0; r_cnt <= 16'd0;
            r_crc <= 8'h00;
            r_is_audio <= 1'b0;
            r_audio_ch <= 8'h00;
            last_seq <= 8'h00;
            seq_first <= 1'b1;
            stat_crc_err   <= 16'd0;
            stat_frame_err <= 16'd0;
            stat_seq_gap   <= 16'd0;
            reg_we <= 1'b0; reg_re <= 1'b0;
            reg_waddr <= 8'h00; reg_wdata <= 8'h00; reg_raddr <= 8'h00;
            ring_valid <= 1'b0; ring_ch <= 8'h00; ring_on <= 1'b0;
            p_audio_cmt <= 1'b0; p_pong <= 1'b0;
            rv_addr <= 8'h00;
        end else begin
            reg_we      <= 1'b0;
            reg_re      <= 1'b0;
            ring_valid  <= 1'b0;
            p_audio_cmt <= 1'b0;
            p_pong      <= 1'b0;

            if (rx_valid) begin
                case (rstate)
                    //--------------------------------------------------
                    R_MAGIC0: begin
                        if (rx_data == 8'hAA) rstate <= R_MAGIC1;
                    end
                    R_MAGIC1: begin
                        if (rx_data == 8'h55)      rstate <= R_TYPE;
                        else if (rx_data == 8'hAA) rstate <= R_MAGIC1;
                        else                       rstate <= R_MAGIC0;
                    end
                    //--------------------------------------------------
                    R_TYPE: begin
                        r_type <= rx_data;
                        r_crc  <= crc8_next(8'h00, rx_data);
                        rstate <= R_SEQ;
                    end
                    R_SEQ: begin
                        r_seq  <= rx_data;
                        r_crc  <= crc8_next(r_crc, rx_data);
                        rstate <= R_LENL;
                    end
                    R_LENL: begin
                        r_len  <= {8'h00, rx_data};
                        r_crc  <= crc8_next(r_crc, rx_data);
                        rstate <= R_LENH;
                    end
                    R_LENH: begin
                        r_crc <= crc8_next(r_crc, rx_data);
                        r_cnt <= 16'd0;
                        if ({rx_data, r_len[7:0]} > 16'd161) begin
                            stat_frame_err <= stat_frame_err + 16'd1;
                            rstate <= R_MAGIC0;
                        end else if ({rx_data, r_len[7:0]} == 16'd0) begin
                            // 无载荷帧（PING/PONG）直接到 CRC
                            r_is_audio <= 1'b0;
                            rstate <= R_CRC;
                        end else begin
                            r_is_audio <= (r_type == 8'h11) &&
                                          ({rx_data, r_len[7:0]} == 16'd161);
                            rstate <= R_PAY;
                        end
                    end
                    //--------------------------------------------------
                    R_PAY: begin
                        r_crc <= crc8_next(r_crc, rx_data);
                        if (r_is_audio) begin
                            if (r_cnt == 16'd0) r_audio_ch <= rx_data;
                            else                r_stage[r_cnt[7:0]] <= rx_data;
                        end else begin
                            r_small[r_cnt[2:0]] <= rx_data;
                        end
                        r_cnt <= r_cnt + 16'd1;
                        if (r_cnt + 16'd1 >= r_len) rstate <= R_CRC;
                    end
                    //--------------------------------------------------
                    R_CRC: begin
                        if (rx_data == r_crc) begin
                            // SEQ 连续性检查（丢帧统计）
                            if (seq_first) seq_first <= 1'b0;
                            else if (r_seq != (last_seq + 8'd1))
                                stat_seq_gap <= stat_seq_gap + 16'd1;
                            last_seq <= r_seq;

                            // 载荷分发
                            if (r_is_audio) begin
                                p_audio_cmt <= 1'b1;
                            end else begin
                                case (r_type)
                                    8'h22: begin              // RING_CMD
                                        ring_valid <= 1'b1;
                                        ring_ch    <= r_small[0];
                                        ring_on    <= r_small[1][0];
                                    end
                                    8'h30: begin              // REG_WR
                                        reg_we    <= 1'b1;
                                        reg_waddr <= r_small[0];
                                        reg_wdata <= r_small[1];
                                    end
                                    8'h31: begin              // REG_RD
                                        reg_re    <= 1'b1;
                                        reg_raddr <= r_small[0];
                                        rv_addr   <= r_small[0];
                                    end
                                    8'h7E: p_pong <= 1'b1;    // PING
                                    default: stat_frame_err <= stat_frame_err + 16'd1;
                                endcase
                            end
                        end else begin
                            stat_crc_err <= stat_crc_err + 16'd1;
                        end
                        rstate <= R_MAGIC0;
                    end

                    default: rstate <= R_MAGIC0;
                endcase
            end
        end
    end

    //==================================================================
    // 4) 音频提交：CRC 通过后把 r_stage[1..160] 写入下行 FIFO
    //==================================================================
    reg        cmt_busy;
    reg [8:0]  cmt_idx;
    reg [7:0]  cmt_ch;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cmt_busy <= 1'b0;
            cmt_idx  <= 9'd0;
            cmt_ch   <= 8'h00;
            dl0_wr <= 1'b0; dl1_wr <= 1'b0;
            dl0_wdata <= 8'h00; dl1_wdata <= 8'h00;
        end else begin
            dl0_wr <= 1'b0;
            dl1_wr <= 1'b0;
            if (!cmt_busy) begin
                if (p_audio_cmt) begin
                    cmt_busy <= 1'b1;
                    cmt_idx  <= 9'd1;
                    cmt_ch   <= r_audio_ch;
                end
            end else begin
                if (cmt_ch == 8'h00) begin
                    dl0_wr    <= 1'b1;
                    dl0_wdata <= r_stage[cmt_idx[7:0]];
                end else begin
                    dl1_wr    <= 1'b1;
                    dl1_wdata <= r_stage[cmt_idx[7:0]];
                end
                if (cmt_idx == 9'd160) begin
                    cmt_busy <= 1'b0;
                    cmt_idx  <= 9'd0;
                end else begin
                    cmt_idx <= cmt_idx + 9'd1;
                end
            end
        end
    end

    //==================================================================
    // 5) 事件队列（深度 4）
    //==================================================================
    reg [7:0]  evq_type [0:3];
    reg [7:0]  evq_d0   [0:3];
    reg [7:0]  evq_d1   [0:3];
    reg [7:0]  evq_d2   [0:3];
    reg [7:0]  evq_d3   [0:3];
    reg [1:0]  evq_wr, evq_rd;
    reg [2:0]  evq_level;
    reg        evq_pop;                // TX 块驱动，单拍

    wire evq_do_push = ev_valid && (evq_level < 3'd4);
    wire evq_do_pop  = evq_pop && (evq_level > 3'd0);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            evq_wr <= 2'd0; evq_rd <= 2'd0;
            evq_level <= 3'd0;
            ev_overflow <= 1'b0;
        end else begin
            if (evq_do_push) begin
                evq_type[evq_wr] <= ev_type;
                evq_d0[evq_wr]   <= ev_ch;
                evq_d1[evq_wr]   <= ev_code;
                evq_d2[evq_wr]   <= ev_dur_ms[7:0];
                evq_d3[evq_wr]   <= ev_dur_ms[15:8];
                evq_wr <= evq_wr + 2'd1;
            end else if (ev_valid) begin
                ev_overflow <= 1'b1;
            end

            if (evq_do_pop) evq_rd <= evq_rd + 2'd1;

            case ({evq_do_push, evq_do_pop})
                2'b10:   evq_level <= evq_level + 3'd1;
                2'b01:   evq_level <= evq_level - 3'd1;
                default: ;
            endcase
        end
    end

    //==================================================================
    // 6) 发送组帧
    //==================================================================
    localparam T_IDLE = 2'd0, T_SEND = 2'd1;

    reg [1:0]  tstate;
    reg [7:0]  t_type, t_seq_r;
    reg [8:0]  t_len, t_idx;
    reg [2:0]  t_src;                  // 0=ev 1=rv 2=pong 3=audio0 4=audio1
    reg [7:0]  t_crc;
    reg [7:0]  t_ev0, t_ev1, t_ev2, t_ev3;   // 事件载荷锁存
    reg        pend_rv, pend_pong;
    reg        ack_rv, ack_pong;       // 单拍，清 pending
    reg        reg_re_d;
    reg [7:0]  rv_val;

    reg        tx_valid_r;
    reg [7:0]  tx_data_r;
    assign tx_valid = tx_valid_r;
    assign tx_data  = tx_data_r;

    wire [8:0] t_total = 9'd7 + t_len;        // 头 6 + 载荷 len + CRC 1

    // 头部字节
    reg [7:0] hdr_byte;
    always @(*) begin
        case (t_idx)
            9'd0: hdr_byte = 8'hAA;
            9'd1: hdr_byte = 8'h55;
            9'd2: hdr_byte = t_type;
            9'd3: hdr_byte = t_seq_r;
            9'd4: hdr_byte = t_len[7:0];
            default: hdr_byte = {7'd0, t_len[8]};
        endcase
    end

    // 载荷字节
    reg [7:0] pay_byte;
    wire [8:0] pay_idx = t_idx - 9'd6;
    always @(*) begin
        case (t_src)
            3'd0: case (pay_idx)                 // 事件：CH, CODE, DUR_L, DUR_H
                      9'd0: pay_byte = t_ev0;
                      9'd1: pay_byte = t_ev1;
                      9'd2: pay_byte = t_ev2;
                      default: pay_byte = t_ev3;
                  endcase
            3'd1: pay_byte = (pay_idx == 9'd0) ? rv_addr : rv_val;
            3'd2: pay_byte = 8'h00;
            3'd3: pay_byte = (pay_idx == 9'd0) ? 8'h00 : fifo0_dout;
            default: pay_byte = (pay_idx == 9'd0) ? 8'h01 : fifo1_dout;
        endcase
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            tstate <= T_IDLE;
            t_type <= 8'h00; t_seq_r <= 8'h00;
            t_len <= 9'd0; t_idx <= 9'd0; t_src <= 3'd0;
            t_crc <= 8'h00;
            t_ev0 <= 8'h00; t_ev1 <= 8'h00; t_ev2 <= 8'h00; t_ev3 <= 8'h00;
            tx_valid_r <= 1'b0; tx_data_r <= 8'h00;
            evq_pop <= 1'b0;
            fifo0_pop <= 1'b0; fifo1_pop <= 1'b0;
            ack_rv <= 1'b0; ack_pong <= 1'b0;
        end else begin
            tx_valid_r <= 1'b0;
            evq_pop    <= 1'b0;
            fifo0_pop  <= 1'b0;
            fifo1_pop  <= 1'b0;
            ack_rv     <= 1'b0;
            ack_pong   <= 1'b0;

            case (tstate)
                T_IDLE: begin
                    t_idx <= 9'd0;
                    t_crc <= 8'h00;
                    if (evq_level > 3'd0) begin
                        t_src  <= 3'd0;
                        t_type <= evq_type[evq_rd];
                        t_len  <= (evq_type[evq_rd] == 8'h20) ? 9'd4 : 9'd2;
                        // 锁存事件载荷（evq_rd 推进后不得再回读队列）
                        t_ev0 <= evq_d0[evq_rd];
                        t_ev1 <= evq_d1[evq_rd];
                        t_ev2 <= evq_d2[evq_rd];
                        t_ev3 <= evq_d3[evq_rd];
                        evq_pop <= 1'b1;
                        tstate <= T_SEND;
                    end else if (pend_rv) begin
                        t_src <= 3'd1; t_type <= 8'h32; t_len <= 9'd2;
                        ack_rv <= 1'b1;
                        tstate <= T_SEND;
                    end else if (pend_pong) begin
                        t_src <= 3'd2; t_type <= 8'h7F; t_len <= 9'd0;
                        ack_pong <= 1'b1;
                        tstate <= T_SEND;
                    end else if (fifo0_level >= 10'd160) begin
                        t_src <= 3'd3; t_type <= 8'h10; t_len <= 9'd161;
                        tstate <= T_SEND;
                    end else if (fifo1_level >= 10'd160) begin
                        t_src <= 3'd4; t_type <= 8'h10; t_len <= 9'd161;
                        tstate <= T_SEND;
                    end
                end

                T_SEND: begin
                    if (!tx_busy && !tx_valid_r) begin
                        tx_valid_r <= 1'b1;
                        if (t_idx < 9'd6) begin
                            tx_data_r <= hdr_byte;
                            if (t_idx >= 9'd2)
                                t_crc <= crc8_next(t_crc, hdr_byte);
                        end else if (t_idx < 9'd6 + t_len) begin
                            tx_data_r <= pay_byte;
                            t_crc <= crc8_next(t_crc, pay_byte);
                            if (t_src == 3'd3 && pay_idx != 9'd0) fifo0_pop <= 1'b1;
                            if (t_src == 3'd4 && pay_idx != 9'd0) fifo1_pop <= 1'b1;
                        end else begin
                            tx_data_r <= t_crc;
                        end

                        if (t_idx + 9'd1 >= t_total) begin
                            tstate  <= T_IDLE;
                            t_seq_r <= t_seq_r + 8'd1;
                        end else begin
                            t_idx <= t_idx + 9'd1;
                        end
                    end
                end

                default: tstate <= T_IDLE;
            endcase
        end
    end

    //==================================================================
    // 7) 寄存器读响应 -> REG_VAL 上行帧
    //==================================================================
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            reg_re_d <= 1'b0;
            rv_val   <= 8'h00;
            pend_rv  <= 1'b0;
            pend_pong<= 1'b0;
        end else begin
            reg_re_d <= reg_re;
            if (reg_re_d) begin
                rv_val  <= reg_rdata;
                pend_rv <= 1'b1;
            end else if (ack_rv) begin
                pend_rv <= 1'b0;
            end

            if (p_pong)     pend_pong <= 1'b1;
            else if (ack_pong) pend_pong <= 1'b0;
        end
    end

endmodule

`default_nettype wire
