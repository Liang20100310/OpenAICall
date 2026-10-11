// =============================================================================
//  tb_slic_pcm.v — slic_pcm 自检测试台（模拟 Si32176 PCM 时序作为激励）
// =============================================================================
//  用例：
//    T-1  PCLK 频率（= clk/96 = 256 kHz）与 FS 帧率（每 32 个 PCLK 一次）
//    T-2  上行：SLIC 模型按帧驱动 DR（slot1=计数图案），ul0/ul1 字节与图案一致、
//         每帧恰好 1 个 valid 脉冲
//    T-3  下行：每帧中段喂 dl 字节，下一帧 DX slot1 应发出该字节（1 帧流水）；
//         欠载帧应发 IDLE_BYTE(0xD5)
//    T-4  摘挂机：slot2 bit0 翻转后，hook_state 在 HOOK_DEB_FRAMES±2 帧内翻转，
//         且 hook_evt 恰好 1 个脉冲
//  退出：$finish，打印 PASS/FAIL 汇总（errors==0 → PASS）
// =============================================================================
`timescale 1ns / 1ps

module tb_slic_pcm;

    // ---- 时钟 24.576 MHz ----
    localparam real HALF_NS = 20.345;
    reg clk = 1'b0;
    always #HALF_NS clk = ~clk;

    reg rst_n = 1'b0;

    // ---- DUT 接口 ----
    wire pclk, fs, dx0, dx1;
    reg  dr0 = 1'b1, dr1 = 1'b1;
    wire [7:0] ul0_byte, ul1_byte;
    wire       ul0_valid, ul1_valid;
    reg  [7:0] dl0_byte = 8'd0, dl1_byte = 8'd0;
    reg        dl0_valid = 1'b0, dl1_valid = 1'b0;
    wire       hook0_state, hook1_state, hook0_evt, hook1_evt;

    slic_pcm #(
        .PCLK_DIV        (96),
        .HOOK_DEB_FRAMES (240)
    ) dut (
        .clk(clk), .rst_n(rst_n),
        .pclk(pclk), .fs(fs), .dr0(dr0), .dr1(dr1), .dx0(dx0), .dx1(dx1),
        .ul0_byte(ul0_byte), .ul0_valid(ul0_valid),
        .ul1_byte(ul1_byte), .ul1_valid(ul1_valid),
        .dl0_byte(dl0_byte), .dl0_valid(dl0_valid),
        .dl1_byte(dl1_byte), .dl1_valid(dl1_valid),
        .hook0_state(hook0_state), .hook1_state(hook1_state),
        .hook0_evt(hook0_evt),     .hook1_evt(hook1_evt)
    );

    // ---- 统计 ----
    integer errors    = 0;
    integer frame_cnt = 0;          // TB 帧计数（fs 上升沿 +1）
    integer ul0_cnt   = 0, ul1_cnt = 0;
    integer hook_evt0_cnt = 0, hook_evt1_cnt = 0;
    integer hook_evt0_frame = -1, hook_evt1_frame = -1;

    localparam [7:0] IDLE_BYTE = 8'hD5;
    localparam integer HOOK_DEB = 240;
    localparam integer FRAME_CHANGE0 = 300;   // 通道0 摘挂机翻转帧
    localparam integer FRAME_CHANGE1 = 500;   // 通道1 摘挂机翻转帧
    localparam integer FRAME_END     = 900;

    // ---- SLIC 模型（Si32176 PCM 时序）----
    reg [31:0] w0, w1;              // 本帧 DR 发送字
    reg [7:0]  ul0_exp, ul1_exp;    // 本帧期望上行图案
    reg        hook_drive0 = 1'b0;  // slot2 bit0 激励（0=挂机）
    reg        hook_drive1 = 1'b1;
    integer    tb_idx = 0;          // 本帧内位序号 0..31

    // 下行激励/期望（1 帧流水）
    reg [7:0]  dl0_pend = 8'd0, dl1_pend = 8'd0;
    reg        dl0_pend_v = 1'b0, dl1_pend_v = 1'b0;
    reg [7:0]  dx0_exp = IDLE_BYTE, dx1_exp = IDLE_BYTE;
    reg [7:0]  dx0_sr = 8'd0, dx1_sr = 8'd0;

    // 帧首：装载 SLIC 发送字 + 期望值
    always @(posedge pclk) begin
        if (fs) begin
            tb_idx    = 0;
            frame_cnt = frame_cnt + 1;
            // 摘挂机激励：0 通道 300 帧后摘机；1 通道 500 帧后挂机
            hook_drive0 = (frame_cnt >= FRAME_CHANGE0);
            hook_drive1 = (frame_cnt <  FRAME_CHANGE1);
            // 上行图案（帧号相关）
            ul0_exp = frame_cnt[7:0] ^ 8'hA5;
            ul1_exp = frame_cnt[7:0] ^ 8'h5A;
            // SLIC 发送字：slot0=0xD5, slot1=语音, slot2=状态, slot3=0xA5
            w0 = {8'hD5, ul0_exp, {7'd0, hook_drive0}, 8'hA5};
            w1 = {8'hD5, ul1_exp, {7'd0, hook_drive1}, 8'hA5};
            dr0 <= w0[31];
            dr1 <= w1[31];
            // 下行期望：本帧 DX 应发“上一帧喂入”的字节，否则 IDLE
            dx0_exp = dl0_pend_v ? dl0_pend : IDLE_BYTE;
            dx1_exp = dl1_pend_v ? dl1_pend : IDLE_BYTE;
            dl0_pend_v = 1'b0;
            dl1_pend_v = 1'b0;
        end else begin
            tb_idx = tb_idx + 1;
            if (tb_idx < 32) begin
                dr0 <= w0[31 - tb_idx];
                dr1 <= w1[31 - tb_idx];
            end
        end
    end

    // DX 采样（PCLK 下降沿=位中点；DUT 在位起点驱动，半位余量）
    always @(negedge pclk) begin
        if (tb_idx < 32) begin
            dx0_sr = {dx0_sr[6:0], dx0};
            dx1_sr = {dx1_sr[6:0], dx1};
            if (tb_idx == 15) begin     // slot1 末位：字节齐
                if (frame_cnt > 2) begin
                    if (dx0_sr !== dx0_exp) begin
                        $display("FAIL frame %0d: DX0 slot1 = %02h, expect %02h",
                                 frame_cnt, dx0_sr, dx0_exp);
                        errors = errors + 1;
                    end
                    if (dx1_sr !== dx1_exp) begin
                        $display("FAIL frame %0d: DX1 slot1 = %02h, expect %02h",
                                 frame_cnt, dx1_sr, dx1_exp);
                        errors = errors + 1;
                    end
                end
            end
        end
    end

    // 下行激励：每帧中段（tb_idx==16）喂一个新字节
    always @(negedge pclk) begin
        if (tb_idx == 16) begin
            dl0_byte  <= frame_cnt[7:0] ^ 8'h3C;
            dl0_valid <= 1'b1;
            dl1_byte  <= frame_cnt[7:0] ^ 8'hC3;
            dl1_valid <= 1'b1;
            dl0_pend   = frame_cnt[7:0] ^ 8'h3C;
            dl1_pend   = frame_cnt[7:0] ^ 8'hC3;
            dl0_pend_v = 1'b1;
            dl1_pend_v = 1'b1;
        end else begin
            dl0_valid <= 1'b0;
            dl1_valid <= 1'b0;
        end
    end

    // ---- 上行检查（ul*_valid 与期望图案比对）----
    always @(posedge clk) begin
        if (ul0_valid) begin
            ul0_cnt = ul0_cnt + 1;
            if (frame_cnt > 2 && ul0_byte !== ul0_exp) begin
                $display("FAIL frame %0d: UL0 = %02h, expect %02h",
                         frame_cnt, ul0_byte, ul0_exp);
                errors = errors + 1;
            end
        end
        if (ul1_valid) begin
            ul1_cnt = ul1_cnt + 1;
            if (frame_cnt > 2 && ul1_byte !== ul1_exp) begin
                $display("FAIL frame %0d: UL1 = %02h, expect %02h",
                         frame_cnt, ul1_byte, ul1_exp);
                errors = errors + 1;
            end
        end
    end

    // ---- 摘挂机事件检查 ----
    always @(posedge clk) begin
        if (hook0_evt) begin
            hook_evt0_cnt   = hook_evt0_cnt + 1;
            hook_evt0_frame = frame_cnt;
        end
        if (hook1_evt) begin
            hook_evt1_cnt   = hook_evt1_cnt + 1;
            hook_evt1_frame = frame_cnt;
        end
    end

    // ---- T-1 时钟检查（前 3 个 PCLK 周期）----
    integer clk_cnt = 0, pclk_period = -1, pclk_per_frame = 0, fs_edges = 0;
    reg pclk_d = 1'b0, fs_d = 1'b0;
    always @(posedge clk) begin
        clk_cnt = clk_cnt + 1;
        pclk_d <= pclk;
        fs_d   <= fs;
        if (pclk && !pclk_d) begin          // PCLK 上升沿
            if (pclk_period < 0) pclk_period = clk_cnt;
            else if (frame_cnt == 1 && pclk_period > 0) begin
                if (clk_cnt - pclk_period != 96) begin
                    $display("FAIL: PCLK period = %0d clk, expect 96", clk_cnt - pclk_period);
                    errors = errors + 1;
                end
                pclk_period = clk_cnt;
            end
            pclk_per_frame = pclk_per_frame + 1;
        end
        if (fs && !fs_d) begin              // FS 上升沿
            if (fs_edges > 0 && pclk_per_frame != 32) begin
                $display("FAIL: %0d PCLK per frame, expect 32", pclk_per_frame);
                errors = errors + 1;
            end
            pclk_per_frame = 0;
            fs_edges = fs_edges + 1;
        end
    end

    // ---- 主流程 ----
    initial begin
        $dumpfile("tb_slic_pcm.vcd");   // 仿真波形（可选）
        $dumpvars(0, tb_slic_pcm);

        rst_n = 1'b0;
        #2000;
        rst_n = 1'b1;

        // 跑到 FRAME_END 帧
        wait (frame_cnt >= FRAME_END);
        #50000;

        // ---- 汇总检查 ----
        if (ul0_cnt < FRAME_END - 4 || ul1_cnt < FRAME_END - 4) begin
            $display("FAIL: UL valid 计数过少 ul0=%0d ul1=%0d（期望 ≈%0d）",
                     ul0_cnt, ul1_cnt, FRAME_END);
            errors = errors + 1;
        end
        // 摘挂机：翻转后 HOOK_DEB±2 帧内出事件，且仅 1 次
        if (hook_evt0_cnt != 1 ||
            hook_evt0_frame < FRAME_CHANGE0 + HOOK_DEB - 2 ||
            hook_evt0_frame > FRAME_CHANGE0 + HOOK_DEB + 2) begin
            $display("FAIL: HOOK0 evt cnt=%0d frame=%0d（期望 1 次 @ %0d±2）",
                     hook_evt0_cnt, hook_evt0_frame, FRAME_CHANGE0 + HOOK_DEB);
            errors = errors + 1;
        end
        if (hook_evt1_cnt != 1 ||
            hook_evt1_frame < FRAME_CHANGE1 + HOOK_DEB - 2 ||
            hook_evt1_frame > FRAME_CHANGE1 + HOOK_DEB + 2) begin
            $display("FAIL: HOOK1 evt cnt=%0d frame=%0d（期望 1 次 @ %0d±2）",
                     hook_evt1_cnt, hook_evt1_frame, FRAME_CHANGE1 + HOOK_DEB);
            errors = errors + 1;
        end
        if (hook0_state !== 1'b1 || hook1_state !== 1'b0) begin
            $display("FAIL: hook0_state=%b（期望 1）hook1_state=%b（期望 0）",
                     hook0_state, hook1_state);
            errors = errors + 1;
        end

        $display("--------------------------------------------------");
        $display("frames=%0d  ul0=%0d  ul1=%0d  hook_evt0=%0d@%0d  hook_evt1=%0d@%0d",
                 frame_cnt, ul0_cnt, ul1_cnt,
                 hook_evt0_cnt, hook_evt0_frame, hook_evt1_cnt, hook_evt1_frame);
        if (errors == 0) $display("tb_slic_pcm: PASS");
        else             $display("tb_slic_pcm: FAIL (%0d errors)", errors);
        $display("--------------------------------------------------");
        $finish;
    end

    // 超时保护
    initial begin
        #400_000_000;   // 400 ms
        $display("FAIL: timeout frame_cnt=%0d tb_idx=%0d fs=%b pclk=%b div=%0d bit=%0d",
                 frame_cnt, tb_idx, fs, pclk, dut.div_cnt, dut.bit_cnt);
        $finish;
    end

    // 调试：每 1 ms 打印一次（定位后可删）
    initial begin
        #1_000_000;
        forever begin
            $display("DBG t=%0t frame=%0d tb_idx=%0d fs=%b pclk=%b",
                     $time, frame_cnt, tb_idx, fs, pclk);
            #1_000_000;
        end
    end

endmodule
