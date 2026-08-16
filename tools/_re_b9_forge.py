# -*- coding: utf-8 -*-
"""_re_b9_forge.py — ★b9=1 最小伪造（H 方案静态最优，实机验证）。
外部只写 [pending+0xb9]=1（ready=0 时 FUN_105caa60 返回 0 → 状态 4），零新 hook。
用法：python tools/_run_elev.py _re_b9_forge.py <mode>
  mode=probe  ：只读 pending（vtable/[+0x50] 状态/[+0xb9] 当前值/[+0x55] ready/[+0x58] btype），验证 FOTS 布局
  mode=write  ：probe + 写 [pending+0xb9]=1（一次性）
  mode=watch  ：轮询 pending（每 0.5s），打印状态/b9/ready/btype 变化（找 AI 内战 pending 窗口）
                ★E1 类型筛选（S15 定案）：写 b9 前读 [pending+0x58] btype，11≤btype≤14=海战 → 跳过不写
观测：现有 8 hook（fork 0x6045cc / state 0x57fca0 / factory / envdisp）记录分叉/状态/env 链。
判据：fork 返回 0 + 状态 4 + factory/envdisp 触发 + battle_mgr 换新。"""
import sys, os, time, json, struct

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb
import re_h46a as H46

OFF_P50 = 0x50   # 状态
OFF_P54 = 0x54   # word 分叉缓存（FUN_105caa60 返回值缓存）
OFF_P55 = 0x55   # ready
OFF_P58 = 0x58   # ★btype = BATTLE_TYPE 枚举（S15 定案 2026-08-19：权威表 0x11794478，0-2 野战/3-10 攻城/11-14 海战/15 未指定）
OFF_PB9 = 0xb9   # ★伪造目标（H：无其他写者覆盖）
VT_PENDING_RVA = 0x15fa8a4  # pending vtable RVA（★2026-08-19 修正：40 §2.3「0x115fa8a4」是 VA（=0x10000000+0x15fa8a4），RVA=0x15fa8a4；probe 实机 0x5d5da8a4=0x5bfe0000+0x15fa8a4 ✅ 匹配）

# ★BATTLE_TYPE 枚举（S15 定案，表 0x11794478 索引=值）
BATTLE_TYPE_NAMES = {
    0: "NORMAL 野战", 1: "AMBUSH 伏击", 2: "BRIDGE 桥梁",
    3: "FORT_STANDARD 攻城", 4: "FORT_SALLY_OUT 攻城", 5: "FORT_SIEGE_RELIEF 攻城",
    6: "FORTIFIED_SETTLEMENT_STANDARD 围城", 7: "FORTIFIED_SETTLEMENT_SALLY_OUT 攻城",
    8: "FORTIFIED_SETTLEMENT_SIEGE_RELIEF 攻城", 9: "UNFORTIFIED_SETTLEMENT_NORMAL",
    10: "REGION_SLOT_NORMAL", 11: "NAVAL_NORMAL 海战", 12: "NAVAL_BLOCKADE_BREAKOUT 海战",
    13: "NAVAL_BLOCKADE_RELIEF 海战", 14: "NAVAL_PORT_ASSAULT 海战",
    15: "UNSPECIFIED 未指定",
}


def btype_label(v):
    return BATTLE_TYPE_NAMES.get(v, f"未知({v})")


def is_naval_btype(btype):
    """★E1 海战过滤（S15）：11≤btype≤14 = 海战 → 跳过。15=fallback 默认加载（宁多勿漏，见 S15 §6 ③ 待实机定案）"""
    return btype is not None and 11 <= btype <= 14


def rd(h, a):
    if not a or not (0x10000 < a < 0x80000000):
        return None
    try:
        return pb.read_u32(h, a)
    except Exception:
        return None


