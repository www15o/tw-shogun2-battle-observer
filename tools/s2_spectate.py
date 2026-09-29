# -*- coding: utf-8 -*-
"""s2_spectate.py — 看海捕捉核心（2026-08-19，整合进 s2_control_gui）
★2026-08-28 升级：A1 = FUN_105caa60 入口 hook（0x5caa60）
  过滤全部前移到写 b9 之前：
    ① vt 匹配（pending）
    ② ready==0（AI 内战未登记）
    ③ 155c==0（排除双人类模式）
    ④ btype 双范围（海战跳过）
    ⑤ 加载前规模（S16：冲突列表 cmgr → [army+0x294] 求和 vs 阈值；⚠️ +0x294 是规模代理非单位数）
    ⑥ 阵营白名单/黑名单（S18：side+0x64 持久 faction 对象指针匹配）
  通过才写 [pending+0xb9]=1 → 引擎同 tick 走状态 4 → 加载。
  外部观测线程仅作兜底/日志（加载后精筛），不再是主要判定。

用法（独立 CLI）：
  python s2_spectate.py --type siege --scale 30 --factions 织田 --auto-esc
  --type siege/field/naval（btype 范围：攻城3-10/野战0-2/海战11-14）
  --scale N：total< N 跳过（0=不筛）；--scale-naval/--scale-field/--scale-siege 分类型
  ⚠️ scale 基于 [army+0x294] 规模代理，非单位总数（PRE=12 vs POST≈19-20），仅当接受该语义时使用
  --factions 白名单（逗号分隔）；--exclude 黑名单
  --auto-esc：判定跳过自动发 ESC（issp=1 才退，issp=0 投降保护）
  --dyn-decisive：动态决战——已捕捉最大 PRE 总单位数×80% 作总下限，双方最不平衡 1:2
  --pre-min-ratio F：手动设置双方最小比例（配合 PRE 单位筛；动态默认 0.5）
  --observe SEC / --restore
"""
import ctypes
import json
import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb
import re_f10_stub_builder as B

# ── A1 入口 hook 常量（E2 主推：FUN_105caa60 入口）──
VT_PENDING_RVA = 0x15fa8a4
MODEL_VTABLE_RVA = 0x1607bb4      # model vtable（40 §8）
ARMY_VTABLE_RVA = 0x1606a08       # S16：战役层军队对象 vtable
FACTION_VTABLE_RVA = 0x15fac30    # 持久 faction vtable
HOOK_RVA = 0x5caa60               # FUN_105caa60 入口
BACK_RVA = 0x5caa65               # 入口后 5 字节
ORIG_BYTES = bytes([0x53, 0x56, 0x8B, 0xF1, 0x57])  # push ebx; push esi; mov esi,ecx; push edi
OLD_HOOK_RVA = 0x6045c4           # 旧版 call-site hook（迁移清理用）
OLD_ORIG_BYTES = bytes([0xE8, 0x97, 0x64, 0xFC, 0xFF])
DISPATCHER_HOOK_RVA = 0x6e9f60     # FUN_106e9f60 pending 构造分发器（cmgr 未清空）
DISPATCHER_BACK_RVA = 0x6e9f65
DISPATCHER_ORIG_BYTES = bytes([0x83, 0xEC, 0x2C, 0x53, 0x55])
DISPATCHER_STUB_OFF = 0x2000        # 第二个 stub 放 region+0x2000

# stub 区布局（region 0x2000，代码从 0 起；数据区放在 0x1000+ 避免与代码重叠）
DATA_CFG = 0x1000                 # 过滤配置
DATA_META = 0x1100                # 诊断/计数
DATA_SCALE = 0x1200               # S16 规模数组（32×u32）
DATA_CACHE = 0x1600                # dispatcher 规模缓存
CACHE_MODEL, CACHE_TOTAL, CACHE_COUNT, CACHE_SEQ = 0x00, 0x04, 0x08, 0x0c
DATA_WL = 0x1300                  # 白名单 faction 指针数组（64×u32）
DATA_BL = 0x1400                  # 黑名单 faction 指针数组（64×u32）
MAX_ARMIES = 32
MAX_FACS = 64

# PRE setup data (Goal3.1 M3)
SETUP_HOOK_RVA = 0x6bba80
SETUP_BACK_RVA = 0x6bba86
SETUP_ORIG6 = bytes([0x81,0xEC,0x8C,0x00,0x00,0x00])
SETUP_STUB_OFF = 0x2800
DATA_PRE = 0x1A00
DATA_PRE_SETUP = 0x1A04
DATA_PRE_SEQ = 0x1A08
DATA_PRE_UNIT = 0x1A0C
DATA_PRE_UNIT_SEQ = 0x1A10
DATA_PRE_UNIT_PENDING = 0x1A14
DATA_PRE_UNIT_ANSWERED_PENDING = 0x1A18   # 宿主写回时标记该判定属于哪个 pending

# ★有界 defer（2026-08-30，PRE PASS 后强制追加 state-1 循环，确保本场消费 b9）
DATA_PRE_UNIT_DEFER = 0x1A1C             # byte: 1=armed
DATA_PRE_UNIT_DEFER_TARGET = 0x1A20      # dword: 本 pending
DATA_PRE_UNIT_DEFER_BUDGET = 0x1A24      # byte: 剩余 defer 次数
DEFER_BUDGET_MAX = 3                     # 先 3 次，实机可调
DEFER_HOOK_RVA = 0x604954                # state-6 结算写点: push 5; mov ecx,edi; call 0x57fca0
DEFER_BACK_RVA = 0x60495d                # 越过被覆盖的 5 字节后继续
DEFER_ORIG5 = bytes([0x6A, 0x05, 0x8B, 0xCF, 0xE8])  # push 5; mov ecx,edi; call(首字节)
DEFER_SETTER_RVA = 0x57fca0              # 6262 defer call 目标（state setter，this=edi=pending+0x4c）
DEFER_STUB_OFF = 0x3000                  # 新 hook stub 放 region+0x3000

# 6118 defer 几何（仅 6118 分支使用；6262 保持 None，见 defer_stub_param_plan_20260904.md）
DEFER_REPLAY_BYTES = None    # 6118: 窗口后需重放的字节（含 call edx）
DEFER_BACK_DEFER_RVA = None  # 6118: defer 改写后跳 merge/放行点 0xc22e24
DEFER_PENDING_SLOT = None    # 6118: pushad 后 saved pending(edi) 在 [esp+slot]（=0）

# ── 引擎化状态机返回址 / battle_mgr 槽（6262 现役默认；6118 由 apply_engine 重绑）──
# A1 stub 内 META_RET 白名单 + pre-unit 自旋分级/写 b9 判据用的三个返回址；
# _units_of/_observe 的 battle mgr 全局槽。值只准经 profile（6118 侧）或本常量（6262 侧）。
SITE_A_RVA = 0x6045a4        # state-1 决策返回址 a
SITE_B_RVA = 0x6045c9        # state-1 决策返回址 b
SITE_C_RVA = 0x604936        # state-6 结算/预热返回址 c
BATTLE_MGR_SLOT_RVA = 0x1bc8180   # battle_mgr 全局槽（观测链入口）

# ★AV 残余风险处置参数（2026-08-30，不破坏已验证骨架）
# SPIN_FULL/SPIN_SHORT 只是 stub 自旋上限；实际卡游戏线程时长由宿主应答时间决定（≤PRE_POLL_MAX）。
SPIN_FULL = 50000000      # state-6（0x604936）保守自旋上限
SPIN_SHORT = 5000000      # state-1（0x6045a4/0x6045c9）短窗口兜底
PRE_POLL_MAX = 0.02       # 宿主每事件最久轮询时间（秒），20ms 级硬上界

# cfg 字段偏移
CFG_MIN1, CFG_MAX1, CFG_MIN2, CFG_MAX2, CFG_FLAGS = 0x00, 0x04, 0x08, 0x0c, 0x10
CFG_NAVAL, CFG_FIELD, CFG_SIEGE = 0x14, 0x18, 0x1c
CFG_WL_COUNT, CFG_BL_COUNT = 0x20, 0x24
CFG_PRE_MIN_TOTAL, CFG_PRE_MIN_PER_SIDE = 0x28, 0x2c
# flags bit
F_BTYPE1, F_BTYPE2, F_SCALE, F_WHITELIST, F_BLACKLIST, F_PRE, F_PRE_UNIT = 1, 2, 4, 8, 16, 32, 64

# meta 字段偏移（DATA_META + off）
META_EVENT, META_PENDING, META_STATUS = 0x00, 0x04, 0x08
META_MODEL, META_MODEL_VT = 0x0c, 0x10
META_COUNT, META_TOTAL, META_TMP = 0x14, 0x18, 0x1c
META_SIDE1, META_SIDE2 = 0x20, 0x24
META_WL_MATCH, META_BL_HIT = 0x28, 0x2c
META_REASON = 0x30              # 0=写b9, 1=vt, 2=ready, 3=155c, 4=btype, 5=scale, 6=whitelist, 7=blacklist, 0xff=未定
META_B9 = 0x34                  # stub 写 b9 后立即回读的字节（1=确认写入，0=异常）
META_RET = 0x38                 # 最近一次状态机 A1 调用的返回地址（区分 state-1/state-6）

REASON_NAMES = {0: "写b9", 1: "vt不匹配", 2: "ready!=0", 3: "双人类155c",
                4: "btype过滤", 5: "规模过滤", 6: "白名单未命中", 7: "黑名单命中", 8: "PRE筛选", 9: "PRE单位过滤", 10: "非状态机调用", 0x0b: "预热未写", 0xff: "未定"}

BTYPE_RANGES = {"field": (0, 2), "siege": (3, 10), "naval": (11, 14)}


def btype_category(btype):
    """按 PRE 规模筛选/动态决战的分桶：野战 0-2，攻城 3-10，其余（海战/未知）None。"""
    if btype is None:
        return None
    if 0 <= btype <= 2:
        return "field"
    if 3 <= btype <= 10:
        return "siege"
    return None
BTYPE_NAMES = {0: "NORMAL野战", 1: "AMBUSH野战", 2: "BRIDGE野战",
               3: "FORT_SIEGE攻城", 4: "FORT_BLOODBATH攻城", 5: "FORTIFIED_SETTLEMENT攻城",
               6: "FORTIFIED_SETTLEMENT_STANDARD攻城", 7: "FORTIFIED_SETTLEMENT_SALLY_OUT攻城",
               8: "FORTIFIED_SETTLEMENT_SIEGE攻城", 9: "UNFORTIFIED_SETTLEMENT攻城",
               10: "REGION_SLOT攻城", 11: "NAVAL_NORMAL海战", 12: "NAVAL_BLOCKADE_BREAKOUT海战",
               13: "NAVAL_BLOCKADE_RELIEF海战", 14: "NAVAL_PORT_ASSAULT海战", 15: "UNSPECIFIED"}

ST_GRP_CNT, ST_GRP_TBL = 0x88, 0x8c
GRP_ARMY_CNT, GRP_ARMY_TBL = 0x20, 0x24
ST_SWITCHED = 0x31f0
ARMY_270 = 0x270          # byte: 1=人类 0=AI
ARMY_28C = 0x28c          # dword: 1=AI 激活
ARMY_290 = 0x290          # float: 速度/激活相关
ARMY_294 = 0x294          # dword: 规模代理
ARMY_UNIT_TBL = 0x118     # 军队单位表指针数组
UNIT_EA8 = 0xea8          # dword: 1=AI 控制
# ★单位数偏移（2026-08-19 FotS 差分：原版 [army+0x114] 定案；FotS +0x114=0、+0x12c=单位数（3v2 精确））
ARMY_UNIT_CNT_VANILLA = 0x114
ARMY_UNIT_CNT_FOTS = 0x12c
FACTION_NAME_OFF = 0x0b14
# ★阵营/宗教（2026-08-31 静态定位）：0x5ff820 = mov eax,[ecx+0x6d4]；返回 state religion key 字符串指针
FACTION_RELIGION_OFF = 0x6d4

# 宗教 key → 显示名。random_sengoku_camps 模组把 religions.loc 显示名改成了东军/西军/第三军；
# 幕末用佐幕派/保皇派/独立；源平用藤原/源/平。未知 key 会回退显示原始 key。
RELIGION_DISPLAY = {
    "rel_buddhist": "东军",           # 模组：原版战国 佛教 → 东军
    "rel_catholic": "西军",           # 模组：原版战国 天主教 → 西军
    "rel_ikko": "第三军",             # 模组：原版战国 一向宗 → 第三军
    "inf_bos_shogunate": "佐幕派",
    "inf_bos_imperial": "保皇派",
    "inf_bos_self": "独立",
    "inf_gem_fujiwara": "藤原氏",
    "inf_gem_minamoto": "源氏",
    "inf_gem_taira": "平氏",
    "inf_gem_neutral": "中立",
}

# ═══════════════════════════════════════════════════════════════════════════
# ★ 引擎装配层（2026-09-05，audit §G 五项之①）：所有引擎差异常量经 apply_engine()
#   从旧引擎 profile 重绑模块全局。6262 = 本文件上方常量（默认，不重绑）；6118 =
#   只从 tools/old_engine_support/profiles/Shogun2.dll_6118.json 的 spectate 节读取
#   （隔离纪律：6118 字面值禁入本文件常量区）。
# ═══════════════════════════════════════════════════════════════════════════
ENGINE_NAME = "6262"          # 当前激活引擎：6262（默认）/ 6118 / 6115
OLD_FAMILY = ("6118", "6115")   # 旧引擎同族（结构同源；profile 各自独立）

# 6262 默认值快照（apply_engine("6262") 时恢复用；6118 重绑不覆盖快照）
_ENGINE_DEFAULTS_6262 = {g: globals()[g] for g in (
    "HOOK_RVA", "BACK_RVA", "ORIG_BYTES",
    "SITE_A_RVA", "SITE_B_RVA", "SITE_C_RVA",
    "VT_PENDING_RVA", "MODEL_VTABLE_RVA", "ARMY_VTABLE_RVA", "FACTION_VTABLE_RVA",
    "DISPATCHER_HOOK_RVA", "DISPATCHER_BACK_RVA", "DISPATCHER_ORIG_BYTES",
    "SETUP_HOOK_RVA", "SETUP_BACK_RVA", "SETUP_ORIG6",
    "BATTLE_MGR_SLOT_RVA", "OLD_HOOK_RVA",
    "DEFER_HOOK_RVA", "DEFER_BACK_RVA", "DEFER_ORIG5", "DEFER_SETTER_RVA",
    "DEFER_REPLAY_BYTES", "DEFER_BACK_DEFER_RVA", "DEFER_PENDING_SLOT",
)}

_ENGINE_MUTABLE = {           # apply_engine 可重绑的全局名（值只从 profile 读，键在此集中声明）
    # a1 入口窗口
    "HOOK_RVA": ("a1", "hook_rva"),
    "BACK_RVA": ("a1", "back_rva"),
    "ORIG_BYTES": ("a1", "orig_bytes"),          # hex str → bytes
    # 状态机返回址白名单
    "SITE_A_RVA": ("a1_sites", "ret_a"),
    "SITE_B_RVA": ("a1_sites", "ret_b"),
    "SITE_C_RVA": ("a1_sites", "ret_c"),
    # vtable 常量
    "VT_PENDING_RVA": ("vtables", "vt_pending_rva"),
    "MODEL_VTABLE_RVA": ("vtables", "vt_model_rva"),
    "ARMY_VTABLE_RVA": ("vtables", "vt_army_rva"),
    "FACTION_VTABLE_RVA": ("vtables", "vt_faction_primary_rva"),
    # dispatcher / setup hook 窗口
    "DISPATCHER_HOOK_RVA": ("dispatcher", "hook_rva"),
    "DISPATCHER_BACK_RVA": ("dispatcher", "back_rva"),
    "DISPATCHER_ORIG_BYTES": ("dispatcher", "orig_bytes"),
    "SETUP_HOOK_RVA": ("setup", "hook_rva"),
    "SETUP_BACK_RVA": ("setup", "back_rva"),
    "SETUP_ORIG6": ("setup", "orig_bytes"),
    # defer（hook 地址 + 原窗口字节 + 6118 几何键）
    "DEFER_HOOK_RVA": ("defer", "hook_rva"),
    "DEFER_ORIG5": ("defer", "window_bytes"),
    "DEFER_BACK_RVA": ("defer", "back_pass_rva"),   # 放行点：6262 0x60495d / 6118 0xc22e21
    "DEFER_REPLAY_BYTES": ("defer", "replay_bytes"),
    "DEFER_BACK_DEFER_RVA": ("defer", "back_defer_rva"),
    "DEFER_PENDING_SLOT": ("defer", "pending_saved_slot"),
    # 观测链
    "BATTLE_MGR_SLOT_RVA": ("battle_mgr_slot_rva", None),
    "OLD_HOOK_RVA": ("old_callsite_cleanup", "hook_rva"),   # 6118 = None → 跳过迁移清理
}

