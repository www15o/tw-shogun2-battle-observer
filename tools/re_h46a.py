# -*- coding: utf-8 -*-
"""re_h46a.py — H46a 实机实验：直写 [pending+0x50]=9 走状态机（目标3 撬动候选）。

依据（2026-08-10 静态，work/re_h45_static_report.md + 32_HANDOFF）：
- pending 状态 +0x50 写者 = FUN_1057fca0（语义：mov [pending+0x50]=arg）
- pending 状态机 = FUN_10604260（主循环 FUN_10703ea0 @0x7045d8 分支：状态!=10 才调）
- 状态：1=激活(处理setup条目) 2=待冲突 5/7/8=推进 9=战斗启动(→FUN_10560470) 10=空/终态
- 29 实验 Q「手动写激活态被清空」只写了 setup 字段没写状态 → H46a 写状态走状态机（未实验）

用法（游戏运行中，看海态等 AI 内战）：
  python -u tools/re_h46a.py --dump          # 只读：锚点 + pending 全字段基线
  python -u tools/re_h46a.py --set-state 9   # 写 [pending+0x50]=9 + 回读（单变量）
  python -u tools/re_h46a.py --watch         # 轮询 conf/pending/state/setup 变化
  python -u tools/re_h46a.py --auto 9        # watch 模式：检测到 conf 登记时自动写一次状态 9

纪律：只写 +0x50 一个字段；写前回读；回滚 = 写回 10。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb
import re_a3_probe as a3

PENDING = 0x14a4
CONF = 0x149c
CONF_BEGIN = 0x20
CONF_END = 0x24

# pending 观测字段（RVA 偏移）
FIELDS = {
    "state(+0x50)": (0x50, "u32"),
    "word54(+0x54)": (0x54, "u16"),
    "ready(+0x55)": (0x55, "u8"),
    "btype(+0x58)": (0x58, "u32"),
    "parts(+0x60)": (0x60, "u32"),
    "parts2(+0x64)": (0x64, "u32"),
    "cnt_b8(+0xb8)": (0xb8, "u32"),
    "tbl_bc(+0xbc)": (0xbc, "u32"),
    "state_c0(+0xc0)": (0xc0, "u32"),
    "c2(+0xc2)": (0xc2, "u8"),
    "c3(+0xc3)": (0xc3, "u8"),
    "f0(+0xf0)": (0xf0, "u8"),
    "f4(+0xf4)": (0xf4, "u32"),
    "setup0(+0xf8)": (0xf8, "u32"),
    "setup_active(+0xfc)": (0xfc, "u32"),
    "setup_cnt(+0x100)": (0x100, "u32"),
    "setup_slots(+0x104)": (0x104, "u32"),
    "setup_x(+0x108)": (0x108, "u32"),
}


def read_uid(p):
    return p is not None and 0x10000 < p < 0x80000000


def read_conf_cnt(h, model):
    """冲突管理器条目数（链表：+0x20 头 → [node+4] next → +0x24 哨兵）"""
    mgr = pb.read_u32(h, model + CONF)
    if not read_uid(mgr):
        return None
    begin = pb.read_u32(h, mgr + CONF_BEGIN)
    end = pb.read_u32(h, mgr + CONF_END)
    cnt = 0
    node = begin
    for _ in range(2000):
        if node == end or not read_uid(node):
            break
        node = pb.read_u32(h, node + 4)
        cnt += 1
    return cnt


def open_proc():
    pid = pb.find_pid()
    if not pid:
        print("未找到游戏进程"); sys.exit(1)
    h = pb.K32.OpenProcess(pb.PROCESS_QUERY_INFORMATION | pb.PROCESS_VM_READ |
                           pb.PROCESS_VM_WRITE | pb.PROCESS_VM_OPERATION, False, pid)
    if not h:
        print("OpenProcess 失败（需管理员权限）"); sys.exit(1)
    base = pb.module_base(h, "Empire.Retail.dll")
    return h, base


def read_pending_fields(h, base, model, pending):
    out = {}
    for name, (off, kind) in FIELDS.items():
        try:
            if kind == "u32":
                out[name] = pb.read_u32(h, pending + off)
            elif kind == "u16":
                # 无 read_u16：读 u32 取低 16 位
                out[name] = pb.read_u32(h, pending + off) & 0xFFFF
            else:
                out[name] = pb.read_u8(h, pending + off)
        except Exception:
            out[name] = None
    out["conf_cnt"] = read_conf_cnt(h, model)
    return out


def anchor(h, base, known_model=None):
    """锚定：known_model 优先；否则扫 model vtable（find_model 无盲区）→ 校验 [model+0x14a4]。
    教训（H44）：find_pending（扫 pending vtable）有盲区——堆上的 pending 扫不到（0x3c66b328
    明明存在）；find_model 能扫到 model（0x4a6fc468）→ 用 model 侧锚定更可靠。"""
    if known_model and 0x10000 < known_model < 0x80000000:
        v = pb.read_u32(h, known_model)
        if v == base + a3.MODEL_VTABLE_RVA:
            p = pb.read_u32(h, known_model + PENDING)
            if p and 0x10000 < p < 0x80000000 and pb.read_u32(h, p) == base + a3.PENDING_VTABLE_RVA:
                return known_model, p
    print("known_model 锚定失败，扫 model vtable…")
    for m in a3.find_model(h, base):
        if not (0x10000 < m < 0x80000000):
            continue
        v = pb.read_u32(h, m)
        if v == base + a3.MODEL_VTABLE_RVA:
            p = pb.read_u32(h, m + PENDING)
            if p and 0x10000 < p < 0x80000000 and pb.read_u32(h, p) == base + a3.PENDING_VTABLE_RVA:
                # 双向校验：[pending+0x4c] 应回指 model
                if pb.read_u32(h, p + 0x4c) == m:
                    return m, p
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--set-state", type=lambda s: int(s, 0))
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--auto", type=lambda s: int(s, 0), default=0)
    ap.add_argument("--log", default="captures/h46a/h46a.json")
    args = ap.parse_args()

    h, base = open_proc()
    model, pending = anchor(h, base)
    if not model or not pending:
        print("锚定失败（需要战役地图界面）"); sys.exit(1)
    print(f"base={base:#x} model={model:#x} pending={pending:#x}")

    if args.dump:
        print(json.dumps(read_pending_fields(h, base, model, pending), indent=1, ensure_ascii=False))
        return

    if args.set_state is not None:
        old = pb.read_u32(h, pending + 0x50)
        pb.write_u32(h, pending + 0x50, args.set_state)
        new = pb.read_u32(h, pending + 0x50)
        print(f"[write] pending+0x50: {old} -> {new}")
        return

    # watch / auto
    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    last = None
    t0 = time.time()
    log = []
    fired = False
    print("watch 开始（Ctrl+C 停止）: conf/pending/state/setup 变化监控")
    while True:
        p = pb.read_u32(h, model + PENDING)
        if p and p != pending:
            print(f"[{time.strftime('%H:%M:%S')}] ⚡ pending 替换: {pending:#x} -> {p:#x}")
            pending = p
        s = read_pending_fields(h, base, model, pending)
        s["t"] = round(time.time() - t0, 2)
        if s != last:
            print(json.dumps(s, ensure_ascii=False))
            last = s
            log.append(s)
            with open(args.log, "w") as f:
                json.dump(log, f, ensure_ascii=False, indent=1)
        if args.auto and not fired and s.get("conf_cnt"):
            old = pb.read_u32(h, pending + 0x50)
            pb.write_u32(h, pending + 0x50, args.auto)
            print(f"[{time.strftime('%H:%M:%S')}] 🎯 AUTO: conf 登记时写 +0x50 {old} -> {args.auto}")
            fired = True
        time.sleep(0.2)


if __name__ == "__main__":
    main()
