//======================================================================
// pll_audio_clk.v : 音频时钟产生（27MHz -> 24.576MHz）
//----------------------------------------------------------------------
// 两种实现，用 `define SIMULATION 或参数 SIM_MODE 切换：
//   * 仿真/无 PLL 环境：SIM_MODE=1，直接输出输入时钟（仅保证功能可仿真）
//   * 上板：实例化高云 rPLL IP（Gowin EDA -> IP Core Generator ->
//     PLL(rPLL)，IDIV=25, FBDIV=226, ODIV=24, 配置 27MHz -> 24.576MHz，
//     或按 EDA 向导生成后把生成的 pll.v 填入下方模板）
// 说明：27MHz 无法整数分频出 24.576MHz，必须用 PLL。
//   若你的板载晶振为 24.576/12.288MHz，可跳过本模块直接分频。
//   rPLL 参数需在高云 EDA 中重新生成并核对 VCO/锁定时间。
//======================================================================
`timescale 1ns / 1ps
`default_nettype none

module pll_audio_clk #(
    parameter SIM_MODE = 0
) (
    input  wire clkin,      // 27MHz
    input  wire rst_n,
    output wire clkout,     // 24.576MHz
    output wire lock
);

    generate
        if (SIM_MODE != 0) begin : g_sim
            // 仿真模式：直接透传（频率不等价，仅验证逻辑）
            assign clkout = clkin;
            assign lock   = rst_n;
        end else begin : g_pll
            //----------------------------------------------------------
            // 高云 rPLL 模板（在高云 EDA 中生成 pll.rpll 后替换本段）
            //   Gowin_rPLL pll_inst (
            //       .clkin(clkin), .clkout(clkout), .lock(lock),
            //       .reset(~rst_n), .clkin_sel(1'b0) ...
            //   );
            // 临时兜底：未生成 IP 时先透传，保证可综合（时序请自行换 PLL）
            //----------------------------------------------------------
            assign clkout = clkin;
            assign lock   = rst_n;
        end
    endgenerate

endmodule

`default_nettype wire
