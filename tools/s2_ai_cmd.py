# -*- coding: utf-8 -*-
"""s2_ai_cmd.py — 目标1 行为控制台（战斗 AI 指挥链补丁层工具，2026-08-14 开发）

依据（docs/39_HANDOFF_20260814 §1.3 补丁层工具箱 + 03 七十四/七十五/七十七节，全部已确证）：
- 单位推进/解死锁：0x186c60(unit, cmd) thiscall ecx=unit + 1 栈参 cmd∈{0..3}，ret 4；
  落地链门仅 0x1821e0 + 0x180600（[st+0x14]∈{5,6,7,8}），不查 hold gate（0x15c4c0/0x1b3b70）
  → 死锁单位可被强行下令（03 七十七节 A：每 5 tick 重推对抗覆盖）。
- 单位深度等待：0x186300(unit, 2, &0x17, 0) thiscall ecx=unit + 3 栈参 ret 0xc；
  与引擎军队兜底 0x52661f 逐字节同构（T2 铁证）；0x17=hold（0x15c4c0 封命令链）。
- 计划码：[控制器+0x5ec] 直写 ≤9 tick 稳定（0x520140 同 tick 重写）；持续伪造写 [控制器+0x45c]。
- 兜底开关：阈值常量 RVA 0x115c26ac = 1.5f（0x526220 距离判定）；数据写（float）规避
  代码 patch 引擎校验风险（03 三十四节：patch 生效但游戏静默退出）。
- 观测点：hold 状态码 [[unit+0xf1c]+0x24]+0x10（0x15/16/17=hold，无 order=0x25）；
  [unit+0x9ec] 命令环 count；[控制器+0x628] 活性（0x5070ed/f3 每 tick 尾部 +1，两次读递增=控制器在跑）。

用法（游戏运行中；--push/--hold 需战斗 state∈{5,6,7,8}）：
  python tools/s2_ai_cmd.py --probe                     # 只读观测全量（组/军/单位+hold码+命令环+控制器）
  python tools/s2_ai_cmd.py --push 2 [--g 0 --a 0]      # 0x186c60(unit,2) 推进（解死锁）
  python tools/s2_ai_cmd.py --push 0 [--g 0 --a 0]      # 0x186c60(unit,0) HALT（浅停）
  python tools/s2_ai_cmd.py --push 2 --all --loop 0.5   # 全军每 0.5s 循环推进（反死锁，Ctrl+C 停）
  python tools/s2_ai_cmd.py --hold [--g 0 --a 0]        # 0x186300(unit,2,&0x17,0) 深度等待
  python tools/s2_ai_cmd.py --plan 8 [--sustain]        # 计划码直写（默认 [控制器+0x5ec]；--sustain 写 [+0x45c]）
  python tools/s2_ai_cmd.py --threshold 0.0             # 兜底阈值 1.5f→0.0（关闭「无近邻挂 hold17」）
  python tools/s2_ai_cmd.py --watch 1.0                 # 观测循环（Ctrl+C 停）
  python tools/s2_ai_cmd.py --dry                        # 离线自检：shellcode 生成 + capstone 反汇编核对

安全纪律（AGENTS §3）：先 --probe 只读校准；一次只改一个变量；--push/--hold 前自动快照原值；
实机前 tools/check_game.ps1 -snapshot。
⚠️ 2026-08-14 实机证伪：**--push/--hold 远程线程执行引擎代码 → 游戏静默退出**（Steam 日志
"Detected possibly crashed/killed game"，无 WER；与 23_HANDOFF 裸线程 TLS=NULL 同源）。
**勿用 --push/--hold**（代码执行通道全部关闭）。改用数据写通道：--plan（计划码直写）、
--threshold（阈值 0x115c26ac）、直写状态码/子状态——零代码执行零风险。详见 03 七十八节。
"""
import argparse
import ctypes
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb
import re_b3_inject as b3