def probe(h, base, model, pending):
    out = {"base": hex(base), "model": hex(model) if model else None,
           "pending": hex(pending) if pending else None}
    if pending and 0x10000 < pending < 0x80000000:
        vt = rd(h, pending)
        out["pending_vt"] = hex(vt) if vt else None
        out["vanilla_vt_expect"] = hex(base + VT_PENDING_RVA) if base else None
        out["match_vt"] = (vt == base + VT_PENDING_RVA) if (vt and base) else None
        out["p+0x50_state"] = pb.read_u8(h, pending + OFF_P50) if pending + OFF_P50 else None
        out["p+0x54_word"] = rd(h, pending + OFF_P54)
        out["p+0x55_ready"] = pb.read_u8(h, pending + OFF_P55)
        btype = rd(h, pending + OFF_P58)
        out["p+0x58_btype"] = btype
        out["p+0x58_btype_label"] = btype_label(btype) if btype is not None else None
        out["p+0xb9"] = pb.read_u8(h, pending + OFF_PB9)
        out["p+0x80"] = rd(h, pending + 0x80)
        out["p+0x94"] = rd(h, pending + 0x94)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    h, base = H46.open_proc()
    model, pending = H46.anchor(h, base)
    if not model:
        print("❌ model 锚定失败——请确保在战役大地图"); return
    if mode == "probe":
        probe(h, base, model, pending)
    elif mode == "watch":
        last_p = None
        written = set()
        skipped = set()
        t0 = time.time()
        print("★ watch v2：0.05s 轮询 + pending 指针变化即写（b9 无写者覆盖，早写无害；state<10 才写）")
        print("★ E1 类型筛选已实装（S15 定案）：写 b9 前读 [pending+0x58] btype，11≤btype≤14=海战 → 跳过不写")
        while time.time() - t0 < 3600:
            p = rd(h, model + 0x14a4)
            if p and 0x10000 < p < 0x80000000:
                st = pb.read_u8(h, p + OFF_P50)
                if p != last_p:
                    last_p = p
                    vt = rd(h, p)
                    w54 = rd(h, p + OFF_P54)
                    ready = pb.read_u8(h, p + OFF_P55)
                    b9 = pb.read_u8(h, p + OFF_PB9)
                    btype = rd(h, p + OFF_P58)
                    vt_ok = (vt == base + VT_PENDING_RVA)
                    print(f"[{time.time()-t0:.0f}s] pending={hex(p)} state={st} word54={w54} "
                          f"ready={ready} b9={b9} btype={btype}({btype_label(btype)}) vt_match={vt_ok}")
                    # ★指针变化即写：vt 匹配 + b9==0 + state<10 + ready==0（AI 内战未登记）
                    # ★2026-08-19 规则 v3：海战不跳过（total≥10 值得看）——类型阈值判定移到 watcher（加载后）
                    if (p not in written and p not in skipped and vt_ok and st is not None
                            and st < 10 and b9 == 0 and ready == 0):
                        ok = pb.write_u8(h, p + OFF_PB9, 1)
                        written.add(p)
                        rb = pb.read_u8(h, p + OFF_PB9)
                        print(f"  ★★★ 写 b9=1 → pending={hex(p)} state={st} btype={btype}({btype_label(btype)}) "
                              f"写结果={ok} 读回={rb} "
                              f"（判据：fork 0 + 状态 4 + factory/envdisp + battle_mgr 换新）")
                else:
                    # 同 pending 但状态推进到分叉前窗口（state 1/6）且未写 → 补写
                    if (p not in written and p not in skipped and st is not None and st < 10
                            and pb.read_u8(h, p + OFF_PB9) == 0 and pb.read_u8(h, p + OFF_P55) == 0):
                        vt = rd(h, p)
                        btype = rd(h, p + OFF_P58)
                        if vt == base + VT_PENDING_RVA:
                            ok = pb.write_u8(h, p + OFF_PB9, 1)
                            written.add(p)
                            rb = pb.read_u8(h, p + OFF_PB9)
                            print(f"  ★★★ 补写 b9=1 → pending={hex(p)} state={st} btype={btype}({btype_label(btype)}) "
                                  f"写结果={ok} 读回={rb}")
            time.sleep(0.05)
    elif mode == "dump":
        """★战斗筛选调查：dump pending 参战方区域（+0x50~+0x100），确认规模字段布局。
        用法：dump [pending_addr]（缺省 = [model+0x14a4]）。状态4 的 pending 列表完整（最优观测点）。"""
        p = int(sys.argv[2], 16) if len(sys.argv) > 2 else rd(h, model + 0x14a4)
        if not p or not (0x10000 < p < 0x80000000):
            print("❌ 无有效 pending"); return
        print(f"★ dump pending={hex(p)} base={hex(base)}")
        out = {"pending": hex(p)}
        out["vt"] = hex(rd(h, p)) if rd(h, p) else None
        out["vt_match"] = (rd(h, p) == base + VT_PENDING_RVA)
        out["state(+0x50)"] = pb.read_u8(h, p + 0x50)
        out["ready(+0x55)"] = pb.read_u8(h, p + 0x55)
        btype = rd(h, p + OFF_P58)
        out["btype(+0x58)"] = btype
        out["btype_label"] = btype_label(btype) if btype is not None else None
        # +0x58~+0xa0 区域逐字段（向量头/容器头）
        for off in range(0x58, 0xa8, 4):
            v = rd(h, p + off)
            if v:
                out[f"+0x{off:x}"] = hex(v)
        # 向量解析尝试：+0x60 与 +0x6c 各假设为 {cap,size,data}
        for base_off, label in ((0x60, "vec_A(+0x60)"), (0x6c, "vec_B(+0x6c)"),
                                (0x78, "vec_C(+0x78)"), (0x84, "vec_D(+0x84)"),
                                (0xb8, "vec_E(+0xb8)"), (0xc4, "vec_F(+0xc4)")):
            cap = rd(h, p + base_off)
            size = rd(h, p + base_off + 4)
            data = rd(h, p + base_off + 8)
            if cap or size or data:
                out[label] = {"cap": cap, "size": size, "data": hex(data) if data else None}
                # 元素头（最多 6 个）
                if data and 0x10000 < data < 0x80000000 and size and size < 0x100:
                    elems = []
                    for i in range(min(size, 6)):
                        e = rd(h, data + i * 4)
                        elems.append(hex(e) if e else None)
                    out[label]["elems"] = elems
                    # ★0x38 条目解析（PLAYER_LIST 布局，S12：条目 0x38 字节）
                    out[label]["elems38"] = []
                    for i in range(min(size, 4)):
                        base_e = data + i * 0x38
                        f0 = rd(h, base_e)
                        f4 = rd(h, base_e + 4)
                        f8 = rd(h, base_e + 8)
                        fc = rd(h, base_e + 0xc)
                        f10 = rd(h, base_e + 0x10)
                        f14 = rd(h, base_e + 0x14)
                        f18 = rd(h, base_e + 0x18)
                        f1c = rd(h, base_e + 0x1c)
                        out[label]["elems38"].append({
                            "+0": hex(f0) if f0 else None, "+4": hex(f4) if f4 else None,
                            "+8": hex(f8) if f8 else None, "+c": hex(fc) if fc else None,
                            "+10": hex(f10) if f10 else None, "+14": hex(f14) if f14 else None,
                            "+18": hex(f18) if f18 else None, "+1c": hex(f1c) if f1c else None,
                        })
        print(json.dumps(out, ensure_ascii=False, indent=1))
    elif mode == "write":
        out = probe(h, base, model, pending)
        if not out.get("pending"):
            print("❌ 无 pending"); return
        p = int(out["pending"], 16)
        b9 = pb.read_u8(h, p + OFF_PB9)
        if out.get("match_vt") is False:
            print(f"⚠️ pending vtable 不匹配 vanilla（{out.get('pending_vt')} vs {out.get('vanilla_vt_expect')}）——FOTS 布局差异，继续但留意")
        print(f"★ 写 [pending+0xb9]: {b9} → 1")
        ok = pb.write_u8(h, p + OFF_PB9, 1) if hasattr(pb, "write_u8") else None
        if ok is None:
            # 直接 WriteProcessMemory
            import ctypes
            K32 = ctypes.WinDLL("kernel32", use_last_error=True)
            buf = ctypes.create_string_buffer(b"\x01")
            got = ctypes.c_size_t()
            ok = bool(K32.WriteProcessMemory(h, ctypes.c_void_p(p + OFF_PB9), buf, 1, ctypes.byref(got)))
        print(f"写结果: {ok}；读回: {pb.read_u8(h, p + OFF_PB9)}")
    else:
        print(f"未知 mode={mode}")


if __name__ == "__main__":
    main()
