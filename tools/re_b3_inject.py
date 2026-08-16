# -*- coding: utf-8 -*-
"""re_b3_inject.py — RE-B3 BCQ 注入：正常战斗（battle_ai=0）单军队切 AI。

依据（work/re_b3_notes.md，2026-08-09 静态 RE 已确证）：
- BNCQ_ARMY_ORDER_SWITCH_AI handler = 0x2abeb0，签名 FUN_102abeb0(reader, e8)：
  e8 = [[base+0x1bc8180]+0x110+8]；st = [e8+0xb4]。
- param 流 = [0x02][group_idx][0x02][army_idx]（0x308020 游标式解码：字段=[1 字节 tag][数据]）。
- reader 结构：+0x04=解码错误标志(0)、+0x08=流指针、+0x0c=0、+0x10=流长、+0x14=游标(0)。
- handler 行为：懒建 [group+0xc] 控制器；Loop1 单位 [unit+0xea8]=1/+0xc01=1；
  Loop2 子对象 [sub+0x1168]=1/+0x1160=1；[st+0x31f0]=1；army[0x28c]=1/[0x290]=1.0f/[0x294]=-1。
- 无 DLL 内发送方 → 直调 handler 等价于调度器（构造 reader → 查表 → 调 handler）。

用法（一次一个变量；battle_ai 必须先清 0；先 --probe 校准再 --switch-ai）：
  python -u tools/re_b3_inject.py --probe                   # 只读：全链校验（st/组/军/单位）+ dump
  python -u tools/re_b3_inject.py --switch-ai --group 0 --army 0   # 注入切 AI（默认 0/0）
  python -u tools/re_b3_inject.py --verify [--group N --army M]    # 回读验证（注入后）
  python -u tools/re_b3_inject.py --switch-human [--group N --army M]  # 切回人控（直接写复位）

安全纪律（AGENTS）：每次写入前重验结构；验证失败禁止注入；先 --probe 校准通过才允许 --switch-ai；
注入时机 = 战斗部署后（st+0x14 ∈ {3,5}）。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb

# ---------------- 锚点链（RVA，base 实取） ----------------
RVA_MGR = 0x1bc8180
OFF_ENV = 0x110              # [mgr+0x110] = env
OFF_E8 = 8                   # [env+8] = e8（handler arg2）
OFF_ST = 0xb4                # [e8+0xb4] = st（战斗对象）
OFF_STATE = 0x14             # [st+0x14] = 状态 1/2/3/5

# st 单位组表（handler 0x2abeb0 用）
ST_GROUP_CNT = 0x88          # [st+0x88] = 组数
ST_GROUP_TBL = 0x8c          # [st+0x8c] = 组表（指针数组）
ST_SWITCHED = 0x31f0         # [st+0x31f0] = 已切 AI 标志 byte

# group
GROUP_CTRL = 0xc             # [group+0xc] = 战斗控制器（懒创建；注入后应非 0）
GROUP_ARMY_CNT = 0x20        # [group+0x20] = 军队数
GROUP_ARMY_TBL = 0x24        # [group+0x24] = 军队表（指针数组）

# army（battle army，与 Chain B 的槽军队对象不同）
ARMY_270 = 0x270             # byte（handler 写 0）
ARMY_28C = 0x28c             # dword（handler 写 1）
ARMY_290 = 0x290             # float（handler 写 1.0f）
ARMY_294 = 0x294             # dword（handler 写 -1）
ARMY_UNIT_CNT = 0x114        # [army+0x114] = 单位数
ARMY_UNIT_TBL = 0x118        # [army+0x118] = 单位表（指针数组）
ARMY_CHILD_CNT = 0x12c       # [army+0x12c] = 子对象数
ARMY_CHILD_TBL = 0x130       # [army+0x130] = 子对象表

# unit / child
UNIT_EA8 = 0xea8             # dword 控制标志（1=AI）
UNIT_C01 = 0xc01             # byte
CHILD_1168 = 0x1168          # dword
CHILD_1160 = 0x1160          # byte

# 合理性界
GRP_MIN, GRP_MAX = 0, 16
ARM_MIN, ARM_MAX = 0, 32
UNT_MIN, UNT_MAX = 0, 256

HANDLER_SWITCH_AI = 0x2abeb0

SNAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "b3_orig.json")


def w8(h, a, v):
    buf = ctypes.create_string_buffer(bytes([v & 0xFF]))
    got = ctypes.c_size_t()
    ok = pb.K32.WriteProcessMemory(h, ctypes.c_void_p(a), buf, 1, ctypes.byref(got))
    return bool(ok) and got.value == 1


def w32(h, a, v):
    buf = ctypes.create_string_buffer(int(v).to_bytes(4, "little"))
    got = ctypes.c_size_t()
    ok = pb.K32.WriteProcessMemory(h, ctypes.c_void_p(a), buf, 4, ctypes.byref(got))
    return bool(ok) and got.value == 4


def resolve_e8(h, base):
    """解析 BCQ 锚点链。返回 (mgr, env, e8, st) 或 (None,)*4。"""
    mgr = pb.read_u32(h, base + RVA_MGR)
    if not mgr:
        return (None,) * 4
    env = pb.read_u32(h, mgr + OFF_ENV)
    if not env:
        return (None,) * 4
    e8 = pb.read_u32(h, env + OFF_E8)
    if not e8:
        return (None,) * 4
    st = pb.read_u32(h, e8 + OFF_ST)
    if not st:
        return (None,) * 4
    return mgr, env, e8, st


def walk_groups(h, st):
    """读 st 单位组表。返回 [(group_ptr, army_cnt, army_tbl), ...] 或 None。"""
    if not st:
        return None
    cnt = pb.read_u32(h, st + ST_GROUP_CNT)
    tbl = pb.read_u32(h, st + ST_GROUP_TBL)
    if cnt is None or tbl is None or not (GRP_MIN < cnt <= GRP_MAX) or not tbl:
        return None
    groups = []
    for i in range(cnt):
        g = pb.read_u32(h, tbl + i * 4)
        if not g:
            return None
        acnt = pb.read_u32(h, g + GROUP_ARMY_CNT)
        atbl = pb.read_u32(h, g + GROUP_ARMY_TBL)
        if acnt is None or atbl is None or acnt > ARM_MAX or not atbl:
            return None
        groups.append((g, acnt, atbl))
    return groups


def walk_army(h, atbl, idx):
    """读军队指针 + 单位/子对象结构。返回 dict 或 None。"""
    a = pb.read_u32(h, atbl + idx * 4)
    if not a:
        return None
    ucnt = pb.read_u32(h, a + ARMY_UNIT_CNT)
    utbl = pb.read_u32(h, a + ARMY_UNIT_TBL)
    ccnt = pb.read_u32(h, a + ARMY_CHILD_CNT)
    ctbl = pb.read_u32(h, a + ARMY_CHILD_TBL)
    if ucnt is None or utbl is None or not (UNT_MIN <= ucnt <= UNT_MAX):
        return None
    if ccnt is None or ctbl is None or ccnt > 64:
        return None
    return {
        "addr": a,
        "unit_cnt": ucnt, "unit_tbl": utbl,
        "child_cnt": ccnt, "child_tbl": ctbl,
        "a270": pb.read_u8(h, a + ARMY_270),
        "a28c": pb.read_u32(h, a + ARMY_28C),
        "a290": pb.read_u32(h, a + ARMY_290),
        "a294": pb.read_u32(h, a + ARMY_294),
    }


def first_units(h, utbl, ucnt, n=4):
    out = []
    for i in range(min(ucnt, n)):
        u = pb.read_u32(h, utbl + i * 4)
        if not u:
            out.append(None)
            continue
        out.append({
            "addr": u,
            "ea8": pb.read_u32(h, u + UNIT_EA8),
            "c01": pb.read_u8(h, u + UNIT_C01),
        })
    return out


def first_children(h, ctbl, ccnt, n=4):
    out = []
    for i in range(min(ccnt, n)):
        c = pb.read_u32(h, ctbl + i * 4)
        if not c:
            out.append(None)
            continue
        out.append({
            "addr": c,
            "x1168": pb.read_u32(h, c + CHILD_1168),
            "x1160": pb.read_u8(h, c + CHILD_1160),
        })
    return out


def probe(h, base):
    print("=" * 72)
    print("PROBE（RE-B3 BCQ 链，只读）：base -> mgr -> env -> e8 -> st -> 组 -> 军 -> 单位")
    print("=" * 72)
    mgr, env, e8, st = resolve_e8(h, base)
    if not st:
        print("BCQ 锚点链解析失败——未进入战斗场景？（state 1 起 st 可读）")
        return 1
    state = pb.read_u8(h, st + OFF_STATE)
    print(f"mgr=0x{mgr:08x} env=0x{env:08x} e8=0x{e8:08x} st=0x{st:08x} 状态=0x{state if state is not None else -1:x}")

    groups = walk_groups(h, st)
    if not groups:
        print("st 单位组表解析失败（[st+0x88]/[st+0x8c] 结构不符？状态3+ 才有组表）")
        return 1
    print(f"组数={len(groups)}")
    switched = pb.read_u8(h, st + ST_SWITCHED)
    print(f"[st+0x31f0](已切AI标志)=0x{switched if switched is not None else -1:x}")
    for gi, (g, acnt, atbl) in enumerate(groups):
        ctrl = pb.read_u32(h, g + GROUP_CTRL)
        print(f"\n组{gi} 0x{g:08x} 军队数={acnt} 控制器=0x{ctrl if ctrl else 0:08x}")
        for ai in range(acnt):
            a = walk_army(h, atbl, ai)
            if not a:
                print(f"  军{ai}: 结构读取失败")
                continue
            u = first_units(h, a["unit_tbl"], a["unit_cnt"])
            c = first_children(h, a["child_tbl"], a["child_cnt"])
            print(f"  军{ai} 0x{a['addr']:08x} 单位数={a['unit_cnt']} 子数={a['child_cnt']} "
                  f"a270=0x{a['a270'] if a['a270'] is not None else -1:x} "
                  f"a28c=0x{a['a28c'] if a['a28c'] is not None else -1:x} "
                  f"a290=0x{a['a290'] if a['a290'] is not None else -1:x} "
                  f"a294=0x{a['a294'] if a['a294'] is not None else -1:x}")
            for ui, uu in enumerate(u):
                if uu:
                    print(f"    单位{ui} 0x{uu['addr']:08x} +0xea8=0x{uu['ea8'] if uu['ea8'] is not None else -1:x} "
                          f"+0xc01=0x{uu['c01'] if uu['c01'] is not None else -1:x}")
            for ci, cc in enumerate(c):
                if cc:
                    print(f"    子{ci} 0x{cc['addr']:08x} +0x1168=0x{cc['x1168'] if cc['x1168'] is not None else -1:x} "
                          f"+0x1160=0x{cc['x1160'] if cc['x1160'] is not None else -1:x}")
    print("\n判读：正常战斗（battle_ai=0）中，玩家军队单位 +0xea8 应为 0（人控）→ 选该军 (group,army) 注入。")
    print("组0军0 通常即玩家军；如不符，用 --group/--army 显式指定。")
    return 0


def snapshot(h, base, group_idx, army_idx, fname=SNAP_FILE):
    """注入前快照（原值），供 --switch-human 恢复。返回 dict 或 None。"""
    mgr, env, e8, st = resolve_e8(h, base)
    groups = walk_groups(h, st) if st else None
    if not groups or group_idx >= len(groups):
        return None
    g, acnt, atbl = groups[group_idx]
    if army_idx >= acnt:
        return None
    a = walk_army(h, atbl, army_idx)
    if not a:
        return None
    rec = {
        "st": st, "st_switched": pb.read_u8(h, st + ST_SWITCHED),
        "group": g, "group_ctrl": pb.read_u32(h, g + GROUP_CTRL),
        "army": a["addr"],
        "a270": a["a270"], "a28c": a["a28c"], "a290": a["a290"], "a294": a["a294"],
        "units": [{"addr": pb.read_u32(h, a["unit_tbl"] + i * 4),
                   "ea8": pb.read_u32(h, pb.read_u32(h, a["unit_tbl"] + i * 4) + UNIT_EA8) if pb.read_u32(h, a["unit_tbl"] + i * 4) else None,
                   "c01": pb.read_u8(h, pb.read_u32(h, a["unit_tbl"] + i * 4) + UNIT_C01) if pb.read_u32(h, a["unit_tbl"] + i * 4) else None}
                  for i in range(a["unit_cnt"])],
        "children": [{"addr": pb.read_u32(h, a["child_tbl"] + i * 4),
                      "x1168": pb.read_u32(h, pb.read_u32(h, a["child_tbl"] + i * 4) + CHILD_1168) if pb.read_u32(h, a["child_tbl"] + i * 4) else None,
                      "x1160": pb.read_u8(h, pb.read_u32(h, a["child_tbl"] + i * 4) + CHILD_1160) if pb.read_u32(h, a["child_tbl"] + i * 4) else None}
                     for i in range(a["child_cnt"])],
    }
    with open(fname, "w") as f:
        json.dump(rec, f, indent=1)
    print(f"快照已存 {fname}（st=0x{st:08x} 组{group_idx} 军{army_idx} 0x{a['addr']:08x}，"
          f"{len(rec['units'])} 单位 + {len(rec['children'])} 子对象）")
    return rec


def build_remote(h, base, e8, group_idx, army_idx, handler_va=HANDLER_SWITCH_AI):
    """构造远程内存：stream + reader + shellcode。返回 (mem_va, tid_ok)。"""
    k32 = pb.K32
    size = 0x200
    MEM_COMMIT = 0x1000
    MEM_RESERVE = 0x2000
    PAGE_EXECUTE_READWRITE = 0x40
    mem = k32.VirtualAllocEx(h, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE)
    if not mem:
        print(f"VirtualAllocEx 失败 err={ctypes.get_last_error()}")
        return None, False

    stream_va = mem + 0x00
    reader_va = mem + 0x10
    sc_va = mem + 0x30

    stream = bytes([0x02, group_idx & 0xFF, 0x02, army_idx & 0xFF])
    reader = struct.pack("<IIIIII", 0, 0, stream_va, 0, 4, 0)
    sc = b""
    sc += b"\xb8" + struct.pack("<I", reader_va)   # mov eax, reader_va
    sc += b"\x50"                                   # push eax (arg1)
    sc += b"\xb8" + struct.pack("<I", e8)          # mov eax, e8
    sc += b"\x50"                                   # push eax (arg2)
    sc += b"\xb8" + struct.pack("<I", base + handler_va)  # mov eax, handler
    sc += b"\xff\xd0"                               # call eax
    sc += b"\x83\xc4\x08"                           # add esp, 8
    sc += b"\x33\xc0"                               # xor eax, eax
    sc += b"\xc3"                                   # ret
    blob = stream + b"\x00" * 12 + reader + b"\x00" * 8 + sc

    buf = ctypes.create_string_buffer(blob)
    got = ctypes.c_size_t()
    ok = bool(k32.WriteProcessMemory(h, ctypes.c_void_p(mem), buf, len(blob), ctypes.byref(got))) \
        and got.value == len(blob)
    if not ok:
        print("WriteProcessMemory 失败")
        return None, False
    return mem, True


def switch_ai(h, base, group_idx, army_idx):
    """注入：直调 handler 0x2abeb0(reader, e8)。"""
    mgr, env, e8, st = resolve_e8(h, base)
    if not st:
        print("锚点链解析失败——未进入战斗？")
        return 1
    state = pb.read_u8(h, st + OFF_STATE)
    if state not in (3, 5):
        print(f"状态=0x{state if state is not None else -1:x} —— 注入时机应在部署后（state 3/5）。"
              f"（可用 --force 强行注入测试，谨慎）")
        return 2
    groups = walk_groups(h, st)
    if not groups:
        print("组表解析失败")
        return 3
    if group_idx >= len(groups):
        print(f"group_idx={group_idx} 超出组数 {len(groups)}")
        return 3
    g, acnt, atbl = groups[group_idx]
    if army_idx >= acnt:
        print(f"army_idx={army_idx} 超出军队数 {acnt}")
        return 3
    # 注入前快照（切回用）
    snap = snapshot(h, base, group_idx, army_idx)
    if not snap:
        print("快照失败（军队结构不可读？）")
        return 3

    print(f"注入：handler=0x{base + HANDLER_SWITCH_AI:08x} group={group_idx} army={army_idx} "
          f"e8=0x{e8:08x} st=0x{st:08x}")
    mem, _ = build_remote(h, base, e8, group_idx, army_idx)
    if not mem:
        return 4
    k32 = pb.K32
    hThread = k32.CreateRemoteThread(h, None, 0, mem + 0x30, None, 0, None)
    if not hThread:
        print(f"CreateRemoteThread 失败 err={ctypes.get_last_error()}")
        return 4
    k32.WaitForSingleObject(hThread, 5000)
    k32.CloseHandle(hThread)
    print(f"线程已执行（shellcode@0x{mem + 0x30:08x}）")
    print(f"  注：远程缓冲 0x{mem:08x} 保留未释放（1 次注入泄漏 0x200B，可接受）")
    return 0


def verify(h, base, group_idx, army_idx):
    """注入后回读验证（handler 副作用）。"""
    mgr, env, e8, st = resolve_e8(h, base)
    groups = walk_groups(h, st) if st else None
    if not groups or group_idx >= len(groups):
        print("链解析失败，无法验证")
        return 1
    g, acnt, atbl = groups[group_idx]
    if army_idx >= acnt:
        print("军队索引越界")
        return 1
    ctrl = pb.read_u32(h, g + GROUP_CTRL)
    switched = pb.read_u8(h, st + ST_SWITCHED)
    a = walk_army(h, atbl, army_idx)
    print(f"组{group_idx} 控制器=0x{ctrl if ctrl else 0:08x}（注入后应非 0 = 懒创建成功）")
    print(f"[st+0x31f0]=0x{switched if switched is not None else -1:x}（注入后应=1）")
    if a:
        print(f"军{army_idx} a28c=0x{a['a28c'] if a['a28c'] is not None else -1:x} "
              f"a290=0x{a['a290'] if a['a290'] is not None else -1:x} "
              f"a294=0x{a['a294'] if a['a294'] is not None else -1:x} "
              f"（注入后应=1/3f800000/ffffffff）")
        for i, uu in enumerate(first_units(h, a["unit_tbl"], a["unit_cnt"], 8)):
            if uu:
                mark = " <-AI" if uu["ea8"] == 1 else ""
                print(f"  单位{i} +0xea8=0x{uu['ea8'] if uu['ea8'] is not None else -1:x}{mark}")
        for i, cc in enumerate(first_children(h, a["child_tbl"], a["child_cnt"], 4)):
            if cc:
                print(f"  子{i} +0x1168=0x{cc['x1168'] if cc['x1168'] is not None else -1:x} "
                      f"+0x1160=0x{cc['x1160'] if cc['x1160'] is not None else -1:x}")
    return 0


def switch_human(h, base, group_idx, army_idx):
    """切回人控：直接写复位（handler 写了什么就复位什么）。"""
    if not os.path.exists(SNAP_FILE):
        print(f"无快照 {SNAP_FILE}——先 --switch-ai（自动快照）才有原值。")
        return 1
    with open(SNAP_FILE) as f:
        snap = json.load(f)
    if snap.get("army") is None:
        print("快照缺 army 字段")
        return 1
    n = 0
    # 单位复位
    for u in snap.get("units", []):
        if not u or not u.get("addr"):
            continue
        if u["ea8"] is not None and w32(h, u["addr"] + UNIT_EA8, u["ea8"]):
            n += 1
        if u["c01"] is not None and w8(h, u["addr"] + UNIT_C01, u["c01"]):
            n += 1
    # 子对象复位
    for c in snap.get("children", []):
        if not c or not c.get("addr"):
            continue
        if c["x1168"] is not None and w32(h, c["addr"] + CHILD_1168, c["x1168"]):
            n += 1
        if c["x1160"] is not None and w8(h, c["addr"] + CHILD_1160, c["x1160"]):
            n += 1
    # army 字段复位
    a = snap["army"]
    for off, key, kind in ((ARMY_270, "a270", "b"), (ARMY_28C, "a28c", "w"),
                           (ARMY_290, "a290", "w"), (ARMY_294, "a294", "w")):
        v = snap.get(key)
        if v is None:
            continue
        ok = w8(h, a + off, v) if kind == "b" else w32(h, a + off, v)
        if ok:
            n += 1
    # st 标志复位
    if snap.get("st_switched") is not None and snap.get("st"):
        if w8(h, snap["st"] + ST_SWITCHED, snap["st_switched"]):
            n += 1
    print(f"已复位 {n} 处（按快照 {SNAP_FILE} 原值）")
    return 0


def bytes_check(h, base):
    """运行时校验：加载的 DLL 关键地址字节 == 静态分析文件（防 build 差异）。只读。"""
    import re_lib
    pe = re_lib.PE()
    print(f"静态 DLL: {pe.path}")
    # 打印加载的模块路径
    psapi = pb.PSAPI
    MAX = 1024
    mods = (ctypes.c_void_p * MAX)()
    cb = ctypes.c_ulong()
    psapi.EnumProcessModulesEx(h, mods, ctypes.sizeof(mods), ctypes.byref(cb), 0x03)
    n = cb.value // ctypes.sizeof(ctypes.c_void_p)
    dll_path = None
    for i in range(n):
        buf = ctypes.create_unicode_buffer(260)
        psapi.GetModuleFileNameExW(h, mods[i], buf, 260)
        if buf.value.lower().endswith("empire.retail.dll"):
            dll_path = buf.value
            break
    print(f"加载 DLL: {dll_path}")
    all_ok = True
    for rva, nbytes in ((0x2abeb0, 0x40), (0x308020, 0x10), (0x284b40, 0x10), (0x2aadd0, 0x40)):
        rt = pb.read_mem(h, base + rva, nbytes)
        off = pe.rva_to_off(rva)
        st = pe.data[off:off + nbytes]
        ok = rt == st
        all_ok &= ok
        print(f"  0x{rva:08x}: {'MATCH' if ok else 'MISMATCH!!'}"
              + ("" if ok else f"  runtime={rt.hex() if rt else None} static={st.hex()}"))
    return 0 if all_ok else 5


def test_decode(h, base):
    """安全诊断：只调 0x284b40 解码器（纯内存读写，不碰游戏对象），
    验证 reader/param 流能否解出 (group, army)。"""
    k32 = pb.K32
    MEM_COMMIT, MEM_RESERVE, PAGE_XRW = 0x1000, 0x2000, 0x40
    mem = k32.VirtualAllocEx(h, None, 0x200, MEM_COMMIT | MEM_RESERVE, PAGE_XRW)
    if not mem:
        print(f"VirtualAllocEx 失败 err={ctypes.get_last_error()}")
        return 4
    stream_va, reader_va = mem + 0, mem + 0x10
    out1_va, out2_va, out3_va = mem + 0x30, mem + 0x34, mem + 0x38
    sc_va = mem + 0x40
    dec_va = base + 0x284b40

    stream = bytes([0x02, 0, 0x02, 0])
    reader = struct.pack("<IIIIII", 0, 0, stream_va, 0, 4, 0)
    # shellcode：0x284b40(reader, out1) → 0x284b40(reader, out2) → out3=[reader+4]
    def call_dec(out_va):
        b = b""
        b += b"\xb8" + struct.pack("<I", out_va) + b"\x50"       # mov eax,out; push eax(arg2)
        b += b"\xb8" + struct.pack("<I", reader_va) + b"\x50"    # mov eax,reader; push eax(arg1)
        b += b"\xb8" + struct.pack("<I", dec_va) + b"\xff\xd0"   # mov eax,dec; call eax
        b += b"\x83\xc4\x08"                                     # add esp,8
        return b
    sc = call_dec(out1_va) + call_dec(out2_va)
    sc += b"\xb8" + struct.pack("<I", reader_va) + b"\x8b\x40\x04"     # mov eax,[reader+4]
    sc += b"\xbb" + struct.pack("<I", out3_va) + b"\x89\x03"           # mov [out3],eax
    sc += b"\x33\xc0\xc3"                                              # xor eax,eax; ret

    blob = stream + b"\x00" * 12 + reader + b"\x00" * 8 + b"\x00" * 0x10 + sc
    buf = ctypes.create_string_buffer(blob)
    got = ctypes.c_size_t()
    ok = bool(k32.WriteProcessMemory(h, ctypes.c_void_p(mem), buf, len(blob), ctypes.byref(got))) \
        and got.value == len(blob)
    if not ok:
        print("WriteProcessMemory 失败")
        return 4
    # 清零 out
    k32.WriteProcessMemory(h, ctypes.c_void_p(out1_va), ctypes.create_string_buffer(b"\xff\xff\xff\xff"), 4, ctypes.byref(ctypes.c_size_t()))
    k32.WriteProcessMemory(h, ctypes.c_void_p(out2_va), ctypes.create_string_buffer(b"\xff\xff\xff\xff"), 4, ctypes.byref(ctypes.c_size_t()))
    k32.WriteProcessMemory(h, ctypes.c_void_p(out3_va), ctypes.create_string_buffer(b"\xff\xff\xff\xff"), 4, ctypes.byref(ctypes.c_size_t()))
    ht = k32.CreateRemoteThread(h, None, 0, sc_va, None, 0, None)
    if not ht:
        print(f"CreateRemoteThread 失败 err={ctypes.get_last_error()}")
        return 4
    wr = k32.WaitForSingleObject(ht, 5000)
    k32.CloseHandle(ht)
    out1 = pb.read_u32(h, out1_va)
    out2 = pb.read_u32(h, out2_va)
    out3 = pb.read_u32(h, out3_va)
    print(f"WaitForSingleObject={wr} (0=线程完成)")
    print(f"解码 group_idx(out1) = {out1}（期望 0）")
    print(f"解码 army_idx (out2) = {out2}（期望 0）")
    print(f"解码错误标志(out3)   = 0x{out3:x}（0=解码成功；1=tag/边界失败）")
    if out3 == 0 and out1 == 0 and out2 == 0:
        print("→ reader/param 流正确，handler 提前退出/崩溃在对象遍历或辅助调用路径")
    else:
        print("→ reader 布局或 param 流有误（tag/边界/结构偏移）")
    return 0


def test_handler(h, base, group_idx, army_idx):
    """诊断：调 handler 0x2abeb0 + shellcode 内即时回读写入结果。
    区分「handler 没写」vs「写了但被游戏帧回滚」。带 WER dump（已启用）防崩溃取证。"""
    k32 = pb.K32
    mgr, env, e8, st = resolve_e8(h, base)
    if not st:
        print("锚点链解析失败")
        return 1
    state = pb.read_u8(h, st + OFF_STATE)
    groups = walk_groups(h, st)
    if not groups or group_idx >= len(groups):
        print("组表解析失败")
        return 3
    g, acnt, atbl = groups[group_idx]
    if army_idx >= acnt:
        print("军队索引越界")
        return 3
    print(f"状态=0x{state if state is not None else -1:x} 注入 group={group_idx} army={army_idx} "
          f"e8=0x{e8:08x} st=0x{st:08x}")

    MEM_COMMIT, MEM_RESERVE, PAGE_XRW = 0x1000, 0x2000, 0x40
    mem = k32.VirtualAllocEx(h, None, 0x200, MEM_COMMIT | MEM_RESERVE, PAGE_XRW)
    if not mem:
        print(f"VirtualAllocEx 失败 err={ctypes.get_last_error()}")
        return 4
    stream_va, reader_va = mem + 0, mem + 0x10
    sc_va = mem + 0x40
    # 回读区（handler 调用后 shellcode 内写回）
    rb_st31, rb_ea8, rb_28c, rb_group_ctrl = mem + 0x60, mem + 0x64, mem + 0x68, mem + 0x6c

    stream = bytes([0x02, group_idx & 0xFF, 0x02, army_idx & 0xFF])
    reader = struct.pack("<IIIIII", 0, 0, stream_va, 0, 4, 0)
    handler_va = base + HANDLER_SWITCH_AI

    # shellcode：call handler(reader, e8) → 即时回读
    sc = b""
    sc += b"\xb8" + struct.pack("<I", reader_va) + b"\x50"     # push reader
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x50"            # push e8
    sc += b"\xb8" + struct.pack("<I", handler_va) + b"\xff\xd0"  # call handler
    sc += b"\x83\xc4\x08"
    # 回读 st+0x31f0: st=[e8+0xb4]
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x8b\x80" + struct.pack("<I", 0xb4)
    sc += b"\x8b\x80" + struct.pack("<I", ST_SWITCHED)         # [st+0x31f0]
    sc += b"\xbb" + struct.pack("<I", rb_st31) + b"\x89\x03"
    # 回读 unit0+0xea8: st→gtbl→g0→atbl→a0→utbl→u0
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x8b\x80" + struct.pack("<I", 0xb4)
    sc += b"\x8b\x80" + struct.pack("<I", ST_GROUP_TBL)
    sc += b"\x8b\x00"                                          # group0
    sc += b"\x8b\x80" + struct.pack("<I", GROUP_ARMY_TBL)
    sc += b"\x8b\x00"                                          # army0
    sc += b"\x8b\x80" + struct.pack("<I", ARMY_UNIT_TBL)
    sc += b"\x8b\x00"                                          # unit0
    sc += b"\x8b\x80" + struct.pack("<I", UNIT_EA8)
    sc += b"\xbb" + struct.pack("<I", rb_ea8) + b"\x89\x03"
    # 回读 army0+0x28c
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x8b\x80" + struct.pack("<I", 0xb4)
    sc += b"\x8b\x80" + struct.pack("<I", ST_GROUP_TBL)
    sc += b"\x8b\x00"
    sc += b"\x8b\x80" + struct.pack("<I", GROUP_ARMY_TBL)
    sc += b"\x8b\x00"
    sc += b"\x8b\x80" + struct.pack("<I", ARMY_28C)
    sc += b"\xbb" + struct.pack("<I", rb_28c) + b"\x89\x03"
    # 回读 group0+0xc
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x8b\x80" + struct.pack("<I", 0xb4)
    sc += b"\x8b\x80" + struct.pack("<I", ST_GROUP_TBL)
    sc += b"\x8b\x00"
    sc += b"\x8b\x80" + struct.pack("<I", GROUP_CTRL)
    sc += b"\xbb" + struct.pack("<I", rb_group_ctrl) + b"\x89\x03"
    sc += b"\x33\xc0\xc3"                                      # xor eax,eax; ret

    blob = stream + b"\x00" * 12 + reader + b"\x00" * 8 + b"\x00" * 0x18 + sc
    buf = ctypes.create_string_buffer(blob)
    got = ctypes.c_size_t()
    ok = bool(k32.WriteProcessMemory(h, ctypes.c_void_p(mem), buf, len(blob), ctypes.byref(got))) \
        and got.value == len(blob)
    if not ok:
        print("WriteProcessMemory 失败")
        return 4
    for va in (rb_st31, rb_ea8, rb_28c, rb_group_ctrl):
        k32.WriteProcessMemory(h, ctypes.c_void_p(va), ctypes.create_string_buffer(b"\xee\xee\xee\xee"), 4,
                               ctypes.byref(ctypes.c_size_t()))
    ht = k32.CreateRemoteThread(h, None, 0, sc_va, None, 0, None)
    if not ht:
        print(f"CreateRemoteThread 失败 err={ctypes.get_last_error()}")
        return 4
    wr = k32.WaitForSingleObject(ht, 5000)
    k32.CloseHandle(ht)
    print(f"WaitForSingleObject={wr} (0=线程完成；0x102=超时→可能挂起)")
    print(f"[进程内即时回读]  st+0x31f0=0x{pb.read_u32(h, rb_st31):08x}（期望1）"
          f" unit0+0xea8=0x{pb.read_u32(h, rb_ea8):08x}（期望1）"
          f" army0+0x28c=0x{pb.read_u32(h, rb_28c):08x}（期望1）"
          f" group0+0xc=0x{pb.read_u32(h, rb_group_ctrl):08x}")
    return 0


def resolve_local_proc(lib, name):
    """从本进程解析函数地址（系统 DLL 全进程同基址，可直接用于目标进程）。"""
    k32 = ctypes.windll.kernel32
    k32.GetModuleHandleW.restype = ctypes.c_void_p
    k32.GetProcAddress.restype = ctypes.c_void_p
    k32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    h = k32.GetModuleHandleW(lib)
    if not h:
        return 0
    return k32.GetProcAddress(ctypes.c_void_p(h), name.encode()) or 0


def resolve_export_in_proc(h, dll_suffix="kernel32.dll", name=b"AddVectoredExceptionHandler"):
    """在目标进程内解析导出函数 VA（游戏是 32 位，必须用其自身加载的 32 位 DLL）。"""
    psapi = pb.PSAPI
    mods = (ctypes.c_void_p * 1024)()
    cb = ctypes.c_ulong()
    psapi.EnumProcessModulesEx(h, mods, ctypes.sizeof(mods), ctypes.byref(cb), 0x03)
    n = cb.value // ctypes.sizeof(ctypes.c_void_p)
    base = None
    for i in range(n):
        buf = ctypes.create_unicode_buffer(260)
        psapi.GetModuleFileNameExW(h, mods[i], buf, 260)
        if buf.value.lower().endswith(dll_suffix):
            base = int(mods[i])
            break
    if not base:
        return 0
    e_lfanew = pb.read_u32(h, base + 0x3c)
    opt = base + e_lfanew + 0x18
    edd_rva = pb.read_u32(h, opt + 0x60)          # PE32 导出目录
    if not edd_rva:
        return 0
    ed = base + edd_rva
    n_names = pb.read_u32(h, ed + 0x18)
    a_names = pb.read_u32(h, ed + 0x20)
    a_funcs = pb.read_u32(h, ed + 0x1c)
    a_ord = pb.read_u32(h, ed + 0x24)
    for i in range(min(n_names, 5000)):
        nrva = pb.read_u32(h, base + a_names + i * 4)
        s = (pb.read_mem(h, base + nrva, 64) or b"").split(b"\0")[0]
        if s == name:
            ord_ = int.from_bytes(pb.read_mem(h, base + a_ord + i * 2, 2), "little")
            frva = pb.read_u32(h, base + a_funcs + ord_ * 4)
            return base + frva
    return 0


def test_veh(h, base, group_idx, army_idx):
    """终极诊断：注册 VEH 捕获 handler 调用的确切崩溃点（EIP/ExceptionCode）。
    + TLS 自检（fs:[0x2c] / tls_index 槽）。VEH 返回 CONTINUE_SEARCH → 引擎错误对话框仍会弹。"""
    k32 = pb.K32
    mgr, env, e8, st = resolve_e8(h, base)
    if not st:
        print("锚点链解析失败")
        return 1
    state = pb.read_u8(h, st + OFF_STATE)
    print(f"状态=0x{state if state is not None else -1:x} e8=0x{e8:08x} st=0x{st:08x}")

    MEM_COMMIT, MEM_RESERVE, PAGE_XRW = 0x1000, 0x2000, 0x40
    mem = k32.VirtualAllocEx(h, None, 0x400, MEM_COMMIT | MEM_RESERVE, PAGE_XRW)
    if not mem:
        print(f"VirtualAllocEx 失败 err={ctypes.get_last_error()}")
        return 4
    stream_va, reader_va = mem + 0, mem + 0x10
    sc_va = mem + 0x80
    veh_va = mem + 0x140
    buf = mem + 0x200          # 捕获区：+0x00 eip, +0x04 code, +0x08 ctx_eip, +0x0c pointers
                               #         +0x40 tls_base, +0x44 tls_slot, +0x48 tls_scratch
    # 回读区
    rb_st31, rb_ea8 = mem + 0x60, mem + 0x64

    stream = bytes([0x02, group_idx & 0xFF, 0x02, army_idx & 0xFF])
    reader = struct.pack("<IIIIII", 0, 0, stream_va, 0, 4, 0)
    handler_va = base + HANDLER_SWITCH_AI
    veh_add = resolve_export_in_proc(h, "kernel32.dll", b"AddVectoredExceptionHandler")
    print(f"AddVectoredExceptionHandler(游戏内32位)=0x{veh_add:08x} handler=0x{handler_va:08x}")

    # ---- VEH stub：捕获异常信息 → buf，返回 CONTINUE_SEARCH(0) ----
    v = b""
    v += b"\x8b\x44\x24\x04"                                   # mov eax,[esp+4] (EXCEPTION_POINTERS)
    v += b"\x8b\x08"                                           # mov ecx,[eax] (ExceptionRecord)
    v += b"\x8b\x51\x0c"                                       # mov edx,[ecx+0xc] (ExceptionAddress)
    v += b"\xbb" + struct.pack("<I", buf) + b"\x89\x13"        # [buf] = EIP
    v += b"\x8b\x11"                                           # mov edx,[ecx] (ExceptionCode)
    v += b"\x89\x53\x04"                                       # [buf+4] = code
    v += b"\x8b\x48\x04"                                       # mov ecx,[eax+4] (Context)
    v += b"\x8b\x91\xb8\x00\x00\x00"                           # mov edx,[ecx+0xb8] (ctx Eip)
    v += b"\x89\x53\x08"                                       # [buf+8] = ctx_eip
    v += b"\x33\xc0"                                           # xor eax,eax (CONTINUE_SEARCH)
    v += b"\xc2\x04\x00"                                       # ret 4 (stdcall)

    # ---- 主 shellcode：注册 VEH → TLS 自检 → 调 handler → 回读 ----
    sc = b""
    sc += b"\x6a\x01" + b"\x68" + struct.pack("<I", veh_va)     # push 1; push veh
    sc += b"\xb8" + struct.pack("<I", veh_add) + b"\xff\xd0"    # call AddVectoredExceptionHandler
    sc += b"\x83\xc4\x08"
    # TLS 自检
    sc += b"\x64\xa1\x2c\x00\x00\x00"                           # mov eax, fs:[0x2c]
    sc += b"\xbb" + struct.pack("<I", buf + 0x40) + b"\x89\x03"  # [buf+0x40]=tls_base
    sc += b"\xb9" + struct.pack("<I", base + 0x1cb06d0)         # mov ecx, &tls_index
    sc += b"\x8b\x09"                                           # mov ecx,[ecx] (tls_index)
    sc += b"\x64\xa1\x2c\x00\x00\x00"                           # mov eax, fs:[0x2c]
    sc += b"\x8b\x04\x88"                                       # mov eax,[eax+ecx*4] (slot)
    sc += b"\x89\x43\x04"                                       # [buf+0x44]=slot
    sc += b"\x8b\x40\x04"                                       # mov eax,[eax+4] (scratch state)
    sc += b"\x89\x43\x08"                                       # [buf+0x48]=scratch_state
    # 调 handler
    sc += b"\xb8" + struct.pack("<I", reader_va) + b"\x50"
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x50"
    sc += b"\xb8" + struct.pack("<I", handler_va) + b"\xff\xd0"
    sc += b"\x83\xc4\x08"
    # 回读 st+0x31f0 / unit0+0xea8
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x8b\x80" + struct.pack("<I", 0xb4)
    sc += b"\x8b\x80" + struct.pack("<I", ST_SWITCHED)
    sc += b"\xbb" + struct.pack("<I", rb_st31) + b"\x89\x03"
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x8b\x80" + struct.pack("<I", 0xb4)
    sc += b"\x8b\x80" + struct.pack("<I", ST_GROUP_TBL)
    sc += b"\x8b\x00" + b"\x8b\x80" + struct.pack("<I", GROUP_ARMY_TBL)
    sc += b"\x8b\x00" + b"\x8b\x80" + struct.pack("<I", ARMY_UNIT_TBL)
    sc += b"\x8b\x00" + b"\x8b\x80" + struct.pack("<I", UNIT_EA8)
    sc += b"\xbb" + struct.pack("<I", rb_ea8) + b"\x89\x03"
    sc += b"\x33\xc0\xc3"

    blob = (stream + b"\x00" * 12 + reader + b"\x00" * 8 + b"\x00" * 0x50
            + sc + b"\x00" * (veh_va - (mem + 0x80 + len(sc))) + v + b"\x00" * 8)
    assert len(blob) <= 0x400
    bufw = ctypes.create_string_buffer(blob)
    got = ctypes.c_size_t()
    ok = bool(k32.WriteProcessMemory(h, ctypes.c_void_p(mem), bufw, len(blob), ctypes.byref(got))) \
        and got.value == len(blob)
    if not ok:
        print("WriteProcessMemory 失败")
        return 4
    for va in (buf, buf + 4, buf + 8, rb_st31, rb_ea8):
        k32.WriteProcessMemory(h, ctypes.c_void_p(va), ctypes.create_string_buffer(b"\xee\xee\xee\xee"), 4,
                               ctypes.byref(ctypes.c_size_t()))
    ht = k32.CreateRemoteThread(h, None, 0, sc_va, None, 0, None)
    if not ht:
        print(f"CreateRemoteThread 失败 err={ctypes.get_last_error()}")
        return 4
    wr = k32.WaitForSingleObject(ht, 5000)
    k32.CloseHandle(ht)
    print(f"WaitForSingleObject={wr}")
    eip = pb.read_u32(h, buf)
    code = pb.read_u32(h, buf + 4)
    ctx_eip = pb.read_u32(h, buf + 8)
    print(f"[VEH] faulting EIP=0x{eip:08x} (DLL内偏移 0x{eip - base:08x}) "
          f"ExceptionCode=0x{code:08x} ctx_Eip=0x{ctx_eip:08x}")
    tls_base = pb.read_u32(h, buf + 0x40)
    tls_slot = pb.read_u32(h, buf + 0x44)
    tls_scr = pb.read_u32(h, buf + 0x48)
    print(f"[TLS] fs:[0x2c]=0x{tls_base:08x} tls_index槽=0x{tls_slot:08x} scratch_state=0x{tls_scr:08x}")
    print(f"[回读] st+0x31f0=0x{pb.read_u32(h, rb_st31):08x} unit0+0xea8=0x{pb.read_u32(h, rb_ea8):08x}")
    return 0


def find_main_thread(h, pid):
    """找游戏窗口的拥有线程（主/UI 线程）。返回 thread_id 或 None。"""
    user32 = ctypes.windll.user32
    tid = None
    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, lparam):
        nonlocal tid
        wpid = wt.DWORD()
        wtid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            tid = wtid
            return False
        return True
    user32.EnumWindows(cb, 0)
    return tid


def switch_ai_apc(h, base, group_idx, army_idx):
    """主线程 APC 注入：handler 在游戏主线程上执行（TLS 正常 + 主线程上下文）。
    QueueUserAPC 到主窗口线程；主线程在 alertable wait 时触发。带进程内回读验证。"""
    k32 = pb.K32
    pid = pb.find_pid()
    tid = find_main_thread(h, pid)
    if not tid:
        print("找不到主窗口线程")
        return 1
    mgr, env, e8, st = resolve_e8(h, base)
    if not st:
        print("锚点链解析失败")
        return 1
    state = pb.read_u8(h, st + OFF_STATE)
    groups = walk_groups(h, st)
    if not groups or group_idx >= len(groups):
        print("组表解析失败")
        return 3
    g, acnt, atbl = groups[group_idx]
    if army_idx >= acnt:
        print("军队索引越界")
        return 3
    print(f"主线程 TID=0x{tid:x} 状态=0x{state if state is not None else -1:x} "
          f"group={group_idx} army={army_idx} e8=0x{e8:08x}")

    MEM_COMMIT, MEM_RESERVE, PAGE_XRW = 0x1000, 0x2000, 0x40
    mem = k32.VirtualAllocEx(h, None, 0x200, MEM_COMMIT | MEM_RESERVE, PAGE_XRW)
    if not mem:
        print(f"VirtualAllocEx 失败 err={ctypes.get_last_error()}")
        return 4
    stream_va, reader_va = mem + 0, mem + 0x10
    sc_va = mem + 0x40
    rb_st31, rb_ea8 = mem + 0x60, mem + 0x64

    stream = bytes([0x02, group_idx & 0xFF, 0x02, army_idx & 0xFF])
    reader = struct.pack("<IIIIII", 0, 0, stream_va, 0, 4, 0)
    handler_va = base + HANDLER_SWITCH_AI

    sc = b""
    sc += b"\xb8" + struct.pack("<I", reader_va) + b"\x50"      # push reader (arg1)
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x50"             # push e8 (arg2)
    sc += b"\xb8" + struct.pack("<I", handler_va) + b"\xff\xd0"  # call handler
    sc += b"\x83\xc4\x08"
    # 回读 st+0x31f0
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x8b\x80" + struct.pack("<I", 0xb4)
    sc += b"\x8b\x80" + struct.pack("<I", ST_SWITCHED)
    sc += b"\xbb" + struct.pack("<I", rb_st31) + b"\x89\x03"
    # 回读 unit0+0xea8
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x8b\x80" + struct.pack("<I", 0xb4)
    sc += b"\x8b\x80" + struct.pack("<I", ST_GROUP_TBL)
    sc += b"\x8b\x00" + b"\x8b\x80" + struct.pack("<I", GROUP_ARMY_TBL)
    sc += b"\x8b\x00" + b"\x8b\x80" + struct.pack("<I", ARMY_UNIT_TBL)
    sc += b"\x8b\x00" + b"\x8b\x80" + struct.pack("<I", UNIT_EA8)
    sc += b"\xbb" + struct.pack("<I", rb_ea8) + b"\x89\x03"
    sc += b"\x33\xc0\xc2\x04\x00"                                # xor eax,eax; ret 4 (stdcall)

    blob = stream + b"\x00" * 12 + reader + b"\x00" * 8 + b"\x00" * 0x10 + sc
    buf = ctypes.create_string_buffer(blob)
    got = ctypes.c_size_t()
    ok = bool(k32.WriteProcessMemory(h, ctypes.c_void_p(mem), buf, len(blob), ctypes.byref(got))) \
        and got.value == len(blob)
    if not ok:
        print("WriteProcessMemory 失败")
        return 4
    for va in (rb_st31, rb_ea8):
        k32.WriteProcessMemory(h, ctypes.c_void_p(va), ctypes.create_string_buffer(b"\xee\xee\xee\xee"), 4,
                               ctypes.byref(ctypes.c_size_t()))
    hThread = k32.OpenThread(0x0010 | 0x0002 | 0x0020 | 0x0008,  # SET_CONTEXT|SUSPEND|QUERY|TERMINATE
                             False, tid)
    if not hThread:
        print(f"OpenThread 失败 err={ctypes.get_last_error()}")
        return 4
    ok = k32.QueueUserAPC(sc_va, hThread, 0)
    print(f"QueueUserAPC = {ok}（1=已排队；主线程 alertable wait 时触发）")
    k32.CloseHandle(hThread)
    # 等待 APC 触发（主线程需进入 alertable wait；轮询回读）
    for i in range(20):
        time.sleep(0.25)
        rb = pb.read_u32(h, rb_st31)
        if rb is not None and rb != 0xEEEEEEEE:
            break
    print(f"[主线程回读] st+0x31f0=0x{pb.read_u32(h, rb_st31):08x} unit0+0xea8=0x{pb.read_u32(h, rb_ea8):08x}")
    if pb.read_u32(h, rb_st31) != 0xEEEEEEEE:
        print("→ APC 已触发，handler 在主线程执行")
    else:
        print("→ APC 未触发（5 秒内主线程无 alertable wait）——需换 per-frame hook 方案")
    return 0


# x86 CONTEXT 关键偏移（32 位线程，64 位宿主用 Wow64Get/SetThreadContext）
CTX_FLAGS = 0x00
CTX_EIP = 0xB8
CTX_ESP = 0xC4
CTX_FULL = 0x10007        # CONTEXT_i386 | CONTEXT_CONTROL | CONTEXT_INTEGER | CONTEXT_SEGMENTS
CTX_FLOAT = 0x8
CTX_EXT = 0x20
CTX_SIZE = 0x2CC


def switch_ai_ctx(h, base, group_idx, army_idx):
    """线程上下文劫持：挂起主线程 → EIP 指向 shellcode（调 handler）→ NtContinue 还原。
    handler 在游戏主线程上下文执行（TLS 正常），规避裸线程 TLS=NULL 崩溃。"""
    k32 = pb.K32
    pid = pb.find_pid()
    tid = find_main_thread(h, pid)
    if not tid:
        print("找不到主窗口线程")
        return 1
    mgr, env, e8, st = resolve_e8(h, base)
    if not st:
        print("锚点链解析失败")
        return 1
    state = pb.read_u8(h, st + OFF_STATE)
    groups = walk_groups(h, st)
    if not groups or group_idx >= len(groups):
        print("组表解析失败")
        return 3
    g, acnt, atbl = groups[group_idx]
    if army_idx >= acnt:
        print("军队索引越界")
        return 3
    print(f"主线程 TID=0x{tid:x} 状态=0x{state if state is not None else -1:x} "
          f"group={group_idx} army={army_idx} e8=0x{e8:08x}")

    ntdll_ntcontinue = resolve_export_in_proc(h, "ntdll.dll", b"NtContinue")
    if not ntdll_ntcontinue:
        print("找不到 ntdll!NtContinue")
        return 4
    print(f"ntdll!NtContinue=0x{ntdll_ntcontinue:08x}")

    MEM_COMMIT, MEM_RESERVE, PAGE_XRW = 0x1000, 0x2000, 0x40
    mem = k32.VirtualAllocEx(h, None, 0x400, MEM_COMMIT | MEM_RESERVE, PAGE_XRW)
    if not mem:
        print(f"VirtualAllocEx 失败 err={ctypes.get_last_error()}")
        return 4
    stream_va, reader_va = mem + 0, mem + 0x10
    sc_va = mem + 0x40
    saved_ctx_va = mem + 0x100
    rb_st31, rb_ea8 = mem + 0x60, mem + 0x64

    stream = bytes([0x02, group_idx & 0xFF, 0x02, army_idx & 0xFF])
    reader = struct.pack("<IIIIII", 0, 0, stream_va, 0, 4, 0)
    handler_va = base + HANDLER_SWITCH_AI

    sc = b""
    sc += b"\xb8" + struct.pack("<I", reader_va) + b"\x50"       # push reader
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x50"              # push e8
    sc += b"\xb8" + struct.pack("<I", handler_va) + b"\xff\xd0"   # call handler
    sc += b"\x83\xc4\x08"
    # 回读 st+0x31f0
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x8b\x80" + struct.pack("<I", 0xb4)
    sc += b"\x8b\x80" + struct.pack("<I", ST_SWITCHED)
    sc += b"\xbb" + struct.pack("<I", rb_st31) + b"\x89\x03"
    # 回读 unit0+0xea8
    sc += b"\xb8" + struct.pack("<I", e8) + b"\x8b\x80" + struct.pack("<I", 0xb4)
    sc += b"\x8b\x80" + struct.pack("<I", ST_GROUP_TBL)
    sc += b"\x8b\x00" + b"\x8b\x80" + struct.pack("<I", GROUP_ARMY_TBL)
    sc += b"\x8b\x00" + b"\x8b\x80" + struct.pack("<I", ARMY_UNIT_TBL)
    sc += b"\x8b\x00" + b"\x8b\x80" + struct.pack("<I", UNIT_EA8)
    sc += b"\xbb" + struct.pack("<I", rb_ea8) + b"\x89\x03"
    # NtContinue(saved_ctx, 0) — 还原上下文（永不返回）
    sc += b"\xb8" + struct.pack("<I", ntdll_ntcontinue)
    sc += b"\x6a\x00" + b"\x68" + struct.pack("<I", saved_ctx_va)
    sc += b"\xff\xd0"
    sc += b"\xcc"  # 不应到达

    blob = stream + b"\x00" * 12 + reader + b"\x00" * 8 + b"\x00" * 0x10 + sc
    buf = ctypes.create_string_buffer(blob)
    got = ctypes.c_size_t()
    ok = bool(k32.WriteProcessMemory(h, ctypes.c_void_p(mem), buf, len(blob), ctypes.byref(got))) \
        and got.value == len(blob)
    if not ok:
        print("WriteProcessMemory 失败")
        return 4
    for va in (rb_st31, rb_ea8):
        k32.WriteProcessMemory(h, ctypes.c_void_p(va), ctypes.create_string_buffer(b"\xee\xee\xee\xee"), 4,
                               ctypes.byref(ctypes.c_size_t()))

    hThread = k32.OpenThread(0x0040 | 0x0010 | 0x0002 | 0x0020 | 0x0008,
                             False, tid)  # GET_CONTEXT|SET_CONTEXT|SUSPEND|QUERY|TERMINATE
    if not hThread:
        print(f"OpenThread 失败 err={ctypes.get_last_error()}")
        return 4
    k32.SuspendThread(hThread)
    # 读取 32 位上下文
    ctx = ctypes.create_string_buffer(CTX_SIZE)
    ctypes.memset(ctx, 0, CTX_SIZE)
    struct.pack_into("<I", ctx, 0, CTX_FULL | CTX_FLOAT | CTX_EXT)
    ok = k32.Wow64GetThreadContext(hThread, ctx)
    if not ok:
        print(f"Wow64GetThreadContext 失败 err={ctypes.get_last_error()}")
        k32.ResumeThread(hThread)
        k32.CloseHandle(hThread)
        return 4
    # 保存原上下文到远程内存（供 NtContinue 还原）
    k32.WriteProcessMemory(h, ctypes.c_void_p(saved_ctx_va), ctx, CTX_SIZE, ctypes.byref(ctypes.c_size_t()))
    orig_eip = int.from_bytes(ctx.raw[CTX_EIP:CTX_EIP + 4], "little")
    print(f"  原 EIP=0x{orig_eip:08x}（挂起点）→ 改为 shellcode 0x{sc_va:08x}")
    struct.pack_into("<I", ctx, CTX_EIP, sc_va)
    ok = k32.Wow64SetThreadContext(hThread, ctx)
    if not ok:
        print(f"Wow64SetThreadContext 失败 err={ctypes.get_last_error()}")
        k32.ResumeThread(hThread)
        k32.CloseHandle(hThread)
        return 4
    k32.ResumeThread(hThread)
    print("已恢复主线程（将执行 shellcode → handler → NtContinue 还原）")
    # 等待回读
    time.sleep(0.5)
    rb1 = pb.read_u32(h, rb_st31)
    rb2 = pb.read_u32(h, rb_ea8)
    print(f"[主线程回读] st+0x31f0=0x{rb1:08x} unit0+0xea8=0x{rb2:08x}")
    if rb1 != 0xEEEEEEEE or rb2 != 0xEEEEEEEE:
        print("→ handler 已在主线程执行（写入可见）")
    else:
        print("→ 未检测到写入（handler 未完成或 NtContinue 前崩溃——查游戏是否仍存活）")
    k32.CloseHandle(hThread)
    return 0


def _apply_ai_army(h, atbl, army_idx, mode):
    """对单个军队应用 AI 接管字段（mode='ai'/'human'），返回写点数。幂等。"""
    a = walk_army(h, atbl, army_idx)
    if not a:
        return 0
    n = 0
    ai = (mode == "ai")
    for i in range(a["unit_cnt"]):
        u = pb.read_u32(h, a["unit_tbl"] + i * 4)
        if not u:
            continue
        if ai and pb.read_u32(h, u + UNIT_EA8) == 1:
            continue  # 已 AI，跳过（幂等；不动引擎已接管的新单位）
        if w32(h, u + UNIT_EA8, 1 if ai else 0):
            n += 1
        if w8(h, u + UNIT_C01, 1 if ai else 0):
            n += 1
    for i in range(a["child_cnt"]):
        c = pb.read_u32(h, a["child_tbl"] + i * 4)
        if not c:
            continue
        if w32(h, c + CHILD_1168, 1 if ai else 0):
            n += 1
        if w8(h, c + CHILD_1160, 1 if ai else 0):
            n += 1
    for off, v_ai, v_h in ((ARMY_28C, 1, 0), (ARMY_294, 0xFFFFFFFF, 0)):
        cur = pb.read_u32(h, a["addr"] + off)
        if cur is not None and ((ai and cur == 1) or (not ai and cur == 0)):
            continue
        if w32(h, a["addr"] + off, v_ai if ai else v_h):
            n += 1
    cur290 = pb.read_u32(h, a["addr"] + ARMY_290)
    if cur290 is None or cur290 == 0 or not ai:
        f290 = struct.pack("<f", 1.0 if ai else 0.0)
        buf = ctypes.create_string_buffer(f290)
        got = ctypes.c_size_t()
        if pb.K32.WriteProcessMemory(h, ctypes.c_void_p(a["addr"] + ARMY_290), buf, 4, ctypes.byref(got)) \
                and got.value == 4:
            n += 1
    cur270 = pb.read_u8(h, a["addr"] + ARMY_270)
    want270 = 0 if ai else 1
    if cur270 is not None and cur270 != want270:
        if w8(h, a["addr"] + ARMY_270, want270):
            n += 1
    return n


def write_ai_fields(h, base, group_idx, army_idx, mode):
    """直接写 handler 0x2abeb0 的可见效果（不执行代码，零崩溃风险）。
    mode='ai': 单位+0xea8=1/+0xc01=1、子对象+0x1168/+0x1160=1、st+0x31f0=1、
               army[0x28c]=1/[0x290]=1.0f/[0x294]=-1/[0x270]=0
    mode='human': 反向复位（0/0/0/0/0.0/0/1）。
    对照组验证：若 AI 不接管 → handler 的 notify/序列化是必要的（BCQ 路由必需）。"""
    mgr, env, e8, st = resolve_e8(h, base)
    groups = walk_groups(h, st) if st else None
    if not groups or group_idx >= len(groups):
        print("组表解析失败")
        return 3
    g, acnt, atbl = groups[group_idx]
    if army_idx >= acnt:
        print("军队索引越界")
        return 3
    n = _apply_ai_army(h, atbl, army_idx, mode)
    if w8(h, st + ST_SWITCHED, 1 if mode == "ai" else 0):
        n += 1
    a = walk_army(h, atbl, army_idx)
    print(f"[{mode}] 已写 {n} 处（组{group_idx}军{army_idx} 0x{a['addr']:08x}，{a['unit_cnt']} 单位 + {a['child_cnt']} 子对象）")
    u0 = pb.read_u32(h, a["unit_tbl"])
    print(f"  回读：unit0+0xea8=0x{pb.read_u32(h, u0 + UNIT_EA8) if u0 else -1:x} "
          f"st+0x31f0=0x{pb.read_u8(h, st + ST_SWITCHED) if st else -1:x} "
          f"army+0x28c=0x{pb.read_u32(h, a['addr'] + ARMY_28C):x}")
    return 0


def watch_ai(h, base, interval=0.5):
    """监控循环：每 interval 秒全量扫组→军队→单位，把 AI 接管字段应用到全部军队。
    覆盖援军/新生成单位（+0xea8=0 → 1）。幂等；敌方已 AI 的单位不变。Ctrl+C 停止。"""
    print(f"监控中：每 {interval}s 对所有军队应用 AI 接管字段（覆盖援军新增单位）Ctrl+C 停止", flush=True)
    last_total = -1
    try:
        while True:
            mgr, env, e8, st = resolve_e8(h, base)
            if not st:
                time.sleep(interval)
                continue
            groups = walk_groups(h, st)
            if not groups:
                time.sleep(interval)
                continue
            total = 0
            details = []
            for gi, (g, acnt, atbl) in enumerate(groups):
                for ai in range(acnt):
                    n = _apply_ai_army(h, atbl, ai, "ai")
                    total += n
                    if n:
                        details.append(f"组{gi}军{ai}+{n}")
            if w8(h, st + ST_SWITCHED, 1):
                total += 1
            if total != last_total:
                print(f"[{time.strftime('%H:%M:%S')}] 写 {total} 处" +
                      (f"（{','.join(details)}）" if details else "（无新增，全部已 AI）"), flush=True)
                last_total = total
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n监控停止")
    return 0



def unlock_speed(h, base):
    """速度解锁测试：清 st+0x31f0=0（疑似时间控制锁死开关），保留单位/军队 AI 接管字段。
    假设：P25 的变速失效 = 引擎看到 st+0x31f0==1（整场AI）→ 玩家时间控制禁用。
    敌方军队 a28c=1 时玩家可调速观战 → 军队级字段不锁时间，全局标志才锁。"""
    mgr, env, e8, st = resolve_e8(h, base)
    if not st:
        print("锚点链解析失败")
        return 1
    v = pb.read_u8(h, st + ST_SWITCHED)
    print(f"st+0x31f0 当前=0x{v if v is not None else -1:x}")
    if w8(h, st + ST_SWITCHED, 0):
        rb = pb.read_u8(h, st + ST_SWITCHED)
        print(f"已清零 → 回读 0x{rb if rb is not None else -1:x}")
        print("请测试：暂停/变速是否恢复？")
    return 0


def resolve_obj(h, base, obj, g, a, u, c):
    """解析链上对象地址。obj ∈ st/group/army/unit/child。返回地址或 None。"""
    mgr, env, e8, st = resolve_e8(h, base)
    if obj == "st":
        return st
    if not st:
        return None
    groups = walk_groups(h, st)
    if not groups or g >= len(groups):
        return None
    gg, acnt, atbl = groups[g]
    if obj == "group":
        return gg
    if a >= acnt:
        return None
    army = walk_army(h, atbl, a)
    if not army:
        return None
    if obj == "army":
        return army["addr"]
    if u >= army["unit_cnt"]:
        return None
    uaddr = pb.read_u32(h, army["unit_tbl"] + u * 4)
    if obj == "unit":
        return uaddr
    if c >= army["child_cnt"]:
        return None
    return pb.read_u32(h, army["child_tbl"] + c * 4)


def field_cmd(h, base, obj, g, a, u, c, off, val, size, dump_len):
    """通用字段读写：--obj st/group/army/unit/child --off <hex> [--val <int> --size b|w|f] 或 --len 转 dump。"""
    addr = resolve_obj(h, base, obj, g, a, u, c)
    if not addr:
        print(f"对象解析失败 obj={obj} g={g} a={a} u={u} c={c}")
        return 3
    full = addr + off
    if dump_len:
        b = pb.read_mem(h, full, dump_len)
        if b is None:
            print("读取失败")
            return 3
        print(f"[{obj}] 0x{addr:08x}+0x{off:x} = 0x{full:08x} ({dump_len} 字节):")
        for i in range(0, len(b), 16):
            chunk = b[i:i + 16]
            hexs = " ".join(f"{x:02x}" for x in chunk)
            txt = "".join(chr(x) if 32 <= x < 127 else "." for x in chunk)
            print(f"  +0x{i:02x}: {hexs:<47} {txt}")
        return 0
    if val is None:
        # 只读
        if size == "b":
            print(f"[{obj}] 0x{full:08x} +0x{off:x} = 0x{pb.read_u8(h, full):02x}")
        else:
            print(f"[{obj}] 0x{full:08x} +0x{off:x} = 0x{pb.read_u32(h, full):08x}")
        return 0
    n = 0
    if size == "b":
        n = 1 if w8(h, full, val & 0xFF) else 0
    elif size == "f":
        fbuf = ctypes.create_string_buffer(struct.pack("<f", val))
        got = ctypes.c_size_t()
        n = 4 if (pb.K32.WriteProcessMemory(h, ctypes.c_void_p(full), fbuf, 4, ctypes.byref(got))
                  and got.value == 4) else 0
    else:
        n = 4 if w32(h, full, val & 0xFFFFFFFF) else 0
    if size == "b":
        rb = pb.read_u8(h, full)
        print(f"[{obj}] 写 0x{val:x} → 回读 0x{rb if rb is not None else -1:x} ({'OK' if n else 'FAIL'})")
    else:
        rb = pb.read_u32(h, full)
        print(f"[{obj}] 写 0x{val:x} → 回读 0x{rb if rb is not None else -1:x} ({'OK' if n else 'FAIL'})")
    return 0


def write_ai_keep1(h, base, group_idx, army_idx, keep_unit=0):
    """方案1变体：除 keep_unit 外全部单位转 AI。玩家保留 1 个单位 → 战斗不进入
    AI-run 退化态（变速/ESC 应保持正常），其余部队 AI 控制。"""
    mgr, env, e8, st = resolve_e8(h, base)
    groups = walk_groups(h, st) if st else None
    if not groups or group_idx >= len(groups):
        print("组表解析失败")
        return 3
    g, acnt, atbl = groups[group_idx]
    if army_idx >= acnt:
        print("军队索引越界")
        return 3
    a = walk_army(h, atbl, army_idx)
    if not a:
        print("军队结构读取失败")
        return 3
    n = 0
    for i in range(a["unit_cnt"]):
        if i == keep_unit:
            continue  # 保留人控
        u = pb.read_u32(h, a["unit_tbl"] + i * 4)
        if not u:
            continue
        if pb.read_u32(h, u + UNIT_EA8) == 1:
            continue
        if w32(h, u + UNIT_EA8, 1):
            n += 1
        if w8(h, u + UNIT_C01, 1):
            n += 1
    print(f"[keep1] 已转 AI {n} 处（组{group_idx}军{army_idx}，保留单位{keep_unit} 人控；"
          f"{a['unit_cnt']} 单位中 {a['unit_cnt'] - 1} 个转 AI）")
    print("  注意：不写 st+0x31f0/军队字段/mgr+0xd18（保留正常战斗态）")
    u0 = pb.read_u32(h, a["unit_tbl"])
    print(f"  回读：unit0+0xea8=0x{pb.read_u32(h, u0 + UNIT_EA8) if u0 else -1:x}（人控应=0）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="RE-B3 BCQ 注入：正常战斗单军队切 AI（直调 handler 0x2abeb0）")
    ap.add_argument("--probe", action="store_true", help="只读：BCQ 链校准 + 组/军/单位 dump")
    ap.add_argument("--bytes", action="store_true", help="只读：校验运行时 DLL 关键地址字节 == 静态分析")
    ap.add_argument("--test-decode", action="store_true",
                    help="安全诊断：只调 0x284b40 解码器验证 reader/param 流（不碰游戏对象）")
    ap.add_argument("--test-handler", action="store_true",
                    help="诊断：调 handler + 进程内即时回读（区分「没写」vs「写了被帧回滚」）")
    ap.add_argument("--test-veh", action="store_true",
                    help="终极诊断：VEH 捕获 handler 崩溃确切 EIP + TLS 自检")
    ap.add_argument("--switch-ai-apc", action="store_true",
                    help="主线程 APC 注入：handler 在游戏主线程执行（TLS 正常）")
    ap.add_argument("--switch-ai-ctx", action="store_true",
                    help="线程上下文劫持：挂起主线程改EIP执行handler，NtContinue 还原")
    ap.add_argument("--write-ai-fields", action="store_true",
                    help="直接写 handler 的可见效果（无代码执行零崩溃风险；对照组验证 AI 接管）")
    ap.add_argument("--write-human-fields", action="store_true",
                    help="直接写复位（切回人控）")
    ap.add_argument("--unlock-speed", action="store_true",
                    help="清 st+0x31f0=0 解锁速度（保留单位 AI 接管）")
    ap.add_argument("--watch-ai", action="store_true",
                    help="监控循环：持续应用 AI 接管字段（覆盖援军/新单位）")
    ap.add_argument("--write-ai-keep1", action="store_true",
                    help="方案1：除指定单位外全部转 AI（保留玩家在场，防 AI-run 退化态）")
    ap.add_argument("--keep-unit", type=int, default=0, help="保留人控的单位索引（配 --write-ai-keep1）")
    ap.add_argument("--field", action="store_true", help="通用字段读写（配 --obj/--g/--a/--u/--c/--off/--val/--size/--len）")
    ap.add_argument("--obj", choices=["st", "group", "army", "unit", "child"], default="st")
    ap.add_argument("--g", type=int, default=0)
    ap.add_argument("--a", type=int, default=0)
    ap.add_argument("--u", type=int, default=0)
    ap.add_argument("--c", type=int, default=0)
    ap.add_argument("--off", type=lambda s: int(s, 0), default=0)
    ap.add_argument("--val", type=lambda s: int(s, 0), default=None)
    ap.add_argument("--size", choices=["b", "w", "f"], default="w")
    ap.add_argument("--len", type=lambda s: int(s, 0), default=0, help="dump 字节数（>0 时只读 dump）")
    ap.add_argument("--switch-ai", action="store_true", help="注入切 AI（先快照原值）")
    ap.add_argument("--verify", action="store_true", help="注入后回读验证")
    ap.add_argument("--switch-human", action="store_true", help="切回人控（按快照复位）")
    ap.add_argument("--group", type=int, default=0, help="组索引（默认 0=玩家组）")
    ap.add_argument("--army", type=int, default=0, help="军队索引（默认 0）")
    ap.add_argument("--force", action="store_true", help="状态非 3/5 也强行注入（谨慎）")
    args = ap.parse_args()

    pid = pb.find_pid()
    if pid is None:
        print("shogun2.exe 未运行。请先启动游戏并进入战斗（battle_ai 保持 0）。")
        return 1
    print(f"PID={pid}")
    h = pb.K32.OpenProcess(pb.PROCESS_QUERY_INFORMATION | pb.PROCESS_VM_READ |
                           pb.PROCESS_VM_WRITE | pb.PROCESS_VM_OPERATION, False, pid)
    if not h:
        print(f"OpenProcess 失败 err={ctypes.get_last_error()}（需管理员权限）")
        return 2
    base = pb.module_base(h, "empire.retail.dll")
    if base is None:
        print("未找到 empire.retail.dll")
        return 3
    print(f"empire.retail.dll base=0x{base:08x} (ASLR +0x{base - 0x10000000:x})")

    if args.probe or not (args.switch_ai or args.verify or args.switch_human
                          or args.bytes or args.test_decode or args.test_handler
                          or args.test_veh or args.switch_ai_apc or args.switch_ai_ctx
                          or args.write_ai_fields or args.write_human_fields
                          or args.unlock_speed or args.watch_ai or args.field
                          or args.write_ai_keep1):
        return probe(h, base)
    if args.bytes:
        return bytes_check(h, base)
    if args.test_decode:
        return test_decode(h, base)
    if args.test_handler:
        return test_handler(h, base, args.group, args.army)
    if args.test_veh:
        return test_veh(h, base, args.group, args.army)
    if args.switch_ai_apc:
        return switch_ai_apc(h, base, args.group, args.army)
    if args.switch_ai_ctx:
        return switch_ai_ctx(h, base, args.group, args.army)
    if args.write_ai_fields:
        return write_ai_fields(h, base, args.group, args.army, "ai")
    if args.write_human_fields:
        return write_ai_fields(h, base, args.group, args.army, "human")
    if args.unlock_speed:
        return unlock_speed(h, base)
    if args.watch_ai:
        return watch_ai(h, base)
    if args.write_ai_keep1:
        return write_ai_keep1(h, base, args.group, args.army, args.keep_unit)
    if args.field:
        return field_cmd(h, base, args.obj, args.g, args.a, args.u, args.c,
                         args.off, args.val, args.size, args.len)

    if args.switch_ai:
        return switch_ai(h, base, args.group, args.army)
    if args.verify:
        return verify(h, base, args.group, args.army)
    if args.switch_human:
        return switch_human(h, base, args.group, args.army)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
