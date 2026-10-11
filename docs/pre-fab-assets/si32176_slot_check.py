#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Si32176 PCM 时隙偏移计算 + 寄存器 dump 比对脚本（源代码）。

对应 PRE_FAB_CHECKLIST.md P0-1：验证 Si32176 的 PCM TX/RX 时隙偏移
与 FPGA tdm_switch 的时隙分配（INTERFACE_SPEC §4）一致。

用法：
    # 1) 计算各时隙应写入的偏移值（默认 8bit 槽、256bit 帧）
    python3 si32176_slot_check.py --slots 1 2 17 18

    # 2) 比对 SPI 回读的寄存器 dump（JSON：寄存器名 → 值）
    python3 si32176_slot_check.py --expect tx=1,rx=17 --dump regs_dump.json

    # 3) 自测（无参数运行）
    python3 si32176_slot_check.py

注意：寄存器名与编号以 Si32175/176 数据手册 "Register Map" 为准；
     若实际编号不同，用 --regmap 覆盖（JSON：语义名 → 寄存器名/编号）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# 默认寄存器命名（语义名 → Si3217x 常用寄存器名；编号请对照数据手册核对）
# ---------------------------------------------------------------------------

DEFAULT_REGMAP = {
    "PCMMODE": "PCMMODE",      # PCM 模式：压缩律（A-law）、使能
    "PCMTXLO": "PCMTXLO",      # 发送（上行）时隙偏移低字节
    "PCMTXHI": "PCMTXHI",      # 发送（上行）时隙偏移高字节
    "PCMRXLO": "PCMRXLO",      # 接收（下行）时隙偏移低字节
    "PCMRXHI": "PCMRXHI",      # 接收（下行）时隙偏移高字节
}

# TDM 帧参数（INTERFACE_SPEC §4）：32 时隙 × 8 bit = 256 bit @ 8 kHz
SLOTS_PER_FRAME = 32
SLOT_WIDTH_BITS = 8
FRAME_BITS = SLOTS_PER_FRAME * SLOT_WIDTH_BITS

# 默认通道映射（SLIC 视角）：TX=上行（SLIC→FPGA），RX=下行（FPGA→SLIC）
DEFAULT_MAP = {0: {"tx": 1, "rx": 17}, 1: {"tx": 2, "rx": 18}}


def slot_to_offset(slot: int, width_bits: int = SLOT_WIDTH_BITS) -> int:
    """时隙号 → 位偏移（PCM 时钟数）。TS N 起始于第 N×width 个 PCLK。"""
    if not (0 <= slot < SLOTS_PER_FRAME):
        raise ValueError(f"时隙号越界: {slot}（0..{SLOTS_PER_FRAME - 1}）")
    return slot * width_bits


def offset_to_reg_bytes(offset: int) -> Tuple[int, int]:
    """位偏移 → (LO, HI) 两寄存器字节（小端 16 bit）。"""
    if not (0 <= offset <= 0xFFFF):
        raise ValueError(f"偏移越界: {offset}")
    return offset & 0xFF, (offset >> 8) & 0xFF


def expected_regs(tx_slot: int, rx_slot: int) -> Dict[str, int]:
    """计算一片 SLIC 的期望寄存器值（TX/RX 偏移的 LO/HI 字节）。"""
    tx_off = slot_to_offset(tx_slot)
    rx_off = slot_to_offset(rx_slot)
    tx_lo, tx_hi = offset_to_reg_bytes(tx_off)
    rx_lo, rx_hi = offset_to_reg_bytes(rx_off)
    return {
        "PCMTXLO": tx_lo, "PCMTXHI": tx_hi,
        "PCMRXLO": rx_lo, "PCMRXHI": rx_hi,
    }


def diff_dump(dump: Dict[str, int], expect: Dict[str, int],
              regmap: Dict[str, str]) -> List[str]:
    """把寄存器 dump 与期望值比对，返回差异列表（空 = 全对）。

    dump 的键支持两种写法：语义名（PCMTXLO）或 --regmap 映射后的实际名。
    """
    problems: List[str] = []
    for sem, want in expect.items():
        actual_name = regmap.get(sem, sem)
        got = dump.get(actual_name, dump.get(sem))
        if got is None:
            problems.append(f"{sem}: dump 中缺失（查 {actual_name}）")
        elif (got & 0xFF) != want:
            problems.append(
                f"{sem}: 期望 0x{want:02X}，实际 0x{got & 0xFF:02X}"
                + (f"（注意：实际值 0x{got:X}，按低字节比对）" if got > 0xFF else ""))
    return problems


