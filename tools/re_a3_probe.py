# -*- coding: utf-8 -*-
"""Route A 目标3 探针：pending battle 生命周期观测（H40 决定性实验）。

背景（27_HANDOFF_ROUTE_A §2.2/§2.3）：
- H40 盲区：AI vs AI（defend 路径 +0x104）是否创建 pending battle？
  - 已静态确证：pending battle 对象**总是**在模型构造时创建（new(0x150) → FUN_105712b0，
    vtable = 0x115fa8a4），存于 [model+0x14a4]；主循环 FUN_10703ea0 正常速度门控之一 =
    [pending+0x55]（ready 标志）。
  - 未确证：AI 内战被结算时 pending battle 是否被**激活**（参与者/状态写入）。
- 本工具运行时回答：战斗中/回合结算时 pending battle 是否出现状态迁移。

用法（游戏运行中，战役地图界面）：
  python re_a3_probe.py --scan              # 锚定 model + pending battle（只读）
  python re_a3_probe.py --read              # 读 pending battle 全状态（只读）
  python re_a3_probe.py --setup             # 读 player-setup 槽位全表（H40 挂载点判据，只读）
  python re_a3_probe.py --watch 100         # 持续轮询记录状态迁移（AI 内战结算时跑）
  python re_a3_probe.py --set-ready 1       # 写 [pending+0x55]=1（Drop-in 式加载触发测试，⚠️ 实验性）
  python re_a3_probe.py --set-ready 0       # 恢复

地址（Empire.Retail.dll build 6262，RVA→VA = RVA + 0x10000000）：
  model vtable      0x11607bb4  （构造 0x6463e0：mov [esi],0x11607bb4）
  pending vtable    0x115fa8a4  （构造 0x5712b0：*param_1 = &PTR_FUN_115fa8a4）
  attack notifier   [model+8]+0xf0  → vtable 0x11607c44
  defend notifier   [model+8]+0x104 → vtable 0x11607c50
  pending battle    [model+0x14a4]  （+0x4c 状态 / +0x55 ready / +0xb8 参与者数 / +0xbc 参与者表 / +0xf8 player-setup）
  主循环 FUN_10703ea0 门控 = [[session+0x1498]+0x2c]+0x6a0（本地派系人类）|| [pending+0x55]!=0 || [[model+0x14a8]+4]==0

教训：写字段前必须记录原始字节（P30）；扫描超时先查步进（P38）；只读观测优先。
"""
import sys
import os
import time
import ctypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb

MODEL_VTABLE_RVA = 0x1607bb4      # model 对象 vtable（静态 VA 0x11607bb4 - 0x10000000）
PENDING_VTABLE_RVA = 0x15fa8a4    # pending battle 对象 vtable（静态 VA 0x115fa8a4 - 0x10000000）
OFF_PENDING = 0x14a4              # [model+0x14a4] = pending battle 对象指针

# pending battle 内部字段（推测级，来自反编译 FUN_105cb870/FUN_105b5140/构造 FUN_105712b0）
PB_STATE = 0x4c      # 状态（win_next 校验 ==1；推断）
PB_READY = 0x55      # ready 标志（主循环门控读；推断）
PB_PART_CNT = 0xb8   # 参与者计数（FUN_105cb870 读 [param_1+0xb8]）
PB_PART_TBL = 0xbc   # 参与者表（FUN_105cb870 读 [param_1+0xbc]）
PB_SETUP = 0xf8      # player-setup 子对象（CCQ ready 链 0x5f6b20 用；0x20 字节条目 +0xc 状态/+0x10 ready/+0x14 标志）
PB_MODEL_BACK = 0x30  # 构造存 *param_2（推测=回指 model/持有者）

# player-setup 子对象布局（0x608cf0 遍历 + FUN_105b5140 写入 反编译确证）
SETUP_CNT = 0x8      # [setup+8] = 槽位数
SETUP_BASE = 0xc     # [setup+0xc] = 槽位数组基
SLOT_STATE = 0xc     # 槽 +0xc = 状态
SLOT_READY = 0x10    # 槽 +0x10 = ready
SLOT_FLAG = 0x14     # 槽 +0x14 = 就绪检查标志
SLOT_SIZE = 0x20     # 槽步长

WINDOW = 0x200000


def _vtable_scan(h, rva, min_obj=0x100):
    """numpy 向量化扫全内存找 vtable==base+rva 的对象起点（复用 re_c2_scan_full 分块法）。"""
    import numpy as np
    base = 0
    # 需要 base 计算 runtime vtable——从调用处传
    raise RuntimeError("use scan_for_vtable(h, base, rva)")