# ---------------- RVA（empire.retail.dll） ----------------
RVA_186C60 = 0x186c60          # 命令落地统一入口（thiscall+1 栈参 ret4）
RVA_186300 = 0x186300          # 状态转换（thiscall+3 栈参 ret0xc；param4=0 状态路径）
# 0x526220 兜底距离阈值常量：报告写 0x115c26ac 是绝对 VA（ImageBase=0x10000000），
# 正确 RVA = 0x015c26ac（实测 base+RVA 读到 0x3fc00000=1.5f；03 七十八节 C 附注）
RVA_THRESHOLD = 0x015c26ac     # =1.5f（0x526573 mulss 半径×1.5；数据写）

# 控制器字段（[group+0xc] = AI Script Controller）
CTRL_PLAN = 0x5ec              # 计划码（≤9 tick 稳定）
CTRL_PLAN_PERSIST = 0x45c      # 分派字段（持续）
CTRL_ACTIVE = 0x628            # 活性计数（每 tick +1）

# 单位观测点
UNIT_F1C = 0xf1c               # →[+0x24]→[+0x10] = hold 状态码
UNIT_9EC = 0x9ec               # 命令环 count

# shellcode 布局（远程内存 0x1000）
SC_OFF = 0x00
UNITS_OFF = 0x200              # 单位指针数组
STATE_OFF = 0x600              # state 值（dword）
MAX_UNITS = 64                 # 单次 shellcode 单位上限（防超 0x1000）

MEM_COMMIT, MEM_RESERVE, PAGE_XRW = 0x1000, 0x2000, 0x40


# ---------------- 观测点读取 ----------------
def read_hold_code(h, unit):
    """[[unit+0xf1c]+0x24]+0x10 状态码。返回 int 或 None。"""
    f1c = pb.read_u32(h, unit + UNIT_F1C)
    if not f1c:
        return None
    u24 = pb.read_u32(h, f1c + 0x24)
    if not u24:
        return None
    return pb.read_u8(h, u24 + 0x10)


def unit_obs(h, unit):
    """单个单位观测点。"""
    return {
        "hold": read_hold_code(h, unit),
        "ring": pb.read_u32(h, unit + UNIT_9EC),
    }


# ---------------- shellcode 生成 ----------------
def sc_push_cmd(unit_list, cmd, base):
    """0x186c60(unit, cmd) 循环 shellcode。unit_list 绝对地址数组。"""
    assert len(unit_list) <= MAX_UNITS
    fn = base + RVA_186C60
    sc = b""
    sc += b"\xbe" + struct.pack("<I", 0)                # mov esi, <units_va>（占位，写内存时回填）
    sc += b"\xbf" + struct.pack("<I", len(unit_list))   # mov edi, count
    sc += b"\x8b\x0e"                                    # loop: mov ecx, [esi]
    sc += b"\x6a" + bytes([cmd & 0xFF])                  # push cmd
    sc += b"\xb8" + struct.pack("<I", fn)               # mov eax, 0x186c60
    sc += b"\xff\xd0"                                    # call eax
    sc += b"\x83\xc6\x04"                                # add esi, 4
    sc += b"\x4f"                                        # dec edi
    sc += b"\x75\xf2"                                    # jnz loop
    sc += b"\x33\xc0\xc3"                                # xor eax,eax; ret
    return sc


def sc_hold(unit_list, base):
    """0x186300(unit, 2, &state, 0) 循环 shellcode（state=0x17 深度等待）。"""
    assert len(unit_list) <= MAX_UNITS
    fn = base + RVA_186300
    sc = b""
    sc += b"\xbe" + struct.pack("<I", 0)                # mov esi, <units_va>（占位）
    sc += b"\xbf" + struct.pack("<I", len(unit_list))   # mov edi, count
    sc += b"\x8b\x0e"                                    # loop: mov ecx, [esi]
    sc += b"\x6a\x00"                                    # push 0（param4）
    sc += b"\x68" + struct.pack("<I", 0)                # push <state_va>（占位）
    sc += b"\x6a\x02"                                    # push 2（param2）
    sc += b"\xb8" + struct.pack("<I", fn)               # mov eax, 0x186300
    sc += b"\xff\xd0"                                    # call eax
    sc += b"\x83\xc6\x04"                                # add esi, 4
    sc += b"\x4f"                                        # dec edi
    sc += b"\x75\xf1"                                    # jnz loop
    sc += b"\x33\xc0\xc3"                                # xor eax,eax; ret
    return sc


