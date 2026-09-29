# -*- coding: utf-8 -*-
"""_prefilter_logic.py — Goal3.1 M3 PRE 筛选计算核心（纯逻辑，可单测）

输入：setup 链军队表条目 `(unit_count, army_ptr)` + 双方持久 faction + 用户配置。
输出：total / per_side / army_count / ratio / pass / reason。

设计意图：这个纯函数先在 Python 侧把“筛选条件”定清楚，后续可：
  1) host 侧用 probe 数据直接调用（host-mediated 方案）；
  2) 作为移植进 x86 stub 的语义基准（in-stub 方案）。

用法：
  python tools/_prefilter_logic.py --selftest
"""
import argparse
import sys

# 默认阈值（用户可配置）
DEFAULT_CFG = {
    "min_total": 20,       # 总单位数下限
    "min_per_side": 5,     # 每侧单位数下限
    "min_armies": 2,       # 至少参战军队数
    "max_ratio": 2.0,      # 双方规模比上限（大/小）
}


def resolve_army_faction(army_ptr, resolver):
    """army 持久 faction = getter(&army+0x25c) = **(army+0x260)。

    resolver 是 `(army_ptr) -> faction_ptr|None` 的可调用；纯逻辑不直接读内存。
    """
    if not army_ptr or not resolver:
        return None
    return resolver(army_ptr)


def evaluate_prefilter(entries, side_factions, cfg=None, resolver=None):
    """计算 PRE 筛选结果。

    entries: iterable of (unit_count, army_ptr)
    side_factions: dict with keys 'sideA'/'sideD' -> persistent faction ptr (or None)
    cfg: 覆盖 DEFAULT_CFG 的阈值 dict
    resolver: army_ptr -> faction_ptr（从内存读 army 持久 faction 的回调；缺省用 None）
    """
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    entries = list(entries or [])

    total = 0
    army_count = len(entries)
    per_side = {"sideA": 0, "sideD": 0}
    facA = side_factions.get("sideA")
    facD = side_factions.get("sideD")

    for unit_count, army_ptr in entries:
        uc = int(unit_count or 0)
        total += uc
        fac = resolve_army_faction(army_ptr, resolver)
        if fac is not None and facA is not None and fac == facA:
            per_side["sideA"] += uc
        elif fac is not None and facD is not None and fac == facD:
            per_side["sideD"] += uc
        else:
            # 无法归侧：先计入总量，per_side 不增；由 fail-open 决定是否放行
            per_side.setdefault("unknown", 0)
            per_side["unknown"] = per_side["unknown"] + uc

    side_vals = [per_side.get("sideA", 0), per_side.get("sideD", 0)]
    small = min(side_vals)
    big = max(side_vals)
    ratio = (big / small) if small > 0 else None

    reasons = []
    if total < cfg.get("min_total", 0):
        reasons.append(f"total {total} < min_total {cfg['min_total']}")
    if any(v < cfg.get("min_per_side", 0) for v in side_vals):
        reasons.append(f"per_side {side_vals} < min_per_side {cfg['min_per_side']}")
    if army_count < cfg.get("min_armies", 0):
        reasons.append(f"armies {army_count} < min_armies {cfg['min_armies']}")
    if cfg.get("max_ratio") and ratio is not None and ratio > cfg["max_ratio"]:
        reasons.append(f"ratio {ratio:.2f} > max_ratio {cfg['max_ratio']}")

    return {
        "total": total,
        "per_side": per_side,
        "army_count": army_count,
        "ratio": ratio,
        "pass": not reasons,
        "reason": "; ".join(reasons) if reasons else "ok",
    }


def selftest():
    # 假 resolver：army 0xA1->facA, 0xA2->facA, 0xB1->facB
    facA, facB = 0x900000, 0xA00000
    resolver = lambda a: {0xA1: facA, 0xA2: facA, 0xB1: facB}.get(a)
    entries = [(7, 0xA1), (12, 0xB1), (5, 0xA2)]
    side_factions = {"sideA": facA, "sideD": facB}
    cfg = {"min_total": 20, "min_per_side": 5, "min_armies": 2, "max_ratio": 2.0}

    r = evaluate_prefilter(entries, side_factions, cfg, resolver)
    print(r)
    ok = (r["total"] == 24 and r["per_side"]["sideA"] == 12
          and r["per_side"]["sideD"] == 12 and r["army_count"] == 3
          and abs(r["ratio"] - 1.0) < 1e-9 and r["pass"] is True)
    print("selftest", "PASS [OK]" if ok else "FAIL [X]")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    print("use --selftest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