def print_plan(maps: Dict[int, Dict[str, int]]) -> None:
    """打印时隙 → 偏移 → 寄存器字节 的配置计划表。"""
    print(f"{'CH':<4}{'方向':<8}{'时隙':<6}{'位偏移':<8}{'LO':<6}{'HI':<6}")
    for ch, m in maps.items():
        for direction in ("tx", "rx"):
            slot = m[direction]
            off = slot_to_offset(slot)
            lo, hi = offset_to_reg_bytes(off)
            name = "上行TX" if direction == "tx" else "下行RX"
            print(f"CH{ch:<3}{name:<8}TS{slot:<5}{off:<8}0x{lo:02X}  0x{hi:02X}")


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------

def _self_test() -> bool:
    """偏移计算 + dump 比对逻辑自测。"""
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

    print("== si32176_slot_check 自测 ==")
    # 偏移计算（INTERFACE_SPEC §4）
    check("TS1 偏移 = 8", slot_to_offset(1) == 8)
    check("TS2 偏移 = 16", slot_to_offset(2) == 16)
    check("TS17 偏移 = 136", slot_to_offset(17) == 136)
    check("TS18 偏移 = 144", slot_to_offset(18) == 144)
    # 寄存器字节
    lo, hi = offset_to_reg_bytes(136)
    check("136 → (LO=0x88, HI=0x00)", lo == 0x88 and hi == 0x00, f"(0x{lo:02X},0x{hi:02X})")
    # CH0 期望寄存器
    exp = expected_regs(1, 17)
    check("CH0 期望寄存器", exp == {"PCMTXLO": 8, "PCMTXHI": 0,
                                    "PCMRXLO": 136, "PCMRXHI": 0}, str(exp))
    # dump 比对：全对
    good_dump = dict(exp)
    check("dump 全对 → 无差异", diff_dump(good_dump, exp, DEFAULT_REGMAP) == [])
    # dump 比对：RX 错槽（写成了 TS18 → 144）
    bad_dump = dict(exp)
    bad_dump["PCMRXLO"] = 144
    problems = diff_dump(bad_dump, exp, DEFAULT_REGMAP)
    check("dump RX 错槽被检出", len(problems) == 1 and "PCMRXLO" in problems[0], str(problems))
    # 越界
    try:
        slot_to_offset(32)
        check("时隙越界报错", False)
    except ValueError:
        check("时隙越界报错", True)

    print("== 结果: %s ==" % ("PASS" if ok else "FAIL"))
    return ok


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def main() -> int:
    """命令行入口。"""
    ap = argparse.ArgumentParser(description="Si32176 PCM 时隙偏移计算/比对")
    ap.add_argument("--slots", type=int, nargs="*", default=None,
                    help="打印这些时隙的偏移与寄存器字节（如 --slots 1 2 17 18）")
    ap.add_argument("--expect", default=None,
                    help="期望映射，如 tx=1,rx=17（配合 --dump）")
    ap.add_argument("--dump", default=None,
                    help="SPI 回读寄存器 dump（JSON 文件）")
    ap.add_argument("--regmap", default=None,
                    help="寄存器名映射 JSON（语义名 → 实际名/编号）")
    ap.add_argument("--all-channels", action="store_true",
                    help="按默认 CH0(TS1/TS17)+CH1(TS2/TS18) 打印配置计划")
    args = ap.parse_args()

    regmap = dict(DEFAULT_REGMAP)
    if args.regmap:
        with open(args.regmap, "r", encoding="utf-8") as f:
            regmap.update({str(k): str(v) for k, v in json.load(f).items()})

    if args.slots is not None:
        for slot in args.slots:
            off = slot_to_offset(slot)
            lo, hi = offset_to_reg_bytes(off)
            print(f"TS{slot:<3} → 偏移 {off:<5} → LO=0x{lo:02X} HI=0x{hi:02X}")
        return 0

    if args.all_channels:
        print_plan(DEFAULT_MAP)
        return 0

    if args.expect:
        kv = dict(p.split("=", 1) for p in args.expect.split(","))
        tx_slot, rx_slot = int(kv["tx"]), int(kv["rx"])
        exp = expected_regs(tx_slot, rx_slot)
        print(f"期望映射：上行 TS{tx_slot} / 下行 TS{rx_slot}")
        for k, v in exp.items():
            print(f"  {regmap.get(k, k)} = 0x{v:02X}")
        if args.dump:
            with open(args.dump, "r", encoding="utf-8") as f:
                dump = {str(k): (int(v, 0) if isinstance(v, str) else int(v))
                    for k, v in json.load(f).items()}
            problems = diff_dump(dump, exp, regmap)
            if problems:
                print("比对结果：FAIL")
                for p in problems:
                    print(f"  - {p}")
                return 1
            print("比对结果：PASS（寄存器与时隙映射一致）")
        return 0

    # 无参数：自测
    return 0 if _self_test() else 1


if __name__ == "__main__":
    sys.exit(main())