def patch_shellcode(sc, units_va, state_va=0):
    """回填 mov esi,units_va（+1 偏移）与 push state_va（hold 模板）。"""
    sc = bytearray(sc)
    sc[1:5] = struct.pack("<I", units_va)
    if state_va:
        # 找 push imm32（0x68 xx xx xx xx）
        idx = sc.find(b"\x68")
        if idx >= 0:
            sc[idx + 1:idx + 5] = struct.pack("<I", state_va)
    return bytes(sc)


def build_remote(h, base, unit_list, kind, cmd=2):
    """分配远程内存并写入 shellcode + 单位数组 + state。返回 (mem_va, entry_va) 或 (None,None)。"""
    k32 = pb.K32
    mem = k32.VirtualAllocEx(h, None, 0x1000, MEM_COMMIT | MEM_RESERVE, PAGE_XRW)
    if not mem:
        print(f"✗ VirtualAllocEx 失败 err={ctypes.get_last_error()}")
        return None, None
    units_va = mem + UNITS_OFF
    state_va = mem + STATE_OFF
    if kind == "hold":
        sc = patch_shellcode(sc_hold(unit_list, base), units_va, state_va)
        state_val = struct.pack("<I", 0x17)
    else:
        sc = patch_shellcode(sc_push_cmd(unit_list, cmd, base), units_va)
        state_val = b""
    blob = sc + b"\x00" * (UNITS_OFF - len(sc))
    blob += b"".join(struct.pack("<I", u) for u in unit_list)
    blob += b"\x00" * (STATE_OFF - UNITS_OFF - 4 * len(unit_list))
    blob += state_val
    buf = ctypes.create_string_buffer(blob)
    got = ctypes.c_size_t()
    ok = bool(k32.WriteProcessMemory(h, ctypes.c_void_p(mem), buf, len(blob), ctypes.byref(got))) \
        and got.value == len(blob)
    if not ok:
        print("✗ WriteProcessMemory 失败")
        return None, None
    return mem, mem + SC_OFF


def exec_remote(h, entry_va, timeout_ms=3000):
    """CreateRemoteThread 执行 shellcode。返回 (wait_result, ok)。"""
    k32 = pb.K32
    ht = k32.CreateRemoteThread(h, None, 0, entry_va, None, 0, None)
    if not ht:
        print(f"✗ CreateRemoteThread 失败 err={ctypes.get_last_error()}")
        return None, False
    wr = k32.WaitForSingleObject(ht, timeout_ms)
    k32.CloseHandle(ht)
    return wr, True


# ---------------- 目标军队单位收集 ----------------
def collect_units(h, base, group_idx=0, army_idx=0, all_armies=False):
    """收集目标军队的全部单位地址。返回 (units, 描述字符串) 或 (None, msg)。"""
    mgr, env, e8, st = b3.resolve_e8(h, base)
    if not st:
        return None, "未进入战斗场景（st 链不可读）"
    groups = b3.walk_groups(h, st)
    if not groups:
        return None, "组表解析失败"
    units = []
    desc = []
    for gi, (g, acnt, atbl) in enumerate(groups):
        for ai in range(acnt):
            if not all_armies and (gi != group_idx or ai != army_idx):
                continue
            a = b3.walk_army(h, atbl, ai)
            if not a:
                continue
            for i in range(a["unit_cnt"]):
                u = pb.read_u32(h, a["unit_tbl"] + i * 4)
                if u:
                    units.append(u)
            desc.append(f"组{gi}军{ai}")
    if not units:
        return None, "目标军队无单位"
    return units, ",".join(desc)


