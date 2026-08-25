# -*- coding: utf-8 -*-
"""s2_autowatch.py — S2 原版「看海 + 战斗接入 + AI 自动作战」循环守护（2026-08-12）

用户需求：玩家执行完军事行动后主动触发看海（AI 接管自己），后台捕捉玩家派系参与的
AI 战斗 → 重新接入玩家（恢复人控）→ 战斗中自动注入（目标1 AI 托管）→ 战斗结束玩家
恢复控制（=「回合结束回来」的替代信号，因为引擎无「轮到玩家」的直接标志可读）。

流程状态机：
  WATCH（看海：+0x6a0=0 + manager=FULL_MANAGER）
    → 20ms 轮询冲突列表 [model+0x149c]：玩家 faction 参与（攻/防任一）
    → RESTORE（+0x6a0=1 + manager=HUMAN，抢在冲突结算前）
    → 等战斗加载（[base+0x1bc8180] battle manager ≠ 0 或 env/st 链可读）
    → BATTLE（后台线程全员 AI 托管 + 持续补写援军）
    → 战斗结束 → 玩家恢复控制，循环完成退出

原理（勿破坏，03 二十九/三十四节 + conf_army_report + 目标1/2）：
  - 战斗加载判定：主循环遍历冲突列表查 faction+0x6a0 → 有人类参战才加载战斗
  - AI 化玩家派系（+0x6a0=0）参与冲突会直接结算（无人类）→「恢复 +0x6a0=1」是重新接入开关
  - 冲突→军队→faction：entry[0x1c]→army+0x258→faction（一重或两重，vtable 校验）
  - conf 条目瞬态 ~0.5s → 20ms 轮询

用法（游戏运行中、S2 原版战役、玩家操作完）：
  python -u tools/s2_autowatch.py --watch [--faction 织田]   # 看海循环（核心）
  python -u tools/s2_autowatch.py --restore [--faction 织田] # 手动恢复人控
  python -u tools/s2_autowatch.py --probe                     # 只读：玩家 faction + 冲突列表概览

依赖：s2_watch（faction/manager 定位）、re_h46a（model 锚定）、re_b3_inject（战斗注入）、
      probe_battle_env / re_c2_faction。
"""
import argparse
import ctypes
import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb
import re_c2_faction as fc
import s2_watch as sw
import re_h46a as H46
import re_b3_inject as b3
import re_a3_probe as a3

FVTABLE_RVA = 0x15fac30          # faction vtable RVA
CONF_OBJ_VTABLE_RVA = 0x1606a08  # [model+0x149c] 冲突对象 vtable（37_HANDOFF §1.4 实机）
                                 # 结构：+0x1c=自身回指 / +0x58+0x5c=内嵌军队链表 / +0x258=faction
BATTLE_MGR_GLOBAL = 0x1bc8180    # battle manager 全局（战役=0/战斗≠0，03 三十三节）
CONF_OFF = 0x149c                # 冲突管理器偏移（re_h46a）
POLL = 0.02                      # 冲突轮询间隔（20ms < conf 瞬态 0.5s）
WATCH_SETTLE = 2.0               # AI 化后延迟（等引擎状态稳定再轮询，防误读垃圾）


def uid(p):
    return p is not None and 0x10000 < p < 0x80000000


def anchor_model(h, base):
    """正确锚定 model/pending（03 十九节 981 行，勿用 find_model 假象路线）：
    扫 pending vtable → [pending+0x4c] model 回指 → 校验 [model+0x14a4] 双向。
    981 行：model vtable 全内存扫描 0 个是假象（真 model 扫不到）→ 必须 pending 侧锚定。"""
    for p in a3.find_pending(h, base):
        if not (0x10000 < p < 0x80000000):
            continue
        if pb.read_u32(h, p) != base + a3.PENDING_VTABLE_RVA:
            continue
        m = pb.read_u32(h, p + 0x4c)          # model 回指在 +0x4c（勿用 +0x30）
        if not (0x10000 < m < 0x80000000):
            continue
        if pb.read_u32(h, m) == base + a3.MODEL_VTABLE_RVA and pb.read_u32(h, m + 0x14a4) == p:
            return m, p
    return None, None


def faction_of_army(h, base, army):
    """army+0x258 → faction 对象（一重或两重，vtable 校验）。"""
    if not uid(army):
        return None
    p1 = pb.read_u32(h, army + 0x258)
    if not uid(p1):
        return None
    if pb.read_u32(h, p1) == base + FVTABLE_RVA:   # 一重：p1 直接是 faction
        return p1
    p2 = pb.read_u32(h, p1)                        # 两重：p1 → faction
    if uid(p2) and pb.read_u32(h, p2) == base + FVTABLE_RVA:
        return p2
    return None


