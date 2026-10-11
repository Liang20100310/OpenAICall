//======================================================================
// top.v : 顶层（电话中继 + AI 语音指挥 FPGA 系统）
//----------------------------------------------------------------------
// 结构（见 docs/INTERFACE_SPEC.md）：
//   座机 -> SLIC -> Codec(I2S) -> G711 编码 -> TDM 时隙交换 -> UART -> PC
//                                              |
//                                          DTMF 检测(2 通道) -> 事件帧
//   PC -> UART -> 帧解析 -> TDM -> G711 解码 -> Codec(I2S) -> SLIC -> 座机
//
// 时钟域：
//   clk_sys   = 27MHz（UART / DTMF / TDM / 配置逻辑）
//   clk_audio = 24.576MHz（I2S，由 PLL 产生；SIM_MODE=1 时透传，仅仿真用）
//   跨时钟域：8kHz 帧节拍经 2FF 同步 + 边沿检测；音频样点 125us 稳定
//
// 寄存器（UART REG 空间，见 INTERFACE_SPEC §3）：
//   0x00 SCRATCH   0x01 CHAN_EN   0x02 LOOPBACK  0x03 DTMF_THR
//   0x04 DTMF_CFG  0x05 STATUS(RO) 0x06 SLOT_MAP0 0x07 SLOT_MAP1
//   0x08 VERSION
//======================================================================
`timescale 1ns / 1ps
`default_nettype none

