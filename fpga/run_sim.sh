#!/bin/sh
# =====================================================================
# run_sim.sh : 一键编译并运行全部 Verilog 测试台（Icarus Verilog）
# 用法： cd fpga && sh run_sim.sh
# 依赖： iverilog/vvp（任意版本 >= 10）；参考表已由 tools/gen_tables.py 生成
# =====================================================================
set -e
export LC_ALL=C

RTL=rtl
TB=tb
SIM=sim
mkdir -p "$SIM"

# 参考表检查
if [ ! -f "$TB/gen/alaw_enc_ref.hex" ]; then
    echo "[init] 生成 G.711/DTMF 参考表 ..."
    python3 tools/gen_tables.py
fi

IVERILOG=${IVERILOG:-iverilog}
VVP=${VVP:-vvp}
FLAGS="-g2005"

run_tb() {
    name="$1"; shift
    echo "=================================================="
    echo "[compile] $name"
    $IVERILOG $FLAGS -o "$SIM/$name.vvp" "$@" 2>&1 | sed 's/^/  /'
    echo "[run] $name"
    ( cd "$TB" && $VVP "../$SIM/$name.vvp" ) 2>&1 | grep -v "VCD info" | sed 's/^/  /'
}

run_tb tb_g711        "$RTL/g711_codec.v"        "$TB/tb_g711.v"
run_tb tb_dtmf        "$RTL/dtmf_detector.v"     "$TB/tb_dtmf.v"
run_tb tb_i2s         "$RTL/i2s_controller.v"    "$TB/tb_i2s.v"
run_tb tb_tdm_switch  "$RTL/tdm_switch.v"        "$TB/tb_tdm_switch.v"
run_tb tb_uart_loop   "$RTL/uart_tx.v" "$RTL/uart_rx.v" "$TB/tb_uart_loop.v"
run_tb tb_uart_bridge "$RTL/uart_bridge.v" "$RTL/fifo_sync.v" "$TB/tb_uart_bridge.v"
run_tb tb_top         "$RTL/top.v" "$RTL/g711_codec.v" "$RTL/dtmf_detector.v" \
                      "$RTL/i2s_controller.v" "$RTL/tdm_switch.v" "$RTL/uart_bridge.v" \
                      "$RTL/fifo_sync.v" "$RTL/uart_tx.v" "$RTL/uart_rx.v" \
                      "$RTL/spi_master.v" "$RTL/i2c_master.v" "$RTL/cfg_sequencer.v" \
                      "$RTL/pll_audio_clk.v" "$TB/tb_top.v"

echo "=================================================="
echo "全部测试台运行完毕（见上方 PASS/FAIL）"