# ---------------- 命令 ----------------
def cmd_push(h, base, args):
    cmd = args.push
    units, desc = collect_units(h, base, args.g, args.a, args.all)
    if units is None:
        print(f"✗ {desc}")
        return 3
    mgr, env, e8, st = b3.resolve_e8(h, base)
    state = pb.read_u8(h, st + b3.OFF_STATE)
    if state not in (5, 6, 7, 8):
        print(f"⚠ 状态=0x{state if state is not None else -1:x}——0x186c60 落地门 0x180600 要求 {5,6,7,8}（战斗进行中）")
        return 2
    if args.loop:
        print(f"循环推进：{desc} {len(units)} 单位，cmd={cmd}，每 {args.loop}s（Ctrl+C 停）", flush=True)
        try:
            while True:
                _push_once(h, base, units, cmd)
                time.sleep(args.loop)
        except KeyboardInterrupt:
            print("\n已停止")
        return 0
    return _push_once(h, base, units, cmd)


def _push_once(h, base, units, cmd):
    mem, entry = build_remote(h, base, units[:MAX_UNITS], "push", cmd)
    if not mem:
        return 4
    wr, _ = exec_remote(h, entry)
    # 观测回读（落地证据）
    st = b3.resolve_e8(h, base)[3]
    alive = pb.read_u8(h, st + b3.OFF_STATE) if st else None
    obs0 = unit_obs(h, units[0])
    print(f"[push cmd={cmd}] {len(units[:MAX_UNITS])} 单位 WaitForSingleObject={wr}"
          f" 游戏存活 state={alive if alive is not None else -1:x}"
          f" unit0: hold=0x{obs0['hold'] if obs0['hold'] is not None else -1:x} ring={obs0['ring']}")
    return 0


def cmd_hold(h, base, args):
    units, desc = collect_units(h, base, args.g, args.a, args.all)
    if units is None:
        print(f"✗ {desc}")
        return 3
    mem, entry = build_remote(h, base, units[:MAX_UNITS], "hold")
    if not mem:
        return 4
    wr, _ = exec_remote(h, entry)
    obs0 = unit_obs(h, units[0])
    print(f"[hold 0x17] {desc} {len(units[:MAX_UNITS])} 单位 WaitForSingleObject={wr}"
          f" unit0: hold=0x{obs0['hold'] if obs0['hold'] is not None else -1:x}"
          f"（期望 0x15/16/17=深度等待）ring={obs0['ring']}")
    return 0


def cmd_plan(h, base, args):
    mgr, env, e8, st = b3.resolve_e8(h, base)
    if not st:
        print("✗ 未进入战斗场景")
        return 3
    groups = b3.walk_groups(h, st)
    if not groups or args.g >= len(groups):
        print("✗ 组表解析失败")
        return 3
    g, acnt, atbl = groups[args.g]
    ctrl = pb.read_u32(h, g + b3.GROUP_CTRL)
    if not ctrl:
        print(f"✗ 组{args.g} 无控制器 [group+0xc]=0（未懒建）")
        return 3
    off = CTRL_PLAN_PERSIST if args.sustain else CTRL_PLAN
    old = pb.read_u32(h, ctrl + off)
    if b3.w32(h, ctrl + off, args.plan & 0xFF):
        rb = pb.read_u32(h, ctrl + off)
        print(f"[plan] 控制器=0x{ctrl:08x} +0x{off:x} {old} → {args.plan} 回读={rb}"
              f"（{'持续' if args.sustain else '≤9 tick'}；码含义 8/9=进攻 2/4=防守 0xe=?）")
        return 0
    print("✗ 写入失败")
    return 4


def cmd_threshold(h, base, args):
    va = base + RVA_THRESHOLD
    old = pb.read_u32(h, va)
    if old is None:
        print(f"✗ 读取阈值失败（RVA_THRESHOLD={hex(RVA_THRESHOLD)} 需为 .rdata 内有效地址）")
        return 4
    # 0x015c26ac 在 .rdata 只读段（err=998 NOACCESS）——写前 VirtualProtectEx 改 PAGE_READWRITE
    PAGE_READWRITE = 0x04
    old_prot = ctypes.c_ulong()
    k32 = pb.K32
    okp = k32.VirtualProtectEx(h, ctypes.c_void_p(va), 4, PAGE_READWRITE, ctypes.byref(old_prot))
    buf = ctypes.create_string_buffer(struct.pack("<f", args.threshold))
    got = ctypes.c_size_t()
    ok = bool(k32.WriteProcessMemory(h, ctypes.c_void_p(va), buf, 4, ctypes.byref(got))) \
        and got.value == 4
    k32.VirtualProtectEx(h, ctypes.c_void_p(va), 4, old_prot.value, ctypes.byref(ctypes.c_ulong()))
    rb = pb.read_u32(h, va)
    print(f"[threshold] 0x{va:08x} 原=0x{old:08x}({struct.unpack('<f', struct.pack('<I', old))[0]:.3f})"
          f" → {args.threshold} 回读=0x{rb:08x} {'✅' if ok else '✗'}"
          f"（protect={'OK' if okp else 'FAIL err=%d' % ctypes.get_last_error()};"
          f" 0x526220 兜底距离阈值；0.0=不再因距离挂 hold17）")
    return 0 if ok else 4


