# -*- coding: utf-8 -*-
"""s2_watch.py — 目标2/3 一键看海工具（全自动定位，2026-08-09；多版本 2026-08-12）

支持版本：S2 原版 / FOTS（武家之殇）/ ROTS（武家崛起）——2023 更新后共用
Empire.Retail.dll，faction 结构（vtable RVA 0x15fac30 / +0x6a0 human / +0x4fc 国库）
与 manager 表（campaign 对象 +0x644 cap / +0x648 cnt / +0x64c tbl，条目 {faction_ptr, m}）
完全同构，本工具版本无关。

所有地址动态定位（跨会话 churn 免疫）：
  1. faction 定位：vtable 0x7a0bac30 全区域扫描 + faction+0x0b14 中文名匹配（UTF-16）
  2. obj_A 定位：cnt/tbl 签名 + manager 表条目 key 是 faction 校验（2026-08-12 修正：
     cap 从 1 动态增长，旧 cap∈[40,100] 条件会漏真对象；条目 key 直接是 faction，
     保留 key+0x164 回退兼容 S2 旧记录）
  3. manager 条目：条目 key==faction（直接）或 key+0x164→faction（回退）
  4. 写入/恢复：+0x6a0 + manager_id，写前结构验证 + 回读

用法：
  python -u tools/s2_watch.py --faction 织田 --watch     # 看海：+0x6a0=0 + manager=FULL_MANAGER(0)
  python -u tools/s2_watch.py --faction 织田 --restore   # 恢复：+0x6a0=1 + manager=HUMAN(7)
  python -u tools/s2_watch.py --list                     # 列出全部 faction 名+状态（只读）
  python -u tools/s2_watch.py --faction 织田 --info      # 只读显示目标 faction 状态
  python -u tools/s2_watch.py --faction 伊达 --watch --cache   # 缓存定位（首次~10s，之后毫秒级）
  python -u tools/s2_watch.py --faction 伊达 --restore --cache # 缓存恢复（毫秒级，AI 过回合快时用）
  python -u tools/s2_watch.py --faction 伊达 --watch --recache # 强制重新定位（读档/重进后地址失效）

缓存说明：--cache 首次扫描定位后把 faction/manager 写点存 work/.watch_cache.json；
之后 --cache 直接读缓存毫秒级执行（适合 AI 过回合极快的场景）。地址失效（读档/重进）自动回退全扫。

安全纪律（AGENTS）：写入前结构验证（vtable/human 字段/表结构），失败禁止写；写后回读确认。
"""
import argparse
import ctypes
import json
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb
import re_c2_faction as fc

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".watch_cache.json")

WINDOW = 0x200000
VTABLE_RVA = 0x15fac30
OFF_HUMAN = 0x6a0
OFF_6D8 = 0x6d8
OFF_4FC = 0x4fc
OFF_0B14 = 0x0b14
OFF_CAP = 0x644
OFF_CNT = 0x648
OFF_TBL = 0x64c

MANAGERS = {
    0: "FULL_MANAGER", 1: "MAINTAINANCE", 2: "REBELLION", 3: "END_TURN",
    4: "DO_NOTHING", 5: "EUROPEAN_TRADERS", 6: "MOVE_THINGS", 7: "HUMAN",
    8: "DB", 9: "END_TURN_ALLOW_DIPLOMACY",
}


def read_utf16_name(h, addr):
    """读 faction+0x0b14 指向的 UTF-16 中文名。"""
    if not addr or not (0x10000 <= addr < 0x80000000):
        return None
    b = pb.read_mem(h, addr, 32)
    if not b:
        return None
    try:
        s = b.decode("utf-16-le").split("\x00")[0]
        return s if s.isprintable() else None
    except Exception:
        return None


def readable_regions_fast(h):
    """VirtualQueryEx 枚举 MEM_COMMIT 可读区域（2026-08-12 修复：readable_regions
    试读法漏 1017 个区域（低地址 0x00xxxxxx），导致 faction 扫描漏派系——用户实机
    发现缺德川/上杉/毛利/长宗我部。VirtualQueryEx 覆盖完整且快（30ms vs 2.3s）。"""
    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", ctypes.c_ulong), ("RegionSize", ctypes.c_size_t),
                    ("State", ctypes.c_ulong), ("Protect", ctypes.c_ulong), ("Type", ctypes.c_ulong)]
    K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    K32.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
    K32.VirtualQueryEx.restype = ctypes.c_size_t
    out = []
    addr = 0x10000
    while addr < 0x7f000000:
        mbi = MBI()
        if not K32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        size = mbi.RegionSize or 0x1000
        p = mbi.Protect
        if (mbi.State & 0x1000) and p != 0x01 and not (p & 0x100) and \
           (p & (0x02 | 0x04 | 0x08 | 0x20 | 0x40 | 0x80)):
            out.append((int(mbi.BaseAddress), int(mbi.BaseAddress) + int(size)))
        addr = int(mbi.BaseAddress) + size
    return out


