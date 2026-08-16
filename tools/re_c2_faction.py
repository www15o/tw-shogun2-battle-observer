# -*- coding: utf-8 -*-
"""re_c2_faction.py — 目标2（看海）运行时 faction 对象直写工具（2026-08-09）

依据（re_c1_notes §12.9 推论）：
- grant_faction_handover 单机必退（本地派系 +0x7a8=1 → 无人类退出）。
- **清 faction+0x6a0（FactionIsHuman）= 0**（不动 +0x7a8）→ 退出检查不读 +0x6a0
  （handover 没碰它却也退出）→ CAI 接管（manager 已是 sho_major）+ 不退出 = 看海。
- SP 战役唯一 FactionIsHuman==1 的 faction = 本地（织田）→ +0x6a0==1 可作扫描判别。

faction 对象签名（静态确证字段）：
  [+0x6a0] byte = FactionIsHuman（1=人类）
  [+0x7a8] byte = 已移交（handover 写 1；0=正常）
  [+0x708] dword 小整数
  [+0x6e0] dword 小整数
  [+0x8c]  ptr → [+8] → [+0x14c4] 链（FUN_1059ab20 用 [faction+0x8c]→[8]→[0x14c4]）
  [+0x53c] ptr（handover 通知表）

用法（先 --scan 校准再写）：
  python -u tools/re_c2_faction.py --scan                 # 扫 faction 对象（默认 +0x6a0==1）
  python -u tools/re_c2_faction.py --scan-any             # 扫全部 faction 形态对象（诊断）
  python -u tools/re_c2_faction.py --probe <ADDR>         # 只读 dump 字段
  python -u tools/re_c2_faction.py --human-flag 0 <ADDR>  # 写 +0x6a0=0（看海）
  python -u tools/re_c2_faction.py --human-flag 1 <ADDR>  # 恢复 +0x6a0=1

安全纪律（AGENTS）：写入前强制结构验证（+0x6a0/+0x708/+0x7a8/+0x8c 链合理），失败禁止写。
"""
import argparse
import ctypes
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb

OFF_HUMAN = 0x6a0    # byte FactionIsHuman
OFF_708 = 0x708      # dword
OFF_7A8 = 0x7a8      # byte handed-over
OFF_6E0 = 0x6e0      # dword
OFF_8C = 0x8c        # ptr → [+8] → [+0x14c4]
OFF_53C = 0x53c      # ptr（handover 通知表）
OFF_0 = 0x0          # vtable ptr


def is_ptr(v):
    return isinstance(v, int) and 0x10000 <= v <= 0x7fffffff and (v & 0xfff) != 0xfffff


def open_game():
    pid = pb.find_pid()
    if pid is None:
        print("✗ shogun2.exe 未运行")
        return None, None
    h = pb.K32.OpenProcess(pb.PROCESS_QUERY_INFORMATION | pb.PROCESS_VM_READ |
                           pb.PROCESS_VM_WRITE | pb.PROCESS_VM_OPERATION, False, pid)
    if not h:
        print(f"✗ OpenProcess 失败 err={ctypes.get_last_error()}（需管理员权限）")
        return None, None
    base = pb.module_base(h, "empire.retail.dll")
    if base is None:
        print("✗ 未找到 empire.retail.dll")
        return None, None
    print(f"✓ PID={pid} base=0x{base:08x}")
    return h, base


def r8(h, a):
    b = pb.read_mem(h, a, 1)
    return b[0] if b else None


def r32(h, a):
    return pb.read_u32(h, a)


def valid_chain_8c(h, addr, base=None):
    """验证 [+0x8c] → [+8] → [+0x14c4] 链可读（+0x14c4 应为 vtable，可选 DLL 范围校验）。"""
    p1 = r32(h, addr + OFF_8C)
    if not is_ptr(p1):
        return False
    p2 = r32(h, p1 + 8)
    if not is_ptr(p2):
        return False
    v = r32(h, p2 + 0x14c4)
    if not is_ptr(v):
        return False
    if base is not None:
        if not (base <= v < base + 0x1900000):
            return False
    return True


def is_faction_like(h, addr, base=None):
    """结构签名验证（宽松）。base 提供时校验 vtable 在 DLL 映射区。"""
    v0 = r32(h, addr + OFF_0)
    if not is_ptr(v0):
        return False
    if base is not None and not (base <= v0 < base + 0x1900000):
        return False
    h6a0 = r8(h, addr + OFF_HUMAN)
    if h6a0 not in (0, 1):
        return False
    h7a8 = r8(h, addr + OFF_7A8)
    if h7a8 not in (0, 1):
        return False
    v708 = r32(h, addr + OFF_708)
    if v708 is None or v708 > 16:
        return False
    v6e0 = r32(h, addr + OFF_6E0)
    if v6e0 is None or v6e0 > 16:
        return False
    v53c = r32(h, addr + OFF_53C)
    if not is_ptr(v53c):
        return False
    return True


