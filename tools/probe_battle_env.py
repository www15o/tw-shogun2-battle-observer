# -*- coding: utf-8 -*-
"""probe_battle_env.py — 存在性探针（只读，校准通过前禁止写注入）。

按 19_HANDOFF §4.1 执行：对 FUN_10303430 自洽字段（+0x14/+0x28278/+0x28280-83）
做逐层存在性探针。修复旧 find_battle_ctx.py 的空循环 bug（64KB 块内读 pos+0x28278
必然越界 → range 为空 → 永远 0 候选），改用「可读区间 + 滑动窗口」跨块读取。

实现（两遍 numpy 向量化，避免上百万候选的 Python 循环卡死）：
  遍1：滑窗扫描，pos+0x14∈{1,2,3} 且 [pos+0x28278]∈指针范围 → 收集候选 v 唯一值集合 + 层计数
  遍2：对候选 v 唯一值逐个 RPM 校验可读 + [+8]/[+10]∈0..15（探针2）→ good_v
  遍3：回扫定位 good_v 对应的 pos（探针3 命中），输出探针4 详情

探针流程（语义对应 19_HANDOFF §4.1）：
  探针0（校准点）：读 battle_ai 命令对象 vtable(+0x00)/value(+0x24) → 验证读取通道有效
  探针1：扫「任意 pos 使 [pos+0x28278] 非 0 且指向可读」→ 命中数
  探针2：探针1 命中里筛「指向对象 [+8]/[+10] ∈ 0~15 小整数」→ 天气状态特征
  探针3：探针2 命中里筛「[pos+0x14] ∈ {1,2,3}」→ 确认与状态机同对象
  探针4：对探针3 命中读 +0x13f / +0x28280-83 当前值 → 确认字段语义

决策（19_HANDOFF §4.1/§4.3）：
  探针3 > 0  → 注入锚点就绪 → E-wB / E-wC
  探针1 == 0 → +0x28278 偏移假设错 / 对象不存在 → 放弃 env 注入 → §4.3 备选
  探针1>0 但探针3 == 0 → 对象存在但 +0x14 偏移或状态值语义不符 → 诊断

用法（游戏黑屏进攻战中）：
  python tools/probe_battle_env.py                # 全流程扫描 + 详情
  python tools/probe_battle_env.py --top 5        # 详情只输出前 5 个（默认 10）
  命中详情写 work/probe_battle_env_hits.txt（供后续注入锚点使用）
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys

import numpy as np

K32 = ctypes.WinDLL("kernel32", use_last_error=True)
PSAPI = ctypes.WinDLL("psapi", use_last_error=True)

# 显式声明类型：未声明参数默认按 C int（32 位有符号）转换，
# mods[i] 等 c_void_p 元素是 ≥2^31 的模块基址 → OverflowError（同 find_battle_ctx.py 注释）
PSAPI.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p),
                                       wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD]
PSAPI.EnumProcessModulesEx.restype = wt.BOOL
PSAPI.GetModuleFileNameExW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]
PSAPI.GetModuleFileNameExW.restype = wt.DWORD
K32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
K32.OpenProcess.restype = wt.HANDLE
K32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
K32.ReadProcessMemory.restype = wt.BOOL
K32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
K32.WriteProcessMemory.restype = wt.BOOL

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008

# ---- 探针偏移（FUN_10303430 自洽字段，19_HANDOFF §3.3） ----
OFF_STATE = 0x14        # env 状态 1/2/3
OFF_WS_PTR = 0x28278    # 天气状态对象指针
OFF_HUMAN = 0x13f       # 「人类」标志（FUN_1019e380 读；待探针验证）
OFF_APP1 = 0x28280      # 状态1「已应用」标志
OFF_APP2 = 0x28281
OFF_APP3 = 0x28282
OFF_TRANS = 0x28283     # 天气过渡挂起

# battle_ai 校准点（探针0 锚点，按引擎 build 区分）
# 新引擎 Empire.Retail.dll build 6262（2023 CA 更新，原 RE 目标）：0x18d2d88 / 0x15a9cf8
# 旧引擎 Shogun2.dll build 6118（2021，2026-08-09 重锚定）：0x1986580 / 0x1460a98
BUILDS = {
    "empire":  {"module": "empire.retail.dll", "rva_battle_ai": 0x18d2d88, "vtable_rva": 0x15a9cf8},
    "shogun2": {"module": "shogun2.dll",       "rva_battle_ai": 0x1986580, "vtable_rva": 0x1460a98},
}
RVA_BATTLE_AI = BUILDS["empire"]["rva_battle_ai"]
VTABLE_RVA = BUILDS["empire"]["vtable_rva"]
EXPECT_VALUE = 0xf8

WINDOW = 0x200000       # 2MB 滑动窗口
PAGE = 0x1000
BLOCK = 0x10000

READABLE_MIN = 0x10000
READABLE_MAX = 0x80000000


def find_pid(name="shogun2.exe"):
    """Toolhelp32 枚举进程（沙箱内 tasklist 被拒，2026-08-14 改用 API）。"""
    import ctypes.wintypes as w
    TH32CS_SNAPPROCESS = 0x2
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap:
        return None

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(w.ULONG)), ("th32ModuleID", w.DWORD),
                    ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                    ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.CHAR * 260)]

    pe = PE()
    pe.dwSize = ctypes.sizeof(PE)
    needle = name.lower()
    try:
        if not k32.Process32First(snap, ctypes.byref(pe)):
            return None
        while True:
            if pe.szExeFile.lower() == needle.encode():
                return pe.th32ProcessID
            if not k32.Process32Next(snap, ctypes.byref(pe)):
                return None
    finally:
        k32.CloseHandle(snap)


def module_base(h, suffix):
    MAX = 1024
    mods = (ctypes.c_void_p * MAX)()
    cb = ctypes.c_ulong()
    if not PSAPI.EnumProcessModulesEx(h, mods, ctypes.sizeof(mods), ctypes.byref(cb), 0x03):
        return None
    n = cb.value // ctypes.sizeof(ctypes.c_void_p)
    for i in range(n):
        buf = ctypes.create_unicode_buffer(260)
        PSAPI.GetModuleFileNameExW(h, mods[i], buf, 260)
        if buf.value.lower().endswith(suffix.lower()):
            return mods[i]
    return None


def detect_build(h):
    """检测运行中的引擎 build。返回 (build_name, base, profile) 或 (None, None, None)。"""
    for name, prof in BUILDS.items():
        b = module_base(h, prof["module"])
        if b:
            return name, b, prof
    return None, None, None


def read_mem(h, addr, n):
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t()
    if not K32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
        return None
    return buf.raw[:got.value]


def read_u32(h, addr):
    b = read_mem(h, addr, 4)
    return None if b is None else int.from_bytes(b, "little")


def read_u8(h, addr):
    b = read_mem(h, addr, 1)
    return None if b is None else b[0]


def write_u8(h, addr, val):
    """写 1 字节，返回是否成功且写满。"""
    buf = ctypes.create_string_buffer(bytes([val]))
    got = ctypes.c_size_t()
    ok = K32.WriteProcessMemory(h, ctypes.c_void_p(addr), buf, 1, ctypes.byref(got))
    return bool(ok) and got.value == 1


def readable_regions(h):
    """试读法枚举连续可读区间（页粒度），返回 [(start, end), ...]。"""
    regions = []
    cur_start = cur_end = None
    addr = 0x10000
    while addr < 0x7f000000:
        got = 0
        b = read_mem(h, addr, BLOCK)
        if b is not None and len(b) == BLOCK:
            got = BLOCK
        else:
            sub = addr
            while sub < addr + BLOCK:
                b2 = read_mem(h, sub, PAGE)
                if b2 is not None and len(b2) == PAGE:
                    got = sub - addr + PAGE
                else:
                    break
                sub += PAGE
        if got:
            if cur_end is not None and addr <= cur_end:
                cur_end = addr + got
            else:
                if cur_end is not None:
                    regions.append((cur_start, cur_end))
                cur_start, cur_end = addr, addr + got
        else:
            if cur_end is not None:
                regions.append((cur_start, cur_end))
                cur_start = cur_end = None
        addr += BLOCK
    if cur_end is not None:
        regions.append((cur_start, cur_end))
    return regions


def window_arrays(h, start, size):
    """读窗口，返回 (pos, st, v, app4) 或 None。
    pos: 每个 4 对齐偏移的绝对地址；
    st:  +0x14 低字节（dword idx 5）；
    v:   +0x28278 dword（idx 41118）；
    app4: +0x28280..+0x28283 的 4 字节小端合并（idx 41120，+0x28280 是低字节）。
    索引对齐到有效范围：i 需 i+5<n4 且 i+41120+1<=n4 → i < n4-41121。"""
    buf = read_mem(h, start, size)
    if buf is None or len(buf) < 0x28284 + 4:
        return None
    n4 = len(buf) // 4
    arr = np.frombuffer(buf, dtype="<u4", count=n4)
    lim = n4 - 41121
    if lim <= 0:
        return None
    i = np.arange(lim, dtype=np.uint32)
    st = (arr[5:5 + lim] & 0xFF).astype(np.uint8)
    v = arr[41118:41118 + lim].astype(np.uint64)
    app4 = arr[41120:41120 + lim].astype(np.uint32)
    pos = start + i.astype(np.uint64) * 4
    return pos, st, v, app4


def probe_scan(h, regions):
    """两遍扫描。返回 (counts, good_v, hits)。
    counts: (st_cnt, v_cnt, app_cnt, stapp_cnt, both_cnt, n_unique_v, ngood, nhit)
    good_v: {v: (ws8, ws10, ws14)} 探针2 通过（天气状态特征）
    hits: [(pos, v, st)] 探针3 命中（全部强约束重验）
    强约束（引擎一致性，FUN_10303430）：
      st=pos+0x14 ∈ {1,2,3}
      app4=pos+0x28280 的 4 字节（app1=byte0, app2=byte1, app3=byte2, trans=byte3）全∈{0,1}
      stapp: 当前状态对应 app 位必须 =1（进入状态 N 时 app[N] 置 1）
      v=pos+0x28278 指向可读对象，其 +8∈{0,1}、+10∈{0,1}、+0x14 小整数(<0x100)
    """
    st_cnt = 0     # pos+0x14 ∈ {1,2,3}
    v_cnt = 0      # 且 [pos+0x28278] ∈ 指针范围
    app_cnt = 0    # 且 +0x28280-83 4 字节均为 0/1
    stapp_cnt = 0  # 且 st 对应 app 位 ==1
    both_cnt = 0   # 联合粗筛
    cand_v = set()
    for rs, re in regions:
        if re - rs < 0x28284 + 8:
            continue
        start = rs
        while start < re - 0x28284 - 4:
            size = min(WINDOW, re - start)
            got = window_arrays(h, start, size)
            if got is not None:
                _, st, v, app4 = got
                smask = (st == 1) | (st == 2) | (st == 3)
                st_cnt += int(smask.sum())
                vmask = (v >= READABLE_MIN) & (v < READABLE_MAX)
                sv = smask & vmask
                v_cnt += int(sv.sum())
                b0 = (app4 & 0xFF) <= 1
                b1 = ((app4 >> 8) & 0xFF) <= 1
                b2 = ((app4 >> 16) & 0xFF) <= 1
                b3 = ((app4 >> 24) & 0xFF) <= 1
                amask = b0 & b1 & b2 & b3
                app_cnt += int((sv & amask).sum())
                # st 对应 app 位 ==1（已应用）：st=1→byte0==1, st=2→byte1==1, st=3→byte2==1
                e0 = (app4 & 0xFF) == 1
                e1 = ((app4 >> 8) & 0xFF) == 1
                e2 = ((app4 >> 16) & 0xFF) == 1
                sa = ((st == 1) & e0) | ((st == 2) & e1) | ((st == 3) & e2)
                sa = sa & amask
                stapp_cnt += int((sv & sa).sum())
                both = sv & sa
                both_cnt += int(both.sum())
                if both.any():
                    u = np.unique(v[both]).tolist()
                    cand_v.update(u)
            start += size - 0x28284

    n_unique_v = len(cand_v)

    # ---- 遍2：探针2 —— 天气状态特征：可读 + [+8]∈{0,1} + [+10]∈{0,1} + [+0x14]小整数 ----
    good_v = {}
    for v in cand_v:
        b = read_mem(h, v, 0x20)
        if b is None:
            continue
        ws14 = int.from_bytes(b[0x14:0x18], "little")
        if (b[8] in (0, 1)) and (b[10] in (0, 1)) and ws14 < 0x100:
            good_v[v] = (b[8], b[10], ws14)

    # ---- 遍3：回扫定位 good_v 对应 pos（强约束重验后为真命中） ----
    hits = []
    if good_v:
        good_arr = np.fromiter(good_v.keys(), dtype="<u8")
        for rs, re in regions:
            if re - rs < 0x28284 + 8:
                continue
            start = rs
            while start < re - 0x28284 - 4:
                size = min(WINDOW, re - start)
                got = window_arrays(h, start, size)
                if got is not None:
                    pos, st, v, app4 = got
                    sel = np.isin(v, good_arr)
                    if sel.any():
                        idx = np.nonzero(sel)[0]
                        for j in idx:
                            p, sv, vv = int(pos[j]), int(st[j]), int(v[j])
                            a = int(app4[j])
                            b0, b1, b2, b3 = (a & 0xFF) <= 1, ((a >> 8) & 0xFF) <= 1, \
                                             ((a >> 16) & 0xFF) <= 1, ((a >> 24) & 0xFF) <= 1
                            if not (b0 and b1 and b2 and b3):
                                continue
                            sa = ((sv == 1) and b0) or ((sv == 2) and b1) or ((sv == 3) and b2)
                            if sa:
                                hits.append((p, vv, sv))
                start += size - 0x28284
    return (st_cnt, v_cnt, app_cnt, stapp_cnt, both_cnt, n_unique_v, len(good_v), len(hits)), good_v, hits


def main():
    ap = argparse.ArgumentParser(description="env 对象存在性探针（只读）")
    ap.add_argument("--top", type=int, default=10, help="详情输出上限")
    args = ap.parse_args()

    pid = find_pid()
    if pid is None:
        print("shogun2.exe 未运行。请先启动游戏并进入战斗。")
        return 1
    print(f"PID={pid}")
    h = K32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ |
                        PROCESS_VM_WRITE | PROCESS_VM_OPERATION, False, pid)
    if not h:
        print(f"OpenProcess 失败 err={ctypes.get_last_error()}（需管理员权限）")
        return 2
    build, base, prof = detect_build(h)
    if base is None:
        print("未找到引擎模块（shogun2.dll / empire.retail.dll）")
        return 3
    RVA_BATTLE_AI = prof["rva_battle_ai"]
    VTABLE_RVA = prof["vtable_rva"]
    print(f"引擎 build={build} base=0x{base:x} (ASLR +0x{base - 0x10000000:x})")

    # ---- 探针0：读取通道校准 ----
    obj = base + RVA_BATTLE_AI
    vtable = read_u32(h, obj)
    value_id = read_u32(h, obj + 0x24)
    ok = (vtable == base + VTABLE_RVA) and (value_id == EXPECT_VALUE)
    print(f"[探针0] battle_ai obj=0x{obj:08x} vtable=0x{vtable:08x} value=0x{value_id:02x} "
          f"→ {'PASS ✅ 读取通道有效' if ok else 'FAIL ❌ 通道/偏移错，停止'} ")
    if not ok:
        return 4

    # ---- 枚举可读区间 ----
    regions = readable_regions(h)
    total = sum(re - rs for rs, re in regions)
    print(f"[扫描] 可读区间 {len(regions)} 个，共 0x{total:x} 字节 ({total / 2**30:.2f} GB)")
    if not regions:
        print("无可读内存——读取通道失效，停止")
        return 5

    counts, good_v, hits = probe_scan(h, regions)
    st_cnt, v_cnt, app_cnt, stapp_cnt, both_cnt, n_unique_v, ngood, nhit = counts

    print(f"\n===== 探针结果（分层计数，每层是上一层命中∩本层特征） =====")
    print(f"层1: pos+0x14∈{{1,2,3}}                  → {st_cnt}")
    print(f"层2: 且 [pos+0x{OFF_WS_PTR:x}]∈指针范围   → {v_cnt}")
    print(f"层3: 且 +0x28280-83 全∈{{0,1}}（标志）    → {app_cnt}")
    print(f"层4: 且 st 对应 app 位==1（引擎一致性）   → {stapp_cnt}")
    print(f"联合粗筛命中 → {both_cnt}（候选 v 去重 {n_unique_v} 个）")
    print(f"探针2: 候选 v 天气状态特征(+8/+10∈{{0,1}},+0x14<0x100) → {ngood}")
    print(f"探针3: 回扫强约束重验命中 → {nhit}")

    if v_cnt == 0:
        print("\n→ 判定：层2 = 0。+0x28278 偏移假设错或 env 对象不存在于内存。")
        print("  放弃 env 对象注入，转 19_HANDOFF §4.3 备选（E4 诊断 / 过渡态 / RE-B3）。")
        return 0
    if nhit == 0:
        print("\n→ 判定：层1/2 有命中但探针3 = 0 —— env 天气对象存在但 +0x14 状态字段")
        print("  偏移或状态值语义不符（非 1/2/3）。需诊断，勿注入。")
        return 0

    # ---- 探针4：详情 ----
    print(f"\n===== 探针4 详情（top {min(args.top, len(hits))} of {len(hits)}） =====")
    details = []
    for pos, v, st in hits:
        human = read_u8(h, pos + OFF_HUMAN)
        a1 = read_u8(h, pos + OFF_APP1)
        a2 = read_u8(h, pos + OFF_APP2)
        a3 = read_u8(h, pos + OFF_APP3)
        tr = read_u8(h, pos + OFF_TRANS)
        ws8, ws10, ws14 = good_v.get(v, (None, None, None))
        details.append(dict(pos=pos, v=v, st=st, human=human, app=(a1, a2, a3),
                            trans=tr, ws8=ws8, ws10=ws10, ws14=ws14))
    for i, d in enumerate(details[:args.top]):
        print(f"  #{i} pos=0x{d['pos']:08x} state=0x{d['st']:x} human=0x{d['human'] if d['human'] is not None else -1:x} "
              f"app1/2/3={d['app'][0]}/{d['app'][1]}/{d['app'][2]} trans=0x{d['trans'] if d['trans'] is not None else -1:x}")
        print(f"      ws=0x{d['v']:08x} [+8]=0x{d['ws8'] if d['ws8'] is not None else -1:x} "
              f"[+10]=0x{d['ws10'] if d['ws10'] is not None else -1:x} [+0x14]=0x{d['ws14'] if d['ws14'] is not None else -1:x}")

    out = "work/probe_battle_env_hits.txt"
    with open(out, "w") as f:
        f.write(f"# probe_battle_env hits ({len(details)} 探针3命中)\n")
        f.write("# pos=env对象候选基址 st=env状态 human=+0x13f app=+0x28280-82 trans=+0x28283 ws=天气状态对象\n")
        for d in details:
            f.write(f"0x{d['pos']:08x} st={d['st']} human={d['human']} "
                    f"app={d['app'][0]},{d['app'][1]},{d['app'][2]} trans={d['trans']} "
                    f"ws=0x{d['v']:08x} ws8={d['ws8']} ws10={d['ws10']} ws14=0x{d['ws14'] if d['ws14'] is not None else -1:x}\n")
    print(f"\n→ 判定：探针3 命中 {len(details)} 个 → 注入锚点就绪。E-wB/E-wC 可用（见 19_HANDOFF §4.2）。")
    print(f"  全部命中已写 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