def _old_profile_path(build="6118"):
    """定位旧引擎 profile（6118/6115 各自文件）。候选顺序：
    ① 源码同目录（开发/仓库直跑）→ ② pyinstaller _MEIPASS（打包内置）→ ③ 当前工作目录。
    全缺失返回 dev 默认路径（报错信息含完整路径，便于诊断）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    meip = getattr(sys, "_MEIPASS", "")
    cwd = os.getcwd()
    fname = f"Shogun2.dll_{build}.json"
    cands = []
    for base in (here, meip, cwd):
        if base:
            cands.append(os.path.join(base, "old_engine_support", "profiles", fname))
    for p in cands:
        if os.path.exists(p):
            return p
    return cands[0] if cands else None


_OLD_PROFILE_PATH = _old_profile_path("6118")
_OLD_PROFILE_PATHS = {"6118": _OLD_PROFILE_PATH,
                      "6115": _old_profile_path("6115")}


def _exe_dir_of_handle(h):
    """取目标进程 shogun2.exe 所在目录（QueryFullProcessImageNameW，读盘定位同目录 Shogun2.dll 用）。"""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.c_ulong(len(buf))
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            p = buf.value
            if p and p.lower().endswith("shogun2.exe"):
                return os.path.dirname(p)
    except Exception:
        pass
    return None


def read_shogun2_build(h):
    """读同目录 Shogun2.dll 内嵌 BUILD 串 → '6118'/'6115'/None（进程磁盘源，引擎身份机器可读）。"""
    d = _exe_dir_of_handle(h)
    if not d:
        return None
    path = os.path.join(d, "Shogun2.dll")
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except Exception:
        return None
    import re as _re
    m = _re.search(rb"BUILD\s+(\d+)", blob)
    return m.group(1).decode() if m else None


def resolve_engine_for_module(h, module):
    """模块名 → 引擎（'empire'→6262；'shogun2'→按内嵌 BUILD 串区分 6115/6118）。
    返回 (engine, err)。err 非空 = 调用方应提示手动选择。"""
    if module == "empire":
        return "6262", None
    if module == "shogun2":
        b = read_shogun2_build(h)
        if b in OLD_FAMILY:
            return b, None
        return None, f"无法确定 Shogun2.dll 内嵌 build（读到 {b!r}）——请手动选 6115/6118"
    return None, f"未知模块 {module!r}"


def _hex_to_bytes(v):
    if isinstance(v, str) and v.startswith("0x"):
        return bytes.fromhex(v[2:])
    return v


def _resolve_profile(sec, key, prof=None, path=None):
    """从 profile spectate 节取叶子值；sec=None 表示 spectate 顶层键。prof=已读 dict 时复用。
    path 指定具体 build profile（缺省 = 6118 旧路径，向后兼容）。"""
    if prof is None:
        try:
            with open(path or _OLD_PROFILE_PATH, encoding="utf-8-sig") as f:
                prof = json.load(f)
        except Exception:
            return None
    s = prof.get("spectate") or {}
    if sec is None:
        return s.get(key)
    sub = s.get(sec) or {}
    if key is None:
        return sub
    return sub.get(key)


def apply_engine(name):
    """按引擎名重绑模块全局。6262 = 本文件默认（恢复）；6118/6115 = 各自 profile spectate 键。
    返回 (engine_name, err)。调用方负责在装 hook 前调用（CLI _open / GUI connect / SpectateCapture init）。"""
    global ENGINE_NAME
    name = str(name or "6262")
    if name == "6262":
        # 恢复默认：从快照写回（若之前重绑过旧引擎，这里把常量还原）
        globals().update(_ENGINE_DEFAULTS_6262)
    elif name in OLD_FAMILY:
        path = _OLD_PROFILE_PATHS.get(name)
        try:
            with open(path, encoding="utf-8-sig") as f:
                prof = json.load(f)
        except Exception as e:
            return name, f"{name} profile 读取失败: {e}"
        errs = []
        for gname, (sec, key) in _ENGINE_MUTABLE.items():
            sub = prof.get("spectate") or {}
            sub = sub.get(sec) if sec else None
            if key is None:
                val = sub                      # sec 整块（当前未用）
            else:
                val = (sub or {}).get(key) if isinstance(sub, dict) else None
                if not isinstance(sub, dict) or key not in (sub or {}):
                    errs.append(f"{gname} ← profile[{sec}][{key}] 缺失")
                    continue
            if isinstance(val, str) and val.lower().startswith("0x"):
                val = int(val, 16)
            elif gname.endswith(("_BYTES", "ORIG5", "ORIG6")) and isinstance(val, str):
                val = bytes.fromhex(val)
            globals()[gname] = val
        if errs:
            return name, "; ".join(errs)
    else:
        return name, f"未知引擎 {name}（支持 6262/{'/'.join(OLD_FAMILY)}）"
    ENGINE_NAME = name
    return name, None


def engine_ok():
    """当前引擎是否旧引擎同族（6118/6115，调用方据此走 fail-closed/同族分支）。"""
    return ENGINE_NAME in OLD_FAMILY


WM_KEYDOWN, WM_KEYUP, VK_ESCAPE = 0x0100, 0x0101, 0x1B
_user32 = ctypes.WinDLL("user32", use_last_error=True)


def _rd32(h, a):
    if not a or not (0x10000 < a < 0x100000000):
        return None
    try:
        return pb.read_u32(h, a)
    except Exception:
        return None


def _write(h, addr, data):
    got = ctypes.c_size_t()
    buf = ctypes.create_string_buffer(bytes(data))
    return bool(pb.K32.WriteProcessMemory(h, ctypes.c_void_p(addr), buf, len(data), ctypes.byref(got)))


def _read_utf16(h, ptr):
    if not ptr or not (0x10000 < ptr < 0x100000000):
        return None
    try:
        raw = bytes(pb.read_mem(h, ptr, 64))
        return raw.decode("utf-16-le", errors="ignore").split("\x00")[0]
    except Exception:
        return None

def _read_cstr(h, ptr, maxlen=64):
    """读 C 字符串（state religion key 是 ASCII/UTF-8 key，如 rel_buddhist）。"""
    if not ptr or not (0x10000 < ptr < 0x100000000):
        return None
    try:
        raw = bytes(pb.read_mem(h, ptr, maxlen))
        s = raw.decode("ascii", errors="ignore").split("\x00")[0]
        return s if s and all(32 <= ord(c) < 127 for c in s) else None
    except Exception:
        return None


def faction_religion_key(h, faction):
    """持久 faction 的 state religion key：getter 0x5ff820 即 mov eax,[faction+0x6d4]。

    实机确认 +0x6d4 指向的不是裸 C 串，而是 UTF-16 string 对象：
      [p+0]  = 长度
      [p+4]  = 容量
      [p+8]  = 数据指针（UTF-16，如 rel_catholic）
    """
    if not faction:
        return None
    p = _rd32(h, faction + FACTION_RELIGION_OFF)
    if not p:
        return None
    data = _rd32(h, p + 8)
    return _read_utf16(h, data)


def faction_religion_display(h, faction):
    """state religion key → 可读显示名；未知 key 显示原始 key，读不到显示 ?。"""
    key = faction_religion_key(h, faction)
    if not key:
        return "?"
    return RELIGION_DISPLAY.get(key, key)


def _find_hwnd(pid):
    hwnds = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _l):
        if _user32.IsWindowVisible(hwnd):
            wpid = ctypes.c_ulong()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid:
                hwnds.append(hwnd)
        return True
    _user32.EnumWindows(cb, 0)
    return hwnds[0] if hwnds else None


def _send_esc(hwnd):
    try:
        _user32.PostMessageW(hwnd, WM_KEYDOWN, VK_ESCAPE, 0x00010001)   # S19: lParam 扫描码 1
        time.sleep(0.05)
        _user32.PostMessageW(hwnd, WM_KEYUP, VK_ESCAPE, 0xC0010001)
        return True
    except Exception:
        return False


def _jcc(code, op, positions):
    """near Jcc rel32（0F 8x + rel32），目标后填。op = 0x84 je / 0x85 jne / 0x82 jb / 0x83 jae / 0x86 jbe / 0x87 ja"""
    positions.append(len(code))
    B.emit(code, b"\x0F" + bytes([op]) + b"\x00\x00\x00\x00")


def _patch_jcc(code, pos, target):
    struct.pack_into("<i", code, pos + 2, target - (pos + 6))


def _jmp(code, pos_list, target=None):
    """E9 rel32 占位；target 为 None 时后填。返回 pos（E9 字节位置）。"""
    pos = len(code)
    pos_list.append(pos)
    B.emit(code, b"\xE9\x00\x00\x00\x00")
    return pos


def _patch_jmp(code, pos, target):
    struct.pack_into("<i", code, pos + 1, target - (pos + 5))


def build_a1_stub_v2(stub_addr, base):
    """A1 入口 stub 的可维护实现：所有跳转通过 labels 字典 + 记录列表回填。"""
    cfg_addr = stub_addr + DATA_CFG
    meta = stub_addr + DATA_META
    scale_arr = stub_addr + DATA_SCALE
    wl_arr = stub_addr + DATA_WL
    bl_arr = stub_addr + DATA_BL
    cache_addr = stub_addr + DATA_CACHE
    code = bytearray()
    labels = {}
    jccs = []   # (pos, op, target_name)
    jmps = []   # (pos, target_name)

    def L(name):
        labels[name] = len(code)

    def jcc(op, target):
        pos = len(code)
        jccs.append((pos, op, target))
        B.emit(code, b"\x0F" + bytes([op]) + b"\x00\x00\x00\x00")

    def jmp(target):
        pos = len(code)
        jmps.append((pos, target))
        B.emit(code, b"\xE9\x00\x00\x00\x00")

    B.emit(code, b"\x60")                                     # pushad
    B.emit(code, b"\x89\xCE")                                 # mov esi,ecx
    # ★状态机返回地址过滤：0x6045a4/0x6045c9（state-1 决策）+ 0x604936（state-6 结算）。
    # 0x604936 保留 = p90 常见首次就绪点；其 PASS 通过 sticky approval 预热后续 state-1 决策调用。
    B.emit(code, b"\x8B\x44\x24\x20")                       # eax = [esp+0x20] return addr
    B.emit(code, b"\x3D" + struct.pack("<I", base + SITE_A_RVA))
    jcc(0x84, "ret_ok")
    B.emit(code, b"\x3D" + struct.pack("<I", base + SITE_B_RVA))
    jcc(0x84, "ret_ok")
    B.emit(code, b"\x3D" + struct.pack("<I", base + SITE_C_RVA))
    jcc(0x84, "ret_ok")
    # 非状态机调用不写 META_REASON：避免覆盖上一个状态1事件的真实判定原因
    # （宿主轮询 0.2s，状态6/包装器高频调用会把 reason 冲成 10，日志失真）
    jmp("skip_write")
    L("ret_ok")
    B.emit(code, b"\xA3" + struct.pack("<I", meta + META_RET))   # 保存返回地址供日志区分调用点
    B.emit(code, b"\x8B\x06")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x01")  # reason=vt不匹配
    B.emit(code, b"\x3D" + struct.pack("<I", base + VT_PENDING_RVA))
    jcc(0x85, "skip_write")
    B.emit(code, b"\xFF\x05" + struct.pack("<I", meta + META_EVENT))
    B.emit(code, b"\x89\x35" + struct.pack("<I", meta + META_PENDING))
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\xFF")  # reason=未定
    B.emit(code, b"\xC6\x86\xB9\x00\x00\x00\x00")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x02")  # reason=ready!=0
    B.emit(code, b"\x80\xBE\x55\x00\x00\x00\x00")
    jcc(0x85, "skip_write")
    B.emit(code, b"\x8B\x46\x30")
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "reason3_skip")
    # ★2026-08-30 AV 加固：+0x30 链必须做指针范围检查，避免状态机过渡期出现
    # 非空但已失效的指针（纯看海/白名单也走这段，崩溃前日志无 写b9）
    B.emit(code, b"\x3D\x00\x00\x01\x00")
    jcc(0x82, "reason3_skip")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")
    jcc(0x83, "reason3_skip")
    B.emit(code, b"\x8B\x40\x08")
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "reason3_skip")
    B.emit(code, b"\x3D\x00\x00\x01\x00")
    jcc(0x82, "reason3_skip")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")
    jcc(0x83, "reason3_skip")
    B.emit(code, b"\x80\xB8\x5C\x15\x00\x00\x00")
    jcc(0x85, "reason3_skip")
    jmp("reason3_ok")
    L("reason3_skip")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x03")  # reason=双人类155c
    jmp("skip_write")
    L("reason3_ok")

    # btype dual ranges（★2026-08-29 修正：两个范围是 OR，不是先只查 range1 就过滤）
    B.emit(code, b"\x0F\xB6\x8E\x58\x00\x00\x00")             # ecx = btype
    B.e_mov_eax_abs(code, cfg_addr + CFG_FLAGS)
    B.e_mov_edx_imm(code, cfg_addr)
    B.emit(code, b"\xA8\x03")                                 # test F_BTYPE1|F_BTYPE2
    jcc(0x84, "do_btype_pass")                                # 两范围都关 = 全捕捉
    B.emit(code, b"\xA8\x01")                                 # test F_BTYPE1
    jcc(0x84, "range2")                                       # range1 未启用 -> 查 range2
    B.emit(code, b"\x3B\x0A")                                 # cmp btype,[min1]
    jcc(0x82, "range2")                                       # btype < min1 -> 仍查 range2
    B.emit(code, b"\x3B\x4A\x04")                             # cmp btype,[max1]
    jcc(0x86, "do_btype_pass")                                # btype <= max1 -> 命中 range1
    L("range2")
    B.emit(code, b"\xA8\x02")                                 # test F_BTYPE2
    jcc(0x84, "btype_fail")                                   # range2 未启用 -> 过滤
    B.emit(code, b"\x3B\x4A\x08")                             # cmp btype,[min2]
    jcc(0x82, "btype_fail")                                   # btype < min2 -> 过滤
    B.emit(code, b"\x3B\x4A\x0C")                             # cmp btype,[max2]
    jcc(0x87, "btype_fail")                                   # btype > max2 -> 过滤
    jmp("do_btype_pass")
    L("btype_fail")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x04")  # reason=btype过滤
    jmp("skip_write")
    L("do_btype_pass")

    # S16 traversal (仅当 F_SCALE 启用才跑；GUI 默认不启用，避免无谓解引用导致 AV)
    B.e_mov_eax_abs(code, cfg_addr + CFG_FLAGS)
    B.emit(code, b"\xA8\x04")                               # test F_SCALE
    jcc(0x84, "scale_pass")                                  # flag clear -> 跳过遍历与阈值
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_STATUS) + b"\x01")
    B.emit(code, b"\x8B\x46\x4C")
    B.emit(code, b"\xA3" + struct.pack("<I", meta + META_MODEL))
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "model_fail")
    B.emit(code, b"\x3D\x00\x00\x01\x00")                   # cmp eax,0x10000
    jcc(0x82, "model_fail")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")                   # cmp eax,0x100000000
    jcc(0x83, "model_fail")
    B.emit(code, b"\x8B\x08")
    B.emit(code, b"\x89\x0D" + struct.pack("<I", meta + META_MODEL_VT))
    B.emit(code, b"\x81\x38" + struct.pack("<I", base + MODEL_VTABLE_RVA))
    jcc(0x85, "model_fail")
    B.emit(code, b"\x8B\x80\x9C\x14\x00\x00")
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "cmgr_fail")
    B.emit(code, b"\x3D\x00\x00\x01\x00")                   # cmp eax,0x10000
    jcc(0x82, "cmgr_fail")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")                   # cmp eax,0x100000000
    jcc(0x83, "cmgr_fail")
    B.emit(code, b"\x8B\x58\x20")
    B.emit(code, b"\x8B\x68\x24")
    B.emit(code, b"\x31\xD2")
    B.emit(code, b"\x31\xC9")
    B.emit(code, b"\xBF" + struct.pack("<I", scale_arr))
    L("loop_top")
    B.emit(code, b"\x85\xDB")                               # test ebx,ebx
    jcc(0x84, "done")
    B.emit(code, b"\x81\xFB\x00\x00\x01\x00")               # cmp ebx,0x10000
    jcc(0x82, "done")
    B.emit(code, b"\x81\xFB\xFF\xFF\xFF\xFF")               # cmp ebx,0x100000000
    jcc(0x83, "done")
    B.emit(code, b"\x39\xEB")
    jcc(0x84, "done")
    B.emit(code, b"\x83\xF9\x40")
    jcc(0x83, "done")
    B.emit(code, b"\x41")
    # ★冲突管理器节点 = entry（conf_army 实机）：node+8=entry；entry+0x1c=armyA；entry+0x5c=尾节点，[尾+8]=armyB
    B.emit(code, b"\x8B\x43\x08")                          # eax=entry
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "next")
    B.emit(code, b"\x3D\x00\x00\x01\x00")                   # cmp eax,0x10000
    jcc(0x82, "next")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")                   # cmp eax,0x100000000
    jcc(0x83, "next")
    # armyA
    B.emit(code, b"\x8B\x40\x1C")                          # eax=[entry+0x1c] armyA
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "army_b")
    B.emit(code, b"\x3D\x00\x00\x01\x00")                   # cmp eax,0x10000
    jcc(0x82, "army_b")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")                   # cmp eax,0x100000000
    jcc(0x83, "army_b")
    B.emit(code, b"\x83\xFA\x20")
    jcc(0x83, "army_b")
    B.emit(code, b"\x8B\x80\x94\x02\x00\x00")
    B.emit(code, b"\x3D" + struct.pack("<I", 0x3F800000))   # cmp eax,1.0f
    jcc(0x82, "army_b")
    B.emit(code, b"\x3D" + struct.pack("<I", 0x42C80000))   # cmp eax,100.0f
    jcc(0x87, "army_b")
    B.emit(code, b"\x89\x04\x97")
    B.emit(code, b"\x42")
    L("army_b")
    # armyB
    B.emit(code, b"\x8B\x43\x08")                          # eax=entry
    B.emit(code, b"\x8B\x40\x5C")                          # eax=[entry+0x5c] tail node
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "next")
    B.emit(code, b"\x3D\x00\x00\x01\x00")                   # cmp eax,0x10000
    jcc(0x82, "next")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")                   # cmp eax,0x100000000
    jcc(0x83, "next")
    B.emit(code, b"\x8B\x40\x08")                          # eax=[tail+8] armyB
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "next")
    B.emit(code, b"\x3D\x00\x00\x01\x00")                   # cmp eax,0x10000
    jcc(0x82, "next")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")                   # cmp eax,0x100000000
    jcc(0x83, "next")
    B.emit(code, b"\x83\xFA\x20")
    jcc(0x83, "next")
    B.emit(code, b"\x8B\x80\x94\x02\x00\x00")
    B.emit(code, b"\x3D" + struct.pack("<I", 0x3F800000))
    jcc(0x82, "next")
    B.emit(code, b"\x3D" + struct.pack("<I", 0x42C80000))
    jcc(0x87, "next")
    B.emit(code, b"\x89\x04\x97")
    B.emit(code, b"\x42")
    L("next")
    B.emit(code, b"\x8B\x5B\x04")
    jmp("loop_top")
    L("done")
    B.emit(code, b"\x89\x15" + struct.pack("<I", meta + META_COUNT))
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_STATUS) + b"\x04")
    B.emit(code, b"\x85\xD2")                                   # test edx,edx (count)
    jcc(0x85, "scale_sum")                                        # jne scale_sum
    jmp("cache_check")
    L("cmgr_fail")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_STATUS) + b"\x03")
    B.emit(code, b"\xC7\x05" + struct.pack("<I", meta + META_COUNT) + b"\x00\x00\x00\x00")
    jmp("cache_check")
    L("model_fail")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_STATUS) + b"\x02")
    B.emit(code, b"\xC7\x05" + struct.pack("<I", meta + META_COUNT) + b"\x00\x00\x00\x00")
    jmp("cache_check")
    L("cache_check")
    # ★cmgr 为空 → 读 dispatcher(FUN_106e9f60) 预存缓存：model 匹配才用，否则拦截
    B.emit(code, b"\xA1" + struct.pack("<I", cache_addr + CACHE_MODEL))
    B.emit(code, b"\x8B\x4E\x4C")                             # ecx=[pending+0x4c] model
    B.emit(code, b"\x39\xC8")                                   # cmp eax,ecx
    jcc(0x85, "cache_miss")
    B.emit(code, b"\xA1" + struct.pack("<I", cache_addr + CACHE_TOTAL))
    B.emit(code, b"\xA3" + struct.pack("<I", meta + META_TOTAL))
    B.emit(code, b"\xA1" + struct.pack("<I", cache_addr + CACHE_COUNT))
    B.emit(code, b"\xA3" + struct.pack("<I", meta + META_COUNT))
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_STATUS) + b"\x06")
    jmp("scale_ready")
    L("cache_miss")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x05")  # 规模不可读 → 拦截
    jmp("skip_write")
    L("scale_sum")
    B.emit(code, b"\xC7\x05" + struct.pack("<I", meta + META_TOTAL) + b"\x00\x00\x00\x00")
    B.emit(code, b"\xA1" + struct.pack("<I", meta + META_COUNT))
    B.emit(code, b"\x85\xC0")
    # 规模读取失败/空不会再走到这里（cache_check 已 fail-closed）
    B.emit(code, b"\x31\xC9")
    B.emit(code, b"\x31\xC0")
    B.emit(code, b"\x8B\x15" + struct.pack("<I", meta + META_COUNT))
    B.emit(code, b"\xBF" + struct.pack("<I", scale_arr))
    L("sum_loop")
    B.emit(code, b"\x39\xD1")
    jcc(0x83, "sum_done")
    B.emit(code, b"\x8B\x1C\x8F")
    B.emit(code, b"\x89\x1D" + struct.pack("<I", meta + META_TMP))
    B.emit(code, b"\xD9\x05" + struct.pack("<I", meta + META_TMP))
    B.emit(code, b"\xDB\x1D" + struct.pack("<I", meta + META_TMP))
    B.emit(code, b"\x8B\x1D" + struct.pack("<I", meta + META_TMP))
    B.emit(code, b"\x01\xD8")
    B.emit(code, b"\x41")
    jmp("sum_loop")
    L("sum_done")
    B.emit(code, b"\xA3" + struct.pack("<I", meta + META_TOTAL))
    L("scale_done")
    L("scale_ready")

    # scale threshold filter
    B.e_mov_eax_abs(code, cfg_addr + CFG_FLAGS)
    B.emit(code, b"\xA8\x04")
    jcc(0x84, "scale_pass")
    B.emit(code, b"\x0F\xB6\x8E\x58\x00\x00\x00")
    B.emit(code, b"\x83\xF9\x0B")
    jcc(0x82, "not_naval")
    B.emit(code, b"\x83\xF9\x0E")
    jcc(0x87, "not_naval")
    B.e_mov_edx_imm(code, cfg_addr)
    B.emit(code, b"\x8B\x52\x14")
    jmp("have_thr")
    L("not_naval")
    B.emit(code, b"\x83\xF9\x02")
    jcc(0x87, "not_field")
    B.e_mov_edx_imm(code, cfg_addr)
    B.emit(code, b"\x8B\x52\x18")
    jmp("have_thr")
    L("not_field")
    B.emit(code, b"\x83\xF9\x03")
    jcc(0x82, "scale_pass")
    B.emit(code, b"\x83\xF9\x0A")
    jcc(0x87, "scale_pass")
    B.e_mov_edx_imm(code, cfg_addr)
    B.emit(code, b"\x8B\x52\x1C")
    L("have_thr")
    B.emit(code, b"\x85\xD2")
    jcc(0x84, "scale_pass")
    B.emit(code, b"\xA1" + struct.pack("<I", meta + META_TOTAL))
    B.emit(code, b"\x39\xD0")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x05")  # reason=规模过滤
    jcc(0x82, "skip_write")
    L("scale_pass")

    # whitelist
    # ★2026-08-28 修正：白名单 = OR（任一“方”命中任一“链”即放行），不是 AND。
    # 且与 _factions_of 日志同源：每个 side 同时检查 [side+0x64]（持久 faction）
    # 和 [side+0xc]（战斗 faction 回退）——否则日志显示“武田/北条”但 stub 只看 +0x64 会漏判。
    B.e_mov_eax_abs(code, cfg_addr + CFG_FLAGS)
    B.emit(code, b"\xA8\x08")
    jcc(0x84, "no_wl")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_WL_MATCH) + b"\x00")
    B.emit(code, b"\x31\xC9")
    L("wl_side_loop")
    B.emit(code, b"\x83\xF9\x02")
    jcc(0x83, "wl_check")
    B.emit(code, b"\x8B\x44\x8E\x60")                # eax = side
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "wl_next")
    B.emit(code, b"\x3D\x00\x00\x01\x00")             # side 指针范围检查
    jcc(0x82, "wl_next")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")
    jcc(0x83, "wl_next")
    # 链1：side+0x64 持久 faction
    B.emit(code, b"\x8B\x40\x64")
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "wl_off_c")
    B.emit(code, b"\x3D\x00\x00\x01\x00")             # faction 指针范围检查
    jcc(0x82, "wl_off_c")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")
    jcc(0x83, "wl_off_c")
    B.emit(code, b"\x81\x38" + struct.pack("<I", base + FACTION_VTABLE_RVA))
    jcc(0x85, "wl_off_c")
    B.e_mov_edx_imm(code, wl_arr)
    B.emit(code, b"\x8B\x1D" + struct.pack("<I", cfg_addr + CFG_WL_COUNT))
    B.emit(code, b"\x31\xFF")
    L("wl_cmp_a")
    B.emit(code, b"\x39\xDF")
    jcc(0x83, "wl_off_c")
    B.emit(code, b"\x3B\x04\xBA")
    jcc(0x84, "wl_match")
    B.emit(code, b"\x47")
    jmp("wl_cmp_a")
    # 链2：side+0xc 战斗 faction 回退
    L("wl_off_c")
    B.emit(code, b"\x8B\x44\x8E\x60")                # 重新取 side
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "wl_next")
    B.emit(code, b"\x3D\x00\x00\x01\x00")             # side 指针范围检查（链2）
    jcc(0x82, "wl_next")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")
    jcc(0x83, "wl_next")
    B.emit(code, b"\x8B\x40\x0C")
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "wl_next")
    B.emit(code, b"\x3D\x00\x00\x01\x00")             # faction 指针范围检查（链2）
    jcc(0x82, "wl_next")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")
    jcc(0x83, "wl_next")
    B.emit(code, b"\x81\x38" + struct.pack("<I", base + FACTION_VTABLE_RVA))
    jcc(0x85, "wl_next")
    B.e_mov_edx_imm(code, wl_arr)
    B.emit(code, b"\x8B\x1D" + struct.pack("<I", cfg_addr + CFG_WL_COUNT))
    B.emit(code, b"\x31\xFF")
    L("wl_cmp_b")
    B.emit(code, b"\x39\xDF")
    jcc(0x83, "wl_next")
    B.emit(code, b"\x3B\x04\xBA")
    jcc(0x84, "wl_match")
    B.emit(code, b"\x47")
    jmp("wl_cmp_b")
    L("wl_match")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_WL_MATCH) + b"\x01")
    jmp("wl_check")
    L("wl_next")
    B.emit(code, b"\x41")
    jmp("wl_side_loop")
    L("wl_check")
    B.emit(code, b"\x80\x3D" + struct.pack("<I", meta + META_WL_MATCH) + b"\x00")
    jcc(0x85, "no_wl")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x06")  # reason=白名单未命中
    jmp("skip_write")
    L("no_wl")

    # blacklist
    # 同样使用 +0x64/+0xc 双链回退；黑名单语义 = 任一方命中任一链即拦截（OR）。
    B.e_mov_eax_abs(code, cfg_addr + CFG_FLAGS)
    B.emit(code, b"\xA8\x10")
    jcc(0x84, "no_bl")
    B.emit(code, b"\x31\xC9")
    L("bl_side_loop")
    B.emit(code, b"\x83\xF9\x02")
    jcc(0x83, "no_bl")
    B.emit(code, b"\x8B\x44\x8E\x60")                # eax = side
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "bl_next")
    B.emit(code, b"\x3D\x00\x00\x01\x00")             # side 指针范围检查
    jcc(0x82, "bl_next")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")
    jcc(0x83, "bl_next")
    # 链1：side+0x64 持久 faction
    B.emit(code, b"\x8B\x40\x64")
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "bl_off_c")
    B.emit(code, b"\x3D\x00\x00\x01\x00")             # faction 指针范围检查
    jcc(0x82, "bl_off_c")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")
    jcc(0x83, "bl_off_c")
    B.emit(code, b"\x81\x38" + struct.pack("<I", base + FACTION_VTABLE_RVA))
    jcc(0x85, "bl_off_c")
    B.e_mov_edx_imm(code, bl_arr)
    B.emit(code, b"\x8B\x1D" + struct.pack("<I", cfg_addr + CFG_BL_COUNT))
    B.emit(code, b"\x31\xFF")
    L("bl_cmp_a")
    B.emit(code, b"\x39\xDF")
    jcc(0x83, "bl_off_c")
    B.emit(code, b"\x3B\x04\xBA")
    jcc(0x84, "bl_hit")
    B.emit(code, b"\x47")
    jmp("bl_cmp_a")
    # 链2：side+0xc 战斗 faction 回退
    L("bl_off_c")
    B.emit(code, b"\x8B\x44\x8E\x60")                # 重新取 side
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "bl_next")
    B.emit(code, b"\x3D\x00\x00\x01\x00")             # side 指针范围检查（链2）
    jcc(0x82, "bl_next")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")
    jcc(0x83, "bl_next")
    B.emit(code, b"\x8B\x40\x0C")
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "bl_next")
    B.emit(code, b"\x3D\x00\x00\x01\x00")             # faction 指针范围检查（链2）
    jcc(0x82, "bl_next")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")
    jcc(0x83, "bl_next")
    B.emit(code, b"\x81\x38" + struct.pack("<I", base + FACTION_VTABLE_RVA))
    jcc(0x85, "bl_next")
    B.e_mov_edx_imm(code, bl_arr)
    B.emit(code, b"\x8B\x1D" + struct.pack("<I", cfg_addr + CFG_BL_COUNT))
    B.emit(code, b"\x31\xFF")
    L("bl_cmp_b")
    B.emit(code, b"\x39\xDF")
    jcc(0x83, "bl_next")
    B.emit(code, b"\x3B\x04\xBA")
    jcc(0x84, "bl_hit")
    B.emit(code, b"\x47")
    jmp("bl_cmp_b")
    L("bl_hit")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_BL_HIT) + b"\x01")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x07")  # reason=黑名单命中
    jmp("skip_write")
    L("bl_next")
    B.emit(code, b"\x41")
    jmp("bl_side_loop")
    L("no_bl")

    # PRE unit filter（★恢复 4d04b3f 已验证的 host-mediated 阻塞自旋 + sticky approval：
    # A1 入口 stub 把 pending 交给宿主并自旋；宿主在自旋窗口内读/轮询 p90/hdr，
    # 一旦同一函数调用期间 p90 就绪就写回 PASS；stub 在函数返回前、b9 读点前写 b9=1。
    # 0x604936 的 PASS 通过 sticky approval 预热后续 state-1 决策调用，因此必须保留。）
    L("pre_unit_check")
    B.e_mov_eax_abs(code, cfg_addr + CFG_FLAGS)
    B.emit(code, b"\xA8\x40")                         # test F_PRE_UNIT
    jcc(0x84, "pre_unit_pass")                          # flag clear -> pass
    B.emit(code, b"\x89\x35" + struct.pack("<I", stub_addr + DATA_PRE_UNIT_PENDING))  # pending -> shared
    B.emit(code, b"\xFF\x05" + struct.pack("<I", stub_addr + DATA_PRE_UNIT_SEQ))      # seq++
    B.emit(code, b"\xC6\x05" + struct.pack("<I", stub_addr + DATA_PRE_UNIT) + b"\xFF") # decision=pending
    # ★分级自旋（AV 残余风险处置）：0x604936(state-6 预热) 用 SPIN_FULL；state-1 决策用 SPIN_SHORT。
    # 实际等待时长由宿主 20ms 必答决定，自旋上限仅兜底宿主彻底失联。
    B.e_mov_eax_abs(code, meta + META_RET)
    B.emit(code, b"\xB9" + struct.pack("<I", SPIN_SHORT))
    B.emit(code, b"\x3D" + struct.pack("<I", base + SITE_C_RVA))
    jcc(0x85, "pre_unit_spin")                           # 非 state-6 -> 短自旋进入循环
    B.emit(code, b"\xB9" + struct.pack("<I", SPIN_FULL))
    L("pre_unit_spin")
    B.emit(code, b"\xF3\x90")                            # pause
    B.emit(code, b"\x80\x3D" + struct.pack("<I", stub_addr + DATA_PRE_UNIT) + b"\xFF")
    jcc(0x85, "pre_unit_owner")                          # != 0xFF -> 检查是否属于本 pending
    jmp("pre_unit_tick")
    L("pre_unit_owner")
    B.emit(code, b"\x39\x35" + struct.pack("<I", stub_addr + DATA_PRE_UNIT_ANSWERED_PENDING))  # cmp [answered], esi
    jcc(0x84, "pre_unit_decision")                       # 属于本 pending -> 消费判定
    L("pre_unit_tick")
    B.emit(code, b"\x49")                               # dec ecx
    jcc(0x84, "pre_unit_fail")                           # timeout -> fail closed
    jmp("pre_unit_spin")
    L("pre_unit_decision")
    B.emit(code, b"\x80\x3D" + struct.pack("<I", stub_addr + DATA_PRE_UNIT) + b"\x01")
    jcc(0x84, "pre_unit_pass")                           # decision == 1 -> pass
    L("pre_unit_fail")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x09")  # reason=PRE单位过滤
    jmp("skip_write")
    L("pre_unit_pass")

    # PRE setup filter (optional; DATA_PRE: 0=intercept, 1=pass, 0xFF=unset fail-open)
    L("pre_check")
    B.e_mov_eax_abs(code, cfg_addr + CFG_FLAGS)
    B.emit(code, b"\xA8\x20")                         # test F_PRE
    jcc(0x84, "pre_pass")
    B.emit(code, b"\x80\x3D" + struct.pack("<I", stub_addr + DATA_PRE) + b"\x01")
    jcc(0x84, "pre_pass")                               # byte==1 -> pass
    B.emit(code, b"\x80\x3D" + struct.pack("<I", stub_addr + DATA_PRE) + b"\x00")
    jcc(0x85, "pre_pass")                               # byte!=0 -> unset fail-open
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x08")
    jmp("skip_write")
    L("pre_pass")

    # ★2026-08-30 有界 defer：state-6（SITE_C_RVA）只做预热点/日志，不写 b9；
    # 写 b9 只在 state-1 决策调用（SITE_A/SITE_B），保证写与读同调用消费。
    B.e_mov_eax_abs(code, meta + META_RET)
    B.emit(code, b"\x3D" + struct.pack("<I", base + SITE_C_RVA))
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x0B")  # reason=预热未写
    jcc(0x84, "skip_write")                            # state-6 不写 b9
    # write b9
    L("write_b9")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x00")  # reason=写b9
    B.emit(code, b"\xC6\x86\xB9\x00\x00\x00\x01")       # [esi+0xb9]=1
    B.emit(code, b"\x8A\x86\xB9\x00\x00\x00")           # al = [esi+0xb9] 立即回读
    B.emit(code, b"\xA2" + struct.pack("<I", meta + META_B9))  # [meta+META_B9]=al
    # ★有界 defer：state-1 已写 b9 确认消费 → 解除 defer
    B.emit(code, b"\x80\x3D" + struct.pack("<I", stub_addr + DATA_PRE_UNIT_DEFER) + b"\x01")
    jcc(0x85, "defer_done")
    B.emit(code, b"\x39\x35" + struct.pack("<I", stub_addr + DATA_PRE_UNIT_DEFER_TARGET))
    jcc(0x85, "defer_done")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", stub_addr + DATA_PRE_UNIT_DEFER) + b"\x00")
    L("defer_done")
    L("skip_write")
    B.emit(code, b"\x61")
    B.emit(code, ORIG_BYTES)
    B.e_jmp_rel(code, base + BACK_RVA, stub_addr + len(code))

    # 回填
    for pos, op, target in jccs:
        t = labels[target]
        _patch_jcc(code, pos, t)
    for pos, target in jmps:
        t = labels[target]
        _patch_jmp(code, pos, t)

    # 确保数据区不重叠代码
    if len(code) > DATA_CFG:
        raise RuntimeError(f"stub too large: {len(code)} > DATA_CFG {DATA_CFG}")
    return bytes(code)


def _resolve_faction_addrs(h, base, names, logfn):
    """把派系名解析为持久 faction 对象指针（S18：side+0x64 链比对的正是这些对象）。
    ★引擎参数化：faction 对象 vtable 随 build（6262 0x15fac30 / 6118 主基 0x1594178），
    由当前 ENGINE_NAME 下的 FACTION_VTABLE_RVA 决定（apply_engine 已重绑）。"""
    if not names:
        return []
    try:
        import s2_watch as sw
        facs = sw.scan_factions(h, base, faction_vtable_rva=FACTION_VTABLE_RVA)
    except Exception as e:
        logfn(f"✗ 派系扫描失败，无法启用阵营过滤: {e}")
        return None
    by_name = {}
    for addr, _human, name, _tr in facs:
        if name:
            by_name.setdefault(name, []).append(addr)
    addrs = []
    missing = []
    for n in names:
        if n in by_name:
            addrs.extend(by_name[n])
        else:
            missing.append(n)
    if missing:
        logfn(f"⚠️ 阵营过滤：以下派系名未找到对象，已忽略: {missing}")
    if not addrs:
        logfn("✗ 阵营过滤：指定派系均未解析到对象，拒绝安装")
        return None
    logfn(f"✓ 阵营过滤：{len(addrs)} 个 faction 对象已解析（{names}）")
    return addrs[:MAX_FACS]

def build_setup_stub(stub_addr, base):
    """Setup chain hook stub: record setup ptr and mark DATA_PRE unset (0xFF)."""
    code = bytearray()
    pre_addr = stub_addr + DATA_PRE
    pre_setup = stub_addr + DATA_PRE_SETUP
    pre_seq = stub_addr + DATA_PRE_SEQ
    B.emit(code, b"\x60")                                     # pushad
    B.emit(code, b"\x8B\x74\x24\x18")                       # esi = orig ecx (setup)
    B.emit(code, b"\xC6\x05" + struct.pack("<I", pre_addr) + b"\xFF")  # DATA_PRE = 0xFF
    B.emit(code, b"\x89\x35" + struct.pack("<I", pre_setup))  # DATA_PRE_SETUP = setup
    B.emit(code, b"\xFF\x05" + struct.pack("<I", pre_seq))    # DATA_PRE_SEQ++
    B.emit(code, b"\x61")                                     # popad
    B.emit(code, SETUP_ORIG6)                                  # replay sub esp,0x8c
    B.e_jmp_rel(code, base + SETUP_BACK_RVA, stub_addr + len(code))
    return bytes(code)


def build_dispatcher_scale_stub(stub_addr, region, base):
    """FUN_106e9f60 入口观测/缓存 stub：此时 cmgr 未清空，遍历 army(+armyB via head58+8) 写规模缓存。
    纯记录，不改行为。"""
    cache_addr = region + DATA_CACHE
    code = bytearray()
    labels = {}
    jccs = []
    jmps = []

    def L(n):
        labels[n] = len(code)

    def jcc(op, tgt):
        pos = len(code)
        jccs.append((pos, op, tgt))
        B.emit(code, b"\x0F" + bytes([op]) + b"\x00\x00\x00\x00")

    def jmp(tgt):
        pos = len(code)
        jmps.append((pos, tgt))
        B.emit(code, b"\xE9\x00\x00\x00\x00")

    def patch_jcc(pos, op, tgt):
        t = labels[tgt]
        struct.pack_into("<i", code, pos + 2, t - (pos + 6))

    def patch_jmp(pos, tgt):
        t = labels[tgt]
        struct.pack_into("<i", code, pos + 1, t - (pos + 5))

    B.emit(code, b"\x60")                                     # pushad
    B.emit(code, b"\x8B\x44\x24\x18")                         # eax=[esp+0x18] model
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "skip")
    # ★2026-08-30 AV 加固：model 指针范围检查（旧版只查非空，状态机过渡期可能拿到失效指针）
    B.emit(code, b"\x3D\x00\x00\x01\x00")                     # cmp eax,0x10000
    jcc(0x82, "skip")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")                     # cmp eax,0x100000000
    jcc(0x83, "skip")
    B.emit(code, b"\x8B\x80\x9C\x14\x00\x00")                 # eax=[model+0x149c] cmgr
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "skip")
    B.emit(code, b"\x3D\x00\x00\x01\x00")                     # cmgr 指针范围检查
    jcc(0x82, "skip")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")
    jcc(0x83, "skip")
    B.emit(code, b"\x8B\x58\x20")                             # ebx=begin
    B.emit(code, b"\x8B\x68\x24")                             # ebp=end
    B.emit(code, b"\x31\xD2")                                 # edx=count
    B.emit(code, b"\x31\xC9")                                 # ecx=guard
    B.emit(code, b"\x31\xFF")                                 # edi=total
    L("loop")
    B.emit(code, b"\x39\xEB")
    jcc(0x84, "done")
    B.emit(code, b"\x83\xF9\x40")
    jcc(0x83, "done")
    B.emit(code, b"\x41")
    B.emit(code, b"\x85\xDB")                                 # node 指针范围检查
    jcc(0x84, "done")
    B.emit(code, b"\x81\xFB\x00\x00\x01\x00")
    jcc(0x82, "done")
    B.emit(code, b"\x81\xFB\xFF\xFF\xFF\xFF")
    jcc(0x83, "done")
    B.emit(code, b"\x8B\x43\x08")                             # eax=army
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "next")
    B.emit(code, b"\x3D\x00\x00\x01\x00")                     # army 指针范围检查
    jcc(0x82, "next")
    B.emit(code, b"\x3D\xFF\xFF\xFF\xFF")
    jcc(0x83, "next")
    # armyA scale
    B.emit(code, b"\x8B\x88\x94\x02\x00\x00")                 # ecx=[army+0x294]
    B.emit(code, b"\x81\xF9" + struct.pack("<I", 0x3F800000))
    jcc(0x82, "army_b")
    B.emit(code, b"\x81\xF9" + struct.pack("<I", 0x42C80000))
    jcc(0x87, "army_b")
    B.emit(code, b"\x89\x0D" + struct.pack("<I", region + DATA_META + META_TMP))
    B.emit(code, b"\xD9\x05" + struct.pack("<I", region + DATA_META + META_TMP))
    B.emit(code, b"\xDB\x1D" + struct.pack("<I", region + DATA_META + META_TMP))
    B.emit(code, b"\x8B\x0D" + struct.pack("<I", region + DATA_META + META_TMP))
    B.emit(code, b"\x01\xCF")
    B.emit(code, b"\x42")
    L("army_b")
    # armyB via head58+8
    B.emit(code, b"\x8B\x70\x58")                             # esi=[army+0x58] head
    B.emit(code, b"\x85\xF6")
    jcc(0x84, "next")
    B.emit(code, b"\x81\xFE\x00\x00\x01\x00")                 # head 指针范围检查
    jcc(0x82, "next")
    B.emit(code, b"\x81\xFE\xFF\xFF\xFF\xFF")
    jcc(0x83, "next")
    B.emit(code, b"\x8B\x76\x08")                             # esi=[head+8] armyB
    B.emit(code, b"\x85\xF6")
    jcc(0x84, "next")
    B.emit(code, b"\x81\xFE\x00\x00\x01\x00")                 # armyB 指针范围检查
    jcc(0x82, "next")
    B.emit(code, b"\x81\xFE\xFF\xFF\xFF\xFF")
    jcc(0x83, "next")
    B.emit(code, b"\x8B\x8E\x94\x02\x00\x00")                 # ecx=[armyB+0x294]
    B.emit(code, b"\x81\xF9" + struct.pack("<I", 0x3F800000))
    jcc(0x82, "next")
    B.emit(code, b"\x81\xF9" + struct.pack("<I", 0x42C80000))
    jcc(0x87, "next")
    B.emit(code, b"\x89\x0D" + struct.pack("<I", region + DATA_META + META_TMP))
    B.emit(code, b"\xD9\x05" + struct.pack("<I", region + DATA_META + META_TMP))
    B.emit(code, b"\xDB\x1D" + struct.pack("<I", region + DATA_META + META_TMP))
    B.emit(code, b"\x8B\x0D" + struct.pack("<I", region + DATA_META + META_TMP))
    B.emit(code, b"\x01\xCF")
    B.emit(code, b"\x42")
    L("next")
    B.emit(code, b"\x8B\x5B\x04")
    jmp("loop")
    L("done")
    B.emit(code, b"\x89\x15" + struct.pack("<I", cache_addr + CACHE_COUNT))
    B.emit(code, b"\x89\x3D" + struct.pack("<I", cache_addr + CACHE_TOTAL))
    B.emit(code, b"\x8B\x44\x24\x18")                         # model again
    B.emit(code, b"\xA3" + struct.pack("<I", cache_addr + CACHE_MODEL))
    B.emit(code, b"\xFF\x05" + struct.pack("<I", cache_addr + CACHE_SEQ))
    L("skip")
    B.emit(code, b"\x61")
    B.emit(code, DISPATCHER_ORIG_BYTES)
    B.e_jmp_rel(code, base + DISPATCHER_BACK_RVA, stub_addr + len(code))

    for pos, op, tgt in jccs:
        patch_jcc(pos, op, tgt)
    for pos, tgt in jmps:
        patch_jmp(pos, tgt)
    return bytes(code)


def build_defer_stub(stub_addr, region, base):
    """state-6 结算写点 hook：DEFER armed 时把写 state 5 改写为写 state 1，
    让下一 tick 重新进入 state-1（给本场 PASS 一次消费机会）；有界 budget，耗尽后放行原结算。

    ★引擎分流：6262 = 0x604954 call-setter 窗（下方默认路径，字节黄金不变）；
    6118 = 0xc22e15 vcall 前取值窗（新几何，见 defer_stub_param_plan_20260904.md §2）。
    6262 调用约定：hook 处 ebx=pending、edi=状态对象（pending+0x4c）；pushad 后
    [esp+0x10]=saved ebx、[esp+0x00]=saved edi。"""
    if ENGINE_NAME in OLD_FAMILY:
        return _build_defer_stub_6118(stub_addr, region, base)
    defer_addr = region + DATA_PRE_UNIT_DEFER
    target_addr = region + DATA_PRE_UNIT_DEFER_TARGET
    budget_addr = region + DATA_PRE_UNIT_DEFER_BUDGET
    code = bytearray()
    labels = {}
    jccs = []
    jmps = []

    def L(n):
        labels[n] = len(code)

    def jcc(op, tgt):
        pos = len(code)
        jccs.append((pos, op, tgt))
        B.emit(code, b"\x0F" + bytes([op]) + b"\x00\x00\x00\x00")

    def jmp(tgt):
        pos = len(code)
        jmps.append((pos, tgt))
        B.emit(code, b"\xE9\x00\x00\x00\x00")

    B.emit(code, b"\x60")                                     # pushad
    # DEFER armed?
    B.emit(code, b"\x80\x3D" + struct.pack("<I", defer_addr) + b"\x01")
    jcc(0x85, "replay")
    # 当前 pending（saved ebx）== TARGET？
    B.emit(code, b"\x8B\x44\x24\x10")                         # eax = saved ebx
    B.emit(code, b"\x3B\x05" + struct.pack("<I", target_addr))
    jcc(0x85, "replay")
    # budget > 0？
    B.emit(code, b"\x80\x3D" + struct.pack("<I", budget_addr) + b"\x00")
    jcc(0x84, "replay")
    # defer：写 state 1（保留 DEFER_SETTER_RVA 通知链）
    B.emit(code, b"\x8B\x4C\x24\x00")                         # ecx = saved edi
    B.emit(code, b"\x68\x01\x00\x00\x00")                     # push 1
    B.emit(code, b"\xE8" + struct.pack("<I", (base + DEFER_SETTER_RVA) - (stub_addr + len(code) + 5) & 0xFFFFFFFF))
    # budget--
    B.emit(code, b"\x8A\x05" + struct.pack("<I", budget_addr))  # al=[budget]
    B.emit(code, b"\xFE\xC8")                                 # dec al
    B.emit(code, b"\xA2" + struct.pack("<I", budget_addr))    # [budget]=al
    B.emit(code, b"\x84\xC0")                                 # test al,al
    jcc(0x85, "done")                                         # 还有 budget -> done
    B.emit(code, b"\xC6\x05" + struct.pack("<I", defer_addr) + b"\x00")  # DEFER=0
    jmp("done")
    L("replay")
    # 原样：push 5; mov ecx,edi; call setter
    B.emit(code, b"\x8B\x4C\x24\x00")                         # ecx = saved edi
    B.emit(code, b"\x68\x05\x00\x00\x00")                     # push 5
    B.emit(code, b"\xE8" + struct.pack("<I", (base + DEFER_SETTER_RVA) - (stub_addr + len(code) + 5) & 0xFFFFFFFF))
    L("done")
    B.emit(code, b"\x61")                                     # popad
    B.e_jmp_rel(code, base + DEFER_BACK_RVA, stub_addr + len(code))

    for pos, op, tgt in jccs:
        t = labels[tgt]
        _patch_jcc(code, pos, t)
    for pos, tgt in jmps:
        t = labels[tgt]
        _patch_jmp(code, pos, t)
    if len(code) > 0x1000:
        raise RuntimeError(f"defer stub too large: {len(code)}")
    return bytes(code)


def _build_defer_stub_6118(stub_addr, region, base):
    """6118 defer stub：hook 0xc22e15（vcall(+0x168) 前两条取值指令 mov edx,[ecx]; mov edx,[edx+8]）。

    ★几何与 6262 不同（defer_stub_param_plan_20260904.md §2.2）：
      · patch 5B 覆盖取值窗；其后的 lea/push/call edx（真正 vcall）也要重放 → replay 12B
        （8b118b5208 8d442412 50 ffd2），且**必须先于 pushad**（replay 用现场 esp/ecx/edx/eax）。
      · pending 在 edi（saved edi = [esp+0x00] after pushad）；ebx=常量5 不许碰 → scratch 只准 eax。
      · 改写 = 内联 mov [pending+0x50],1（state 1，无 state-10 通知，等价）；放行 = 跳 back_pass
        0xc22e21 执行原 mov [edi+0x50],ebx；改写后跳 back_defer 0xc22e24（merge cmp 1 vs 5 → jne
        跳过结算主体）。"""
    defer_addr = region + DATA_PRE_UNIT_DEFER
    target_addr = region + DATA_PRE_UNIT_DEFER_TARGET
    budget_addr = region + DATA_PRE_UNIT_DEFER_BUDGET
    replay = DEFER_REPLAY_BYTES or bytes.fromhex("8b118b52088d44241250ffd2")
    back_pass = base + DEFER_BACK_RVA          # 0xc22e21（原写 state5）
    back_defer = base + (DEFER_BACK_DEFER_RVA or 0xc22e24)   # merge cmp
    code = bytearray()
    labels = {}
    jccs = []
    jmps = []

    def L(n):
        labels[n] = len(code)

    def jcc(op, tgt):
        pos = len(code)
        jccs.append((pos, op, tgt))
        B.emit(code, b"\x0F" + bytes([op]) + b"\x00\x00\x00\x00")

    def jmp(tgt):
        pos = len(code)
        jmps.append((pos, tgt))
        B.emit(code, b"\xE9\x00\x00\x00\x00")

    # ① 重放窗口（esp 未变 → esp 相对寻址有效；先于 pushad）
    B.emit(code, replay)
    # ② pushad（此后 saved edi 在 [esp+0x00]，可用 scratch 但不得碰 ebx=5）
    B.emit(code, b"\x60")
    # ③ DEFER armed?
    B.emit(code, b"\x80\x3D" + struct.pack("<I", defer_addr) + b"\x01")
    jcc(0x85, "keep")
    # ④ 当前 pending（saved edi = [esp+0]）== TARGET？
    B.emit(code, b"\x8B\x44\x24\x00")                         # eax = saved edi (pending)
    B.emit(code, b"\x3B\x05" + struct.pack("<I", target_addr))
    jcc(0x85, "keep")
    # ⑤ budget > 0？
    B.emit(code, b"\x80\x3D" + struct.pack("<I", budget_addr) + b"\x00")
    jcc(0x84, "keep")
    # ⑥ defer 改写：mov dword [pending+0x50], 1（eax=pending 仍有效）
    B.emit(code, b"\xC7\x40\x50\x01\x00\x00\x00")
    # ⑦ budget--（al 路径，eax 已被改写破坏 → 重新取）
    B.emit(code, b"\x8A\x05" + struct.pack("<I", budget_addr))  # al=[budget]
    B.emit(code, b"\xFE\xC8")                                 # dec al
    B.emit(code, b"\xA2" + struct.pack("<I", budget_addr))    # [budget]=al
    B.emit(code, b"\x84\xC0")                                 # test al,al
    jcc(0x85, "defer_done")                                   # 还有 budget
    B.emit(code, b"\xC6\x05" + struct.pack("<I", defer_addr) + b"\x00")  # DEFER=0
    L("defer_done")
    # ⑧ popad
    B.emit(code, b"\x61")
    # ⑨ 改写路径：jmp merge（back_defer 0xc22e24）——原 cmp [edi+0x50],ebx 现比 1 vs 5 → jne 跳过结算
    B.e_jmp_rel(code, back_defer, stub_addr + len(code))
    # keep：popad 后跳 back_pass 0xc22e21（让原 mov [edi+0x50],ebx 执行 = 放行原结算写）
    L("keep")
    B.emit(code, b"\x61")                                     # popad
    B.e_jmp_rel(code, back_pass, stub_addr + len(code))

    for pos, op, tgt in jccs:
        t = labels[tgt]
        _patch_jcc(code, pos, t)
    for pos, tgt in jmps:
        t = labels[tgt]
        _patch_jmp(code, pos, t)
    if len(code) > 0x1000:
        raise RuntimeError(f"defer stub too large: {len(code)}")
    return bytes(code)


class SpectateCapture:
    """看海捕捉：A1 入口 hook + 写 b9 前过滤 + 观测兜底/ESC（引擎 = apply_engine 全局）"""

    def __init__(self, h, base, logfn=print, engine=None):
        self.h, self.base, self.log = h, base, logfn
        if engine is None:
            # auto-detect：GUI 只传 h/base 时按运行中引擎模块装配
            try:
                bname, _b, _p = pb.detect_build(h)
                engine, _err = resolve_engine_for_module(h, bname)
                if engine:
                    ename, _e = apply_engine(engine)
                    engine = ename
            except Exception:
                engine = None
        self.engine = engine or ENGINE_NAME          # 记录装配时引擎（诊断/卸载用）
        self.region = None
        self._obs_thread = None
        self._intercept_thread = None
        self._watchdog_thread = None
        self._exit_logged = False
        self._running = False
        self._last_mgr = None
        self._defer_hooked = False
        self._dyn_decisive = False      # 动态决战：下限 = 已捕捉最大规模×80%
        self._dyn_max_total = 0         # 已捕捉最大 PRE 总单位数（动态基准，不区分类型时用）
        self._dyn_max_field = 0         # 野战动态基准
        self._dyn_max_siege = 0         # 攻城动态基准
        self._split_type = False        # 区分野战/攻城
        self._dyn_cb = None             # 动态基准变化回调（GUI 刷新用）

    def install(self, btype_ranges=None, scale=None, factions=None, exclude=None, auto_esc=False,
                unit_off=None, prefilter_cfg=None, pre_unit_cfg=None,
                dyn_decisive=False, dyn_cb=None):
        """装 A1（0x5caa60 入口）hook。
        btype_ranges=[(min,max),...]（0-2 个范围；空=全捕捉）；
        scale=阈值 dict {naval, field, siege}（None=不筛）；
        factions 白名单（任一方命中才推）；exclude 黑名单（任一方命中即不推）；
        auto_esc=默认关（★2026-08-19 用户否决：加载后 ESC 浪费性能——现在规模/阵营已前移到写 b9 前）；
        unit_off=加载后精筛单位数偏移（None=自动：优先 0x114，读 0 回退 0x12c）；
        prefilter_cfg=可选 dict：启用 setup 链 PRE 筛选（min_total/min_per_side/min_armies/max_ratio）"""
        self.unit_off = unit_off
        if self.region:
            self.log("⚠️ 已安装，先 stop()")
            return False
        region = pb.K32.VirtualAllocEx(self.h, None, 0x4000, 0x1000 | 0x2000, 0x40)
        if not region:
            self.log(f"✗ VirtualAllocEx 失败 err={ctypes.get_last_error()}")
            return False
        stub = build_a1_stub_v2(region, self.base)
        if not _write(self.h, region, stub):
            self.log("✗ 写 stub 失败")
            return False
        d_stub = build_dispatcher_scale_stub(region + DISPATCHER_STUB_OFF, region, self.base)
        if not _write(self.h, region + DISPATCHER_STUB_OFF, d_stub):
            self.log("✗ 写 dispatcher stub 失败")
            return False
        # btype 双范围
        rs = (btype_ranges or [])[:2]
        m1, x1 = rs[0] if len(rs) > 0 else (0, 0)
        m2, x2 = rs[1] if len(rs) > 1 else (0, 0)
        flags = (F_BTYPE1 if len(rs) > 0 else 0) | (F_BTYPE2 if len(rs) > 1 else 0)
        # ★引擎门控（fail-closed，audit C5/C7）：旧引擎族（6118/6115）army+0x294 恒 nan → scale 代理不可用
        is_6118 = ENGINE_NAME in OLD_FAMILY
        _scale_req = (scale or {}).get("naval") or (scale or {}).get("field") or (scale or {}).get("siege")
        if is_6118 and _scale_req:
            self.log("✗ 6118 引擎不支持 scale 阈值过滤：army+0x294 规模代理恒 nan（C5 未定，audit §F5）")
            self.log("  → 请改用 PRE 单位数精确筛（pre_unit_cfg / GUI PRE 单位数），或 6262 引擎再开 scale")
            return False
        if is_6118 and prefilter_cfg:
            self.log("✗ 6118 引擎不支持 PRE setup 链预筛（setup+8→mid 链未在 6118 验证，fail-closed）")
            return False
        # 规模阈值
        scale = scale or {}
        naval = int(scale.get("naval") or 0)
        field = int(scale.get("field") or 0)
        siege = int(scale.get("siege") or 0)
        if naval or field or siege:
            flags |= F_SCALE
            self.log("⚠️ 规模阈值使用 S16 [+army+0x294] 规模代理，不是精确单位数"
                     "（实机 PRE=12 vs POST≈19-20）；请勿把它当单位总数验收")
        # 阵营解析
        wl_addrs = bl_addrs = []
        if factions:
            wl_addrs = _resolve_faction_addrs(self.h, self.base, factions, self.log)
            if wl_addrs is None:
                return False
            if wl_addrs:
                flags |= F_WHITELIST
        if exclude:
            bl_addrs = _resolve_faction_addrs(self.h, self.base, exclude, self.log)
            if bl_addrs is None:
                return False
            if bl_addrs:
                flags |= F_BLACKLIST
        # PRE setup 筛（可选，默认关闭）
        if prefilter_cfg:
            flags |= F_PRE
        # PRE 单位数精确筛（可选，默认关闭；hdr+0x18/+0x78 已实机验证 = POST Σ[army+0x114]）
        self._dyn_decisive = bool(dyn_decisive)
        self._dyn_cb = dyn_cb
        self._dyn_max_total = 0
        self._dyn_max_field = 0
        self._dyn_max_siege = 0
        pre_unit_cfg = dict(pre_unit_cfg or {})
        self._split_type = bool(pre_unit_cfg.get("split_type"))
        pre_min_total = int(pre_unit_cfg.get("min_total") or 0)
        pre_min_per_side = int(pre_unit_cfg.get("min_per_side") or 0)
        pre_min_total_field = int(pre_unit_cfg.get("min_total_field") or 0)
        pre_min_per_side_field = int(pre_unit_cfg.get("min_per_side_field") or 0)
        pre_min_total_siege = int(pre_unit_cfg.get("min_total_siege") or 0)
        pre_min_per_side_siege = int(pre_unit_cfg.get("min_per_side_siege") or 0)
        if self._dyn_decisive:
            pre_unit_cfg.setdefault("min_ratio", 0.5)   # 动态决战默认双方最不平衡 1:2
        if (pre_min_total or pre_min_per_side or pre_min_total_field or pre_min_per_side_field
                or pre_min_total_siege or pre_min_per_side_siege or self._dyn_decisive
                or float(pre_unit_cfg.get("min_ratio") or 0)):
            flags |= F_PRE_UNIT
        # 写 cfg
        cfg = struct.pack("<IIIIIIIIII", m1, x1, m2, x2, flags,
                          naval, field, siege, len(wl_addrs), len(bl_addrs))
        if not _write(self.h, region + DATA_CFG, cfg):
            self.log("✗ 写 cfg 失败")
            return False
        if not _write(self.h, region + DATA_CFG + CFG_PRE_MIN_TOTAL, struct.pack("<II", pre_min_total, pre_min_per_side)):
            self.log("✗ 写 PRE 单位数阈值失败")
            return False
        # 显式初始化 PRE 共享状态（不依赖 VirtualAlloc 清零）
        _write(self.h, region + DATA_PRE_UNIT_PENDING, b"\x00\x00\x00\x00")
        _write(self.h, region + DATA_PRE_UNIT, b"\xFF")
        _write(self.h, region + DATA_PRE_UNIT_SEQ, b"\x00\x00\x00\x00")
        _write(self.h, region + DATA_PRE_UNIT_ANSWERED_PENDING, b"\x00\x00\x00\x00")
        _write(self.h, region + DATA_PRE_UNIT_DEFER, b"\x00")
        _write(self.h, region + DATA_PRE_UNIT_DEFER_TARGET, b"\x00\x00\x00\x00")
        _write(self.h, region + DATA_PRE_UNIT_DEFER_BUDGET, b"\x00")
        if wl_addrs and not _write(self.h, region + DATA_WL, struct.pack("<%dI" % len(wl_addrs), *wl_addrs)):
            self.log("✗ 写白名单数组失败")
            return False
        if bl_addrs and not _write(self.h, region + DATA_BL, struct.pack("<%dI" % len(bl_addrs), *bl_addrs)):
            self.log("✗ 写黑名单数组失败")
            return False
        # PRE setup hook（可选）
        self._prefilter_cfg = prefilter_cfg
        self._pre_unit_cfg = pre_unit_cfg or None
        self._preunit_verdict = {}
        if prefilter_cfg:
            setup_stub = build_setup_stub(region + SETUP_STUB_OFF, self.base)
            if not _write(self.h, region + SETUP_STUB_OFF, setup_stub):
                self.log("✗ 写 setup stub 失败")
                return False
            s_patch = self.base + SETUP_HOOK_RVA
            s_orig = bytes(pb.read_mem(self.h, s_patch, len(SETUP_ORIG6)))
            if s_orig != SETUP_ORIG6:
                if s_orig[0] == 0xE9:
                    s_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{SETUP_HOOK_RVA:x}_orig.bin")
                    if os.path.exists(s_bak):
                        with open(s_bak, "rb") as f:
                            s_saved = f.read(len(SETUP_ORIG6))
                        if s_saved == SETUP_ORIG6:
                            _write(self.h, s_patch, s_saved)
                            s_orig = bytes(pb.read_mem(self.h, s_patch, len(SETUP_ORIG6)))
                        else:
                            self.log("✗ setup 备份不符，无法自动恢复（重启游戏后重试）")
                            return False
                    else:
                        self.log("✗ setup 无备份，无法自动恢复（重启游戏后重试）")
                        return False
                else:
                    self.log(f"✗ 0x6bba80 异常 {s_orig.hex()}，拒绝装")
                    return False
            s_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{SETUP_HOOK_RVA:x}_orig.bin")
            with open(s_bak, "wb") as f:
                f.write(s_orig)
            s_patch_bytes = b"\xE9" + struct.pack("<I", ((region + SETUP_STUB_OFF) - (s_patch + 5)) & 0xFFFFFFFF) + b"\x90"
            s_ok = _write(self.h, s_patch, s_patch_bytes)
            self.log(f"✓ setup hook 0x6bba80 → {hex(region + SETUP_STUB_OFF)} result={s_ok}")
            if not s_ok:
                return False
            if not _write(self.h, region + DATA_PRE, b"\xFF"):
                self.log("✗ 写 DATA_PRE 失败")
                return False
        # 迁移：若旧版 0x6045c4 call-site hook 仍在，先恢复（避免双 hook）
        try:
            old_patch = self.base + OLD_HOOK_RVA
            old_cur = bytes(pb.read_mem(self.h, old_patch, len(OLD_ORIG_BYTES)))
            if old_cur and old_cur[0] == 0xE9:
                old_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{OLD_HOOK_RVA:x}_orig.bin")
                if os.path.exists(old_bak):
                    with open(old_bak, "rb") as f:
                        old_saved = f.read(len(OLD_ORIG_BYTES))
                    if old_saved == OLD_ORIG_BYTES:
                        _write(self.h, old_patch, old_saved)
                        self.log("⚠️ 已迁移：恢复旧 0x6045c4 hook 后继续安装新入口 hook")
                    else:
                        self.log("⚠️ 旧 0x6045c4 备份不符，未自动恢复（请重启游戏）")
        except Exception:
            pass
        # patch FUN_106e9f60（规模缓存源）——仅当启用规模阈值才需要；
        # ★2026-08-30 AV 加固：纯白名单/PRE 单位筛不装它，缩小游戏线程内解引用面。
        d_patch = self.base + DISPATCHER_HOOK_RVA
        d_cur = pb.read_mem(self.h, d_patch, len(DISPATCHER_ORIG_BYTES))
        if flags & F_SCALE:
            d_orig = bytes(d_cur) if d_cur else None
            if d_orig != DISPATCHER_ORIG_BYTES:
                if d_orig and d_orig[0] == 0xE9:
                    d_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{DISPATCHER_HOOK_RVA:x}_orig.bin")
                    if os.path.exists(d_bak):
                        with open(d_bak, "rb") as f:
                            d_saved = f.read(len(DISPATCHER_ORIG_BYTES))
                        if d_saved == DISPATCHER_ORIG_BYTES:
                            _write(self.h, d_patch, d_saved)
                            d_orig = bytes(pb.read_mem(self.h, d_patch, len(DISPATCHER_ORIG_BYTES)))
                            self.log("⚠️ dispatcher E9 残留已自动恢复，继续安装")
                        else:
                            self.log("✗ dispatcher 备份不符，无法自动恢复（重启游戏后重试）")
                            return False
                    else:
                        self.log("✗ dispatcher 无备份，无法自动恢复（重启游戏后重试）")
                        return False
                else:
                    self.log(f"✗ 0x6e9f60 异常 {d_orig.hex() if d_orig else '?'}，拒绝装")
                    return False
            d_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{DISPATCHER_HOOK_RVA:x}_orig.bin")
            with open(d_bak, "wb") as f:
                f.write(d_orig)
            d_ok = _write(self.h, d_patch, b"\xE9" + struct.pack("<I", ((region + DISPATCHER_STUB_OFF) - (d_patch + 5)) & 0xFFFFFFFF))
            self.log(f"✓ dispatcher hook {hex(d_patch)}→{hex(region + DISPATCHER_STUB_OFF)} result={d_ok}")
        else:
            if d_cur and d_cur[0] == 0xE9:
                # 旧版遗留的 dispatcher hook 无法在本 exe 内恢复（备份可能不在 dist），
                # 但不阻断安装；彻底清理需重启游戏后不再安装旧版。
                self.log("⚠️ 检测到 dispatcher E9 残留（旧版遗留），未启用规模故不处理；如需彻底清理请重启游戏")
            else:
                self.log("ℹ️ 未启用规模阈值，跳过 dispatcher hook（降低 AV 面）")
        # patch 0x5caa60 入口
        patch_addr = self.base + HOOK_RVA
        orig = bytes(pb.read_mem(self.h, patch_addr, len(ORIG_BYTES)))
        if orig != ORIG_BYTES:
            if orig[0] == 0xE9:
                bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{HOOK_RVA:x}_orig.bin")
                if os.path.exists(bak):
                    with open(bak, "rb") as f:
                        saved = f.read(len(ORIG_BYTES))
                    if saved == ORIG_BYTES:
                        _write(self.h, patch_addr, saved)
                        orig = bytes(pb.read_mem(self.h, patch_addr, len(ORIG_BYTES)))
                        self.log("⚠️ E9 残留已自动恢复，继续安装")
                    else:
                        self.log("✗ 备份不符，无法自动恢复（重启游戏后重试）")
                        return False
                else:
                    self.log("✗ 无备份，无法自动恢复（重启游戏后重试）")
                    return False
            else:
                self.log(f"⚠️ 0x5caa60 异常 {orig.hex()}，拒绝装")
                return False
        bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{HOOK_RVA:x}_orig.bin")
        with open(bak, "wb") as f:
            f.write(orig)
        ok = _write(self.h, patch_addr, b"\xE9" + struct.pack("<I", (region - (patch_addr + 5)) & 0xFFFFFFFF))
        # ★有界 defer hook：PRE 单位数过滤一启用就必须安装（0x604954 结算写点 → state 1 重入）。
        # 2026-09-01 修复：旧条件只查 pre_min_total/pre_min_per_side，导致分类型/动态模式
        # F_PRE_UNIT 已置位但 defer 未装，state-6 PASS 无法把本场拉回 state-1，泄漏到下一场。
        self._defer_hooked = False
        if flags & F_PRE_UNIT:
            defer_stub = build_defer_stub(region + DEFER_STUB_OFF, region, self.base)
            if not _write(self.h, region + DEFER_STUB_OFF, defer_stub):
                self.log("✗ 写 defer stub 失败")
                return False
            defer_patch = self.base + DEFER_HOOK_RVA
            defer_orig = bytes(pb.read_mem(self.h, defer_patch, len(DEFER_ORIG5)))
            if defer_orig != DEFER_ORIG5:
                if defer_orig[0] == 0xE9:
                    defer_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{DEFER_HOOK_RVA:x}_orig.bin")
                    if os.path.exists(defer_bak):
                        with open(defer_bak, "rb") as f:
                            defer_saved = f.read(len(DEFER_ORIG5))
                        if defer_saved == DEFER_ORIG5:
                            _write(self.h, defer_patch, defer_saved)
                            defer_orig = bytes(pb.read_mem(self.h, defer_patch, len(DEFER_ORIG5)))
                            self.log("⚠️ defer E9 残留已自动恢复，继续安装")
                        else:
                            self.log("✗ defer 备份不符，无法自动恢复（重启游戏后重试）")
                            return False
                    else:
                        self.log("✗ defer 无备份，无法自动恢复（重启游戏后重试）")
                        return False
                else:
                    self.log(f"✗ 0x604954 异常 {defer_orig.hex()}，拒绝装")
                    return False
            defer_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{DEFER_HOOK_RVA:x}_orig.bin")
            with open(defer_bak, "wb") as f:
                f.write(defer_orig)
            defer_ok = _write(self.h, defer_patch, b"\xE9" + struct.pack("<I", ((region + DEFER_STUB_OFF) - (defer_patch + 5)) & 0xFFFFFFFF))
            self.log(f"✓ defer hook 0x604954 → {hex(region + DEFER_STUB_OFF)} result={defer_ok}")
            if not defer_ok:
                return False
            self._defer_hooked = True
        self.region = region
        self._cfg = {"btype_ranges": btype_ranges, "scale": scale,
                     "factions": factions, "exclude": exclude, "auto_esc": auto_esc}
        self._hwnd = _find_hwnd(pb.find_pid())
        names = [f"{BTYPE_NAMES.get(a,'?')}~{BTYPE_NAMES.get(b,'?')}" for a, b in rs]
        self.log(f"✓ A1({hex(HOOK_RVA)} 入口，引擎 {ENGINE_NAME}) 已装 {hex(patch_addr)}→{hex(region)} "
                 f"类型={names if names else '全捕捉'} "
                 f"规模(海>={naval}/野>={field}/攻>={siege}) "
                 f"白名单={factions} 黑名单={exclude} "
                 f"加载后ESC={'开' if auto_esc else '关'}")
        return ok

    def uninstall(self):
        """恢复 0x5caa60 原字节"""
        # 旧 hook 残留清理（新安装时通常已迁移，这里兜底）
        try:
            old_patch = self.base + OLD_HOOK_RVA
            old_cur = bytes(pb.read_mem(self.h, old_patch, len(OLD_ORIG_BYTES)))
            if old_cur and old_cur[0] == 0xE9:
                old_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{OLD_HOOK_RVA:x}_orig.bin")
                if os.path.exists(old_bak):
                    with open(old_bak, "rb") as f:
                        old_saved = f.read(len(OLD_ORIG_BYTES))
                    if old_saved == OLD_ORIG_BYTES:
                        _write(self.h, old_patch, old_saved)
                        self.log("✓ 旧 0x6045c4 hook 已清理")
        except Exception:
            pass
        bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{HOOK_RVA:x}_orig.bin")
        if os.path.exists(bak):
            with open(bak, "rb") as f:
                orig = f.read(len(ORIG_BYTES))
            if orig == ORIG_BYTES:
                _write(self.h, self.base + HOOK_RVA, orig)
                self.log("✓ A1 已卸载（0x5caa60 恢复）")
        d_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{DISPATCHER_HOOK_RVA:x}_orig.bin")
        if os.path.exists(d_bak):
            with open(d_bak, "rb") as f:
                d_orig = f.read(len(DISPATCHER_ORIG_BYTES))
            if d_orig == DISPATCHER_ORIG_BYTES:
                _write(self.h, self.base + DISPATCHER_HOOK_RVA, d_orig)
                self.log("✓ dispatcher hook 已卸载（0x6e9f60 恢复）")
        s_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{SETUP_HOOK_RVA:x}_orig.bin")
        if os.path.exists(s_bak):
            with open(s_bak, "rb") as f:
                s_orig = f.read(len(SETUP_ORIG6))
            if s_orig == SETUP_ORIG6:
                _write(self.h, self.base + SETUP_HOOK_RVA, s_orig)
                self.log("✓ setup hook 已卸载（0x6bba80 恢复）")
        defer_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"re_a1_{DEFER_HOOK_RVA:x}_orig.bin")
        if os.path.exists(defer_bak) and getattr(self, "_defer_hooked", False):
            with open(defer_bak, "rb") as f:
                defer_orig = f.read(len(DEFER_ORIG5))
            if defer_orig == DEFER_ORIG5:
                _write(self.h, self.base + DEFER_HOOK_RVA, defer_orig)
                self.log("✓ defer hook 已卸载（0x604954 恢复）")
        self._defer_hooked = False
        self.region = None

    def start_observe(self):
        """后台观测线程：battle_mgr 变化 → 单位数/阵营 → 兜底筛选 → ESC"""
        if self._running:
            return
        self._running = True
        self._exit_logged = False
        if BATTLE_MGR_SLOT_RVA:
            self._last_mgr = _rd32(self.h, self.base + BATTLE_MGR_SLOT_RVA)
        else:
            # ★槽位未锁定的 build（如 6115 静态未定位）：观测兜底停用，不崩（决策主链走 pending 侧，不依赖 mgr）
            self._last_mgr = None
            self.log("⚠️ 本 build 未锁定 battle_mgr 槽 → 战斗观测兜底停用（决策/PRE 主链正常；"
                     "需要完整观测请在真实战斗中做槽位动态锁定）")
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()
        self._obs_thread = threading.Thread(target=self._observe_loop, daemon=True)
        self._obs_thread.start()
        self._intercept_thread = threading.Thread(target=self._intercept_log_loop, daemon=True)
        self._intercept_thread.start()
        if getattr(self, "_prefilter_cfg", None):
            self._prefilter_thread = threading.Thread(target=self._prefilter_loop, daemon=True)
            self._prefilter_thread.start()
            self.log("▶ PRE setup 预筛线程已启动")
        # ★PRE 单位数必须启动宿主线程应答 stub 自旋（host-mediated 阻塞判定）
        if getattr(self, "_pre_unit_cfg", None):
            self._preunit_thread = threading.Thread(target=self._preunit_loop, daemon=True)
            self._preunit_thread.start()
            self.log("▶ PRE 单位数预筛线程已启动（host-mediated 阻塞判定）")
        self.log("▶ 观测中（battle_mgr 变化 → 规模/阵营兜底判定）…")

    def stop(self):
        self._running = False

    def _game_alive(self):
        """通过 GetExitCodeProcess 判断游戏进程是否存活（handle 需 PROCESS_QUERY_INFORMATION）。"""
        code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(self.h, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE

    def _watchdog_loop(self):
        """后台看门狗：游戏进程退出时明确告警并停止 spectate 线程，避免 #0 哨兵误读。"""
        while self._running and not self._exit_logged:
            time.sleep(0.5)
            if not self._game_alive():
                self._exit_logged = True
                code = ctypes.c_ulong()
                if ctypes.windll.kernel32.GetExitCodeProcess(self.h, ctypes.byref(code)):
                    self.log(f"✗ 游戏进程已退出 (exit code={code.value})，spectate 已停止，请重新连接")
                else:
                    self.log("✗ 游戏进程已退出（无法读取 exit code），spectate 已停止，请重新连接")
                self._running = False
                break

    # ---------- 观测 ----------
    def _factions_of(self, pending):
        """攻守阵营名（双链回退：+0x64 持久 / +0xc 战斗，UTF-16 合法才取）"""
        fv = self.base + FACTION_VTABLE_RVA
        names = []
        for off in (0x60, 0x64):
            side = _rd32(self.h, pending + off)
            n = None
            if side and 0x10000 < side < 0x100000000:
                for foff in (0x64, 0xc):
                    f = _rd32(self.h, side + foff)
                    if f and 0x10000 < f < 0x100000000 and _rd32(self.h, f) == fv:
                        nm = _read_utf16(self.h, _rd32(self.h, f + FACTION_NAME_OFF))
                        if nm:
                            n = nm
                            break
            names.append(n)
        return names

    def _religions_of(self, pending):
        """攻守阵营的 state religion 显示名（与 _factions_of 同链：+0x64 持久 / +0xc 战斗）。"""
        fv = self.base + FACTION_VTABLE_RVA
        rels = []
        for off in (0x60, 0x64):
            side = _rd32(self.h, pending + off)
            r = None
            if side and 0x10000 < side < 0x100000000:
                for foff in (0x64, 0xc):
                    f = _rd32(self.h, side + foff)
                    if f and 0x10000 < f < 0x100000000 and _rd32(self.h, f) == fv:
                        d = faction_religion_display(self.h, f)
                        if d:
                            r = d
                            break
            rels.append(r)
        return rels


    def _army_faction(self, army):
        """army persistent faction = getter(&army+0x25c) = **(army+0x260)."""
        if not army:
            return None
        q = _rd32(self.h, army + 0x260)
        if not q:
            return None
        return _rd32(self.h, q)

    def _pre_units(self, pending):
        """PRE unit count candidates: p90->hdr, side counts = hdr+0x18 / hdr+0x78."""
        if not pending:
            return None, None, None
        p90 = _rd32(self.h, pending + 0x90)
        if not p90:
            return None, None, None
        hdr = _rd32(self.h, p90 + 0x1c)
        if not hdr:
            return p90, None, None
        return p90, _rd32(self.h, hdr + 0x18), _rd32(self.h, hdr + 0x78)

    def _prefilter_loop(self):
        """Host-mediated PRE setup filter: watch setup stub, compute pass/fail, write DATA_PRE."""
        import datetime
        if getattr(sys, "frozen", False):
            log_dir = os.path.dirname(sys.executable)
        else:
            log_dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(log_dir, "spectate_prefilter.log")
        last = 0
        while self._running:
            try:
                if not self.region:
                    time.sleep(0.05)
                    continue
                seq = _rd32(self.h, self.region + DATA_PRE_SEQ) or 0
                if seq != last:
                    last = seq
                    setup = _rd32(self.h, self.region + DATA_PRE_SETUP)
                    if not setup:
                        continue
                    entries = []
                    mid = _rd32(self.h, setup + 8)
                    battle = _rd32(self.h, mid + 0x14e0) if mid else None
                    table = _rd32(self.h, battle + 0x140) if battle else None
                    count = _rd32(self.h, table + 4) if table else None
                    data = _rd32(self.h, table + 8) if table else None
                    if table and count and data:
                        for i in range(min(count, 32)):
                            e = data + i * 0x38
                            uc = _rd32(self.h, e + 4)
                            ap = _rd32(self.h, e + 8)
                            if uc is not None:
                                entries.append((uc, ap))
                    pending = _rd32(self.h, mid + 0x14a4) if mid else None
                    side_factions = {"sideA": None, "sideD": None}
                    if pending:
                        for k, off in (("sideA", 0x60), ("sideD", 0x64)):
                            side = _rd32(self.h, pending + off)
                            if side:
                                side_factions[k] = _rd32(self.h, side + 0x64)
                    from _prefilter_logic import evaluate_prefilter
                    r = evaluate_prefilter(entries, side_factions, self._prefilter_cfg, self._army_faction)
                    val = 1 if r["pass"] else 0
                    _write(self.h, self.region + DATA_PRE, bytes([val]))
                    line = (f"[prefilter #{seq}] total={r['total']} per_side={r['per_side']} "
                            f"armies={r['army_count']} ratio={r['ratio']} -> "
                            f"{'PASS' if r['pass'] else 'FAIL'} ({r['reason']})")
                    self.log(line)
                    try:
                        with open(log_path, "a", encoding="utf-8") as f:
                            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.01)

    def _units_of(self, mgr):
        """单位数：st 链组内求和 + issp。★偏移自动探测（2026-08-19）：0x114 读 0 → 回退 0x12c（FotS）"""
        env = _rd32(self.h, mgr + 0x110)
        if not env:
            return None, None
        issp = None
        try:
            issp = pb.read_u8(self.h, env + 0x281e8)
        except Exception:
            pass
        e8 = _rd32(self.h, env + 8)
        st = _rd32(self.h, e8 + 0xb4) if e8 else None
        if not st:
            return None, issp
        gcnt = _rd32(self.h, st + ST_GRP_CNT)
        gtbl = _rd32(self.h, st + ST_GRP_TBL)
        offs = [self.unit_off] if self.unit_off else [ARMY_UNIT_CNT_VANILLA, ARMY_UNIT_CNT_FOTS]
        totals = []
        for gi in range(min(gcnt or 0, 8)):
            g = _rd32(self.h, gtbl + gi * 4) if gtbl else None
            if not g or not (0x10000 < g < 0x100000000):
                continue
            acnt = _rd32(self.h, g + GRP_ARMY_CNT)
            atbl = _rd32(self.h, g + GRP_ARMY_TBL)
            gtot = 0
            for ai in range(min(acnt or 0, 16)):
                a = _rd32(self.h, atbl + ai * 4) if atbl else None
                if not a or not (0x10000 < a < 0x100000000):
                    continue
                for off in offs:
                    u = _rd32(self.h, a + off)
                    if u and 0 < u < 500:
                        gtot += u
                        break
            totals.append(gtot)
        return totals, issp

    def _army_unit_count(self, a):
        """单军单位数：原版 [army+0x114]，读 0 回退 [army+0x12c]（FotS）"""
        offs = [self.unit_off] if self.unit_off else [ARMY_UNIT_CNT_VANILLA, ARMY_UNIT_CNT_FOTS]
        for off in offs:
            u = _rd32(self.h, a + off)
            if u and 0 < u < 500:
                return u
        return None

    def _army_deployed_summary(self, a):
        """纯观测：抽样 army 单位表 [army+0x118] 前 8 个单位，读 [unit+0x29c] 部署标志。
        返回 'deployed/read'；读不到/表异常返回可读提示。"""
        uc = self._army_unit_count(a) or 0
        if uc <= 0:
            return "n/a"
        tbl = _rd32(self.h, a + 0x118)
        if not tbl or not (0x10000 < tbl < 0x100000000):
            return "tbl?"
        sample = min(uc, 8)
        deployed = 0
        read = 0
        for i in range(sample):
            u = _rd32(self.h, tbl + i * 4)
            if u and 0x10000 < u < 0x100000000:
                read += 1
                try:
                    if pb.read_u32(self.h, u + 0x29c):
                        deployed += 1
                except Exception:
                    pass
        return f"{deployed}/{read}" if read else "?"

    def _battle_armies_dump(self, mgr):
        """纯观测诊断：新战斗加载后按 st 组/军队输出 faction、单位数、部署抽样。"""
        env = _rd32(self.h, mgr + 0x110)
        if not env:
            return None
        e8 = _rd32(self.h, env + 8)
        st = _rd32(self.h, e8 + 0xb4) if e8 else None
        if not st:
            return None
        gcnt = _rd32(self.h, st + ST_GRP_CNT) or 0
        gtbl = _rd32(self.h, st + ST_GRP_TBL)
        st_sw = _rd32(self.h, st + ST_SWITCHED)
        lines = [f"st={hex(st)} st_switched={st_sw}"]
        for gi in range(min(gcnt or 0, 8)):
            g = _rd32(self.h, gtbl + gi * 4) if gtbl else None
            if not g or not (0x10000 < g < 0x100000000):
                continue
            acnt = _rd32(self.h, g + GRP_ARMY_CNT) or 0
            atbl = _rd32(self.h, g + GRP_ARMY_TBL)
            army_lines = []
            for ai in range(min(acnt or 0, 16)):
                a = _rd32(self.h, atbl + ai * 4) if atbl else None
                if not a or not (0x10000 < a < 0x100000000):
                    continue
                u = self._army_unit_count(a)
                f = self._army_faction(a)
                nm = None
                if f:
                    nm = _read_utf16(self.h, _rd32(self.h, f + FACTION_NAME_OFF))
                rel = faction_religion_display(self.h, f) if f else None
                dep = self._army_deployed_summary(a)
                a270 = _rd32(self.h, a + ARMY_270)
                a28c = _rd32(self.h, a + ARMY_28C)
                a290 = _rd32(self.h, a + ARMY_290)
                a294 = _rd32(self.h, a + ARMY_294)
                utbl = _rd32(self.h, a + ARMY_UNIT_TBL)
                u0 = _rd32(self.h, utbl) if utbl else None
                u0ea8 = _rd32(self.h, u0 + UNIT_EA8) if u0 else None
                army_lines.append(
                    f"army{ai}={hex(a)} faction={nm}({rel or '?'}) units={u} deployed={dep} "
                    f"a270={a270} a28c={a28c} a290={a290} a294={a294} u0ea8={u0ea8}")
            lines.append(f"group{gi}={hex(g)} count={acnt} [{', '.join(army_lines)}]")
        return lines


    def _preunit_loop(self):
        """Host-mediated PRE unit filter: stub waits, host safely reads p90/hdr and writes decision.
        None = 数据未就绪：本事件仍 fail-closed（不误放行），但日志标为 PENDING 而不是 FAIL；
        同一 (pending, side0, side1, btype) 一旦有真实判定，后续数据未就绪的 None 事件沿用该判定；
        具体数据在场时一律按当前数据重新判定（防 pending 指针复用污染，2026-08-30）；
        PASS 后 arm 有界 DEFER（0x604954 把结算改写为 state-1），给本场下一次 state-1 消费机会。"""
        last_seq = 0
        try:
            # ★宿主线程提权：ABOVE_NORMAL，缩短自旋窗口内的应答延迟
            ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), 1)
        except Exception:
            pass
        while self._running:
            try:
                seq = _rd32(self.h, self.region + DATA_PRE_UNIT_SEQ) or 0
                if seq != last_seq:
                    if seq == 0 and last_seq != 0 and not self._game_alive():
                        self.log("✗ 游戏进程不可读（已退出），PRE 单位线程停止")
                        self._running = False
                        break
                    last_seq = seq
                    pending = _rd32(self.h, self.region + DATA_PRE_UNIT_PENDING)
                    # ★host-mediated 快速轮询：stub 在自旋窗口内，宿主最多等 PRE_POLL_MAX（20ms）；
                    # p90 一旦在同一次 A1 调用期间就绪就立刻算 PASS/FAIL。
                    _, pre_a, pre_b = self._pre_units(pending)
                    if pre_a is None or pre_b is None:
                        poll_deadline = time.time() + PRE_POLL_MAX
                        while time.time() < poll_deadline:
                            _, pre_a, pre_b = self._pre_units(pending)
                            if pre_a is not None and pre_b is not None and 0 <= pre_a < 500 and 0 <= pre_b < 500:
                                break
                            time.sleep(0.001)
                    cfg = self._pre_unit_cfg or {}
                    min_ratio = float(cfg.get("min_ratio") or 0)
                    split = getattr(self, "_split_type", False)
                    btype_now = _rd32(self.h, pending + 0x58) if pending else None
                    cat = btype_category(btype_now)
                    # ★分类型：野战/攻城分别取手动阈值与动态基准；海战/未知不套用规模阈值
                    if split and cat == "field":
                        min_total = int(cfg.get("min_total_field") or 0)
                        min_per_side = int(cfg.get("min_per_side_field") or 0)
                        dyn_base = self._dyn_max_field
                    elif split and cat == "siege":
                        min_total = int(cfg.get("min_total_siege") or 0)
                        min_per_side = int(cfg.get("min_per_side_siege") or 0)
                        dyn_base = self._dyn_max_siege
                    elif split:
                        min_total = 0
                        min_per_side = 0
                        dyn_base = 0
                    else:
                        min_total = int(cfg.get("min_total") or 0)
                        min_per_side = int(cfg.get("min_per_side") or 0)
                        dyn_base = self._dyn_max_total
                    # ★动态决战：下限 = 该类型已捕捉最大 PRE 总单位数 × 80%
                    if self._dyn_decisive:
                        min_total = max(min_total, int(dyn_base * 0.8))
                    verdicts = self._preunit_verdict
                    now = time.time()
                    expired = [k for k, v in verdicts.items() if now - v[1] > 10]
                    for k in expired:
                        del verdicts[k]
                    # ★2026-08-30 实机 #11 暴露：pending 指针会在 battle_mgr 变化前被复用
                    # （同一 pending=0x48716f48 出现 btype 0→11→6），sticky 若只看 pending
                    # 会把 #3 的 20/14 PASS 污染到 #11 的 8/1，导致阈值 30/10 仍写 b9。
                    # 修复：sticky 键 = (pending, side0, side1, btype)（战斗身份）；
                    # 且仅当当前 PRE 数据未就绪时继承，具体数据在场时一律按当前数据重新判定。
                    btype = _rd32(self.h, pending + 0x58) if pending else None
                    side0 = _rd32(self.h, pending + 0x60) if pending else None
                    side1 = _rd32(self.h, pending + 0x64) if pending else None
                    pstate = _rd32(self.h, pending + 0x50) if pending else None
                    ret = _rd32(self.h, self.region + DATA_META + META_RET)
                    ret_rva = (ret - self.base) if ret and ret >= self.base else None
                    vkey = (pending, side0, side1, btype)
                    sticky_ok = vkey in verdicts and verdicts[vkey][0]
                    data_ok = pre_a is not None and pre_b is not None and 0 <= pre_a < 500 and 0 <= pre_b < 500
                    if data_ok:
                        ok = True
                        if min_per_side and (pre_a < min_per_side or pre_b < min_per_side):
                            ok = False
                        if min_total and (pre_a + pre_b) < min_total:
                            ok = False
                        if min_ratio:
                            small, big = sorted((pre_a, pre_b))
                            if big <= 0 or small <= 0 or (small / big) < min_ratio:
                                ok = False
                        if ok:
                            # 记录候选规模；state-6 预热点不立即更新动态基准，
                            # 等 state-1 真正写 b9 时再计入“已捕捉最大规模”。
                            verdicts[vkey] = (True, now, pre_a + pre_b, cat)
                    else:
                        ok = sticky_ok
                    # ★动态决战：只在 state-1 决策调用（会写 b9）PASS 时更新基准；
                    # state-6 只是预热点，可能被后续 155c/数据未就绪拦截，不能把未加载的战斗计入最大规模。
                    if self._dyn_decisive and ok and ret_rva in (SITE_A_RVA, SITE_B_RVA):
                        if data_ok:
                            dyn_total = pre_a + pre_b
                            dyn_cat = cat
                        else:
                            _v = verdicts.get(vkey)
                            dyn_total = _v[2] if _v and _v[0] else None
                            dyn_cat = _v[3] if _v and _v[0] else cat
                        if dyn_total is not None:
                            if split and dyn_cat == "field":
                                new_max = max(self._dyn_max_field, dyn_total)
                                if new_max != self._dyn_max_field:
                                    self._dyn_max_field = new_max
                                    if self._dyn_cb:
                                        try:
                                            self._dyn_cb({"field": new_max, "siege": self._dyn_max_siege})
                                        except Exception:
                                            pass
                            elif split and dyn_cat == "siege":
                                new_max = max(self._dyn_max_siege, dyn_total)
                                if new_max != self._dyn_max_siege:
                                    self._dyn_max_siege = new_max
                                    if self._dyn_cb:
                                        try:
                                            self._dyn_cb({"field": self._dyn_max_field, "siege": new_max})
                                        except Exception:
                                            pass
                            else:
                                new_max = max(self._dyn_max_total, dyn_total)
                                if new_max != self._dyn_max_total:
                                    self._dyn_max_total = new_max
                                    if self._dyn_cb:
                                        try:
                                            self._dyn_cb(new_max, int(new_max * 0.8))
                                        except Exception:
                                            pass
                    # ★无条件写回：先标 answered_pending（本判定归属），再给判定。
                    # 保证 stub 在自旋窗口内一定拿到判定；按 pending 隔离，避免跨 pending 误用。
                    _write(self.h, self.region + DATA_PRE_UNIT_ANSWERED_PENDING, struct.pack("<I", pending or 0))
                    # ★有界 defer：PASS 后立即 arm（预算 3 次），由 0x604954 hook 把结算写成 state-1，
                    # 给本场下一次 state-1 消费 b9 的机会；stub 写 b9 确认后 disarm。
                    if ok:
                        _write(self.h, self.region + DATA_PRE_UNIT_DEFER, b"\x01")
                        _write(self.h, self.region + DATA_PRE_UNIT_DEFER_TARGET, struct.pack("<I", pending or 0))
                        _write(self.h, self.region + DATA_PRE_UNIT_DEFER_BUDGET, bytes([DEFER_BUDGET_MAX]))
                    _write(self.h, self.region + DATA_PRE_UNIT, bytes([1 if ok else 0]))
                    if ok:
                        state = "PASS"
                    elif pre_a is None or pre_b is None:
                        state = "PENDING(None未就绪)"
                    else:
                        state = "FAIL"
                    self.log(f"[PRE单位 #{seq}] pending={hex(pending) if pending else '?'} "
                             f"ret={hex(ret_rva) if ret_rva is not None else '?'} state={pstate} "
                             f"side={hex(side0) if side0 else '?'}/{hex(side1) if side1 else '?'} "
                             f"btype={btype} PRE={pre_a}/{pre_b} min_total={min_total} "
                             + (f"dynF={self._dyn_max_field}/dynS={self._dyn_max_siege}"
                                if self._dyn_decisive and split else
                                f"dyn={self._dyn_max_total}" if self._dyn_decisive else "") + " -> " + str(state))
            except Exception as e:
                self.log(f"✗ PRE单位线程异常 {e}")
            time.sleep(0.0002)

    def _intercept_log_loop(self):
        """轻量轮询 stub meta，输出每次 A1 拦截的简单日志（GUI log + 文件）。"""
        import datetime
        if getattr(sys, "frozen", False):
            log_dir = os.path.dirname(sys.executable)
        else:
            log_dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(log_dir, "spectate_intercept.log")
        last = 0
        while self._running:
            try:
                if not self.region:
                    time.sleep(0.2)
                    continue
                ev = _rd32(self.h, self.region + DATA_META + META_EVENT) or 0
                if ev != last:
                    if ev == 0 and last != 0 and not self._game_alive():
                        self.log("✗ 游戏进程不可读（已退出），拦截日志线程停止")
                        self._running = False
                        break
                    last = ev
                    reason = 0xff
                    try:
                        reason = pb.read_u8(self.h, self.region + DATA_META + META_REASON)
                    except Exception:
                        pass
                    pending = _rd32(self.h, self.region + DATA_META + META_PENDING)
                    btype = _rd32(self.h, pending + 0x58) if pending else None
                    total = _rd32(self.h, self.region + DATA_META + META_TOTAL) or 0
                    count = _rd32(self.h, self.region + DATA_META + META_COUNT) or 0
                    names = self._factions_of(pending) if pending else [None, None]
                    rels = self._religions_of(pending) if pending else [None, None]
                    # ★155c 诊断：区分“指针链不可读”和“真的 155c!=0”（双人类/人类战斗模式）
                    p30 = _rd32(self.h, pending + 0x30) if pending else None
                    p8 = _rd32(self.h, p30 + 8) if p30 and 0x10000 < p30 < 0x100000000 else None
                    flag155c = None
                    if p8 and 0x10000 < p8 < 0x100000000:
                        try:
                            flag155c = pb.read_u8(self.h, p8 + 0x155c)
                        except Exception:
                            pass
                    p7c = _rd32(self.h, pending + 0x7c) if pending else None
                    _, pre_a, pre_b = self._pre_units(pending) if pending else (None, None, None)
                    b9v = None
                    b9s = None
                    ret = _rd32(self.h, self.region + DATA_META + META_RET)
                    ret_rva = (ret - self.base) if ret and ret >= self.base else None
                    if reason == 0 and pending:
                        try:
                            b9v = pb.read_u8(self.h, pending + 0xb9)
                        except Exception:
                            pass
                    if reason == 0:
                        try:
                            b9s = pb.read_u8(self.h, self.region + DATA_META + META_B9)
                        except Exception:
                            pass
                    line = (f"[拦截 #{ev}] pending={hex(pending) if pending else '?'} "
                            f"ret={hex(ret_rva) if ret_rva is not None else '?'} "
                            f"btype={btype}({BTYPE_NAMES.get(btype, '?')}) "
                            f"规模={total}(军{count}) 阵营="
                            f"{names[0] or '?'}({rels[0] or '?'})/{names[1] or '?'}({rels[1] or '?'}) "
                            f"PRE单位={'未就绪' if pre_a is None or pre_b is None else f'{pre_a}/{pre_b}'} "
                            f"b9stub={b9s if b9s is not None else '?'} b9回读={b9v if b9v is not None else '?'} "
                            f"→ {REASON_NAMES.get(reason, reason)}")
                    if reason == 3:
                        line += (f" 155c链={hex(p30) if p30 else '?'}/{hex(p8) if p8 else '?'} "
                                 f"155c={flag155c if flag155c is not None else '?'} "
                                 f"7c={hex(p7c) if p7c is not None else '?'}")
                    self.log(line)
                    try:
                        with open(log_path, "a", encoding="utf-8") as f:
                            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.2)


    def _observe_loop(self):
        import datetime
        if getattr(sys, "frozen", False):
            log_dir = os.path.dirname(sys.executable)
        else:
            log_dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(log_dir, "spectate_battle.log")
        model = None
        while self._running:
            try:
                if model is None and ENGINE_NAME == "6262":
                    # ★6262 专属锚定：扫 model vtable（6118 用 vt_model 0x15ac334，本循环不依赖它——
                    # pending 侧信息在 6118 由 battle mgr 链 + stub meta 提供；避免全堆扫描卡顿）
                    from re_h46a import anchor
                    model, _ = anchor(self.h, self.base)
                mgr = _rd32(self.h, self.base + BATTLE_MGR_SLOT_RVA) if BATTLE_MGR_SLOT_RVA else None
                if mgr and mgr != self._last_mgr:
                    self._last_mgr = mgr
                    if getattr(self, "_preunit_verdict", None):
                        self._preunit_verdict.clear()
                    if self.region:
                        _write(self.h, self.region + DATA_PRE_UNIT_PENDING, b"\x00\x00\x00\x00")
                        _write(self.h, self.region + DATA_PRE_UNIT, b"\xFF")
                        _write(self.h, self.region + DATA_PRE_UNIT_ANSWERED_PENDING, b"\x00\x00\x00\x00")
                        _write(self.h, self.region + DATA_PRE_UNIT_DEFER, b"\x00")
                        _write(self.h, self.region + DATA_PRE_UNIT_DEFER_TARGET, b"\x00\x00\x00\x00")
                        _write(self.h, self.region + DATA_PRE_UNIT_DEFER_BUDGET, b"\x00")
                    btype = None
                    p = _rd32(self.h, model + 0x14a4) if model else None
                    if p and 0x10000 < p < 0x100000000:
                        btype = _rd32(self.h, p + 0x58)
                    names = self._factions_of(p) if p else [None, None]
                    rels = self._religions_of(p) if p else [None, None]
                    _, pre_a, pre_b = self._pre_units(p) if p else (None, None, None)
                    totals, issp = self._units_of(mgr)
                    # ★纯观测诊断：输出每军 faction/单位数/部署抽样 + AI 字段，不改任何行为
                    try:
                        for _dline in (self._battle_armies_dump(mgr) or []):
                            _line = "  ⚙ " + _dline
                            self.log(_line)
                            try:
                                with open(log_path, "a", encoding="utf-8") as f:
                                    f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {_line}\n")
                            except Exception:
                                pass
                    except Exception:
                        pass
                    cfg = self._cfg
                    skip = False
                    reason = ""
                    if cfg.get("factions"):
                        if not any(n in cfg["factions"] for n in names if n):
                            skip, reason = True, f"阵营拦截 {names}"
                    if cfg.get("exclude"):
                        if any(n in cfg["exclude"] for n in names if n):
                            skip, reason = True, f"黑名单 {names}"
                    if not skip and totals and len(totals) >= 2:
                        total = sum(totals)
                        thr = None
                        if cfg.get("scale"):
                            if btype is not None:
                                if 11 <= btype <= 14:
                                    thr = cfg["scale"].get("naval")
                                elif 0 <= btype <= 2:
                                    thr = cfg["scale"].get("field")
                                elif 3 <= btype <= 10:
                                    thr = cfg["scale"].get("siege")
                        if thr:
                            if total < thr:
                                skip, reason = True, f"规模 {totals}(sum {total}) < {thr}({['naval','field','siege'][0 if 11<=btype<=14 else 1 if 0<=btype<=2 else 2]})"
                    _battle_line = (f"★新战斗 mgr={hex(mgr)} btype={btype}({BTYPE_NAMES.get(btype, '?')}) "
                                    f"阵营={names[0] or '?'}({rels[0] or '?'})/{names[1] or '?'}({rels[1] or '?'}) "
                                    f"PRE单位={pre_a}/{pre_b} 单位数={totals} issp={issp} "
                                    f"{'→ ❌兜底跳过 ' + reason if skip else '→ ✅保留'}")
                    self.log(_battle_line)
                    try:
                        with open(log_path, "a", encoding="utf-8") as f:
                            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {_battle_line}\n")
                    except Exception:
                        pass
                    if skip and cfg.get("auto_esc") and self._hwnd:
                        if issp == 0:
                            self.log("  ⛔ issp=0（本地参战）→ 不 ESC（投降保护），等自然结束")
                        else:
                            time.sleep(2.0)
                            ok = _send_esc(self._hwnd)
                            self.log(f"  ⛔ 自动 ESC {'✅' if ok else '✗'}")
            except Exception:
                pass
            time.sleep(0.5)