module top #(
    parameter CLK_HZ     = 27_000_000,
    parameter BAUD       = 921_600,
    parameter SIM_MODE   = 0,          // 1 = 仿真（PLL 透传）
    parameter CFG_WAIT_MS= 20          // 上电配置延时（仿真可设 0）
) (
    input  wire        clk_27m,
    input  wire        rst_n,

    // UART
    input  wire        uart_rxd,
    output wire        uart_txd,

    // I2S / Codec
    output wire        codec_mclk,
    output wire        codec_bclk,
    output wire        codec_lrclk,
    output wire        codec_sdout,
    input  wire        codec_sdin,

    // I2C（Codec 配置，开漏 + 外部上拉）
    output wire        i2c_scl,
    inout  wire        i2c_sda,

    // SPI（SLIC 配置）
    output wire        spi_sclk,
    output wire        spi_cs_n,
    output wire        spi_mosi,
    input  wire        spi_miso,

    // SLIC 接口（2 通道）
    output wire [1:0]  slic_ring,     // 振铃使能
    input  wire [1:0]  slic_hook,     // 摘挂机检测（1 = 摘机）
    input  wire [1:0]  slic_int,      // SLIC 中断（预留）

    // 状态 / 调试
    output wire [5:0]  led,
    output wire        dbg_frame_sync,
    output wire        dbg_dtmf_valid
);

    //=================================================================
    // 0) 声明区
    //=================================================================
    // 复位 / 时钟
    reg  [1:0]  rst_sys_sync, rst_aud_sync;
    reg  [2:0]  fv_sync;
    wire        clk_audio, pll_lock;
    wire        rst_sys_n, rst_aud_n;
    wire        frame_tick;

    // UART
    wire [7:0]  urx_data, utx_data;
    wire        urx_valid, urx_frame_err, utx_valid, utx_busy;

    // I2S
    wire [15:0] i2s_rx_l, i2s_rx_r, i2s_tx_l, i2s_tx_r;
    wire        i2s_frame_valid;

    // G.711
    wire [7:0]  enc0_code, enc1_code;
    wire        enc0_valid, enc1_valid;
    wire signed [15:0] dec0_pcm, dec1_pcm;
    wire        dec0_valid, dec1_valid;

    // TDM
    wire [7:0]  tdm_ul0_q, tdm_ul1_q, tdm_dl0_q, tdm_dl1_q;
    wire        tdm_ul0_qv, tdm_ul1_qv, tdm_dl0_qv, tdm_dl1_qv;
    wire [4:0]  tdm_slot_cnt;
    wire        tdm_slot_stb;

    // 下行音频 FIFO（在 uart_bridge 内）
    wire [7:0]  dl0_byte, dl1_byte;
    wire        dl0_empty, dl1_empty;
    reg         dl0_pop, dl1_pop;
    reg  [7:0]  dl0_byte_r, dl1_byte_r;
    reg         dl0_wr, dl1_wr;

    // UART 桥
    wire [15:0] stat_crc_err, stat_frame_err, stat_seq_gap;
    wire        ev_overflow;
    wire        ring_valid, ring_on;
    wire [7:0]  ring_ch;
    wire        reg_we, reg_re;
    wire [7:0]  reg_waddr, reg_wdata, reg_raddr;

    // 事件
    reg         ev_valid;
    reg  [7:0]  ev_type, ev_ch, ev_code;
    reg  [15:0] ev_dur;
    reg         p_dtmf0, p_dtmf1, p_hook0, p_hook1;
    reg  [3:0]  d0_lat, d1_lat;
    reg  [15:0] dur0_lat, dur1_lat;
    reg         h0_state, h1_state, h0_prev, h1_prev;

    // DTMF
    wire [3:0]  dtmf0_digit, dtmf1_digit;
    wire        dtmf0_valid, dtmf1_valid;
    wire [15:0] dtmf0_dur, dtmf1_dur;

    // 寄存器文件
    reg  [7:0]  reg_scratch, chan_en_r, loopback_r, dtmf_thr_r, dtmf_cfg_r;
    reg  [4:0]  slot_map0_r, slot_map1_r;
    reg  [7:0]  reg_rdata;
    wire        dtmf_en  = dtmf_cfg_r[0];
    wire [7:0]  dtmf_thr = dtmf_thr_r;
    wire [2:0]  loopback = loopback_r[2:0];
    wire [1:0]  chan_en  = chan_en_r[1:0];

    // 上行字节选择
    reg  [7:0]  ul_byte;
    reg         ul_valid, ul_is_ch0;

    // 配置外设
    wire        spi_start, spi_done, spi_busy;
    wire [15:0] spi_tx, spi_rx;
    wire        i2c_cmd_valid, i2c_done, i2c_busy, i2c_ack_err;
    wire [2:0]  i2c_cmd;
    wire [7:0]  i2c_tx_byte, i2c_rx_byte;
    wire        i2c_scl_oen, i2c_sda_oen;
    wire        cfg_done, cfg_err;

    // 振铃
    reg  [1:0]  ring_state;

    //=================================================================
    // 1) 复位 / 时钟
    //=================================================================
    always @(posedge clk_27m or negedge rst_n)
        if (!rst_n) rst_sys_sync <= 2'b00;
        else        rst_sys_sync <= {rst_sys_sync[0], 1'b1};
    assign rst_sys_n = rst_sys_sync[1];

    pll_audio_clk #(.SIM_MODE(SIM_MODE)) u_pll (
        .clkin(clk_27m), .rst_n(rst_n),
        .clkout(clk_audio), .lock(pll_lock)
    );

    always @(posedge clk_audio or negedge rst_n)
        if (!rst_n) rst_aud_sync <= 2'b00;
        else        rst_aud_sync <= {rst_aud_sync[0], 1'b1};
    assign rst_aud_n = rst_aud_sync[1];

    // 8kHz 帧节拍（clk_audio -> clk_sys，2FF + 边沿）
    always @(posedge clk_27m or negedge rst_sys_n)
        if (!rst_sys_n) fv_sync <= 3'b000;
        else            fv_sync <= {fv_sync[1:0], i2s_frame_valid};
    assign frame_tick = (fv_sync[2:1] == 2'b10);
    assign dbg_frame_sync = frame_tick;

    //=================================================================
    // 2) UART
    //=================================================================
    uart_rx #(.CLK_HZ(CLK_HZ), .BAUD(BAUD)) u_uart_rx (
        .clk(clk_27m), .rst_n(rst_sys_n),
        .rxd(uart_rxd), .data(urx_data), .valid(urx_valid),
        .frame_err(urx_frame_err)
    );
    uart_tx #(.CLK_HZ(CLK_HZ), .BAUD(BAUD)) u_uart_tx (
        .clk(clk_27m), .rst_n(rst_sys_n),
        .data(utx_data), .valid(utx_valid), .busy(utx_busy), .txd(uart_txd)
    );

    //=================================================================
    // 3) I2S 控制器
    //=================================================================
    i2s_controller #(.SLOT_WIDTH(32), .DATA_WIDTH(16)) u_i2s (
        .clk_audio(clk_audio), .rst_n(rst_aud_n),
        .tx_left(i2s_tx_l), .tx_right(i2s_tx_r),
        .rx_left(i2s_rx_l), .rx_right(i2s_rx_r),
        .rx_frame_valid(i2s_frame_valid),
        .mclk(codec_mclk), .bclk(codec_bclk), .lrclk(codec_lrclk),
        .sdout(codec_sdout), .sdin(codec_sdin),
        .loopback_en(loopback[1]),
        .bit_cnt_dbg()
    );

    //=================================================================
    // 4) G.711 编解码
    //=================================================================
    g711_codec #(.LAW(0)) u_enc0 (
        .clk(clk_27m), .rst_n(rst_sys_n),
        .enc_pcm_in(i2s_rx_l), .enc_valid(frame_tick),
        .enc_code_out(enc0_code), .enc_code_valid(enc0_valid),
        .dec_code_in(8'h00), .dec_valid(1'b0),
        .dec_pcm_out(), .dec_pcm_valid()
    );
    g711_codec #(.LAW(0)) u_enc1 (
        .clk(clk_27m), .rst_n(rst_sys_n),
        .enc_pcm_in(i2s_rx_r), .enc_valid(frame_tick),
        .enc_code_out(enc1_code), .enc_code_valid(enc1_valid),
        .dec_code_in(8'h00), .dec_valid(1'b0),
        .dec_pcm_out(), .dec_pcm_valid()
    );
    // 解码器输入 = TDM 下行读口（G.711 码字）
    g711_codec #(.LAW(0)) u_dec0 (
        .clk(clk_27m), .rst_n(rst_sys_n),
        .enc_pcm_in(16'sd0), .enc_valid(1'b0),
        .enc_code_out(), .enc_code_valid(),
        .dec_code_in(tdm_dl0_q), .dec_valid(tdm_dl0_qv),
        .dec_pcm_out(dec0_pcm), .dec_pcm_valid(dec0_valid)
    );
    g711_codec #(.LAW(0)) u_dec1 (
        .clk(clk_27m), .rst_n(rst_sys_n),
        .enc_pcm_in(16'sd0), .enc_valid(1'b0),
        .enc_code_out(), .enc_code_valid(),
        .dec_code_in(tdm_dl1_q), .dec_valid(tdm_dl1_qv),
        .dec_pcm_out(dec1_pcm), .dec_pcm_valid(dec1_valid)
    );

    // 下行 PCM -> I2S 发送（125us 稳定，安全跨域）
    assign i2s_tx_l = dec0_pcm;
    assign i2s_tx_r = dec1_pcm;

    //=================================================================
    // 5) TDM 时隙交换
    //=================================================================
    wire cross_en = loopback[2];
    wire [4:0] map_ul0 = cross_en ? 5'd2  : 5'd1;
    wire [4:0] map_ul1 = cross_en ? 5'd1  : 5'd2;
    wire [4:0] map_dl0 = cross_en ? slot_map1_r : slot_map0_r;
    wire [4:0] map_dl1 = cross_en ? slot_map0_r : slot_map1_r;

    tdm_switch u_tdm (
        .clk(clk_27m), .rst_n(rst_sys_n),
        .frame_sync(frame_tick),
        .ul0_data(enc0_code), .ul0_wr(enc0_valid),
        .ul1_data(enc1_code), .ul1_wr(enc1_valid),
        .dl0_data(dl0_byte_r), .dl0_wr(dl0_wr),
        .dl1_data(dl1_byte_r), .dl1_wr(dl1_wr),
        .map_ul0(map_ul0), .map_ul1(map_ul1),
        .map_dl0(map_dl0), .map_dl1(map_dl1),
        .ul0_q(tdm_ul0_q), .ul0_q_valid(tdm_ul0_qv),
        .ul1_q(tdm_ul1_q), .ul1_q_valid(tdm_ul1_qv),
        .dl0_q(tdm_dl0_q), .dl0_q_valid(tdm_dl0_qv),
        .dl1_q(tdm_dl1_q), .dl1_q_valid(tdm_dl1_qv),
        .slot_cnt(tdm_slot_cnt), .slot_stb(tdm_slot_stb)
    );

    //=================================================================
    // 6) UART 帧桥
    //=================================================================
    uart_bridge u_bridge (
        .clk(clk_27m), .rst_n(rst_sys_n),
        .rx_data(urx_data), .rx_valid(urx_valid),
        .tx_data(utx_data), .tx_valid(utx_valid), .tx_busy(utx_busy),
        .ul0_byte(ul_byte), .ul0_valid(ul_valid && ul_is_ch0),
        .ul1_byte(ul_byte), .ul1_valid(ul_valid && !ul_is_ch0),
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

    //=================================================================
    // 7) 上行字节选择（LOOPBACK[0] = UART 音频环回：下行直通上行）
    //=================================================================
    always @(posedge clk_27m or negedge rst_sys_n) begin
        if (!rst_sys_n) begin
            ul_byte    <= 8'h00;
            ul_valid   <= 1'b0;
            ul_is_ch0  <= 1'b1;
        end else begin
            ul_valid <= 1'b0;
            if (loopback[0]) begin
                if (dl0_pop) begin
                    ul_byte <= dl0_byte; ul_valid <= 1'b1; ul_is_ch0 <= 1'b1;
                end
                if (dl1_pop) begin
                    ul_byte <= dl1_byte; ul_valid <= 1'b1; ul_is_ch0 <= 1'b0;
                end
            end else begin
                if (tdm_ul0_qv && chan_en[0]) begin
                    ul_byte <= tdm_ul0_q; ul_valid <= 1'b1; ul_is_ch0 <= 1'b1;
                end
                if (tdm_ul1_qv && chan_en[1]) begin
                    ul_byte <= tdm_ul1_q; ul_valid <= 1'b1; ul_is_ch0 <= 1'b0;
                end
            end
        end
    end

    //=================================================================
    // 8) 下行 FIFO 读节奏（每帧一字节）-> TDM 下行时隙
    //=================================================================
    always @(posedge clk_27m or negedge rst_sys_n) begin
        if (!rst_sys_n) begin
            dl0_pop <= 1'b0; dl1_pop <= 1'b0;
            dl0_wr  <= 1'b0; dl1_wr  <= 1'b0;
            dl0_byte_r <= 8'h00; dl1_byte_r <= 8'h00;
        end else begin
            dl0_pop <= 1'b0; dl1_pop <= 1'b0;
            dl0_wr  <= 1'b0; dl1_wr  <= 1'b0;

            // 每帧从下行 FIFO 取一字节（环回模式同样需要取，供上行回环）
            if (frame_tick) begin
                if (!dl0_empty) dl0_pop <= 1'b1;
                if (!dl1_empty) dl1_pop <= 1'b1;
            end
            if (dl0_pop) begin
                dl0_byte_r <= dl0_byte;
                // 环回模式只回上行，不写 TDM 下行时隙
                if (!loopback[0]) dl0_wr <= 1'b1;
            end
            if (dl1_pop) begin
                dl1_byte_r <= dl1_byte;
                if (!loopback[0]) dl1_wr <= 1'b1;
            end
        end
    end

    //=================================================================
    // 9) DTMF 检测（2 通道）
    //=================================================================
    dtmf_detector #(.N_SAMPLES(256), .STABLE_N(2)) u_dtmf0 (
        .clk(clk_27m), .rst_n(rst_sys_n),
        .pcm_in(i2s_rx_l), .pcm_valid(frame_tick),
        .cfg_enable(dtmf_en), .cfg_threshold(dtmf_thr),
        .digit(dtmf0_digit), .digit_valid(dtmf0_valid),
        .digit_dur_ms(dtmf0_dur),
        .dbg_row_max(), .dbg_col_max(), .dbg_win_cnt()
    );
    dtmf_detector #(.N_SAMPLES(256), .STABLE_N(2)) u_dtmf1 (
        .clk(clk_27m), .rst_n(rst_sys_n),
        .pcm_in(i2s_rx_r), .pcm_valid(frame_tick),
        .cfg_enable(dtmf_en), .cfg_threshold(dtmf_thr),
        .digit(dtmf1_digit), .digit_valid(dtmf1_valid),
        .digit_dur_ms(dtmf1_dur),
        .dbg_row_max(), .dbg_col_max(), .dbg_win_cnt()
    );
    assign dbg_dtmf_valid = dtmf0_valid | dtmf1_valid;

    //=================================================================
    // 10) 事件仲裁（DTMF x2 + 摘挂机 x2 -> 帧桥事件队列）
    //=================================================================
    always @(posedge clk_27m or negedge rst_sys_n) begin
        if (!rst_sys_n) begin
            p_dtmf0 <= 1'b0; p_dtmf1 <= 1'b0;
            p_hook0 <= 1'b0; p_hook1 <= 1'b0;
            d0_lat <= 4'd0; d1_lat <= 4'd0;
            dur0_lat <= 16'd0; dur1_lat <= 16'd0;
            h0_prev <= 1'b0; h1_prev <= 1'b0;
            h0_state <= 1'b0; h1_state <= 1'b0;
            ev_valid <= 1'b0;
            ev_type <= 8'h00; ev_ch <= 8'h00;
            ev_code <= 8'h00; ev_dur <= 16'd0;
        end else begin
            ev_valid <= 1'b0;

            if (dtmf0_valid) begin
                p_dtmf0 <= 1'b1; d0_lat <= dtmf0_digit; dur0_lat <= dtmf0_dur;
            end
            if (dtmf1_valid) begin
                p_dtmf1 <= 1'b1; d1_lat <= dtmf1_digit; dur1_lat <= dtmf1_dur;
            end

            h0_prev <= slic_hook[0];
            h1_prev <= slic_hook[1];
            if (slic_hook[0] != h0_prev) begin
                p_hook0 <= 1'b1; h0_state <= slic_hook[0];
            end
            if (slic_hook[1] != h1_prev) begin
                p_hook1 <= 1'b1; h1_state <= slic_hook[1];
            end

            if (p_dtmf0) begin
                ev_valid <= 1'b1; ev_type <= 8'h20; ev_ch <= 8'h00;
                ev_code <= {4'h0, d0_lat}; ev_dur <= dur0_lat;
                p_dtmf0 <= 1'b0;
            end else if (p_dtmf1) begin
                ev_valid <= 1'b1; ev_type <= 8'h20; ev_ch <= 8'h01;
                ev_code <= {4'h0, d1_lat}; ev_dur <= dur1_lat;
                p_dtmf1 <= 1'b0;
            end else if (p_hook0) begin
                ev_valid <= 1'b1; ev_type <= 8'h21; ev_ch <= 8'h00;
                ev_code <= {7'd0, h0_state}; ev_dur <= 16'd0;
                p_hook0 <= 1'b0;
            end else if (p_hook1) begin
                ev_valid <= 1'b1; ev_type <= 8'h21; ev_ch <= 8'h01;
                ev_code <= {7'd0, h1_state}; ev_dur <= 16'd0;
                p_hook1 <= 1'b0;
            end
        end
    end

    //=================================================================
    // 11) 寄存器文件
    //=================================================================
    always @(posedge clk_27m or negedge rst_sys_n) begin
        if (!rst_sys_n) begin
            reg_scratch <= 8'h00;
            chan_en_r   <= 8'h03;
            loopback_r  <= 8'h00;
            dtmf_thr_r  <= 8'h40;
            dtmf_cfg_r  <= 8'h01;
            slot_map0_r <= 5'd17;
            slot_map1_r <= 5'd18;
            reg_rdata   <= 8'h00;
        end else begin
            if (reg_we) begin
                case (reg_waddr)
                    8'h00: reg_scratch <= reg_wdata;
                    8'h01: chan_en_r   <= reg_wdata;
                    8'h02: loopback_r  <= reg_wdata;
                    8'h03: dtmf_thr_r  <= reg_wdata;
                    8'h04: dtmf_cfg_r  <= reg_wdata;
                    8'h06: slot_map0_r <= reg_wdata[4:0];
                    8'h07: slot_map1_r <= reg_wdata[4:0];
                    default: ;
                endcase
            end
            if (reg_re) begin
                case (reg_raddr)
                    8'h00: reg_rdata <= reg_scratch;
                    8'h01: reg_rdata <= chan_en_r;
                    8'h02: reg_rdata <= loopback_r;
                    8'h03: reg_rdata <= dtmf_thr_r;
                    8'h04: reg_rdata <= dtmf_cfg_r;
                    8'h05: reg_rdata <= {2'b00, slic_hook, 1'b0,
                                         (stat_crc_err != 16'd0)};
                    8'h06: reg_rdata <= {3'b000, slot_map0_r};
                    8'h07: reg_rdata <= {3'b000, slot_map1_r};
                    8'h08: reg_rdata <= 8'h10;
                    default: reg_rdata <= 8'h00;
                endcase
            end
        end
    end

    //=================================================================
    // 12) 振铃控制
    //=================================================================
    always @(posedge clk_27m or negedge rst_sys_n) begin
        if (!rst_sys_n) ring_state <= 2'b00;
        else if (ring_valid) ring_state[ring_ch[0]] <= ring_on;
    end
    assign slic_ring = ring_state;

    //=================================================================
    // 13) 配置外设（I2C Codec + SPI SLIC）
    //=================================================================
    cfg_sequencer #(.CLK_HZ(CLK_HZ), .WAIT_MS(CFG_WAIT_MS)) u_cfg (
        .clk(clk_27m), .rst_n(rst_sys_n),
        .spi_start(spi_start), .spi_tx(spi_tx),
        .spi_done(spi_done), .spi_busy(spi_busy),
        .i2c_cmd_valid(i2c_cmd_valid), .i2c_cmd(i2c_cmd),
        .i2c_tx(i2c_tx_byte), .i2c_done(i2c_done),
        .i2c_busy(i2c_busy), .i2c_ack_err(i2c_ack_err),
        .done(cfg_done), .err(cfg_err), .entry_idx()
    );

    spi_master #(.CLK_DIV(14), .WIDTH(16)) u_spi (
        .clk(clk_27m), .rst_n(rst_sys_n),
        .start(spi_start), .tx_data(spi_tx), .rx_data(spi_rx),
        .busy(spi_busy), .done(spi_done),
        .sclk(spi_sclk), .cs_n(spi_cs_n), .mosi(spi_mosi), .miso(spi_miso)
    );

    i2c_master #(.CLK_DIV(68)) u_i2c (
        .clk(clk_27m), .rst_n(rst_sys_n),
        .cmd_valid(i2c_cmd_valid), .cmd(i2c_cmd),
        .tx_byte(i2c_tx_byte), .rx_byte(i2c_rx_byte),
        .busy(i2c_busy), .done(i2c_done), .ack_err(i2c_ack_err),
        .scl_oen(i2c_scl_oen), .sda_oen(i2c_sda_oen), .sda_in(i2c_sda)
    );

    // 开漏输出 + 外部上拉
    assign i2c_scl = i2c_scl_oen ? 1'bz : 1'b0;
    assign i2c_sda = i2c_sda_oen ? 1'bz : 1'b0;

    //=================================================================
    // 14) LED 状态
    //=================================================================
    assign led = {cfg_done, pll_lock, ring_state[0], slic_hook[0],
                  (stat_crc_err != 16'd0), utx_busy};

    // 未使用信号占位（避免 lint 告警）
    wire _unused = &{1'b0, spi_rx, i2c_rx_byte, cfg_err, urx_frame_err,
                     tdm_slot_cnt, tdm_slot_stb, dec0_valid, dec1_valid,
                     slic_int, 1'b0};

endmodule

`default_nettype wire