def player_in_conflict(h, base, model, player_faction):
    """轮询冲突列表 [model+0x149c]：玩家 faction 是否参与。
    ⚠️ 结构（37_HANDOFF §1.4，勿按 conf_army_report 的 entry+0x1c 读）：
      [model+0x149c]=冲突管理器；vector begin/end 在 +0x20/+0x24；
      node+8 = 冲突对象（vtable 0x1606a08），其 +0x1c=自身回指、+0x258=faction、
      +0x58/+0x5c=内嵌军队链表（节点{prev,next,obj}，+8=军队）。
    返回 (obj, army, faction) 或 None。"""
    mgr = pb.read_u32(h, model + CONF_OFF)
    if not uid(mgr):
        return None
    begin = pb.read_u32(h, mgr + 0x20)
    end = pb.read_u32(h, mgr + 0x24)
    node = begin
    for _ in range(64):
        if node == end or not uid(node):
            break
        obj = pb.read_u32(h, node + 8)
        if uid(obj) and pb.read_u32(h, obj) == base + CONF_OBJ_VTABLE_RVA:
            # 主军队 faction
            f = faction_of_army(h, base, obj)
            if f == player_faction:
                return (obj, obj, f)
            # 内嵌军队链表（节点 {prev,next,obj}，节点+8=军队）
            for off in (0x58, 0x5c):
                n = pb.read_u32(h, obj + off)
                if uid(n):
                    army2 = pb.read_u32(h, n + 8)
                    f2 = faction_of_army(h, base, army2)
                    if f2 == player_faction:
                        return (obj, army2, f2)
        node = pb.read_u32(h, node + 4)
    return None


def in_battle(h, base):
    """战斗加载检测：[base+0x1bc8180] battle manager ≠ 0。"""
    v = pb.read_u32(h, base + BATTLE_MGR_GLOBAL)
    return v is not None and v != 0


def locate_player(h, base, faction_name=None):
    """定位玩家 faction + manager 写点。返回 (faction_addr, manager_target, name)。"""
    facs = sw.scan_factions(h, base)
    target = None
    if faction_name:
        for a, h6, name, tr in facs:
            if name == faction_name:
                target = (a, h6, name, tr)
                break
    else:
        human = [x for x in facs if x[1] == 1]
        if human:
            target = human[0]
        elif facs:
            # 已 AI 化（无 human=1）：用第一个（需 --faction 才准）
            target = facs[0]
    if not target:
        print("✗ 未找到 faction（可能不在战役中）")
        return None, None, None
    a, h6, name, tr = target
    print(f"✓ faction {name!r} 0x{a:08x} human={h6} 国库={tr}")
    # manager 写点
    objs = sw.scan_objA(h, base, max_cands=10)
    if not objs:
        print("✗ manager 表未定位（scan_objA 0 候选）")
        return a, None, name
    objs.sort(key=lambda x: -x[3])
    objA, cnt, tbl, nh = objs[0]
    entry = sw.find_manager_entry(h, objA, tbl, cnt, a)
    if not entry:
        print("✗ 未找到该 faction 的 manager 条目")
        return a, None, name
    idx, key, m = entry
    mgr_tgt = tbl + idx * 8 + 4
    print(f"✓ manager[{idx}] m={m}({sw.MANAGERS.get(m,'?')}) 写点=0x{mgr_tgt:08x}")
    return a, mgr_tgt, name


