# -*- coding: utf-8 -*-
"""s2_money.py — 幕府2（原版 S2 / 武家之殇 FOTS）改钱工具（2026-08-12）

定位原理（2026-08-12 实机确证，FOTS 战役）：
- **原版 S2**：国库 = faction 对象 [+0x4fc]（权威字段，s2_watch 已能读）。
- **FOTS**：国库在「经济子对象 +0x54」（vtable RVA ~0x172225c），但它是**显示镜像**
  ——每帧被引擎从权威副本覆盖，写入无效。
- **FOTS 权威字段**：值差分法定位 + 写入测试区分（写后等 0.3s 重读：
  保持新值=权威；被覆盖回旧值=镜像）。权威字段写入 UI 立即生效。
- 跨会话地址 churn：每轮用 --auto 重新差分，或用 --cache 复用已定位地址（校验后）。

用法：
  # 已知地址直接设/读
  python -u tools/s2_money.py --addr 0x25640544 --set 1000000
  python -u tools/s2_money.py --addr 0x25640544 --get
  # 自动差分定位（两轮，每轮需用户在游戏内改国库值）
  python -u tools/s2_money.py --auto --v1 9000      # 第1轮：扫当前值落盘
  python -u tools/s2_money.py --auto --v2 2565      # 第2轮：扫新值+差分+权威判定
  python -u tools/s2_money.py --auto --v2 2565 --set 1000000   # 判定后直接设目标值
  # 缓存复用（跨会话地址失效自动重扫提示）
  python -u tools/s2_money.py --cache --set 1000000
  python -u tools/s2_money.py --cache --get

权威判定细节：对每个差分候选写 旧值+777，sleep 0.4s 后重读：
  - 仍 == 旧值+777 → 权威（写有效，UI 直读）
  - 被覆盖回旧值     → 镜像（引擎同步，写无效）
  判权威后立即恢复旧值，避免脏数据。
"""
import argparse
import ctypes
import json
import os
import struct
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb
import re_c2_faction as fc

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".money_cache.json")
DIFF_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".money_v1.bin")


def r32(h, a):
    return pb.read_u32(h, a)


def w32(h, a, v):
    buf = ctypes.create_string_buffer(struct.pack("<I", v))
    got = ctypes.c_size_t()
    ok = pb.K32.WriteProcessMemory(h, ctypes.c_void_p(a), buf, 4, ctypes.byref(got))
    return bool(ok) and got.value == 4


def load_bin(path):
    if not os.path.exists(path):
        return None
    return np.fromfile(path, dtype="<u4")


def readable_regions_fast(h):
    """VirtualQueryEx 枚举 MEM_COMMIT 可读区域（CE 同款，毫秒级）。"""
    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", ctypes.c_ulong), ("RegionSize", ctypes.c_size_t),
                    ("State", ctypes.c_ulong), ("Protect", ctypes.c_ulong), ("Type", ctypes.c_ulong)]
    K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    K32.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
    K32.VirtualQueryEx.restype = ctypes.c_size_t
    regions = []
    addr = 0x10000
    while addr < 0x7f000000:
        mbi = MBI()
        if not K32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        size = mbi.RegionSize or 0x1000
        p = mbi.Protect
        if (mbi.State & 0x1000) and p != 0x01 and not (p & 0x100) and \
           (p & (0x02 | 0x04 | 0x08 | 0x20 | 0x40 | 0x80)):
            regions.append((int(mbi.BaseAddress), int(size)))
        addr = int(mbi.BaseAddress) + size
    return regions