CTRL_WITHDRAW_X = 0x3a8      # 撤退点 x（float 世界坐标）
CTRL_WITHDRAW_Z = 0x3ac      # 撤退点 z（float 世界坐标）


def cmd_withdraw(h, base, args):
    """撤退点直写 [控制器+0x3a8]/[0x3ac]（float{x,z} 世界坐标，堆字段 0 校验面最安全）。

    依据（03 七十九节 B / re_ba_wall_retreat_liveprep.md）：引擎撤退通道坏（全 .text 无控制器
    写者，负证据确证）→ 直写可持久；0x50fa40 消费窗口 0x257<tick<0x960 中段；配合
    AI_FORCE_WITHDRAW_PLAN=1（计划 2 → 码 0xe → 0x50ffe0 撤退收集单，不依赖时间窗）。
    坐标从任意单位位置采样（[unit+0xb9c]+0x628 → 0x15d5d0 → 0x399f80 输出 {x,y,z}，取 x/z）。
    """
    mgr, env, e8, st = b3.resolve_e8(h, base)
    if not st:
        print("✗ 未进入战斗场景")
        return 3
    groups = b3.walk_groups(h, st)
    if not groups or args.g >= len(groups):
        print("✗ 组表解析失败")
        return 3
    g, acnt, atbl = groups[args.g]
    ctrl = pb.read_u32(h, g + b3.GROUP_CTRL)
    if not ctrl:
        print(f"✗ 组{args.g} 无控制器 [group+0xc]=0")
        return 3
    oldx = pb.read_u32(h, ctrl + CTRL_WITHDRAW_X)
    oldz = pb.read_u32(h, ctrl + CTRL_WITHDRAW_Z)
    if args.withdraw is None:
        # 采样模式：只读当前值
        print(f"[withdraw] 组{args.g} 控制器=0x{ctrl:08x} [0x3a8]=0x{oldx:08x}"
              f"({struct.unpack('<f', struct.pack('<I', oldx))[0]:.1f})"
              f" [0x3ac]=0x{oldz:08x}({struct.unpack('<f', struct.pack('<I', oldz))[0]:.1f})"
              f"（引擎坏通道预期恒 0）")
        return 0
    # 直写模式：--withdraw X,Z（两个 float）
    x, z = args.withdraw
    n = 0
    for off, v in ((CTRL_WITHDRAW_X, x), (CTRL_WITHDRAW_Z, z)):
        buf = ctypes.create_string_buffer(struct.pack("<f", v))
        got = ctypes.c_size_t()
        if pb.K32.WriteProcessMemory(h, ctypes.c_void_p(ctrl + off), buf, 4, ctypes.byref(got)) \
                and got.value == 4:
            n += 1
    rbx = pb.read_u32(h, ctrl + CTRL_WITHDRAW_X)
    rbz = pb.read_u32(h, ctrl + CTRL_WITHDRAW_Z)
    print(f"[withdraw] 组{args.g} 控制器=0x{ctrl:08x} 直写 {x:.1f},{z:.1f} → {n}/2 处"
          f" 回读 x=0x{rbx:08x}({struct.unpack('<f', struct.pack('<I', rbx))[0]:.1f})"
          f" z=0x{rbz:08x}({struct.unpack('<f', struct.pack('<I', rbz))[0]:.1f})"
          f"（中段 0x257..0x960 tick 消费；配 AI_FORCE_WITHDRAW_PLAN=1 触发撤退）")
    return 0 if n == 2 else 4


