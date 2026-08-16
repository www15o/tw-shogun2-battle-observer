# -*- coding: utf-8 -*-
"""_re_battle_watcher.py — 常驻战斗观察器：轮询 battle_mgr 变化 → 每场自动 dump st 链
（army vtable=战斗类型线索 + [army+0x114] 单位数 + 筛选判定）。
用法：python tools/_run_elev.py _re_battle_watcher.py [--once]
输出：控制台 + captures/h47a/battle_watch_*.jsonl（每场一条）
零写入游戏。解决手动 dump 赶不上战斗结束（海战 1v1 几十秒）的问题。"""
import sys, os, json, time, ctypes
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb
import re_h46a as H46

# ★自动跳过（2026-08-19）：判定 ❌ → 发 ESC 退出战斗（用户确认 ESC 有效）
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_ESCAPE = 0x1B
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "captures", "h47a")

_user32 = ctypes.WinDLL("user32", use_last_error=True)


def find_game_hwnd(pid):
    """EnumWindows 找指定 PID 的主窗口句柄"""
    hwnds = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lparam):
        if _user32.IsWindowVisible(hwnd):
            wpid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid:
                hwnds.append(hwnd)
        return True
    _user32.EnumWindows(cb, 0)
    return hwnds[0] if hwnds else None


def send_esc(hwnd):
    """向游戏窗口发 ESC（WM_KEYDOWN+KEYUP）——退出战斗/菜单。
    ★S19 修复（2026-08-19，re_s19_battle_exit_report.md）：lParam 不能为 0——
    0xbccee0 从 lParam bits16-24 取扫描码，0 → 游戏键 -1 → WndProc js 静默丢弃。
    修正 = KEYDOWN lParam=0x00010001（重复计数1+扫描码1=ESC）/ KEYUP lParam=0xC0010001（释放位）。"""
    try:
        _user32.PostMessageW(hwnd, WM_KEYDOWN, VK_ESCAPE, 0x00010001)
        time.sleep(0.05)
        _user32.PostMessageW(hwnd, WM_KEYUP, VK_ESCAPE, 0xC0010001)
        return True
    except Exception:
        return False

ST_GRP_CNT = 0x88
ST_GRP_TBL = 0x8c
GRP_ARMY_CNT = 0x20
GRP_ARMY_TBL = 0x24
ARMY_UNIT_CNT = 0x114
ARMY_UNIT_TBL = 0x118
ARMY_FACTION = 0x258
OFF_P58_BTYPE = 0x58   # pending+0x58 = BATTLE_TYPE（S15 定案）

# ★类型阈值（2026-08-19 用户定：海战 total≥10 / 野战 total≥40 / 攻城 total≥30）
BTYPE_THRESHOLD = {
    (0, 1, 2): 40,        # 野战 NORMAL/AMBUSH/BRIDGE
    (3, 4, 5, 6, 7, 8, 9, 10): 30,  # 攻城 FORT_*/FORTIFIED_*/UNFORTIFIED/REGION_SLOT
    (11, 12, 13, 14): 10, # 海战 NAVAL_*
}


def btype_threshold(btype):
    for keys, thr in BTYPE_THRESHOLD.items():
        if btype in keys:
            return thr
    return None  # 15=UNSPECIFIED 或未知 → 不筛（默认加载）


def rd32(h, a):
    if not a or not (0x10000 < a < 0x80000000):
        return None
    try:
        return pb.read_u32(h, a)
    except Exception:
        return None