def scan(h, base, human_only=True, max_cands=40):
    """全内存扫 faction 对象。human_only=True 时只收 +0x6a0==1。"""
    regions = pb.readable_regions(h)
    cands = []
    for rgn_start, rgn_size in regions:
        if rgn_size < 0x800:
            continue
        buf = pb.read_mem(h, rgn_start, min(rgn_size, 0x200000))
        if not buf:
            continue
        limit = len(buf) - 0x7b0
        for off in range(0, limit, 4):
            a = rgn_start + off
            if human_only and buf[off + OFF_HUMAN] != 1:
                continue
            if not human_only and buf[off + OFF_HUMAN] not in (0, 1):
                continue
            if buf[off + OFF_7A8] not in (0, 1):
                continue
            v708 = struct.unpack_from('<I', buf, off + OFF_708)[0]
            v6e0 = struct.unpack_from('<I', buf, off + OFF_6E0)[0]
            if v708 > 16 or v6e0 > 16:
                continue
            if not is_ptr(struct.unpack_from('<I', buf, off + OFF_53C)[0]):
                continue
            v0 = struct.unpack_from('<I', buf, off + OFF_0)[0]
            if not is_ptr(v0) or not (base <= v0 < base + 0x1900000):
                continue
            if not valid_chain_8c(h, a, base):
                continue
            cands.append(a)
            if len(cands) >= max_cands:
                return cands
    return cands


def dump_faction(h, addr):
    print(f"--- faction 0x{addr:08x} ---")
    for off, lab in [(OFF_0, "vtable"), (OFF_6E0, "+0x6e0"), (OFF_HUMAN, "+0x6a0 human"),
                     (OFF_708, "+0x708"), (OFF_7A8, "+0x7a8 handed"), (OFF_53C, "+0x53c"),
                     (OFF_8C, "+0x8c")]:
        if lab in ("+0x6a0 human", "+0x7a8 handed"):
            v = r8(h, addr + off)
            print(f"  {lab:12s} = {v}")
        else:
            v = r32(h, addr + off)
            print(f"  {lab:12s} = 0x{v:08x}" if v and v > 0x10000 else f"  {lab:12s} = {v}")
    # +0x8c 链
    p1 = r32(h, addr + OFF_8C)
    if is_ptr(p1):
        p2 = r32(h, p1 + 8)
        if is_ptr(p2):
            print(f"  [+0x8c]->[8]->[0x14c4] = 0x{r32(h, p2 + 0x14c4):08x}")


def validate_for_write(h, addr):
    """写入前结构验证（AGENTS：验证失败禁止写）。"""
    if not is_ptr(r32(h, addr + OFF_0)):
        print("✗ vtable 无效，拒绝写")
        return False
    if r8(h, addr + OFF_HUMAN) not in (0, 1):
        print("✗ +0x6a0 非法，拒绝写")
        return False
    if not valid_chain_8c(h, addr):
        print("✗ +0x8c 链无效，拒绝写")
        return False
    return True


def write_byte(h, addr, val):
    buf = ctypes.create_string_buffer(struct.pack("<B", val))
    got = ctypes.c_size_t()
    ok = bool(pb.K32.WriteProcessMemory(h, ctypes.c_void_p(addr), buf, 1, ctypes.byref(got)))
    return ok and got.value == 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="扫 +0x6a0==1 的 faction 对象")
    ap.add_argument("--scan-any", action="store_true", help="扫全部 faction 形态对象")
    ap.add_argument("--probe", metavar="ADDR", help="只读 dump faction 字段")
    ap.add_argument("--human-flag", metavar="0|1", help="写 +0x6a0（0=看海，1=恢复）")
    ap.add_argument("addr", nargs="?", help="faction 地址（写模式用）")
    args = ap.parse_args()

    h, base = open_game()
    if not h:
        sys.exit(1)

    if args.scan or args.scan_any:
        cands = scan(h, base, human_only=not args.scan_any)
        print(f"候选 {len(cands)} 个（{'人类 +0x6a0==1' if not args.scan_any else '全部形态'}）:")
        for a in cands:
            print(f"  0x{a:08x}  6a0={r8(h, a+OFF_HUMAN)} 7a8={r8(h, a+OFF_7A8)} 708={r32(h, a+OFF_708)} 6e0={r32(h, a+OFF_6E0)}")
        return

    if args.probe:
        dump_faction(h, int(args.probe, 16))
        return

    if args.human_flag is not None and args.addr:
        addr = int(args.addr, 16)
        val = int(args.human_flag)
        if val not in (0, 1):
            print("✗ human-flag 只能 0/1")
            sys.exit(1)
        cur = r8(h, addr + OFF_HUMAN)
        print(f"写入前: +0x6a0={cur}（{'人类' if cur else '非人类'}）")
        if not validate_for_write(h, addr):
            sys.exit(1)
        ok = write_byte(h, addr + OFF_HUMAN, val)
        back = r8(h, addr + OFF_HUMAN)
        print(f"写 +0x6a0={val} → {'✓ 回读 ' + str(back) if ok else '✗ 写失败'}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