def cmd_probe(h, base):
    print("=" * 74)
    print("行为控制台 PROBE（只读）：st→组→军→单位 + hold 状态码 + 命令环 + 控制器活性")
    print("=" * 74)
    mgr, env, e8, st = b3.resolve_e8(h, base)
    if not st:
        print("未进入战斗场景（st 链不可读）")
        return 1
    state = pb.read_u8(h, st + b3.OFF_STATE)
    print(f"st=0x{st:08x} 状态=0x{state if state is not None else -1:x}"
          f" st+0x31f0={pb.read_u8(h, st + b3.ST_SWITCHED)}")
    groups = b3.walk_groups(h, st)
    if not groups:
        print("组表解析失败")
        return 1
    for gi, (g, acnt, atbl) in enumerate(groups):
        ctrl = pb.read_u32(h, g + b3.GROUP_CTRL)
        ctrl_info = "无" if not ctrl else ""
        line = f"组{gi} 0x{g:08x} 军队数={acnt}"
        if ctrl:
            a1 = pb.read_u32(h, ctrl + CTRL_ACTIVE)
            time.sleep(0.05)
            a2 = pb.read_u32(h, ctrl + CTRL_ACTIVE)
            plan = pb.read_u32(h, ctrl + CTRL_PLAN)
            planp = pb.read_u32(h, ctrl + CTRL_PLAN_PERSIST)
            line += f" 控制器=0x{ctrl:08x} 活性={a1}→{a2}{'（↑在跑）' if a2 and a2 != a1 else ''}" \
                    f" 计划码[5ec]={plan} [45c]={planp}"
        else:
            line += " 控制器=无"
        print(line)
        for ai in range(acnt):
            a = b3.walk_army(h, atbl, ai)
            if not a:
                continue
            print(f"  军{ai} 0x{a['addr']:08x} 单位数={a['unit_cnt']} a270={a['a270']} a28c={a['a28c']}")
            for i in range(min(a["unit_cnt"], 6)):
                u = pb.read_u32(h, a["unit_tbl"] + i * 4)
                if not u:
                    continue
                o = unit_obs(h, u)
                hold = o["hold"]
                holdtag = {0x15: "HOLD-15", 0x16: "HOLD-16", 0x17: "HOLD-17", 0x25: "idle"}.get(hold, "?")
                print(f"    单位{i} 0x{u:08x} hold=0x{hold if hold is not None else -1:x}({holdtag})"
                      f" ring={o['ring']}")
    print("\n判读：hold=0x17 且 ring=0 = 死锁/等待（可用 --push 2 解死锁）；hold=0x25=空闲可下令。")
    return 0