def dump_battle(h, base, mgr, pending_btype=None):
    env = rd32(h, mgr + 0x110)
    st = None
    if env:
        e8 = rd32(h, env + 8)
        st = rd32(h, e8 + 0xb4) if e8 else None
    rec = {"t": time.strftime('%H:%M:%S'), "mgr": hex(mgr) if mgr else None,
           "env": hex(env) if env else None, "st": hex(st) if st else None,
           "btype": pending_btype}
    if not st:
        return rec
    gcnt = rd32(h, st + ST_GRP_CNT)
    gtbl = rd32(h, st + ST_GRP_TBL)
    rec["grp_cnt"] = gcnt
    groups = []
    totals = []
    for gi in range(min(gcnt or 0, 8)):
        g = rd32(h, gtbl + gi * 4) if gtbl else None
        if not g or not (0x10000 < g < 0x80000000):
            continue
        acnt = rd32(h, g + GRP_ARMY_CNT)
        atbl = rd32(h, g + GRP_ARMY_TBL)
        gvt = rd32(h, g)
        gtot = 0
        armies = []
        for ai in range(min(acnt or 0, 16)):
            a = rd32(h, atbl + ai * 4) if atbl else None
            if not a or not (0x10000 < a < 0x80000000):
                continue
            ucnt = rd32(h, a + ARMY_UNIT_CNT)
            avt = rd32(h, a)
            fct = rd32(h, a + ARMY_FACTION)
            if ucnt and 0 < ucnt < 500:
                gtot += ucnt
            armies.append({"army": hex(a), "vtable": hex(avt) if avt else None,
                           "units": ucnt, "faction": hex(fct) if fct else None})
        groups.append({"group": hex(g), "gvtable": hex(gvt) if gvt else None,
                       "army_cnt": acnt, "armies": armies, "unit_total": gtot})
        totals.append(gtot)
    rec["groups"] = groups
    rec["unit_totals"] = totals
    # ★筛选判定（2026-08-19 用户规则 v3：按类型阈值 total——海战≥10/野战≥40/攻城≥30）
    if len(totals) >= 2:
        a, d = totals[0], totals[1]
        total = a + d
        thr = btype_threshold(pending_btype) if pending_btype is not None else None
        if thr is None:
            rec["screening"] = {"att": a, "def": d, "total": total, "btype": pending_btype,
                                "rule": "无类型阈值（btype未知/15）→ 默认加载", "verdict": "✅加载"}
        else:
            skip = total < thr
            rec["screening"] = {"att": a, "def": d, "total": total, "btype": pending_btype,
                                "threshold": thr, "rule": f"total<{thr}→跳过（类型阈值）",
                                "verdict": "❌跳过" if skip else "✅值得看"}
    return rec


def maybe_auto_skip(h, base, hwnd, mgr, btype, battle_tag):
    """★自动跳过执行 v2（2026-08-19 援军机制发现）：判定前延迟 5s 重读单位数——
    援军加入会让 total 变化（16v11→16v14 实机），2s 判定会误杀。5s 等援军窗口后重判。"""
    if not hwnd:
        return False
    print(f"  ⏳ 等待援军窗口（5s）后重判…")
    time.sleep(5.0)
    rec2 = dump_battle(h, base, mgr, btype)
    scr2 = rec2.get("screening")
    print(f"  [5s 重判] 单位数={rec2.get('unit_totals')} 筛选={scr2}")
    if scr2 and scr2.get("verdict") == "❌跳过":
        ok = send_esc(hwnd)
        print(f"  ⛔ 自动跳过（重判确认低价值）→ ESC {'✅' if ok else '❌ 失败'}")
        return True
    print("  ✅ 重判值得看（不跳过）")
    return False


def main():
    once = "--once" in sys.argv
    h, base = H46.open_proc()
    print(f"base={base:#x} 战斗观察器启动（轮询 battle_mgr 变化，类型阈值筛选 v3 + 自动 ESC 跳过）…")
    # 找游戏窗口（自动跳过用）
    from probe_battle_env import find_pid
    gpid = find_pid()
    hwnd = find_game_hwnd(gpid) if gpid else None
    print(f"游戏 PID={gpid} 窗口={hex(hwnd) if hwnd else '未找到（自动跳过禁用）'}")
    jp = os.path.join(OUT, f"battle_watch_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
    model = None
    # ★启动基线（2026-08-19）：启动时已在进行的战斗不判定（损耗后单位数会误判跳过）
    last_mgr = rd32(h, base + 0x1bc8180)
    print(f"启动基线 mgr={hex(last_mgr) if last_mgr else None}（进行中战斗不判定）")
    t0 = time.time()
    while True:
        mgr = rd32(h, base + 0x1bc8180)
        if mgr and mgr != last_mgr:
            last_mgr = mgr
            # 读 pending btype（类型阈值需要）
            btype = None
            try:
                if model is None:
                    model, _p = H46.anchor(h, base)
                p = rd32(h, model + 0x14a4)
                if p and 0x10000 < p < 0x80000000:
                    btype = pb.read_u8(h, p + OFF_P58_BTYPE) if hasattr(pb, "read_u8") else rd32(h, p + OFF_P58_BTYPE)
            except Exception:
                pass
            rec = dump_battle(h, base, mgr, btype)
            line = json.dumps(rec, ensure_ascii=False)
            scr = rec.get("screening")
            print(f"\n[{time.strftime('%H:%M:%S')}] ★新战斗 mgr={rec.get('mgr')} "
                  f"btype={rec.get('btype')} 组数={rec.get('grp_cnt')} 单位数={rec.get('unit_totals')} "
                  f"筛选={scr}")
            for g in rec.get("groups", []):
                for am in g.get("armies", []):
                    print(f"  army={am['army']} vtable={am['vtable']} 单位数={am['units']}")
            with open(jp, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            # ★自动跳过 v2：延迟 5s 重判（援军窗口）后决定 ESC
            maybe_auto_skip(h, base, hwnd, mgr, btype, f"mgr={rec.get('mgr')}")
            if once:
                break
        time.sleep(0.5)


if __name__ == "__main__":
    main()