def _safe_print(*args):
    """CLI 日志兜底：GBK/非 UTF-8 控制台下不因 ✓/ℹ️/emoji 崩（errors=replace）。"""
    try:
        print(*args)
    except UnicodeEncodeError:
        try:
            sys.stdout.buffer.write((" ".join(str(a) for a in args) + "\n").encode("utf-8", "replace"))
        except Exception:
            pass


def _cli():
    args = sys.argv[1:]
    if "--restore" in args:
        h, base, eng = _open()
        sc = SpectateCapture(h, base, engine=eng, logfn=_safe_print)
        sc.uninstall()
        return
    h, base, eng = _open()
    btype_ranges = None
    if "--type" in args:   # 多类型：--type siege,naval
        btype_ranges = []
        for t in args[args.index("--type") + 1].split(","):
            r = BTYPE_RANGES.get(t.strip())
            if r:
                btype_ranges.append(r)
    scale = None
    scale_args = {"naval": "--scale-naval", "field": "--scale-field", "siege": "--scale-siege"}
    for k, arg in scale_args.items():
        if arg in args:
            scale = scale or {}
            scale[k] = int(args[args.index(arg) + 1])
    if "--scale" in args:   # 兼容：统一阈值（三类型同值）
        v = int(args[args.index("--scale") + 1])
        scale = {"naval": v, "field": v, "siege": v}
    factions = exclude = None
    if "--factions" in args:
        factions = [s.strip() for s in args[args.index("--factions") + 1].split(",") if s.strip()]
    if "--exclude" in args:
        exclude = [s.strip() for s in args[args.index("--exclude") + 1].split(",") if s.strip()]
    auto_esc = "--no-esc" not in args
    unit_off = None
    if "--unit-off" in args:
        unit_off = int(args[args.index("--unit-off") + 1], 0)
    prefilter_cfg = None
    pre_keys = ("--prefilter-min-total", "--prefilter-min-per-side",
                "--prefilter-min-armies", "--prefilter-max-ratio")
    if any(k in args for k in pre_keys):
        prefilter_cfg = {}
        for k in pre_keys:
            if k in args:
                val = args[args.index(k) + 1]
                prefilter_cfg[k.replace("--prefilter-", "").replace("-", "_")] = float(val) if "ratio" in k else int(val)
    pre_unit_cfg = None
    pre_unit_keys = ("--pre-min-total", "--pre-min-per-side", "--pre-min-ratio",
                     "--pre-min-total-field", "--pre-min-per-side-field",
                     "--pre-min-total-siege", "--pre-min-per-side-siege")
    if any(k in args for k in pre_unit_keys):
        pre_unit_cfg = {}
        for k in pre_unit_keys:
            if k in args:
                val = args[args.index(k) + 1]
                pre_unit_cfg[k.replace("--pre-min-", "min_").replace("-", "_")] = float(val) if "ratio" in k else int(val)
    if "--split-type" in args:
        pre_unit_cfg = pre_unit_cfg or {}
        pre_unit_cfg["split_type"] = True
    dyn_decisive = "--dyn-decisive" in args
    if dyn_decisive:
        pre_unit_cfg = pre_unit_cfg or {}
        pre_unit_cfg.setdefault("min_ratio", 0.5)
    sc = SpectateCapture(h, base, engine=eng, logfn=_safe_print)
    if not sc.install(btype_ranges, scale, factions, exclude, auto_esc, unit_off,
                      prefilter_cfg=prefilter_cfg, pre_unit_cfg=pre_unit_cfg,
                      dyn_decisive=dyn_decisive):
        return
    sc.start_observe()
    observe = 3600
    if "--observe" in args:
        observe = int(args[args.index("--observe") + 1])
    try:
        time.sleep(observe)
    except KeyboardInterrupt:
        pass
    sc.stop()
    sc.uninstall()