def scan_factions(h, base):
    """全区域扫 faction 对象，返回 [(addr, human, name, treasury)]。
    区域枚举用 VirtualQueryEx（readable_regions 试读法漏低地址区域 → 漏派系）。"""
    regions = readable_regions_fast(h)
    runtime_vtable = base + VTABLE_RVA
    facs = []
    for rs, re in regions:
        if re - rs < 0x800:
            continue
        start = rs
        while start < re:
            size = min(WINDOW, re - start)
            if size <= 0x800:
                break
            buf = pb.read_mem(h, start, size)
            if buf is None or len(buf) < 0x800:
                start += size
                continue
            n4 = len(buf) // 4
            lim = n4 - (0x800 // 4)
            arr = np.frombuffer(buf, dtype="<u4", count=n4)
            m = arr[:lim] == runtime_vtable
            idx = np.nonzero(m)[0]
            for j in idx:
                a = start + int(j) * 4
                h6 = buf[j * 4 + OFF_HUMAN]
                if h6 not in (0, 1):
                    continue
                h7a8 = buf[j * 4 + 0x7a8]
                if h7a8 not in (0, 1):
                    continue
                name_ptr = struct.unpack_from("<I", buf, j * 4 + OFF_0B14)[0]
                name = read_utf16_name(h, name_ptr)
                tr = struct.unpack_from("<I", buf, j * 4 + OFF_4FC)[0]
                facs.append((a, h6, name, tr))
            start += size - 0x800
    return facs


def scan_objA(h, base, max_cands=5):
    """定位 obj_A（campaign AI manager，含 manager 表 +0x644 cap/+0x648 cnt/+0x64c tbl）。

    2026-08-12 静态修正（re_fots_manager_static_report.md）：cap 从 1 动态增长
    （0xb4d274 扩容逻辑），旧 cap∈[40,100] 条件会漏真对象（FOTS 实机教训）；
    改为 cnt∈[30,120] + 表指针合法粗筛，再由 verify_manager_table 精筛
    （条目 key 是 faction vtable 或 key+0x164→faction）。"""
    regions = pb.readable_regions(h)
    found = []
    for rs, re in regions:
        if re - rs < 0x700:
            continue
        start = rs
        while start < re:
            size = min(WINDOW, re - start)
            if size <= 0x700:
                break
            buf = pb.read_mem(h, start, size)
            if buf is None or len(buf) < 0x700:
                start += size
                continue
            n4 = len(buf) // 4
            lim = n4 - (0x700 // 4)
            arr = np.frombuffer(buf, dtype="<u4", count=n4)
            cnt_a = arr[OFF_CNT // 4: OFF_CNT // 4 + lim]
            tbl_a = arr[OFF_TBL // 4: OFF_TBL // 4 + lim]
            m = (cnt_a >= 30) & (cnt_a <= 120) & (tbl_a >= 0x10000) & (tbl_a < 0x80000000)
            idx = np.nonzero(m)[0]
            for j in idx:
                a = start + int(j) * 4
                cnt = int(cnt_a[j])
                tbl = int(tbl_a[j])
                ok, nh = verify_manager_table(h, a, cnt, tbl, base)
                if ok:
                    found.append((a, cnt, tbl, nh))
                    if len(found) >= max_cands:
                        return found
            start += size - 0x700
    return found


def verify_manager_table(h, obj, cnt, tbl, base=None):
    """验证 manager 表：cnt 条 {key, m}，m<=9，且条目 key 是 faction
    （vtable==runtime faction vtable，或 key+0x164→faction 回退兼容）。
    至少 60% 条目命中才认（防链表/数组误报）。返回 (ok, n_human)。"""
    if cnt <= 0 or cnt > 200:
        return False, 0
    fvt = (base + VTABLE_RVA) if base else 0
    key_hits = 0
    nh = 0
    for i in range(cnt):
        b = pb.read_mem(h, tbl + i * 8, 8)
        if b is None:
            return False, 0
        k, m = struct.unpack("<II", b)
        if m > 9:
            return False, 0
        if not (0x10000 <= k < 0x80000000):
            return False, 0
        if m == 7:
            nh += 1
        if fvt:
            # 模式1：key 直接是 faction 对象（[k]==faction vtable，0xb4d210 确证）
            if pb.read_u32(h, k) == fvt:
                key_hits += 1
            else:
                # 模式2：key+0x164 → faction 对象指针（S2 旧记录），再解引用验 vtable
                fp = pb.read_u32(h, k + 0x164)
                if fp and 0x10000 <= fp < 0x80000000 and pb.read_u32(h, fp) == fvt:
                    key_hits += 1
    return (key_hits * 10 >= cnt * 6), nh


def find_manager_entry(h, objA, tbl, cnt, faction_addr):
    """在 manager 表找指向 faction_addr 的条目。
    模式1：条目 key 直接 == faction（2026-08-12 静态确证 0xb4d210，FOTS/ROTS）；
    模式2：key+0x164 → faction（S2 旧记录兼容回退）。返回 (idx, key, m)。"""
    # 模式1：key 直接是 faction 对象
    for i in range(cnt):
        k, m = struct.unpack("<II", pb.read_mem(h, tbl + i * 8, 8))
        if k == faction_addr:
            return i, k, m
    # 模式2：key 对象 +0x164 → faction（S2 兼容）
    for i in range(cnt):
        k, m = struct.unpack("<II", pb.read_mem(h, tbl + i * 8, 8))
        if pb.read_u32(h, k + 0x164) == faction_addr:
            return i, k, m
    return None


def write32(h, addr, v):
    buf = ctypes.create_string_buffer(struct.pack("<I", v))
    got = ctypes.c_size_t()
    ok = bool(pb.K32.WriteProcessMemory(h, ctypes.c_void_p(addr), buf, 4, ctypes.byref(got)))
    return ok and got.value == 4


def write8(h, addr, v):
    buf = ctypes.create_string_buffer(struct.pack("<B", v))
    got = ctypes.c_size_t()
    ok = bool(pb.K32.WriteProcessMemory(h, ctypes.c_void_p(addr), buf, 1, ctypes.byref(got)))
    return ok and got.value == 1


def validate_faction(h, base, a):
    """写入前验证 faction 结构。"""
    v0 = pb.read_u32(h, a)
    if not (base <= v0 < base + 0x1900000):
        return False, f"vtable 无效 0x{v0:08x}"
    if pb.read_u8(h, a + OFF_HUMAN) not in (0, 1):
        return False, "+0x6a0 非法"
    return True, "OK"


def do_watch(h, base, faction_addr, manager_target):
    """看海：+0x6a0=0 + manager=FULL_MANAGER(0)。"""
    ok, msg = validate_faction(h, base, faction_addr)
    if not ok:
        print(f"✗ faction 验证失败：{msg}")
        return False
    # 写 +0x6a0=0
    w1 = write8(h, faction_addr + OFF_HUMAN, 0)
    r1 = pb.read_u8(h, faction_addr + OFF_HUMAN)
    # 写 manager=FULL_MANAGER(0)
    w2 = write32(h, manager_target, 0)
    r2 = pb.read_u32(h, manager_target)
    print(f"  看海: +0x6a0→0 {'✓' if w1 and r1==0 else '✗'}  回读={r1}")
    print(f"  manager→FULL_MANAGER(0) {'✓' if w2 and r2==0 else '✗'}  回读={r2}")
    return (w1 and r1 == 0) and (w2 and r2 == 0)


def do_restore(h, base, faction_addr, manager_target):
    """恢复：+0x6a0=1 + manager=HUMAN(7)。"""
    ok, msg = validate_faction(h, base, faction_addr)
    if not ok:
        print(f"✗ faction 验证失败：{msg}")
        return False
    w1 = write8(h, faction_addr + OFF_HUMAN, 1)
    r1 = pb.read_u8(h, faction_addr + OFF_HUMAN)
    w2 = write32(h, manager_target, 7)
    r2 = pb.read_u32(h, manager_target)
    print(f"  恢复: +0x6a0→1 {'✓' if w1 and r1==1 else '✗'}  回读={r1}")
    print(f"  manager→HUMAN(7) {'✓' if w2 and r2==7 else '✗'}  回读={r2}")
    return (w1 and r1 == 1) and (w2 and r2 == 7)


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_cache(faction_addr, manager_target, objA):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"faction_addr": faction_addr, "manager_target": manager_target,
                       "objA": objA}, f)
    except Exception as e:
        print(f"⚠️ 缓存写入失败: {e}")


def cache_valid(h, base, cache):
    """缓存地址仍有效（vtable 匹配 + human 字段合法）。"""
    a = cache.get("faction_addr")
    if not a:
        return False
    v0 = pb.read_u32(h, a)
    if not (base <= v0 < base + 0x1900000):
        return False
    if pb.read_u8(h, a + OFF_HUMAN) not in (0, 1):
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description="s2_watch — 一键看海/恢复")
    ap.add_argument("--faction", help="派系中文名（如 织田/武田/伊达）")
    ap.add_argument("--watch", action="store_true", help="看海：+0x6a0=0 + manager=FULL_MANAGER")
    ap.add_argument("--restore", action="store_true", help="恢复：+0x6a0=1 + manager=HUMAN")
    ap.add_argument("--list", action="store_true", help="列出全部 faction（只读）")
    ap.add_argument("--info", action="store_true", help="显示目标 faction 状态（只读）")
    ap.add_argument("--cache", action="store_true", help="用缓存地址（首次自动定位并缓存）")
    ap.add_argument("--recache", action="store_true", help="强制重新定位并更新缓存")
    args = ap.parse_args()

    h, base = fc.open_game()
    if not h:
        sys.exit(1)

    try:
        # 缓存路径：优先读缓存（若 --cache 且缓存有效且未 --recache）
        cache = load_cache()
        if (args.cache and not args.recache and cache and cache_valid(h, base, cache)
                and not args.list):
            a = cache["faction_addr"]
            manager_target = cache["manager_target"]
            name = read_utf16_name(h, pb.read_u32(h, a + OFF_0B14))
            print(f"⚡ 缓存命中: {name!r} faction=0x{a:08x} manager写点=0x{manager_target:08x}")
            if args.info:
                print(f"  状态: human={pb.read_u8(h, a+OFF_HUMAN)} "
                      f"manager={pb.read_u32(h, manager_target)}({MANAGERS.get(pb.read_u32(h, manager_target),'?')}) "
                      f"国库={pb.read_u32(h, a+OFF_4FC)}")
                return
            if args.watch:
                ok = do_watch(h, base, a, manager_target)
            elif args.restore:
                ok = do_restore(h, base, a, manager_target)
            else:
                print("需 --watch 或 --restore")
                return
            print("✅" if ok else "⚠️ 部分失败，检查回读")
            return

        # 常规路径：全扫定位
        facs = scan_factions(h, base)
        print(f"扫描到 {len(facs)} 个 faction")

        if args.list:
            for a, h6, name, tr in facs:
                print(f"  0x{a:08x} human={h6} 国库={tr:6d} 名={name!r}")
            return

        # 未指定派系名 → 自动选人类派系（玩家）
        if not args.faction:
            human = [x for x in facs if x[1] == 1]
            if not human:
                print("✗ 未找到人类派系（玩家），请用 --faction 指定")
                ap.print_help()
                return
            args.faction = human[0][2] or f"<human@{human[0][0]:x}>"
            print(f"▶ 自动选玩家派系: {args.faction}")

        target = None
        for a, h6, name, tr in facs:
            if name == args.faction:
                target = (a, h6, name, tr)
                break
        if not target:
            print(f"✗ 未找到派系 {args.faction!r}（可用 --list 查看）")
            sys.exit(1)
        a, h6, name, tr = target
        print(f"✓ 目标: {name!r} 0x{a:08x} human={h6} 国库={tr}")

        objAs = scan_objA(h, base)
        if not objAs:
            print("✗ obj_A 未定位")
            sys.exit(1)
        objAs.sort(key=lambda x: -x[3])
        objA, cnt, tbl, nh = objAs[0]
        print(f"✓ obj_A=0x{objA:08x} cnt={cnt} tbl=0x{tbl:08x} HUMAN条目={nh}")

        entry = find_manager_entry(h, objA, tbl, cnt, a)
        if not entry:
            print("✗ 未找到该 faction 的 manager 条目")
            sys.exit(1)
        idx, key, m = entry
        manager_target = tbl + idx * 8 + 4
        print(f"✓ manager[{idx}] key=0x{key:08x} m={m}({MANAGERS.get(m,'?')}) 写点=0x{manager_target:08x}")

        if args.cache:
            save_cache(a, manager_target, objA)
            print("💾 缓存已保存（下次 --cache 毫秒级）")

        if args.info:
            print(f"\n目标状态: human={pb.read_u8(h, a+OFF_HUMAN)} "
                  f"manager={pb.read_u32(h, manager_target)}({MANAGERS.get(pb.read_u32(h, manager_target),'?')}) "
                  f"国库={pb.read_u32(h, a+OFF_4FC)}")
            return

        if args.watch:
            print("\n== 看海 ==")
            ok = do_watch(h, base, a, manager_target)
        elif args.restore:
            print("\n== 恢复 ==")
            ok = do_restore(h, base, a, manager_target)
        else:
            print("需 --watch 或 --restore")
            return
        print("✅" if ok else "⚠️ 部分失败，检查回读")
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


if __name__ == "__main__":
    main()