def scan_value(h, value):
    """全内存扫 int32==value，返回 np uint32 数组。"""
    regions = readable_regions_fast(h)
    hits = []
    for rs, size in regions:
        start, remain = rs, size
        while remain > 0:
            chunk = min(remain, 0x800000)
            buf = pb.read_mem(h, start, chunk)
            if buf is None or len(buf) < 4:
                start += chunk
                remain -= chunk
                continue
            arr = np.frombuffer(buf, dtype="<u4", count=len(buf) // 4)
            m = arr == value
            if m.any():
                idx = np.nonzero(m)[0]
                hits.append((start + idx.astype(np.uint64) * 4).astype(np.uint32))
            start += chunk
            remain -= chunk
    return np.concatenate(hits) if hits else np.array([], dtype="<u4")


def probe_authoritative(h, cand, old_val):
    """写候选测试权威性：写 old_val+777 → 等 0.4s → 重读。
    返回 (addr, is_authoritative)。判定后恢复 old_val。"""
    test = old_val + 777
    w32(h, cand, test)
    time.sleep(0.4)
    now = r32(h, cand)
    if now == test:
        w32(h, cand, old_val)  # 恢复原值
        return cand, True
    return cand, False


def do_auto_v1(h, v1):
    print(f"第1轮：扫当前国库值 {v1} ...")
    hits = scan_value(h, v1)
    print(f"  命中 {len(hits)} 个，落盘 {DIFF_BIN}")
    hits.tofile(DIFF_BIN)
    print("  下一步：在游戏内改变国库（招募/过回合），然后跑 --auto --v2 <新值>")


def do_auto_v2(h, v2, set_amount=None):
    prev = load_bin(DIFF_BIN)
    if prev is None:
        print("✗ 无第1轮数据，先跑 --auto --v1 <值>")
        return 1
    print(f"第2轮：扫新值 {v2} ...")
    hits = scan_value(h, v2)
    common = sorted({int(a) for a in prev} & {int(a) for a in hits})
    print(f"  差分：前次 {len(prev)} ∩ 本次 {len(hits)} = {len(common)} 个")
    if not common:
        print("✗ 无交集——国库值没变或数值不符")
        return 3
    # 权威判定
    auth = []
    for a in common:
        addr, is_auth = probe_authoritative(h, a, v2)
        if is_auth:
            auth.append(addr)
            print(f"  ✅ 权威字段 0x{a:08x}（写入有效，UI 直读）")
        else:
            print(f"  ⚪ 镜像字段 0x{a:08x}（被引擎覆盖，写入无效）")
    if not auth:
        print("✗ 无权威字段——建议再跑一轮差分（值变化后 --v2）")
        return 4
    addr = auth[0]
    print(f"▶ 权威国库地址 = 0x{addr:08x}")
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"addr": addr}, f)
    print(f"💾 已缓存 {CACHE_FILE}")
    if set_amount is not None:
        return set_money(h, addr, set_amount)
    return 0


def set_money(h, addr, amount):
    old = r32(h, addr)
    print(f"国库 0x{addr:08x}：{old} → {amount}")
    if not w32(h, addr, amount):
        print("✗ 写入失败")
        return 5
    back = r32(h, addr)
    ok = back == amount
    print(f"  写入 → 回读 {back} {'✅' if ok else '✗ 不一致'}")
    return 0 if ok else 6


def main():
    ap = argparse.ArgumentParser(description="s2_money — 改钱工具（S2/FOTS）")
    ap.add_argument("--addr", default=None, help="国库地址（十六进制）")
    ap.add_argument("--set", type=int, default=None, help="设置国库金额")
    ap.add_argument("--get", action="store_true", help="读取国库金额")
    ap.add_argument("--auto", action="store_true", help="自动差分定位模式")
    ap.add_argument("--v1", type=int, default=None, help="第1轮：当前国库值")
    ap.add_argument("--v2", type=int, default=None, help="第2轮：变化后的国库值")
    ap.add_argument("--cache", action="store_true", help="用缓存地址（校验后使用）")
    args = ap.parse_args()

    h, base = fc.open_game()
    if not h:
        sys.exit(1)
    try:
        # 自动差分模式
        if args.auto:
            if args.v1 is not None:
                do_auto_v1(h, args.v1)
                return 0
            if args.v2 is not None:
                return do_auto_v2(h, args.v2, args.set)
            print("✗ --auto 需 --v1 或 --v2")
            return 2

        # 地址来源：--addr 或 --cache
        addr = None
        if args.addr:
            addr = int(args.addr, 16)
        elif args.cache:
            try:
                with open(CACHE_FILE, encoding="utf-8") as f:
                    addr = json.load(f)["addr"]
            except Exception:
                print(f"✗ 缓存不可用（{CACHE_FILE}）——请先 --auto 定位")
                return 2
            # 校验缓存地址仍有效：可读且值合理
            v = r32(h, addr)
            if v is None or v > 0x10000000:
                print(f"✗ 缓存地址 0x{addr:08x} 已失效（读={v}）——请重新 --auto")
                return 2
            print(f"⚡ 缓存地址 0x{addr:08x} 当前值 {v}")

        if addr is None:
            ap.print_help()
            return 2
        if args.get:
            print(f"国库 = {r32(h, addr)}")
            return 0
        if args.set is not None:
            return set_money(h, addr, args.set)
        ap.print_help()
        return 2
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


if __name__ == "__main__":
    raise SystemExit(main())