def _open():
    """打开游戏进程并按运行中的引擎模块装配常量。
    返回 (h, base, engine)；engine ∈ {"6262"(Empire.Retail.dll), "6118"/"6115"(Shogun2.dll 按 BUILD 串)}。"""
    pid = pb.find_pid("shogun2.exe")
    if not pid:
        raise SystemExit("未找到 shogun2.exe 进程")
    h = pb.K32.OpenProcess(pb.PROCESS_QUERY_INFORMATION | pb.PROCESS_VM_READ |
                           pb.PROCESS_VM_WRITE | pb.PROCESS_VM_OPERATION, False, pid)
    if not h:
        raise SystemExit(f"OpenProcess 失败（需管理员权限）err={ctypes.get_last_error()}")
    build, base, _prof = pb.detect_build(h)
    if not base:
        raise SystemExit("未找到引擎模块（Empire.Retail.dll=6262 / Shogun2.dll=6118 或 6115）")
    eng, err = resolve_engine_for_module(h, build)
    if not eng:
        raise SystemExit(f"引擎识别失败: {err}（可手动 --engine 指定）")
    ename, err = apply_engine(eng)
    if err:
        raise SystemExit(f"引擎装配失败: {err}")
    return h, base, ename


if __name__ == "__main__":
    _cli()