def watch_loop(h, base, model, player_faction, manager_target, name):
    """看海 + 冲突轮询 + 恢复 + 战斗注入 + 战斗结束退出。"""
    # 1. 看海
    print("\n== 看海启动 ==")
    if not sw.do_watch(h, base, player_faction, manager_target):
        print("✗ 看海写入失败，中止")
        return 1
    print(f"看海已生效（+0x6a0=0 + FULL_MANAGER），{WATCH_SETTLE}s 后开始轮询冲突…", flush=True)
    time.sleep(WATCH_SETTLE)   # 等引擎状态稳定，防误读 AI 化瞬间的垃圾
    print(f"20ms 轮询冲突列表，等待 {name} 派系参战…（Ctrl+C 停止）", flush=True)

    try:
        while True:
            if in_battle(h, base):
                break
            hit = player_in_conflict(h, base, model, player_faction)
            if hit:
                entry, army, f = hit
                print(f"\n[{time.strftime('%H:%M:%S')}] 🎯 捕捉到 {name} 派系参战冲突"
                      f"（entry=0x{entry:08x} army=0x{army:08x}）——恢复人控…", flush=True)
                if not sw.do_restore(h, base, player_faction, manager_target):
                    print("✗ 恢复人控失败，继续轮询")
                    time.sleep(POLL)
                    continue
                # 2. 等战斗加载（恢复后引擎因人类参战处理冲突 → 加载）
                t0 = time.time()
                while time.time() - t0 < 8.0:
                    if in_battle(h, base):
                        break
                    time.sleep(0.02)
                if not in_battle(h, base):
                    print("⚠️ 战斗未在 8s 内加载（冲突可能已结算/恢复后进玩家回合）"
                          "——自动重新看海，等下一场冲突", flush=True)
                    if not sw.do_watch(h, base, player_faction, manager_target):
                        print("✗ 重新看海失败，中止")
                        return 1
                    time.sleep(WATCH_SETTLE)
                    continue
                print(f"[{time.strftime('%H:%M:%S')}] ✅ 战斗已加载，注入 AI 托管…", flush=True)
                # 3. 战斗 AI 托管（后台线程：全员 AI + 补写援军）
                stop = threading.Event()
                t = threading.Thread(target=battle_ai_worker, args=(h, base, stop),
                                     daemon=True)
                t.start()
                # 4. 等战斗结束
                while in_battle(h, base):
                    time.sleep(0.5)
                stop.set()
                t.join(timeout=2)
                print(f"[{time.strftime('%H:%M:%S')}] 🏁 战斗结束，{name} 派系已恢复人控。"
                      f"看海循环完成（可用 --watch 再次看海）")
                return 0
            time.sleep(POLL)
    except KeyboardInterrupt:
        print("\n已停止。玩家派系当前 human=", pb.read_u8(h, player_faction + 0x6a0),
              "——如需恢复人控：--restore")
        return 0


def battle_ai_worker(h, base, stop):
    """战斗 AI 托管：全员 AI 接管 + 持续补写援军（参考 s2_ai_ctl.cmd_auto）。"""
    while not stop.is_set():
        mgr, env, e8, st = b3.resolve_e8(h, base)
        if st:
            state = pb.read_u8(h, st + b3.OFF_STATE)
            if state is not None and state >= 5:
                groups = b3.walk_groups(h, st)
                if groups:
                    total = 0
                    for gi, (g, acnt, atbl) in enumerate(groups):
                        for ai in range(acnt):
                            total += b3._apply_ai_army(h, atbl, ai, "ai")
                    b3.w8(h, st + b3.ST_SWITCHED, 1)
                    if total:
                        print(f"  [AI托管] 补写 {total} 处", flush=True)
        time.sleep(0.5)


def cmd_probe(h, base):
    model, pending = anchor_model(h, base)
    print(f"model=0x{model:08x} pending=0x{pending:08x}" if model else "model 未锚定（可能不在战役）")
    facs = sw.scan_factions(h, base)
    print(f"faction {len(facs)} 个（human=1 见下）：")
    for a, h6, name, tr in facs:
        if h6 == 1:
            print(f"  ★ 0x{a:08x} {name!r} 国库={tr}")
    if model:
        mgr = pb.read_u32(h, model + CONF_OFF)
        print(f"冲突管理器 [model+0x149c]=0x{mgr:08x}" if uid(mgr) else "冲突管理器空")
    return 0


def main():
    ap = argparse.ArgumentParser(description="s2_autowatch — 看海循环守护（S2 原版）")
    ap.add_argument("--watch", action="store_true", help="看海循环：AI化→捕捉冲突→恢复→战斗注入→恢复玩家")
    ap.add_argument("--restore", action="store_true", help="手动恢复人控")
    ap.add_argument("--probe", action="store_true", help="只读探测（faction/冲突列表）")
    ap.add_argument("--faction", default=None, help="派系中文名（默认自动选 human=1）")
    args = ap.parse_args()

    h, base = fc.open_game()
    if not h:
        sys.exit(1)
    try:
        if args.probe:
            return cmd_probe(h, base)
        a, mgr_tgt, name = locate_player(h, base, args.faction)
        if not a:
            sys.exit(2)
        if args.restore:
            if not mgr_tgt:
                print("✗ 无 manager 写点，无法完整恢复（只能 +0x6a0=1）")
            ok = sw.do_restore(h, base, a, mgr_tgt) if mgr_tgt else False
            if not mgr_tgt:
                ok = fc.write_byte(h, a + 0x6a0, 1)
            print("✅ 已恢复人控" if ok else "⚠️ 恢复失败")
            return 0 if ok else 3
        if args.watch:
            if not mgr_tgt:
                print("✗ 无 manager 写点，无法看海（--probe 诊断）")
                return 4
            model, pending = anchor_model(h, base)
            if not model:
                print("✗ model 未锚定（看海需要冲突检测，需在战役中）")
                return 5
            return watch_loop(h, base, model, a, mgr_tgt, name)
        ap.print_help()
        return 0
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


if __name__ == "__main__":
    raise SystemExit(main())
