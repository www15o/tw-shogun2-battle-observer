# -*- coding: utf-8 -*-
"""battle_ai_ctl.py — battle_ai 注入/取消器（tweaker 命令值字节直写，2026-08-13 封装）。

原理（已确证，re_b1_report.md §1-2 / 03_DISCOVERIES 六十八节 / 04_PROBLEMS P-14）：
- battle_ai = tweaker 命令对象，位于 base+rva_battle_ai（empire.retail.dll=0x18d2d88 / shogun2.dll=0x1986580）
- 对象布局：+0x00=vtable（empire=0x15a9cf8 / shogun2=0x1460a98）、+0x24=value_id(0xf8)、+0x59=set、+0x5c=值字节
- AI 托管决策函数 0x1adf50（battle_ai 唯一挂钩）读 [+0x5c]：battle_ai=1 → 军队不归手动控制（全员 AI 托管）
- 注入=写 set/value=1；取消=写 0（P-14「关不掉」解法组合 c：上游根开关）
- 消费时机=战斗加载时（getter 0xa0b70）；建议战前/战役地图写，一次管整个会话；重启游戏需重写
- 写前 vtable + value_id 双重校准，防误写（AGENTS §3 验证闭环）
- 历史结论（03 六十八节）：battle_ai 已定案为非最终方案（不可暂停/变速/手动结算 + 卡天气 UI 风险），
  但作为「阶段性/自定义战」注入开关仍有效（R2 已注入打通目标1）。GUI 直写字段方案（s2_ai_ctl）是现役推荐。

用法：
  python tools/battle_ai_ctl.py            # 只读状态（校准 + 当前 set/value）
  python tools/battle_ai_ctl.py inject     # 注入 battle_ai=1（带回读验证）
  python tools/battle_ai_ctl.py cancel     # 取消 battle_ai=0（带回读验证）
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb

# ---- 命令对象字段偏移（re_b1_force.py 同款，已实机确证） ----
OFF_VTABLE = 0x00
OFF_VALUE_ID = 0x24
OFF_SET = 0x59
OFF_VALUE = 0x5c
EXPECT_VALUE = 0xf8  # battle_ai 注册 value_id

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008


def open_game():
    """定位 shogun2.exe + 打开进程 + 检测引擎。返回 (h, base, build, pid) 或 (None,)*4。"""
    pid = pb.find_pid()
    if pid is None:
        print("✗ shogun2.exe 未运行（请先启动游戏）")
        return None, None, None, None
    h = pb.K32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ |
                           PROCESS_VM_WRITE | PROCESS_VM_OPERATION, False, pid)
    if not h:
        print(f"✗ OpenProcess 失败 err={ctypes.get_last_error()}（需管理员权限）")
        return None, None, None, None
    build, base, prof = pb.detect_build(h)
    if base is None:
        print("✗ 未找到引擎模块（empire.retail.dll / shogun2.dll）")
        return None, None, None, None
    print(f"✓ PID={pid} 引擎={build} base=0x{base:08x}")
    return h, base, build, pid


def describe(h, base):
    """只读描述 battle_ai 命令对象（校准 vtable/value_id + 当前 set/value）。
    返回 dict：obj/vtable/vtable_ok/value_id/value_ok/set/value/ok（ok=校准通过）。"""
    prof = pb.detect_build(h)[2]
    if prof is None:
        return {"ok": False, "obj": 0, "vtable": None, "vtable_ok": False,
                "value_id": None, "value_ok": False, "set": None, "value": None}
    obj = base + prof["rva_battle_ai"]
    vtable = pb.read_u32(h, obj + OFF_VTABLE)
    value_id = pb.read_u32(h, obj + OFF_VALUE_ID)
    setb = pb.read_u8(h, obj + OFF_SET)
    val = pb.read_u8(h, obj + OFF_VALUE)
    vtable_ok = (vtable == base + prof["vtable_rva"])
    value_ok = (value_id == EXPECT_VALUE)
    return {
        "obj": obj, "vtable": vtable, "vtable_ok": vtable_ok,
        "value_id": value_id, "value_ok": value_ok,
        "set": setb, "value": val, "ok": vtable_ok and value_ok,
    }


def set_battle_ai(h, base, on):
    """注入(on=True)/取消(on=False) battle_ai。校准失败则拒绝写入。
    返回 (ok, d)，d=describe() + write 结果（w1/w2/rb_set/rb_value/verified）。"""
    d = describe(h, base)
    if not d["ok"]:
        return False, d
    want = 1 if on else 0
    w1 = pb.write_u8(h, d["obj"] + OFF_SET, want)
    w2 = pb.write_u8(h, d["obj"] + OFF_VALUE, want)
    rb_set = pb.read_u8(h, d["obj"] + OFF_SET)
    rb_val = pb.read_u8(h, d["obj"] + OFF_VALUE)
    verified = bool(w1 and w2 and rb_set == want and rb_val == want)
    d.update({"w1": w1, "w2": w2, "rb_set": rb_set, "rb_value": rb_val,
              "verified": verified})
    return verified, d


def fmt_status(d):
    if not d["ok"]:
        return "(引擎 profile 未找到)"
    return (f"obj=0x{d['obj']:08x} vtable=0x{d['vtable']:08x} "
            f"{'OK' if d['vtable_ok'] else 'MISMATCH'} "
            f"value_id=0x{d['value_id']:02x} {'OK' if d['value_ok'] else 'MISMATCH'} "
            f"set=0x{d['set']:02x} value=0x{d['value']:02x}")


def main():
    args = [a for a in sys.argv[1:]]
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    h, base, build, pid = open_game()
    if not h:
        return 2

    d = describe(h, base)
    print(f"[battle_ai] {fmt_status(d)}")
    if not d["ok"]:
        print("✗ 校准失败（vtable/value_id 不符）——拒绝写入，请检查引擎 build / 锚点")
        return 3

    if not args or args[0] == "status":
        print(f"当前 battle_ai = {d['value']}（{'注入中' if d['value'] else '未注入'}；"
              f"消费时机=战斗加载时，战前写管整个会话，重启游戏需重写）")
        return 0

    cmd = args[0]
    if cmd in ("inject", "cancel"):
        on = (cmd == "inject")
        ok, d2 = set_battle_ai(h, base, on)
        act = "注入 battle_ai=1" if on else "取消 battle_ai=0"
        if ok:
            print(f"✓ {act}：set=0x{d2['rb_set']:02x} value=0x{d2['rb_value']:02x} 回读验证通过")
        else:
            print(f"✗ {act} 写入失败/回读不符：set=0x{d2['rb_set']:02x} value=0x{d2['rb_value']:02x} "
                  f"(w1={d2.get('w1')} w2={d2.get('w2')})")
            return 4
        return 0

    print(f"未知命令 {cmd}\n" + __doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
