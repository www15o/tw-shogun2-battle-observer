# -*- coding: utf-8 -*-
"""s2_ai_ctl.py — 幕府2 战斗 AI 托管控制台（RE-B3 直接写字段方案，封装版）。

原理（2026-08-09 实机确证，见 docs/23_HANDOFF + work/re_b3_notes.md）：
- AI 激活 = 单位 [+0xea8]=1/[+0xc01]=1 + 军队字段 [a28c]=1/[a290]=1.0f/[a294]=-1/[a270]=0
- a270=0（清人类标志）是 AI 激活前提，但同时触发速度锁（P25 同款；无解耦字段）
- 全程 WriteProcessMemory 直写，零代码执行、零崩溃风险、无黑屏（绕开 battle_ai 天气UI机制）
- 援军/新单位需持续补写（--watch 循环）

用法：
  python tools/s2_ai_ctl.py                  # 交互菜单
  python tools/s2_ai_ctl.py probe            # 只读状态探测（含组/军/单位/各字段）
  python tools/s2_ai_ctl.py all-ai           # 全员 AI 托管 + 援军监控（Ctrl+C 停监控）
  python tools/s2_ai_ctl.py auto [N]         # 【自动托管】监测战斗，state>=N(默认5) 自动全员接管+补援军，待命下一场
  python tools/s2_ai_ctl.py keep1 [N]        # 保留单位N人控，其余转 AI + 军队字段
  python tools/s2_ai_ctl.py human            # 切回人控（恢复原值）
  python tools/s2_ai_ctl.py watch            # 仅启动援军监控（补写新单位）

前置：游戏运行中且处于战斗场景（state 1+ 可写，建议部署/战斗态）。
注意：battle_ai 值字节必须为 0（python tools/re_b1_force.py --cmd battle_ai 校验）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb
import re_b3_inject as b3


def open_game():
    pid = pb.find_pid()
    if pid is None:
        print("✗ shogun2.exe 未运行")
        return None, None, None, None
    h = pb.K32.OpenProcess(pb.PROCESS_QUERY_INFORMATION | pb.PROCESS_VM_READ |
                           pb.PROCESS_VM_WRITE | pb.PROCESS_VM_OPERATION, False, pid)
    if not h:
        print(f"✗ OpenProcess 失败 err={__import__('ctypes').get_last_error()}（需管理员权限）")
        return None, None, None, None
    build, base, prof = pb.detect_build(h)
    if base is None:
        print("✗ 未找到引擎模块（shogun2.dll / empire.retail.dll）")
        return None, None, None, None
    print(f"✓ PID={pid} 引擎={build} base=0x{base:08x}")
    if build != "empire":
        print("  ⚠ resolve_e8 链仅在新引擎（empire.retail.dll）验证；旧引擎（shogun2.dll）请用 battle_ai 注入")
    return h, base, pid, build


WRITE_CMDS = ("all-ai", "auto", "keep1", "human", "watch")


def build_guard(build, cmd):
    """写命令仅允许新引擎（resolve_e8 链在旧引擎未验证，防误写）。"""
    if build != "empire":
        print(f"✗ {cmd} 仅支持新引擎（empire.retail.dll）。旧引擎（shogun2.dll）请用：")
        print("    python work\\re_b1_force.py --cmd battle_ai --write")
        return False
    return True


def battle_ok(h, base):
    """检查是否在战斗场景（st 可读）。"""
    mgr, env, e8, st = b3.resolve_e8(h, base)
    if not st:
        print("✗ 未进入战斗场景（env/st 链不可读）——请先进入战斗")
        return False
    state = pb.read_u8(h, st + b3.OFF_STATE)
    print(f"✓ 战斗中：状态=0x{state if state is not None else -1:x}（3=部署 5=战斗进行）")
    return True


def cmd_probe(h, base):
    return b3.probe(h, base)


def cmd_all_ai(h, base):
    """全员 AI 托管：全组全军应用 AI 字段 + st 标志，然后启动援军监控。"""
    if not battle_ok(h, base):
        return 1
    mgr, env, e8, st = b3.resolve_e8(h, base)
    groups = b3.walk_groups(h, st)
    if not groups:
        print("✗ 组表解析失败")
        return 1
    total = 0
    for gi, (g, acnt, atbl) in enumerate(groups):
        for ai in range(acnt):
            n = b3._apply_ai_army(h, atbl, ai, "ai")
            total += n
            print(f"  组{gi}军{ai}: +{n} 处")
    if b3.w8(h, st + b3.ST_SWITCHED, 1):
        total += 1
    print(f"✓ 全员 AI 托管完成（{total} 处写入）")
    print("  注：全员 AI 后战斗会自动开打，变速锁死（P25 同款）——这是引擎对'玩家军队 AI 激活'的固有行为")
    print("  现在启动援军监控（新单位自动补写，Ctrl+C 停止）...")
    return b3.watch_ai(h, base, interval=0.5)


def cmd_keep1(h, base, keep_unit=0):
    """保留 keep_unit 人控，其余转 AI + 军队字段（AI 激活生效）。"""
    if not battle_ok(h, base):
        return 1
    mgr, env, e8, st = b3.resolve_e8(h, base)
    groups = b3.walk_groups(h, st)
    if not groups:
        print("✗ 组表解析失败")
        return 1
    # 组0军0 = 玩家军（probe 判读）
    g, acnt, atbl = groups[0]
    n = b3._apply_ai_army(h, atbl, 0, "ai")   # 先全量 AI 字段
    # 再把 keep_unit 单位还原人控
    a = b3.walk_army(h, atbl, 0)
    if a and keep_unit < a["unit_cnt"]:
        u = pb.read_u32(h, a["unit_tbl"] + keep_unit * 4)
        if u:
            b3.w32(h, u + b3.UNIT_EA8, 0)
            b3.w8(h, u + b3.UNIT_C01, 0)
            print(f"  单位{keep_unit} 还原人控（+0xea8=0）")
    print(f"✓ 保留单位{keep_unit} 人控，其余 AI（组0军0，{n} 处字段）")
    print("  注：军队字段生效后变速锁死（引擎行为）；援军需 --watch 补写")
    return 0


def cmd_human(h, base):
    """切回人控：全组全军恢复原字段（单位+0xea8=0 等）。"""
    if not battle_ok(h, base):
        return 1
    mgr, env, e8, st = b3.resolve_e8(h, base)
    groups = b3.walk_groups(h, st)
    if not groups:
        print("✗ 组表解析失败")
        return 1
    total = 0
    for gi, (g, acnt, atbl) in enumerate(groups):
        for ai in range(acnt):
            n = b3._apply_ai_army(h, atbl, ai, "human")
            total += n
    if b3.w8(h, st + b3.ST_SWITCHED, 0):
        total += 1
    print(f"✓ 已切回人控（{total} 处复位）")
    return 0


def cmd_auto(h, base, min_state=5, interval=0.5):
    """自动托管：后台监测战斗状态，进入 min_state（默认5=战斗进行）后自动全员 AI 接管，
    持续补写覆盖援军/新单位，战斗结束自动待命下一场。Ctrl+C 停止。"""
    print(f"自动托管已启用：监测战斗状态，进入 state>={min_state} 自动全员 AI 接管"
          f"（每 {interval}s 检测，Ctrl+C 停止）", flush=True)
    armed = False      # 本场战斗是否已接管
    last_state = None
    try:
        while True:
            mgr, env, e8, st = b3.resolve_e8(h, base)
            if not st:
                if last_state is not None:
                    print(f"[{time.strftime('%H:%M:%S')}] 战斗结束/场景释放，待命下一场", flush=True)
                last_state = None
                armed = False
                time.sleep(interval)
                continue
            state = pb.read_u8(h, st + b3.OFF_STATE)
            if state != last_state:
                print(f"[{time.strftime('%H:%M:%S')}] 状态=0x{state if state is not None else -1:x}",
                      flush=True)
                last_state = state
            if state is not None and state >= min_state:
                if not armed:
                    groups = b3.walk_groups(h, st)
                    if not groups:
                        time.sleep(interval)
                        continue
                    total = 0
                    for gi, (g, acnt, atbl) in enumerate(groups):
                        for ai in range(acnt):
                            total += b3._apply_ai_army(h, atbl, ai, "ai")
                    if b3.w8(h, st + b3.ST_SWITCHED, 1):
                        total += 1
                    print(f"[{time.strftime('%H:%M:%S')}] 全员 AI 接管完成（{total} 处）"
                          f"——注：变速锁死（引擎对玩家军队 AI 激活的固有行为）", flush=True)
                    armed = True
                else:
                    # 持续补写（覆盖援军/新单位）
                    groups = b3.walk_groups(h, st)
                    if groups:
                        total = 0
                        for gi, (g, acnt, atbl) in enumerate(groups):
                            for ai in range(acnt):
                                total += b3._apply_ai_army(h, atbl, ai, "ai")
                        if total:
                            print(f"[{time.strftime('%H:%M:%S')}] 补写 {total} 处（援军/新单位接管）",
                                  flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n自动托管已停止")
    return 0


def cmd_watch(h, base):
    if not battle_ok(h, base):
        return 1
    print("援军监控：新单位自动补写 AI（Ctrl+C 停止）")
    return b3.watch_ai(h, base, interval=0.5)


def main():
    args = [a for a in sys.argv[1:]]
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    h, base, pid, build = open_game()
    if not h:
        return 2

    if args:
        cmd = args[0]
        if cmd in WRITE_CMDS and not build_guard(build, cmd):
            return 6
        if cmd == "probe":
            return cmd_probe(h, base)
        if cmd == "all-ai":
            return cmd_all_ai(h, base)
        if cmd == "auto":
            return cmd_auto(h, base, int(args[1]) if len(args) > 1 else 5)
        if cmd == "keep1":
            return cmd_keep1(h, base, int(args[1]) if len(args) > 1 else 0)
        if cmd == "human":
            return cmd_human(h, base)
        if cmd == "watch":
            return cmd_watch(h, base)
        print(f"未知命令 {cmd}\n" + __doc__)
        return 1

    # 交互菜单
    while True:
        print("\n" + "=" * 52)
        print(" 幕府2 AI 托管控制台（RE-B3 直写方案）")
        print("=" * 52)
        print(" 1) 状态探测（probe）")
        print(" 2) 全员 AI 托管 + 援军监控（all-ai）")
        print(" 3) 【自动托管】监测战斗自动接管（auto）")
        print(" 4) 保留单位0人控，其余AI（keep1）")
        print(" 5) 切回人控（human）")
        print(" 6) 仅援军监控（watch）")
        print(" 0) 退出")
        try:
            choice = input("选择: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if choice in ("2", "3", "4", "5", "6") and not build_guard(build, "menu-" + choice):
            continue
        if choice == "1":
            cmd_probe(h, base)
        elif choice == "2":
            cmd_all_ai(h, base)
        elif choice == "3":
            cmd_auto(h, base)
        elif choice == "4":
            cmd_keep1(h, base)
        elif choice == "5":
            cmd_human(h, base)
        elif choice == "6":
            cmd_watch(h, base)
        elif choice == "0":
            break
    print("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
