//======================================================================
// tb_g711.v : G.711 编解码全码表比对测试
//----------------------------------------------------------------------
// 激励与判据：
//   1. 解码：256 个 A-law / mu-law 码字逐一与参考表（Python audioop /
//      Sun g711.c）比对，要求逐位一致
//   2. 编码：全部 65536 个 16bit 线性样点逐一与参考表比对，要求逐位一致
// 参考表由 fpga/tools/gen_tables.py 生成于 tb/gen/
//======================================================================
`timescale 1ns / 1ps

module tb_g711;

    reg clk = 0;
    reg rst_n = 0;
    always #5 clk = ~clk;

    // A-law
    reg signed [15:0] a_pcm_in; reg a_enc_valid;
    wire [7:0] a_code_out; wire a_code_valid;
    reg [7:0] a_code_in; reg a_dec_valid;
    wire signed [15:0] a_pcm_out; wire a_pcm_valid;

    g711_codec #(.LAW(0)) u_alaw (
        .clk(clk), .rst_n(rst_n),
        .enc_pcm_in(a_pcm_in), .enc_valid(a_enc_valid),
        .enc_code_out(a_code_out), .enc_code_valid(a_code_valid),
        .dec_code_in(a_code_in), .dec_valid(a_dec_valid),
        .dec_pcm_out(a_pcm_out), .dec_pcm_valid(a_pcm_valid)
    );

    // mu-law
    reg signed [15:0] m_pcm_in; reg m_enc_valid;
    wire [7:0] m_code_out; wire m_code_valid;
    reg [7:0] m_code_in; reg m_dec_valid;
    wire signed [15:0] m_pcm_out; wire m_pcm_valid;

    g711_codec #(.LAW(1)) u_mulaw (
        .clk(clk), .rst_n(rst_n),
        .enc_pcm_in(m_pcm_in), .enc_valid(m_enc_valid),
        .enc_code_out(m_code_out), .enc_code_valid(m_code_valid),
        .dec_code_in(m_code_in), .dec_valid(m_dec_valid),
        .dec_pcm_out(m_pcm_out), .dec_pcm_valid(m_pcm_valid)
    );

    // 参考表
    reg [15:0] a_dec_ref [0:255];
    reg [15:0] m_dec_ref [0:255];
    reg [7:0]  a_enc_ref [0:65535];
    reg [7:0]  m_enc_ref [0:65535];

    integer i;
    integer errors = 0;

    initial begin
        $readmemh("gen/alaw_dec_ref.hex",  a_dec_ref);
        $readmemh("gen/mulaw_dec_ref.hex", m_dec_ref);
        $readmemh("gen/alaw_enc_ref.hex",  a_enc_ref);
        $readmemh("gen/mulaw_enc_ref.hex", m_enc_ref);
    end

    initial begin
        if ($test$plusargs("dump")) begin
            $dumpfile("sim/tb_g711.vcd");
            $dumpvars(0, tb_g711);
        end

        a_pcm_in = 0; a_enc_valid = 0; a_code_in = 0; a_dec_valid = 0;
        m_pcm_in = 0; m_enc_valid = 0; m_code_in = 0; m_dec_valid = 0;
        repeat (4) @(negedge clk);
        rst_n = 1;
        repeat (2) @(negedge clk);

        //------------------------------------------------------------
        // 1. 解码比对（256 码字 x 2 law）
        //------------------------------------------------------------
        for (i = 0; i < 256; i = i + 1) begin
            @(negedge clk);
            a_code_in <= i[7:0]; a_dec_valid <= 1'b1;
            m_code_in <= i[7:0]; m_dec_valid <= 1'b1;
            @(negedge clk);
            a_dec_valid <= 1'b0; m_dec_valid <= 1'b0;
            @(negedge clk);
            if (a_pcm_out !== $signed(a_dec_ref[i])) begin
                errors = errors + 1;
                if (errors < 10)
                    $display("ALAW DEC MISMATCH code=%02x got=%0d exp=%0d",
                             i, a_pcm_out, $signed(a_dec_ref[i]));
            end
            if (m_pcm_out !== $signed(m_dec_ref[i])) begin
                errors = errors + 1;
                if (errors < 10)
                    $display("MULAW DEC MISMATCH code=%02x got=%0d exp=%0d",
                             i, m_pcm_out, $signed(m_dec_ref[i]));
            end
        end
        $display("decode compare done, errors so far = %0d", errors);

        //------------------------------------------------------------
        // 2. 编码比对（65536 样点 x 2 law）
        //------------------------------------------------------------
        for (i = 0; i < 65536; i = i + 1) begin
            @(negedge clk);
            a_pcm_in <= i[15:0] - 16'd32768; a_enc_valid <= 1'b1;
            m_pcm_in <= i[15:0] - 16'd32768; m_enc_valid <= 1'b1;
            @(negedge clk);
            a_enc_valid <= 1'b0; m_enc_valid <= 1'b0;
            @(negedge clk);
            if (a_code_out !== a_enc_ref[i]) begin
                errors = errors + 1;
                if (errors < 20)
                    $display("ALAW ENC MISMATCH x=%0d got=%02x exp=%02x",
                             $signed(i[15:0] - 16'd32768), a_code_out, a_enc_ref[i]);
            end
            if (m_code_out !== m_enc_ref[i]) begin
                errors = errors + 1;
                if (errors < 20)
                    $display("MULAW ENC MISMATCH x=%0d got=%02x exp=%02x",
                             $signed(i[15:0] - 16'd32768), m_code_out, m_enc_ref[i]);
            end
        end

        if (errors == 0) $display("TB_G711 PASS: 全码表比对一致 (256+65536) x2 law");
        else             $display("TB_G711 FAIL: %0d errors", errors);
        $finish;
    end

endmodule