def scan_for_vtable(h, base, rva, min_obj=0x100):
    """numpy 向量化扫全内存找 vtable==base+rva 的对象起点。返回 [addr] 去重排序。"""
    import numpy as np
    va = base + rva
    out = []
    for rs, re in pb.readable_regions(h):
        if re - rs < min_obj:
            continue
        start = rs
        while start < re:
            size = min(WINDOW, re - start)
            if size <= min_obj:
                break
            buf = pb.read_mem(h, start, size)
            if buf is None or len(buf) < min_obj:
                start += size - 4
                continue
            n4 = len(buf) // 4
            arr = np.frombuffer(buf, dtype="<u4", count=n4)
            hits = np.nonzero(arr == va)[0]
            for i in hits:
                out.append(int(start + i * 4))
            start += size - 4
    return sorted(set(out))


def find_pending(h, base):
    """扫内存找 pending battle 对象（vtable 精确匹配）。返回 [addr] 或 []。"""
    return scan_for_vtable(h, base, PENDING_VTABLE_RVA, min_obj=0x150)


def find_model(h, base):
    """扫内存找 model 对象（vtable 0x11607bb4）。返回 [addr] 候选。"""
    return scan_for_vtable(h, base, MODEL_VTABLE_RVA, min_obj=0x1a00)


def verify_pending_at(h, base, addr):
    """校验 addr 是 model：读 [addr+0x14a4] → 应为 pending battle 且 vtable 匹配。"""
    p = pb.read_u32(h, addr + OFF_PENDING)
    if not p:
        return None
    v = pb.read_u32(h, p)
    if v == base + PENDING_VTABLE_RVA:
        return p
    return None


def dump_pending(h, p):
    """dump pending battle 关键字段（全部只读）。"""
    d = {}
    d["addr"] = "0x%08x" % p
    d["vtable"] = "0x%08x" % pb.read_u32(h, p)
    d["state(+0x4c)"] = pb.read_u32(h, p + PB_STATE)
    d["ready(+0x55)"] = pb.read_u8(h, p + PB_READY)
    d["part_cnt(+0xb8)"] = pb.read_u32(h, p + PB_PART_CNT)
    part_tbl = pb.read_u32(h, p + PB_PART_TBL)
    d["part_tbl(+0xbc)"] = "0x%08x" % part_tbl if part_tbl else "0"
    d["model_back(+0x30)"] = "0x%08x" % pb.read_u32(h, p + PB_MODEL_BACK)
    # player-setup 子对象（+0xf8）：0x20 字节条目
    setup = p + PB_SETUP
    d["setup_cnt(+0xf8+8)"] = pb.read_u32(h, setup + SETUP_CNT)
    d["setup_base(+0xf8+c)"] = "0x%08x" % pb.read_u32(h, setup + SETUP_BASE)
    # 参与者表头几条
    cnt = d["part_cnt(+0xb8)"]
    if cnt and cnt < 64:
        tbl = pb.read_u32(h, p + PB_PART_TBL)
        parts = []
        for i in range(min(cnt, 8)):
            parts.append("0x%08x" % pb.read_u32(h, tbl + i * 4))
        d["participants"] = parts
    return d


def dump_setup_slots(h, p):
    """dump player-setup 全槽位（H40 挂载点判据：AI 内战是否生成槽位）。"""
    setup = p + PB_SETUP
    cnt = pb.read_u32(h, setup + SETUP_CNT)
    base = pb.read_u32(h, setup + SETUP_BASE)
    out = ["setup @ 0x%08x  slots=%d base=0x%08x" % (setup, cnt, base or 0)]
    if not cnt or not base or cnt > 32:
        return out
    for i in range(cnt):
        s = base + i * SLOT_SIZE
        out.append("  slot[%d] @0x%08x  state=%d ready=%d flag=%d  u8s=[%s]" % (
            i, s,
            pb.read_u32(h, s + SLOT_STATE),
            pb.read_u8(h, s + SLOT_READY),
            pb.read_u8(h, s + SLOT_FLAG),
            ",".join(str(pb.read_u8(h, s + k)) for k in (0, 1, 2, 4, 5, 6, 0x11, 0x12))))
    return out


