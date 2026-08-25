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
  --observe SEC / --restore
"""
import ctypes
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

# cfg 字段偏移
CFG_MIN1, CFG_MAX1, CFG_MIN2, CFG_MAX2, CFG_FLAGS = 0x00, 0x04, 0x08, 0x0c, 0x10
CFG_NAVAL, CFG_FIELD, CFG_SIEGE = 0x14, 0x18, 0x1c
CFG_WL_COUNT, CFG_BL_COUNT = 0x20, 0x24
# flags bit
F_BTYPE1, F_BTYPE2, F_SCALE, F_WHITELIST, F_BLACKLIST = 1, 2, 4, 8, 16

# meta 字段偏移（DATA_META + off）
META_EVENT, META_PENDING, META_STATUS = 0x00, 0x04, 0x08
META_MODEL, META_MODEL_VT = 0x0c, 0x10
META_COUNT, META_TOTAL, META_TMP = 0x14, 0x18, 0x1c
META_SIDE1, META_SIDE2 = 0x20, 0x24
META_WL_MATCH, META_BL_HIT = 0x28, 0x2c
META_REASON = 0x30              # 0=写b9, 1=vt, 2=ready, 3=155c, 4=btype, 5=scale, 6=whitelist, 7=blacklist, 0xff=未定

REASON_NAMES = {0: "写b9", 1: "vt不匹配", 2: "ready!=0", 3: "双人类155c",
                4: "btype过滤", 5: "规模过滤", 6: "白名单未命中", 7: "黑名单命中", 0xff: "未定"}

BTYPE_RANGES = {"field": (0, 2), "siege": (3, 10), "naval": (11, 14)}
BTYPE_NAMES = {0: "NORMAL野战", 1: "AMBUSH野战", 2: "BRIDGE野战",
               3: "FORT_SIEGE攻城", 4: "FORT_BLOODBATH攻城", 5: "FORTIFIED_SETTLEMENT攻城",
               6: "FORTIFIED_SETTLEMENT_STANDARD攻城", 7: "FORTIFIED_SETTLEMENT_SALLY_OUT攻城",
               8: "FORTIFIED_SETTLEMENT_SIEGE攻城", 9: "UNFORTIFIED_SETTLEMENT攻城",
               10: "REGION_SLOT攻城", 11: "NAVAL_NORMAL海战", 12: "NAVAL_BLOCKADE_BREAKOUT海战",
               13: "NAVAL_BLOCKADE_RELIEF海战", 14: "NAVAL_PORT_ASSAULT海战", 15: "UNSPECIFIED"}

ST_GRP_CNT, ST_GRP_TBL = 0x88, 0x8c
GRP_ARMY_CNT, GRP_ARMY_TBL = 0x20, 0x24
# ★单位数偏移（2026-08-19 FotS 差分：原版 [army+0x114] 定案；FotS +0x114=0、+0x12c=单位数（3v2 精确））
ARMY_UNIT_CNT_VANILLA = 0x114
ARMY_UNIT_CNT_FOTS = 0x12c
FACTION_NAME_OFF = 0x0b14

WM_KEYDOWN, WM_KEYUP, VK_ESCAPE = 0x0100, 0x0101, 0x1B
_user32 = ctypes.WinDLL("user32", use_last_error=True)


def _rd32(h, a):
    if not a or not (0x10000 < a < 0x80000000):
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
    if not ptr or not (0x10000 < ptr < 0x80000000):
        return None
    try:
        raw = bytes(pb.read_mem(h, ptr, 64))
        return raw.decode("utf-16-le", errors="ignore").split("\x00")[0]
    except Exception:
        return None


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
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x03")  # reason=双人类155c
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "skip_write")
    B.emit(code, b"\x8B\x40\x08")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x03")  # reason=双人类155c
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "skip_write")
    B.emit(code, b"\x80\xB8\x5C\x15\x00\x00\x00")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x03")  # reason=双人类155c
    jcc(0x85, "skip_write")

    # btype dual ranges
    B.emit(code, b"\x0F\xB6\x8E\x58\x00\x00\x00")
    B.e_mov_eax_abs(code, cfg_addr + CFG_FLAGS)
    B.e_mov_edx_imm(code, cfg_addr)
    B.emit(code, b"\xA8\x01")
    jcc(0x84, "range2")
    B.emit(code, b"\x3B\x0A")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x04")  # reason=btype过滤
    jcc(0x82, "skip_write")
    B.emit(code, b"\x3B\x4A\x04")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x04")  # reason=btype过滤
    jcc(0x87, "skip_write")
    jmp("do_btype_pass")
    L("range2")
    B.emit(code, b"\xA8\x02")
    jcc(0x84, "do_btype_pass")
    B.emit(code, b"\x3B\x4A\x08")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x04")  # reason=btype过滤
    jcc(0x82, "skip_write")
    B.emit(code, b"\x3B\x4A\x0C")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x04")  # reason=btype过滤
    jcc(0x87, "skip_write")
    L("do_btype_pass")

    # S16 traversal
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_STATUS) + b"\x01")
    B.emit(code, b"\x8B\x46\x4C")
    B.emit(code, b"\xA3" + struct.pack("<I", meta + META_MODEL))
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "model_fail")
    B.emit(code, b"\x8B\x08")
    B.emit(code, b"\x89\x0D" + struct.pack("<I", meta + META_MODEL_VT))
    B.emit(code, b"\x81\x38" + struct.pack("<I", base + MODEL_VTABLE_RVA))
    jcc(0x85, "model_fail")
    B.emit(code, b"\x8B\x80\x9C\x14\x00\x00")
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "cmgr_fail")
    B.emit(code, b"\x8B\x58\x20")
    B.emit(code, b"\x8B\x68\x24")
    B.emit(code, b"\x31\xD2")
    B.emit(code, b"\x31\xC9")
    B.emit(code, b"\xBF" + struct.pack("<I", scale_arr))
    L("loop_top")
    B.emit(code, b"\x39\xEB")
    jcc(0x84, "done")
    B.emit(code, b"\x83\xF9\x40")
    jcc(0x83, "done")
    B.emit(code, b"\x41")
    # ★冲突管理器节点 = entry（conf_army 实机）：node+8=entry；entry+0x1c=armyA；entry+0x5c=尾节点，[尾+8]=armyB
    B.emit(code, b"\x8B\x43\x08")                          # eax=entry
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "next")
    # armyA
    B.emit(code, b"\x8B\x40\x1C")                          # eax=[entry+0x1c] armyA
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "army_b")
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
    B.emit(code, b"\x8B\x40\x08")                          # eax=[tail+8] armyB
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "next")
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
    # 链1：side+0x64 持久 faction
    B.emit(code, b"\x8B\x40\x64")
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "wl_off_c")
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
    B.emit(code, b"\x8B\x40\x0C")
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "wl_next")
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
    # 链1：side+0x64 持久 faction
    B.emit(code, b"\x8B\x40\x64")
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "bl_off_c")
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
    B.emit(code, b"\x8B\x40\x0C")
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "bl_next")
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

    # write b9
    L("write_b9")
    B.emit(code, b"\xC6\x05" + struct.pack("<I", meta + META_REASON) + b"\x00")  # reason=写b9
    B.emit(code, b"\xC6\x86\xB9\x00\x00\x00\x01")
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
    """把派系名解析为持久 faction 对象指针（S18：side+0x64 链比对的正是这些对象）。"""
    if not names:
        return []
    try:
        import s2_watch as sw
        facs = sw.scan_factions(h, base)
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
    B.emit(code, b"\x8B\x80\x9C\x14\x00\x00")                 # eax=[model+0x149c] cmgr
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "skip")
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
    B.emit(code, b"\x8B\x43\x08")                             # eax=army
    B.emit(code, b"\x85\xC0")
    jcc(0x84, "next")
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
    B.emit(code, b"\x8B\x76\x08")                             # esi=[head+8] armyB
    B.emit(code, b"\x85\xF6")
    jcc(0x84, "next")
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


class SpectateCapture:
    """看海捕捉：A1（0x5caa60 入口）hook + 写 b9 前过滤 + 观测兜底/ESC"""

    def __init__(self, h, base, logfn=print):
        self.h, self.base, self.log = h, base, logfn
        self.region = None
        self._obs_thread = None
        self._intercept_thread = None
        self._running = False
        self._last_mgr = None

    def install(self, btype_ranges=None, scale=None, factions=None, exclude=None, auto_esc=False,
                unit_off=None):
        """装 A1（0x5caa60 入口）hook。
        btype_ranges=[(min,max),...]（0-2 个范围；空=全捕捉）；
        scale=阈值 dict {naval, field, siege}（None=不筛）；
        factions 白名单（任一方命中才推）；exclude 黑名单（任一方命中即不推）；
        auto_esc=默认关（★2026-08-19 用户否决：加载后 ESC 浪费性能——现在规模/阵营已前移到写 b9 前）；
        unit_off=加载后精筛单位数偏移（None=自动：优先 0x114，读 0 回退 0x12c）"""
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
        # 写 cfg
        cfg = struct.pack("<IIIIIIIIII", m1, x1, m2, x2, flags,
                          naval, field, siege, len(wl_addrs), len(bl_addrs))
        if not _write(self.h, region + DATA_CFG, cfg):
            self.log("✗ 写 cfg 失败")
            return False
        if wl_addrs and not _write(self.h, region + DATA_WL, struct.pack("<%dI" % len(wl_addrs), *wl_addrs)):
            self.log("✗ 写白名单数组失败")
            return False
        if bl_addrs and not _write(self.h, region + DATA_BL, struct.pack("<%dI" % len(bl_addrs), *bl_addrs)):
            self.log("✗ 写黑名单数组失败")
            return False
        # 迁移：若旧版 0x6045c4 call-site hook 仍在，先恢复（避免双 hook）
        try:
            old_patch = self.base + OLD_HOOK_RVA
            old_cur = bytes(pb.read_mem(self.h, old_patch, len(OLD_ORIG_BYTES)))
            if old_cur and old_cur[0] == 0xE9:
                old_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), "re_a1_6045c4_orig.bin")
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
        # patch FUN_106e9f60（规模缓存源）
        d_patch = self.base + DISPATCHER_HOOK_RVA
        d_orig = bytes(pb.read_mem(self.h, d_patch, len(DISPATCHER_ORIG_BYTES)))
        if d_orig != DISPATCHER_ORIG_BYTES:
            if d_orig[0] == 0xE9:
                d_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), "re_a1_6e9f60_orig.bin")
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
                self.log(f"✗ 0x6e9f60 异常 {d_orig.hex()}，拒绝装")
                return False
        d_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), "re_a1_6e9f60_orig.bin")
        with open(d_bak, "wb") as f:
            f.write(d_orig)
        d_ok = _write(self.h, d_patch, b"\xE9" + struct.pack("<I", ((region + DISPATCHER_STUB_OFF) - (d_patch + 5)) & 0xFFFFFFFF))
        self.log(f"✓ dispatcher hook {hex(d_patch)}→{hex(region + DISPATCHER_STUB_OFF)} result={d_ok}")
        # patch 0x5caa60 入口
        patch_addr = self.base + HOOK_RVA
        orig = bytes(pb.read_mem(self.h, patch_addr, len(ORIG_BYTES)))
        if orig != ORIG_BYTES:
            if orig[0] == 0xE9:
                bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), "re_a1_5caa60_orig.bin")
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
        bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), "re_a1_5caa60_orig.bin")
        with open(bak, "wb") as f:
            f.write(orig)
        ok = _write(self.h, patch_addr, b"\xE9" + struct.pack("<I", (region - (patch_addr + 5)) & 0xFFFFFFFF))
        self.region = region
        self._cfg = {"btype_ranges": btype_ranges, "scale": scale,
                     "factions": factions, "exclude": exclude, "auto_esc": auto_esc}
        self._hwnd = _find_hwnd(pb.find_pid())
        names = [f"{BTYPE_NAMES.get(a,'?')}~{BTYPE_NAMES.get(b,'?')}" for a, b in rs]
        self.log(f"✓ A1(0x5caa60 入口) 已装 {hex(patch_addr)}→{hex(region)} "
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
                old_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), "re_a1_6045c4_orig.bin")
                if os.path.exists(old_bak):
                    with open(old_bak, "rb") as f:
                        old_saved = f.read(len(OLD_ORIG_BYTES))
                    if old_saved == OLD_ORIG_BYTES:
                        _write(self.h, old_patch, old_saved)
                        self.log("✓ 旧 0x6045c4 hook 已清理")
        except Exception:
            pass
        bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), "re_a1_5caa60_orig.bin")
        if os.path.exists(bak):
            with open(bak, "rb") as f:
                orig = f.read(len(ORIG_BYTES))
            if orig == ORIG_BYTES:
                _write(self.h, self.base + HOOK_RVA, orig)
                self.log("✓ A1 已卸载（0x5caa60 恢复）")
        d_bak = os.path.join(os.path.dirname(os.path.abspath(__file__)), "re_a1_6e9f60_orig.bin")
        if os.path.exists(d_bak):
            with open(d_bak, "rb") as f:
                d_orig = f.read(len(DISPATCHER_ORIG_BYTES))
            if d_orig == DISPATCHER_ORIG_BYTES:
                _write(self.h, self.base + DISPATCHER_HOOK_RVA, d_orig)
                self.log("✓ dispatcher hook 已卸载（0x6e9f60 恢复）")
        self.region = None

    def start_observe(self):
        """后台观测线程：battle_mgr 变化 → 单位数/阵营 → 兜底筛选 → ESC"""
        if self._running:
            return
        self._running = True
        self._last_mgr = _rd32(self.h, self.base + 0x1bc8180)
        self._obs_thread = threading.Thread(target=self._observe_loop, daemon=True)
        self._obs_thread.start()
        self._intercept_thread = threading.Thread(target=self._intercept_log_loop, daemon=True)
        self._intercept_thread.start()
        self.log("▶ 观测中（battle_mgr 变化 → 规模/阵营兜底判定）…")

    def stop(self):
        self._running = False

    # ---------- 观测 ----------
    def _factions_of(self, pending):
        """攻守阵营名（双链回退：+0x64 持久 / +0xc 战斗，UTF-16 合法才取）"""
        fv = self.base + FACTION_VTABLE_RVA
        names = []
        for off in (0x60, 0x64):
            side = _rd32(self.h, pending + off)
            n = None
            if side and 0x10000 < side < 0x80000000:
                for foff in (0x64, 0xc):
                    f = _rd32(self.h, side + foff)
                    if f and 0x10000 < f < 0x80000000 and _rd32(self.h, f) == fv:
                        nm = _read_utf16(self.h, _rd32(self.h, f + FACTION_NAME_OFF))
                        if nm:
                            n = nm
                            break
            names.append(n)
        return names

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
            if not g or not (0x10000 < g < 0x80000000):
                continue
            acnt = _rd32(self.h, g + GRP_ARMY_CNT)
            atbl = _rd32(self.h, g + GRP_ARMY_TBL)
            gtot = 0
            for ai in range(min(acnt or 0, 16)):
                a = _rd32(self.h, atbl + ai * 4) if atbl else None
                if not a or not (0x10000 < a < 0x80000000):
                    continue
                for off in offs:
                    u = _rd32(self.h, a + off)
                    if u and 0 < u < 500:
                        gtot += u
                        break
            totals.append(gtot)
        return totals, issp

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
                    line = (f"[拦截 #{ev}] pending={hex(pending) if pending else '?'} "
                            f"btype={btype}({BTYPE_NAMES.get(btype, '?')}) "
                            f"规模={total}(军{count}) 阵营={names[0] or '?'}/{names[1] or '?'} "
                            f"→ {REASON_NAMES.get(reason, reason)}")
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
        model = None
        while self._running:
            try:
                if model is None:
                    from re_h46a import anchor
                    model, _ = anchor(self.h, self.base)
                mgr = _rd32(self.h, self.base + 0x1bc8180)
                if mgr and mgr != self._last_mgr:
                    self._last_mgr = mgr
                    btype = None
                    p = _rd32(self.h, model + 0x14a4) if model else None
                    if p and 0x10000 < p < 0x80000000:
                        btype = _rd32(self.h, p + 0x58)
                    names = self._factions_of(p) if p else [None, None]
                    totals, issp = self._units_of(mgr)
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
                    self.log(f"★新战斗 mgr={hex(mgr)} btype={btype}({BTYPE_NAMES.get(btype, '?')}) "
                             f"阵营={names} 单位数={totals} issp={issp} "
                             f"{'→ ❌兜底跳过 ' + reason if skip else '→ ✅保留'}")
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


def _cli():
    args = sys.argv[1:]
    if "--restore" in args:
        h, base = _open()
        sc = SpectateCapture(h, base)
        sc.uninstall()
        return
    h, base = _open()
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
    sc = SpectateCapture(h, base, logfn=print)
    if not sc.install(btype_ranges, scale, factions, exclude, auto_esc, unit_off):
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
    import re_h46a as H46
    h, base = H46.open_proc()
    return h, base


if __name__ == "__main__":
    _cli()