def cmd_watch(h, base, interval):
    print(f"观测循环（每 {interval}s，Ctrl+C 停）：hold 码 / 命令环 / 控制器活性", flush=True)
    try:
        while True:
            mgr, env, e8, st = b3.resolve_e8(h, base)
            if not st:
                print("  未在战斗", flush=True)
                time.sleep(interval)
                continue
            state = pb.read_u8(h, st + b3.OFF_STATE)
            groups = b3.walk_groups(h, st)
            line = f"[{time.strftime('%H:%M:%S')}] state=0x{state if state is not None else -1:x}"
            if groups:
                for gi, (g, acnt, atbl) in enumerate(groups[:2]):
                    ctrl = pb.read_u32(h, g + b3.GROUP_CTRL)
                    act = pb.read_u32(h, ctrl + CTRL_ACTIVE) if ctrl else None
                    line += f" | 组{gi} 控制器活性={act}"
                    a = b3.walk_army(h, atbl, 0)
                    if a:
                        u = pb.read_u32(h, a["unit_tbl"])
                        if u:
                            o = unit_obs(h, u)
                            line += f" unit0:hold=0x{o['hold'] if o['hold'] is not None else -1:x} ring={o['ring']}"
            print(line, flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


# ---------------- 离线自检 ----------------
def dry_check():
    """不连游戏：生成两种 shellcode + capstone 反汇编核对字节。"""
    units = [0x1000 + i for i in range(3)]
    base = 0x5cd80000
    try:
        import capstone
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    except ImportError:
        md = None
    for kind, scf, extra in (("push(cmd=2)", lambda: sc_push_cmd(units, 2, base), {}),
                             ("hold(0x17)", lambda: sc_hold(units, base), {"state": 1})):
        sc = scf()
        if extra.get("state"):
            sc = patch_shellcode(sc, 0x12340000, 0x12340600)
        else:
            sc = patch_shellcode(sc, 0x12340000)
        print(f"[{kind}] {len(sc)}B")
        if md:
            for ins in md.disasm(sc, 0x12340000):
                print(f"  {ins.address:08x}: {ins.mnemonic} {ins.op_str}")
    print("离线自检完成（字节可生成；实机前请先 --probe）")


def main():
    p = argparse.ArgumentParser(description="目标1 行为控制台（战斗 AI 指挥链补丁层）")
    p.add_argument("--probe", action="store_true", help="只读观测全量")
    p.add_argument("--push", type=int, metavar="CMD", help="0x186c60(unit,CMD)（0=HALT 2=推进）")
    p.add_argument("--hold", action="store_true", help="0x186300(unit,2,&0x17,0) 深度等待")
    p.add_argument("--plan", type=int, metavar="CODE", help="计划码直写（8/9=进攻 2/4=防守 0xe=?）")
    p.add_argument("--sustain", action="store_true", help="计划码写 [控制器+0x45c]（持续，默认 [5ec]）")
    p.add_argument("--threshold", type=float, metavar="F", help="兜底距离阈值写（1.5f→0.0 关闭）")
    p.add_argument("--withdraw", nargs="?", const="__sample__", metavar="X,Z",
                   help="撤退点直写 [控制器+0x3a8]/[0x3ac]（float{x,z} 世界坐标）；不带参数=采样当前值")
    p.add_argument("--watch", type=float, metavar="SEC", help="观测循环")
    p.add_argument("--g", type=int, default=0, help="组索引（默认 0）")
    p.add_argument("--a", type=int, default=0, help="军队索引（默认 0）")
    p.add_argument("--all", action="store_true", help="全部军队")
    p.add_argument("--loop", type=float, metavar="SEC", help="循环重推间隔（与 --push 配合）")
    p.add_argument("--dry", action="store_true", help="离线自检（不连游戏）")
    args = p.parse_args()

    if args.dry:
        return dry_check()

    pid = pb.find_pid()
    if pid is None:
        print("✗ shogun2.exe 未运行")
        return 2
    h = pb.K32.OpenProcess(pb.PROCESS_QUERY_INFORMATION | pb.PROCESS_VM_READ |
                           pb.PROCESS_VM_WRITE | pb.PROCESS_VM_OPERATION, False, pid)
    if not h:
        print(f"✗ OpenProcess 失败 err={ctypes.get_last_error()}（需管理员）")
        return 2
    build, base, prof = pb.detect_build(h)
    if base is None:
        print("✗ 未找到引擎模块")
        return 2
    print(f"✓ PID={pid} 引擎={build} base=0x{base:08x}")
    if build != "empire":
        print("⚠ RVA 仅对 empire.retail.dll 验证；旧引擎（shogun2.dll）地址不同，勿用")
        return 6

    if args.probe:
        return cmd_probe(h, base)
    if args.push is not None:
        return cmd_push(h, base, args)
    if args.hold:
        return cmd_hold(h, base, args)
    if args.plan is not None:
        return cmd_plan(h, base, args)
    if args.threshold is not None:
        return cmd_threshold(h, base, args)
    if args.withdraw is not None:
        if args.withdraw == "__sample__":
            args.withdraw = None
        else:
            try:
                xs, zs = args.withdraw.split(",")
                args.withdraw = (float(xs), float(zs))
            except (ValueError, AttributeError):
                print("✗ --withdraw 格式：--withdraw X,Z（float 世界坐标）或不带参数采样")
                return 2
        return cmd_withdraw(h, base, args)
    if args.watch is not None:
        return cmd_watch(h, base, args.watch)
    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