def sig(d):
    """状态签名（watch 用）：激活特征 = player-setup count(+0x100)/base(+0x104)。
    实机校准（2026-08-09 pre-battle 对照）：空闲 0/0，人类攻击 pre-battle 1/heap。"""
    return (d["setup_cnt(+0xf8+8)"], d["setup_base(+0xf8+c)"], d["ready(+0x55)"])


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    pid = pb.find_pid()
    if not pid:
        print("shogun2.exe 未运行")
        return 1
    h = pb.K32.OpenProcess(pb.PROCESS_QUERY_INFORMATION | pb.PROCESS_VM_READ |
                           pb.PROCESS_VM_WRITE | pb.PROCESS_VM_OPERATION, False, pid)
    if not h:
        print("OpenProcess 失败 err=%d（需管理员权限？）" % ctypes.get_last_error())
        return 1
    base = pb.module_base(h, "Empire.Retail.dll")
    if not base:
        print("Empire.Retail.dll 未加载（旧引擎=Shogun2.dll 不支持本探针）")
        return 1
    print("pid %d, dll base = 0x%08x" % (pid, base), flush=True)

    def real_pending():
        """优先用 model 验证过的真实 pending battle；退回 vtable 扫描（排除 DLL 区假阳性）。"""
        for m in find_model(h, base):
            p = verify_pending_at(h, base, m)
            if p:
                return p
        pend = find_pending(h, base)
        for p in pend:
            # 排除 DLL 区（[base, base+0x2000000)）与 0x7903xxxx 类假阳性：对象应在堆区
            if not (base - 0x100000 <= p <= base + 0x2000000):
                return p
        return pend[0] if pend else None

    if cmd == "--scan":
        t0 = time.time()
        mods = find_model(h, base)
        print("model vtable(base+0x%08x) 候选 %d 个（%.1fs）" % (MODEL_VTABLE_RVA, len(mods), time.time() - t0))
        for m in mods:
            p = verify_pending_at(h, base, m)
            print("  model @ 0x%08x -> pending @ %s" % (m, ("0x%08x" % p) if p else "None"))
        pend = find_pending(h, base)
        print("pending vtable(base+0x%08x) 实例 %d 个" % (PENDING_VTABLE_RVA, len(pend)))
        for p in pend[:10]:
            print("  pending @ 0x%08x  state=0x%x ready=%d part_cnt=%d" % (
                p, pb.read_u32(h, p + PB_STATE), pb.read_u8(h, p + PB_READY),
                pb.read_u32(h, p + PB_PART_CNT)))
        return 0

    if cmd == "--read":
        p = real_pending()
        if not p:
            print("未找到 pending battle 对象")
            return 1
        d = dump_pending(h, p)
        for k, v in d.items():
            print("  %-20s %s" % (k, v))
        return 0

    if cmd == "--setup":
        p = real_pending()
        if not p:
            print("未找到 pending battle 对象")
            return 1
        for line in dump_setup_slots(h, p):
            print(line)
        return 0

    if cmd == "--watch":
        ms = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        p = real_pending()
        if not p:
            print("未找到 pending battle 对象")
            return 1
        last = sig(dump_pending(h, p))
        print("watch pending @ 0x%08x every %dms (Ctrl+C 退出)  监控: setup_cnt/setup_base/ready" % (p, ms), flush=True)
        reanchor = 0
        while True:
            d = dump_pending(h, p)
            s = sig(d)
            if s != last:
                print("[%s] setup_cnt=%d setup_base=0x%08x ready=%d" % (
                    time.strftime("%H:%M:%S"), s[0], s[1], s[2]), flush=True)
                for line in dump_setup_slots(h, p):
                    print("    " + line, flush=True)
                last = s
            reanchor += 1
            if reanchor % 200 == 0:  # 每 10s 重锚一次（对象 churn 防御）
                p2 = real_pending()
                if p2 and p2 != p:
                    p = p2
                    print("[%s] re-anchor pending -> 0x%08x" % (time.strftime("%H:%M:%S"), p), flush=True)
                    last = sig(dump_pending(h, p))
            time.sleep(ms / 1000.0)
        return 0

    if cmd == "--set-ready":
        val = int(sys.argv[2])
        p = real_pending()
        if not p:
            print("未找到 pending battle 对象")
            return 1
        old = pb.read_u8(h, p + PB_READY)
        pb.write_u8(h, p + PB_READY, val)
        print("pending @ 0x%08x  [%s] ready: %d -> %d（原始字节已记录）" % (
            p, "+0x55", old, val))
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
