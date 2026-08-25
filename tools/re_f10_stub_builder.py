# -*- coding: utf-8 -*-
"""re_f10_stub_builder.py — F10 确认轮 stub 字节生成原型（纯静态，可离线自校验）。

目标：为下次实机（U2/U3/U4 确认）生成 4 个 inline hook stub + 共享初始化子程序 +
VEH 崩溃面包屑 handler，全部 stub 内「直写日志」——日志载体 = 进程外命名共享内存
（主方案，文件页后备，宿主持句柄 → 崩溃后数据可读），WriteFile 落盘为备选变体。

相对 re_f7_p6_inject.py 的改进：
  1. marker 只写内存、崩溃即丢 → 本设计：stub 内调 kernel32（经游戏自身 IAT 槽，
     见 re_f10_confirm_design.md §3.1）写命名共享内存 + 宿主 50ms 落盘快照；
  2. U2：新增 0x6045cc fork 决策 hook（记录 FUN_105caa60 返回值=分叉决定）+
     FUN_1057fca0 状态写者 hook（事件驱动状态序列，含状态 4 瞬时窗口）；
  3. U4：inject stub 记录 [faction+0x6a0/+0x798/+0x7a0] 前值 + 补齐器 FUN_10600c20
     调用后 [+0x6a0/+0x6bc/+0x6b8/+0x6d8] 后值（补齐前后全记录）。

hook 点（原字节静态确认，2026-08-17 re_f10_confirm_design.md §2）：
  inject     0x5abf34  8B 7C 24 14 8B F1         back 0x5abf3a
  fork       0x6045cc  8B CB 0F B6 83 14 01 00 00 back 0x6045d5
  state      0x57fca0  8B 44 24 04 56            back 0x57fca5
  factory    0x1655c0  56 6A 01 8B F1            back 0x1655c5

用法：
  python -u tools/re_f10_stub_builder.py            # 生成 + capstone 自校验 + 布局 dump
  python -u tools/re_f10_stub_builder.py --dry-run  # 追加：本机共享内存环协议回环（无需游戏）
  python -u tools/re_f10_stub_builder.py --writefile# 追加：生成 WriteFile 变体并校验
"""
import os
import struct
import sys

# ───────────────────────── 共享常量（宿主观测脚本 import 本模块复用） ─────────────────────────
IMAGE_BASE = 0x10000000

# ★2026-08-17 min_test 三轮实机（step1/2/3 全过）后改造：弃用共享内存环，
# 事件环 + meta 改放「marker」（游戏内 VirtualAllocEx 内存，region+MARKER_OFF，绝对地址立即数）。
# stub 零 API 依赖（除 GetTickCount 经 IAT）——min_test 已验证该机制可靠（step2/3 11 命中无崩）。

HDR_MAGIC = 0x50463130          # 'F10P'
HDR_VERSION = 2                 # v2 = marker 环

# header 布局（marker 内）
OFF_MAGIC = 0x00
OFF_VERSION = 0x04
OFF_COUNT = 0x08                # u32 事件序号（stub 递增）
OFF_LAST_TAG = 0x0c             # u8 最后写入者 tag
OFF_PENDING_FILTER = 0x10       # u32 宿主写入当前 pending（stub 过滤）
# inject meta（14 × u32，stub 写、宿主读；+0x14..+0x4c）
OFF_IM_ATTACKER = 0x14
OFF_IM_FACTION = 0x18
OFF_IM_PENDING = 0x1c
OFF_IM_F6A0_B = 0x20            # 补齐前 [faction+0x6a0]
OFF_IM_F798 = 0x24              # [faction+0x798]（登记门控输入）
OFF_IM_F7A0 = 0x28              # [faction+0x7a0]（登记门控输入）
OFF_IM_F6BC_B = 0x2c
OFF_IM_F6B8_B = 0x30
OFF_IM_F6D8_B = 0x34
OFF_IM_F6A0_A = 0x38            # 补齐后
OFF_IM_F6BC_A = 0x3c
OFF_IM_F6B8_A = 0x40
OFF_IM_F6D8_A = 0x44
OFF_IM_C20 = 0x48               # u32 1=已调 FUN_10600c20
OFF_IM_PRE = 0x4c               # u32 0x13=未开火 / 0x12=链NULL跳过 / 0x11=待调 / 0x14=同tick去重（★SA-A 面包屑）
OFF_IM_POST = 0x50              # u32 0x21=FUN_10600c20 已返回
OFF_IM_NAME = 0x54              # u32 [faction+0x51c] 名字字符串指针（★2026-08-17 新增：势力识别用；
                                #   0x5ecf1f `mov eax,[esi+0x51c]` = faction→名字 getter，0xee2c0 字符串比较）
# envdisp meta（FUN_102a0450 入口裁决；10 × u32，stub 写、宿主读；+0x60..+0x88）
OFF_ED_ENV = 0x60               # env（ecx）
OFF_ED_TABLE = 0x64             # [env+0x281e4] 表 data 指针
OFF_ED_CAP = 0x68               # [env+0x281dc] 表 cap
OFF_ED_SIZE = 0x6c              # [env+0x281e0] 表 size
OFF_ED_ISSP = 0x70              # [env+0x281e8] IsSpectator（byte）
OFF_ED_CNT64 = 0x74             # [env+0x28064] env 军队数
OFF_ED_WRAP = 0x78              # [env+0x2831c] wrapper 槽
OFF_ED_ST = 0x7c                # st = [[env+8]+0xb4]
OFF_ED_ST88 = 0x80              # [st+0x88] 军队计数
OFF_ED_ST8C = 0x84              # [st+0x8c] 军队数组 base
# 事件环（0x100 起，0x100 槽 × 0x20 = 0x2000）
RING_OFF = 0x100
RING_SLOTS = 0x100
REC_SIZE = 0x20
# 记录布局：[+0]seq u32 [+4]tick u32 [+8]tag u8 [+9]state u8 [+0xc]retaddr u32 [+0x10]obj u32
#           [+0x14]v0 u32 [+0x18]v1 u32 [+0x1c]v2 u32
TAG_INJECT = 1
TAG_FORK = 2
TAG_STATE = 3
TAG_FACTORY = 4
TAG_VEH = 5
TAG_ENVDISP = 6
TAG_OFFER = 7          # offer 创建器（FUN_1085b290 入口）
TAG_OFFERCTOR = 8      # offer 构造器（FUN_106e50b0 入口）

# ── kernel32 IAT 槽 RVA（★2026-08-17 修正：re_f10_verify_iat.py 从 DLL 导入表实测；VA = base + RVA） ──
# ⚠️ U24 原值（0x1790d58 等）为错误值（差固定 0x3DFB30），stub 内经错误 IAT 槽 call → 垃圾地址 → 崩。
IAT_CreateFileMappingA = 0x15a1228
IAT_MapViewOfFile = 0x15a120c
IAT_GetTickCount = 0x15a1274
IAT_GetModuleHandleW = 0x15a1100
IAT_GetProcAddress = 0x15a1190
IAT_WriteFile = 0x15a1154
IAT_CreateFileA = 0x15a11a4
IAT_SetFilePointer = 0x15a12e0
IAT_CloseHandle = 0x15a1158

# 游戏函数（绝对 VA，宿主安装时 = base + RVA）
GETTER_FACTION = 0x103e5960    # 绝对 VA（本 stub 不使用；若用须 base + RVA）
FACTION_VTABLE_RVA = 0x15fac30 # faction 对象 vtable（40 §8）——getter 结果守卫用（RVA）
FUN_C20 = 0x600c20             # ★RVA（★B1 修复 2026-08-17：旧值 0x10600c20 是绝对 VA，base+FUN_C20 会 double-add
                               # 0x10000000 → 调用目标 0x6C5E0C20 超出镜像（SizeOfImage=0x1D88000）→ 两轮实机全污染；
                               # 正确取址 = base+RVA（对照 re_h47_6084db_inject.py C20_RVA=0x600c20 实证 a6a0=0x101））

# hook 定义
HOOKS = {
    "inject":  {"rva": 0x5abf4d, "orig": bytes([0x8B, 0xC8, 0x56, 0x8D, 0x41, 0x24]), "back": 0x5abf53,
                "name": "inject(0x5abf4d) 游戏getter后捕获faction+门控字段+转人类"},
    "fork":    {"rva": 0x6045cc, "orig": bytes([0x8B, 0xCB, 0x0F, 0xB6, 0x83, 0x14, 0x01, 0x00, 0x00]), "back": 0x6045d5,
                "name": "fork(0x6045cc) FUN_105caa60 分叉决策记录"},
    "state":   {"rva": 0x57fca0, "orig": bytes([0x8B, 0x44, 0x24, 0x04, 0x56]), "back": 0x57fca5,
                "name": "state(0x57fca0) 状态写者事件记录"},
    "factory": {"rva": 0x1655c0, "orig": bytes([0x56, 0x6A, 0x01, 0x8B, 0xF1]), "back": 0x1655c5,
                "name": "factory(0x1655c0) 工厂触发面包屑"},
    "envdisp": {"rva": 0x2a0450, "orig": bytes([0x83, 0xEC, 0x38, 0x53, 0x8B, 0xD9]), "back": 0x2a0456,
                "name": "envdisp(0x2a0450) FUN_102a0450 入口裁决（表/军队数组/IsSpectator/wrapper）"},
    "regconv": {"rva": 0x5c4718, "orig": bytes([0x8B, 0xF8, 0x32, 0xDB, 0x38, 0x9F, 0xA0, 0x06, 0x00, 0x00]), "back": 0x5c4722,
                "name": "regconv(0x5c4718) ★v3 登记内部转换（FUN_105c4700 算完 edi=faction 后、6a0 判定前）"},
    "offer":    {"rva": 0x85b2d0, "orig": bytes([0x83, 0xEC, 0x24, 0x53, 0x55]), "back": 0x85b2d5,
                "name": "offer(0x85b2d0) ★FUN_1085b290 解析层offer创建器入口（人类在场→建offer门，F报告决定性检测点）"},
    "offerctor":{"rva": 0x6e50b0, "orig": bytes([0x83, 0xEC, 0x10, 0x53, 0x55]), "back": 0x6e50b5,
                "name": "offerctor(0x6e50b0) ★FUN_106e50b0 offer构造器入口（事件0x133注册前，offer链触发确认）"},
}

# stub 区布局（region 0x10000，PAGE_EXECUTE_READWRITE；marker = region+0x4000 大小 0x2200）
REGION_SIZE = 0x10000
OFF_INJECT = 0x0000
OFF_FORK = 0x0800
OFF_STATE = 0x1000
OFF_REGCONV = 0x1400
OFF_FACTORY = 0x1800
OFF_ENVDISP = 0x1c00
OFF_OFFER = 0x1e00          # ★2026-08-18 新增：解析层 offer 创建器观测 stub（纯记录，tag=7）
OFF_OFFERCTOR = 0x1f00      # ★2026-08-18 新增：offer 构造器观测 stub（纯记录，tag=8）
OFF_INIT = 0x2000
OFF_VEH = 0x2400
OFF_DATA = 0x2800
MARKER_OFF = 0x4000          # 事件环 + meta（stub 绝对地址立即数引用，宿主 ReadProcessMemory 读）
DATA_VIEW = 0x2800          # u32（保留槽，不再使用）
DATA_INIT_FLAG = 0x2804     # u32 1=已初始化（VEH 注册幂等）
DATA_INJECT = 0x2808        # u32 1=inject stub 执行转人类（P6 注入）/ 0=纯观测
DATA_HMAP = 0x280c          # u32（保留槽，不再使用）
DATA_SCRATCH = 0x2810       # 0x20：+0 tick +4 pending +8 retaddr +0xc state(dl) +0x10 faction +0x14 v0 +0x18 v1 +0x1c v2
DATA_NAME = 0x2830          # 0x20（保留槽）
DATA_K32 = 0x2850           # 0x10 "kernel32.dll"（VEH 注册用）
DATA_VEHN = 0x2860          # 0x20 "AddVectoredExceptionHandler"
DATA_LOGPATH = 0x2880       # 0x80（保留槽）

# ───────────────────────── 字节发射助手 ─────────────────────────
def emit(code, bs):
    code.extend(bs)


def e_mov_eax_abs(code, addr):
    emit(code, b"\xA1" + struct.pack("<I", addr))          # mov eax,[addr]


def e_mov_eax_imm(code, imm):
    emit(code, b"\xB8" + struct.pack("<I", imm))           # mov eax,imm32（取地址本身）


def e_mov_edx_imm(code, imm):
    emit(code, b"\xBA" + struct.pack("<I", imm))           # mov edx,imm32


def e_call_edx(code):
    emit(code, b"\xFF\xD2")                                # call edx


def e_call_eax(code):
    emit(code, b"\xFF\xD0")                                # call eax


def e_push_imm(code, imm):
    emit(code, b"\x68" + struct.pack("<I", imm & 0xFFFFFFFF))


def e_jmp_rel(code, dst, src):
    emit(code, b"\xE9" + struct.pack("<I", (dst - (src + 5)) & 0xFFFFFFFF))


def ring_preamble(code, region, base):
    """事件环写前置（共享）：取 tick → marker 槽位计算 → edi=记录地址。
    ★marker 环：marker = region+MARKER_OFF（绝对地址立即数，无 API、无判空）。
    调用后寄存器约定：ecx=count、edi=记录地址；dl 由调用方在 emit_rec_fields 前恢复。
    返回 None（无长跳回填）。"""
    # eax = GetTickCount()（经游戏 IAT 槽）
    e_mov_eax_abs(code, base + IAT_GetTickCount)
    e_call_eax(code)
    emit(code, b"\xA3" + struct.pack("<I", region + DATA_SCRATCH))  # [scratch+0]=tick
    # eax = marker 地址；slot = count & 0xFF; addr = marker + RING_OFF + slot*0x20
    # ★marker 地址寻址修正（2026-08-17 实机根因）：A1 加载 [marker] 内容=magic(0x50463130) 再 [eax+8] 解引用
    # → AV/写垃圾崩；须 B8 imm 取 marker 地址本身（min_test 直写 marker 故未暴露，全功能 stub 一跑就崩）
    e_mov_eax_imm(code, region + MARKER_OFF)
    emit(code, b"\x8B\x48\x08")                            # mov ecx,[eax+8]  count
    emit(code, b"\x8B\xD1")                                # mov edx,ecx
    emit(code, b"\x81\xE2\xFF\x00\x00\x00")                # and edx,0xFF
    emit(code, b"\xC1\xE2\x05")                            # shl edx,5
    emit(code, b"\x8D\xBC\x10" + struct.pack("<I", RING_OFF))  # lea edi,[eax+edx+RING_OFF]
    return None


def backfill_jmp(code, jmp_at, target):
    struct.pack_into("<i", code, jmp_at + 1, target - (jmp_at + 5))


def emit_rec_fields(code, region, tag, with_magic_wf=False):
    """写记录字段（ecx=count、edi=记录地址、dl=state、ebx=retaddr、esi=obj、scratch+0x14..0x1c=v0..v2）。
    scratch 槽存「值」非指针：A1 直接取值，禁止再解引用。"""
    emit(code, b"\x89\x4F\x00")                            # [edi+0] = seq
    e_mov_eax_abs(code, region + DATA_SCRATCH)             # eax = [scratch+0] tick（值）
    emit(code, b"\x89\x47\x04")                            # [edi+4] = tick
    emit(code, b"\xC6\x47\x08" + bytes([tag]))             # [edi+8] = tag
    emit(code, b"\x88\x57\x09")                            # [edi+9] = state(dl)
    emit(code, b"\x89\x5F\x0C")                            # [edi+0xc] = retaddr(ebx)
    emit(code, b"\x89\x77\x10")                            # [edi+0x10] = obj(esi)
    if with_magic_wf:
        emit(code, b"\xC7\x47\x14" + struct.pack("<I", REC_MAGIC_WF))
        emit(code, b"\xC7\x47\x18" + struct.pack("<I", 0))
        emit(code, b"\xC7\x47\x1C" + struct.pack("<I", 0))
    else:
        for slot, off in ((0x14, 0x14), (0x18, 0x18), (0x1c, 0x1c)):
            e_mov_eax_abs(code, region + DATA_SCRATCH + slot)  # eax = [scratch+slot] 值
            emit(code, b"\x89\x47" + bytes([off]))         # mov [edi+off],eax
    # ★marker 地址寻址修正（2026-08-17 实机根因）：A1 加载 [marker] 内容=magic(0x50463130) 再 [eax+8] 解引用
    # → AV/写垃圾崩；须 B8 imm 取 marker 地址本身（min_test 直写 marker 故未暴露，全功能 stub 一跑就崩）
    e_mov_eax_imm(code, region + MARKER_OFF)
    emit(code, b"\xFF\x40\x08")                            # inc dword [eax+8]


def save_dl(code, region):
    """把 dl（state/决策）保存到 [scratch+0xc]（preamble 会毁 edx）。"""
    emit(code, b"\x88\x15" + struct.pack("<I", region + DATA_SCRATCH + 0x0c))


def restore_dl(code, region):
    emit(code, b"\x8A\x15" + struct.pack("<I", region + DATA_SCRATCH + 0x0c))


# ───────────────────────── init 子程序 + VEH handler ─────────────────────────
def build_init_sub(stub_addr, region, base):
    """init 子程序：★marker 环版 no-op（仅设 flag）。
    2026-08-17 实机教训：init 内 VEH 注册（GetModuleHandleW+GetProcAddress+AddVectoredExceptionHandler
    经 IAT 调用）在游戏高频路径执行导致游戏 6.7s 内崩溃——stub 内除 GetTickCount 外一律零 API。
    崩溃取证改由宿主读 marker + Windows 事件日志兜底（VEH handler 保留代码但不注册）。"""
    code = bytearray()
    emit(code, b"\x60")                                    # pushad
    emit(code, b"\xC7\x05" + struct.pack("<I", region + DATA_INIT_FLAG) + struct.pack("<I", 1))
    emit(code, b"\x61")                                    # popad
    emit(code, b"\xC3")                                    # ret
    return bytes(code)


def build_veh_handler(region):
    """VEH handler（无 API 调用，纯内存写，崩溃现场安全）：记录 {tag=5, v0=code, v1=addr, v2=eip} 到 marker 环。
    stdcall: 返回 EXCEPTION_CONTINUE_SEARCH(0)，ret 4。"""
    code = bytearray()
    emit(code, b"\x60")                                    # pushad
    # ★marker 地址寻址修正（2026-08-17 实机根因）：A1 加载 [marker] 内容=magic(0x50463130) 再 [eax+8] 解引用
    # → AV/写垃圾崩；须 B8 imm 取 marker 地址本身（min_test 直写 marker 故未暴露，全功能 stub 一跑就崩）
    e_mov_eax_imm(code, region + MARKER_OFF)
    emit(code, b"\x85\xC0")
    jz_done_at = len(code)
    emit(code, b"\x74\x00")                                # jz done
    emit(code, b"\x8B\x48\x08")                            # mov ecx,[eax+8]
    emit(code, b"\x8B\xD1")
    emit(code, b"\x81\xE2\xFF\x00\x00\x00")
    emit(code, b"\xC1\xE2\x05")
    emit(code, b"\x8D\xBC\x10" + struct.pack("<I", RING_OFF))
    emit(code, b"\x89\x4F\x00")                            # seq
    emit(code, b"\xC7\x47\x04\x00\x00\x00\x00")            # tick=0
    emit(code, b"\xC6\x47\x08" + bytes([TAG_VEH]))
    emit(code, b"\xC6\x47\x09\x00")
    emit(code, b"\xC7\x47\x0C\x00\x00\x00\x00")            # retaddr=0
    emit(code, b"\xC7\x47\x10\x00\x00\x00\x00")            # obj=0
    # PEXCEPTION_POINTERS = [esp+0x24]（pushad 后）
    emit(code, b"\x8B\x44\x24\x24")                        # mov eax,[esp+0x24]
    emit(code, b"\x85\xC0")
    jz_skip_ctx_at = len(code)
    emit(code, b"\x74\x00")
    emit(code, b"\x8B\x10")                                # mov edx,[eax]  ExceptionRecord
    emit(code, b"\x8B\x0A")                                # mov ecx,[edx]  code
    emit(code, b"\x89\x4F\x14")                            # v0=code
    emit(code, b"\x8B\x4A\x0C")                            # mov ecx,[edx+0xc] ExceptionAddress
    emit(code, b"\x89\x4F\x18")                            # v1=addr
    emit(code, b"\x8B\x40\x04")                            # mov eax,[eax+4] ContextRecord
    emit(code, b"\x8B\x88\xB8\x00\x00\x00")                # mov ecx,[eax+0xb8] Eip
    emit(code, b"\x89\x4F\x1C")                            # v2=eip
    skip_ctx_at = len(code)
    # ★marker 地址寻址修正（2026-08-17 实机根因）：A1 加载 [marker] 内容=magic(0x50463130) 再 [eax+8] 解引用
    # → AV/写垃圾崩；须 B8 imm 取 marker 地址本身（min_test 直写 marker 故未暴露，全功能 stub 一跑就崩）
    e_mov_eax_imm(code, region + MARKER_OFF)
    emit(code, b"\xFF\x40\x08")                            # inc [marker+8]
    done_at = len(code)
    emit(code, b"\x61")                                    # popad
    emit(code, b"\x31\xC0")                                # xor eax,eax (CONTINUE_SEARCH)
    emit(code, b"\xC2\x04\x00")                            # ret 4
    code[jz_done_at + 1] = (done_at - (jz_done_at + 2)) & 0xFF
    code[jz_skip_ctx_at + 1] = (skip_ctx_at - (jz_skip_ctx_at + 2)) & 0xFF
    return bytes(code)


# ───────────────────────── 各 hook stub ─────────────────────────
def _build_conv_stub(stub_addr, region, base, back_addr, name, esp_off, direct=False):
    """转换 stub 核心（inject v2 / regconv v3/v4 共用）。
    name="inject"：hook 0x5abf4d（游戏自身 getter 0x5abf48 返回后，eax=faction post-登记有效；esp_off=0x1C）。
    name="regconv"：hook 0x5c4718（★v3：FUN_105c4700 登记函数内部、登记自己算完 edi=faction、6a0 判定之前；
     esp_off=0x00=EDI——★这是登记真正检查的对象（getter(&attacker+0x254)），区别于外层 +0x25c 的对象）。
    direct=False：调 FUN_10600c20 转人类（需 [faction+0x8c] 链有效——v3 实测登记 faction 链全 NULL → 全被拦截）。
    direct=True（★v4）：直接写 byte[faction+0x6a0]=1（登记只查 6a0/798/7a0，零调用零链依赖；缺补齐字段的
     P42 风险待后续观察）。
    ★v2 动机（2026-08-17 实机 4 轮钉死）：0x5abf34 构造前调 getter 时 attacker+0x260 未初始化
    （0x5abf3d 登记后才写）→ getter 内 [[..]] 解引用垃圾 → AV 静默退出（ring 仅见 255 哨兵）。
    v2 直接捕获游戏算好的 faction，不再自己调 getter（stub 零游戏函数调用）。"""
    m = lambda off: region + DATA_SCRATCH + off
    code = bytearray()
    emit(code, b"\x60")                                    # pushad
    # faction = [esp+esp_off]（pushad 保存的 EAX(0x1C) / EDI(0x00)）
    emit(code, bytes([0x8B, 0x44, 0x24, esp_off]))          # mov eax,[esp+esp_off]
    emit(code, b"\xA3" + struct.pack("<I", m(0x10)))       # [scratch+0x10]=faction
    emit(code, b"\xA3" + struct.pack("<I", m(0x14)))       # [scratch+0x14]=faction(v0 入口记录)
    # ★入口面包屑（state=0xFF 哨兵 + v0=faction）——hook 触发证明 + faction 值
    emit(code, b"\x31\xDB")                                # xor ebx,ebx  retaddr=0
    emit(code, b"\x31\xF6")                                # xor esi,esi  obj=0
    emit(code, b"\xB2\xFF")                                # mov dl,0xFF（哨兵 state）
    save_dl(code, region)
    ring_preamble(code, region, base)
    restore_dl(code, region)
    emit_rec_fields(code, region, TAG_INJECT)
    # faction 守卫：NULL/越界（<0x10000 或 >=0x80000000）→ skip；再验 vtable（防 in-range 垃圾指针）
    e_mov_eax_abs(code, m(0x10))
    emit(code, b"\x85\xC0")                                # test eax,eax
    emit(code, b"\x75\x05")                                # jnz +5 → continue
    jmp_guard_at = len(code)
    emit(code, b"\xE9\x00\x00\x00\x00")                    # jmp skip_all（NULL → skip）
    emit(code, b"\x3D\x00\x00\x01\x00")                    # cmp eax,0x10000
    emit(code, b"\x73\x05")                                # jae +5 → continue
    jmp2_at = len(code)
    emit(code, b"\xE9\x00\x00\x00\x00")                    # jmp skip_all（<0x10000 → skip）
    emit(code, b"\x3D\x00\x00\x00\x80")                    # cmp eax,0x80000000
    emit(code, b"\x72\x05")                                # jb +5 → continue
    jmp3_at = len(code)
    emit(code, b"\xE9\x00\x00\x00\x00")                    # jmp skip_all（>=0x80000000 → skip）
    emit(code, b"\x81\x38" + struct.pack("<I", base + FACTION_VTABLE_RVA))  # cmp [eax], faction_vtable
    emit(code, b"\x75\x05")                                # jne +5 → continue（vtable 匹配）
    jmp4_at = len(code)
    emit(code, b"\xE9\x00\x00\x00\x00")                    # jmp skip_all（vtable 不符 → skip）
    faction_ok_at = len(code)
    emit(code, b"\x89\xC3")                                # mov ebx,eax  ebx=faction
    # ebp = marker（inject_meta 写 marker 偏移 OFF_IM_*）
    e_mov_eax_imm(code, region + MARKER_OFF)
    emit(code, b"\x89\xC5")                                # mov ebp,eax  ebp=marker
    # 补齐前记录（门控字段）
    emit(code, b"\x0F\xB6\x83\xA0\x06\x00\x00")            # movzx eax,byte[ebx+0x6a0]
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_IM_F6A0_B))
    emit(code, b"\x8B\x83\x98\x07\x00\x00")                # mov eax,[ebx+0x798]
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_IM_F798))
    emit(code, b"\x8B\x83\xA0\x07\x00\x00")                # mov eax,[ebx+0x7a0]
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_IM_F7A0))
    emit(code, b"\x8B\x83\xBC\x06\x00\x00")                # mov eax,[ebx+0x6bc]
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_IM_F6BC_B))
    emit(code, b"\x8B\x83\xB8\x06\x00\x00")                # mov eax,[ebx+0x6b8]
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_IM_F6B8_B))
    emit(code, b"\x8B\x83\xD8\x06\x00\x00")                # mov eax,[ebx+0x6d8]
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_IM_F6D8_B))
    emit(code, b"\x8B\x83\x14\x0B\x00\x00")                # mov eax,[ebx+0x0b14] 名字指针（★03 已确证：持久 faction +0x0b14 = 中文名 UTF-16，非 +0x51c）
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_IM_NAME))
    # 若注入开关（[region+DATA_INJECT]!=0）且 AI（+0x6a0==0）→ call FUN_10600c20（转人类）
    # ★SA-A 加固（2026-08-17）：3 级链守卫（FUN_10753740/107fc7e0/10644010 无条件解引用
    #   [faction+0x8c]→[+8]→[+0x1494]）+ PRE/POST 面包屑（区分 未开火/链NULL/同tick去重/待调/完成/调用中崩）
    #   + 同 tick 单转换去重（攻守双列表同一 tick 命中 → 只转一次防双人类）。
    e_mov_eax_abs(code, region + DATA_INJECT)
    emit(code, b"\x85\xC0")
    emit(code, b"\x75\x05")                                # jnz +5（注入开通过）
    jz_inj_at = len(code)
    emit(code, b"\xE9\x00\x00\x00\x00")                    # jmp notfired（回填）
    emit(code, b"\x80\xBB\xA0\x06\x00\x00\x00")            # cmp byte[ebx+0x6a0],0
    emit(code, b"\x74\x05")                                # je +5（AI 通过）
    jne_c20_at = len(code)
    emit(code, b"\xE9\x00\x00\x00\x00")                    # jmp notfired（回填）
    # 同 tick 去重（★B2 修复：前置——同 tick 重入直接 dedupskip，不做任何危险 deref/写）
    e_mov_eax_abs(code, region + DATA_SCRATCH)             # eax = [scratch+0] = tick
    emit(code, b"\x3B\x05" + struct.pack("<I", region + DATA_SCRATCH + 0x20))  # cmp eax,[scratch+0x20]
    emit(code, b"\x75\x05")                                # jne +5（不同 tick 通过）
    je_dedup_at = len(code)
    emit(code, b"\xE9\x00\x00\x00\x00")                    # jmp dedupskip（回填）
    emit(code, b"\xA3" + struct.pack("<I", region + DATA_SCRATCH + 0x20))      # [scratch+0x20]=tick（转换前写，崩也留痕）
    # PRE=0x11 待调
    emit(code, b"\xC7\x85" + struct.pack("<I", OFF_IM_PRE) + struct.pack("<I", 0x11))
    jmp_ch1a_at = jmp_ch1b_at = jmp_ch1c_at = None
    jmp_ch2a_at = jmp_ch2b_at = jmp_ch2c_at = None
    jmp_ch3a_at = jmp_ch3b_at = jmp_ch3c_at = None
    if direct:
        # ★v4 直接写登记门控三字节（★2026-08-17 修正：登记门控 = 6a0!=0 && byte[798]!=0 && byte[7a0]!=0，
        #   只写 6a0 不够——v3 数据 6a0==0 faction 大多 798=0 → 门控仍失败 = 鲁棒性问题根源；
        #   零调用零链依赖，登记自己判定前写字节）
        emit(code, b"\xC6\x83\xA0\x06\x00\x00\x01")        # mov byte[ebx+0x6a0],1
        emit(code, b"\xC6\x83\x98\x07\x00\x00\x01")        # mov byte[ebx+0x798],1
        emit(code, b"\xC6\x83\xA0\x07\x00\x00\x01")        # mov byte[ebx+0x7a0],1
    else:
        # 3 级链守卫（★B2 修复：每级 NULL + in-range(0x10000..0x80000000) 检查，任一失败 → chainskip；
        #   旧版只查 NULL 且去重在其后 → 非 NULL 垃圾 [+0x8c] → [eax+8] AV = seq14/seq66 崩因）
        emit(code, b"\x8B\x83\x8C\x00\x00\x00")            # mov eax,[ebx+0x8c]
        emit(code, b"\x85\xC0")
        emit(code, b"\x75\x05")                            # jnz +5（非 NULL 通过）
        jmp_ch1a_at = len(code)
        emit(code, b"\xE9\x00\x00\x00\x00")                # jmp chainskip（回填）
        emit(code, b"\x3D\x00\x00\x01\x00")                # cmp eax,0x10000
        emit(code, b"\x73\x05")                            # jae +5（>=0x10000 通过）
        jmp_ch1b_at = len(code)
        emit(code, b"\xE9\x00\x00\x00\x00")                # jmp chainskip（回填）
        emit(code, b"\x3D\x00\x00\x00\x80")                # cmp eax,0x80000000
        emit(code, b"\x72\x05")                            # jb +5（<0x80000000 通过）
        jmp_ch1c_at = len(code)
        emit(code, b"\xE9\x00\x00\x00\x00")                # jmp chainskip（回填）
        emit(code, b"\x8B\x40\x08")                        # mov eax,[eax+8]
        emit(code, b"\x85\xC0")
        emit(code, b"\x75\x05")
        jmp_ch2a_at = len(code)
        emit(code, b"\xE9\x00\x00\x00\x00")
        emit(code, b"\x3D\x00\x00\x01\x00")
        emit(code, b"\x73\x05")
        jmp_ch2b_at = len(code)
        emit(code, b"\xE9\x00\x00\x00\x00")
        emit(code, b"\x3D\x00\x00\x00\x80")
        emit(code, b"\x72\x05")
        jmp_ch2c_at = len(code)
        emit(code, b"\xE9\x00\x00\x00\x00")
        emit(code, b"\x8B\x80\x94\x14\x00\x00")            # mov eax,[eax+0x1494]
        emit(code, b"\x85\xC0")
        emit(code, b"\x75\x05")
        jmp_ch3a_at = len(code)
        emit(code, b"\xE9\x00\x00\x00\x00")
        emit(code, b"\x3D\x00\x00\x01\x00")
        emit(code, b"\x73\x05")
        jmp_ch3b_at = len(code)
        emit(code, b"\xE9\x00\x00\x00\x00")
        emit(code, b"\x3D\x00\x00\x00\x80")
        emit(code, b"\x72\x05")
        jmp_ch3c_at = len(code)
        emit(code, b"\xE9\x00\x00\x00\x00")
        emit(code, b"\x89\xD9")                            # mov ecx,ebx
        e_mov_edx_imm(code, base + FUN_C20)
        e_call_edx(code)
    emit(code, b"\xC7\x85" + struct.pack("<I", OFF_IM_C20) + struct.pack("<I", 1))
    emit(code, b"\xC7\x85" + struct.pack("<I", OFF_IM_POST) + struct.pack("<I", 0x21))
    emit(code, b"\xEB\x00")                                # jmp skip_c20（回填）
    jmp_done_at = len(code) - 1
    notfired_at = len(code)
    emit(code, b"\xC7\x85" + struct.pack("<I", OFF_IM_PRE) + struct.pack("<I", 0x13))
    emit(code, b"\xEB\x00")                                # jmp skip_c20（回填）
    jmp_nf_at = len(code) - 1
    chainskip_at = len(code)
    emit(code, b"\xC7\x85" + struct.pack("<I", OFF_IM_PRE) + struct.pack("<I", 0x12))
    emit(code, b"\xEB\x00")                                # jmp skip_c20（回填）
    jmp_cs_at = len(code) - 1
    dedupskip_at = len(code)
    emit(code, b"\xC7\x85" + struct.pack("<I", OFF_IM_PRE) + struct.pack("<I", 0x14))
    skip_c20_at = len(code)
    # 补齐后记录
    emit(code, b"\x0F\xB6\x83\xA0\x06\x00\x00")
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_IM_F6A0_A))
    emit(code, b"\x8B\x83\xBC\x06\x00\x00")
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_IM_F6BC_A))
    emit(code, b"\x8B\x83\xB8\x06\x00\x00")
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_IM_F6B8_A))
    emit(code, b"\x8B\x83\xD8\x06\x00\x00")
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_IM_F6D8_A))
    # 指针三件套（attacker/pending 0x5abf4d 处不可得 → 0；faction 有效）
    emit(code, b"\xC7\x85" + struct.pack("<I", OFF_IM_ATTACKER) + struct.pack("<I", 0))
    e_mov_eax_abs(code, m(0x10))
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_IM_FACTION))
    emit(code, b"\xC7\x85" + struct.pack("<I", OFF_IM_PENDING) + struct.pack("<I", 0))
    # 真实环记录（state=0，obj=faction，v0=[faction+0x6a0]，v1=[faction+0x798]，v2=[faction+0x7a0]）
    emit(code, b"\x89\xDE")                                # mov esi,ebx → esi=faction（obj）
    emit(code, b"\x0F\xB6\x83\xA0\x06\x00\x00")            # movzx eax,byte[ebx+0x6a0]
    emit(code, b"\xA3" + struct.pack("<I", m(0x14)))       # [scratch+0x14]=v0(6a0)
    emit(code, b"\x8B\x83\x98\x07\x00\x00")                # mov eax,[ebx+0x798]
    emit(code, b"\xA3" + struct.pack("<I", m(0x18)))       # [scratch+0x18]=v1(798)
    emit(code, b"\x8B\x83\xA0\x07\x00\x00")                # mov eax,[ebx+0x7a0]
    emit(code, b"\xA3" + struct.pack("<I", m(0x1c)))       # [scratch+0x1c]=v2(7a0)
    emit(code, b"\x31\xDB")                                # xor ebx,ebx  retaddr=0
    emit(code, b"\x31\xD2")                                # xor edx,edx  state=0
    save_dl(code, region)
    jmp_at = ring_preamble(code, region, base)
    restore_dl(code, region)
    emit_rec_fields(code, region, TAG_INJECT)
    skip_all_at = len(code)
    emit(code, b"\x61")                                    # popad
    emit(code, HOOKS[name]["orig"])                        # 重放 hook 原字节
    e_jmp_rel(code, back_addr, stub_addr + len(code))
    # 回填
    backfill_jmp(code, jmp_guard_at, skip_all_at)
    backfill_jmp(code, jmp2_at, skip_all_at)
    backfill_jmp(code, jmp3_at, skip_all_at)
    backfill_jmp(code, jmp4_at, skip_all_at)
    backfill_jmp(code, jz_inj_at, notfired_at)
    backfill_jmp(code, jne_c20_at, notfired_at)
    for _at in (jmp_ch1a_at, jmp_ch1b_at, jmp_ch1c_at, jmp_ch2a_at, jmp_ch2b_at, jmp_ch2c_at, jmp_ch3a_at, jmp_ch3b_at, jmp_ch3c_at):
        if _at is not None:
            backfill_jmp(code, _at, chainskip_at)
    backfill_jmp(code, je_dedup_at, dedupskip_at)
    code[jmp_done_at] = (skip_c20_at - (jmp_done_at + 1)) & 0xFF
    code[jmp_nf_at] = (skip_c20_at - (jmp_nf_at + 1)) & 0xFF
    code[jmp_cs_at] = (skip_c20_at - (jmp_cs_at + 1)) & 0xFF
    if jmp_at is not None:
        backfill_jmp(code, jmp_at, skip_all_at)
    return bytes(code)


def build_inject_stub(stub_addr, region, base, back_addr):
    return _build_conv_stub(stub_addr, region, base, back_addr, "inject", 0x1C)


def build_regconv_stub(stub_addr, region, base, back_addr):
    return _build_conv_stub(stub_addr, region, base, back_addr, "regconv", 0x00, direct=True)



def build_fork_stub(stub_addr, region, base, back_addr):
    """fork stub：hook 0x6045cc（mov ecx,ebx; movzx eax,[ebx+0x114]）。
    入口：ebx=pending、al=FUN_105caa60() 返回值（分叉决策）。"""
    m = lambda off: region + DATA_SCRATCH + off
    code = bytearray()
    emit(code, b"\x60")                                    # pushad
    emit(code, b"\x0F\xB6\xD0")                            # movzx edx,al  决策
    emit(code, b"\x89\xDE")                                # mov esi,ebx   pending（89 DE）
    emit(code, b"\x31\xDB")                                # xor ebx,ebx   retaddr=0
    save_dl(code, region)                                  # [scratch+0xc]=决策
    # v0=ready[+0x55] v1=b9[+0xb9] v2=participants[+0x7c]
    emit(code, b"\x0F\xB6\x86\x55\x00\x00\x00")
    emit(code, b"\xA3" + struct.pack("<I", m(0x14)))
    emit(code, b"\x0F\xB6\x86\xB9\x00\x00\x00")
    emit(code, b"\xA3" + struct.pack("<I", m(0x18)))
    emit(code, b"\x8B\x86\x7C\x00\x00\x00")
    emit(code, b"\xA3" + struct.pack("<I", m(0x1c)))
    # init
    emit(code, b"\xE8" + struct.pack("<I", (region + OFF_INIT - (stub_addr + len(code) + 5)) & 0xFFFFFFFF))
    jmp_at = ring_preamble(code, region, base)
    restore_dl(code, region)
    emit_rec_fields(code, region, TAG_FORK)
    skip_at = len(code)
    emit(code, b"\x61")                                    # popad
    emit(code, HOOKS["fork"]["orig"])                      # 重放 9 字节
    e_jmp_rel(code, back_addr, stub_addr + len(code))
    if jmp_at is not None:
        backfill_jmp(code, jmp_at, skip_at)
    return bytes(code)


def build_state_stub(stub_addr, region, base, back_addr):
    """state stub：hook FUN_1057fca0 入口（mov eax,[esp+4]; push esi）。
    入口：ecx=&obj+0x4c、[esp+4]=新状态、[esp]=返回地址。
    过滤：obj==pending_filter 或 状态∈{3,4} 才记录。"""
    m = lambda off: region + DATA_SCRATCH + off
    code = bytearray()
    emit(code, b"\x60")                                    # pushad
    emit(code, b"\x89\xCE")                                # mov esi,ecx
    emit(code, b"\x83\xEE\x4C")                            # sub esi,0x4c  esi=obj
    emit(code, b"\x8B\x5C\x24\x20")                        # mov ebx,[esp+0x20] retaddr
    emit(code, b"\x8B\x54\x24\x24")                        # mov edx,[esp+0x24] 状态
    save_dl(code, region)
    # v0=ready[+0x55] v1=b9[+0xb9] v2=d0[+0xd0]
    emit(code, b"\x0F\xB6\x86\x55\x00\x00\x00")
    emit(code, b"\xA3" + struct.pack("<I", m(0x14)))
    emit(code, b"\x0F\xB6\x86\xB9\x00\x00\x00")
    emit(code, b"\xA3" + struct.pack("<I", m(0x18)))
    emit(code, b"\x0F\xB6\x86\xD0\x00\x00\x00")
    emit(code, b"\xA3" + struct.pack("<I", m(0x1c)))
    # init + 过滤（filter = [marker+0x10] = 宿主当前 pending；记录条件 = pending==filter 或 state∈{3,4}）
    # ★修正：原版 mov eax,[marker] 读到 magic(0x50463130) 后当指针解引用 [eax+0x10] → AV；且两处 75 06 off-by-one
    emit(code, b"\xE8" + struct.pack("<I", (region + OFF_INIT - (stub_addr + len(code) + 5)) & 0xFFFFFFFF))
    e_mov_eax_abs(code, region + MARKER_OFF + 0x10)        # mov eax,[marker+0x10] = pending_filter
    emit(code, b"\x85\xC0")                                # test eax,eax
    emit(code, b"\x75\x05")                                # jnz +5 → filter_ok（跳过下方 5B jmp）
    jmp_skip1_at = len(code)
    emit(code, b"\xE9\x00\x00\x00\x00")                    # jmp skip（回填）
    filter_ok_at = len(code)
    emit(code, b"\x39\xC6")                                # cmp esi,eax
    je_at = len(code)
    emit(code, b"\x74\x00")                                # je do_rec
    emit(code, b"\x80\xFA\x04")                            # cmp dl,4
    je2_at = len(code)
    emit(code, b"\x74\x00")
    emit(code, b"\x80\xFA\x03")                            # cmp dl,3
    je3_at = len(code)
    emit(code, b"\x74\x00")
    emit(code, b"\xEB\x00")                                # jmp skip（回填）
    jmp_skip_at = len(code) - 1
    do_rec_at = len(code)
    jmp_at = ring_preamble(code, region, base)
    restore_dl(code, region)
    emit_rec_fields(code, region, TAG_STATE)
    skip_at = len(code)
    emit(code, b"\x61")                                    # popad
    emit(code, HOOKS["state"]["orig"])                     # 重放 8B 44 24 04 56
    e_jmp_rel(code, back_addr, stub_addr + len(code))
    # 回填
    backfill_jmp(code, jmp_skip1_at, skip_at)
    code[je_at + 1] = (do_rec_at - (je_at + 2)) & 0xFF
    code[je2_at + 1] = (do_rec_at - (je2_at + 2)) & 0xFF
    code[je3_at + 1] = (do_rec_at - (je3_at + 2)) & 0xFF
    code[jmp_skip_at] = (skip_at - (jmp_skip_at + 1)) & 0xFF
    if jmp_at is not None:
        backfill_jmp(code, jmp_at, skip_at)
    return bytes(code)


def build_factory_stub(stub_addr, region, base, back_addr):
    """factory stub：hook 0x1655c0 入口（push esi; push 1; mov esi,ecx）。
    入口：ecx=this（工厂类对象）、[esp]=返回地址。tag=4 面包屑。
    v0=[this+0x190]（env ctor 拷贝源向量 ptr）、v1=[this+0x194]（源 size）——配合
    F10 崩因确认（AI 内战源向量空 → [env+0x281e4]=NULL → 0x2a0477 解引用 [NULL+4] 崩）。"""
    m = lambda off: region + DATA_SCRATCH + off
    code = bytearray()
    emit(code, b"\x60")                                    # pushad
    emit(code, b"\x89\xCE")                                # mov esi,ecx
    emit(code, b"\x8B\x5C\x24\x20")                        # mov ebx,[esp+0x20] retaddr
    emit(code, b"\x31\xD2")                                # state=0
    save_dl(code, region)
    # v0=[esi+0x190] 源向量 ptr；v1=[esi+0x194] 源 size；v2=0
    emit(code, b"\x8B\x86\x90\x01\x00\x00")                # mov eax,[esi+0x190]
    emit(code, b"\xA3" + struct.pack("<I", m(0x14)))
    emit(code, b"\x8B\x86\x94\x01\x00\x00")                # mov eax,[esi+0x194]
    emit(code, b"\xA3" + struct.pack("<I", m(0x18)))
    emit(code, b"\xC7\x05" + struct.pack("<I", m(0x1c)) + struct.pack("<I", 0))
    emit(code, b"\xE8" + struct.pack("<I", (region + OFF_INIT - (stub_addr + len(code) + 5)) & 0xFFFFFFFF))
    jmp_at = ring_preamble(code, region, base)
    restore_dl(code, region)
    emit_rec_fields(code, region, TAG_FACTORY)
    skip_at = len(code)
    emit(code, b"\x61")                                    # popad
    emit(code, HOOKS["factory"]["orig"])                   # 重放 56 6A 01 8B F1
    e_jmp_rel(code, back_addr, stub_addr + len(code))
    if jmp_at is not None:
        backfill_jmp(code, jmp_at, skip_at)
    return bytes(code)


def build_envdisp_stub(stub_addr, region, base, back_addr):
    """envdisp stub：hook FUN_102a0450 入口（0x2a0450，83 EC 38 53 8B D9，back 0x2a0456）。
    入口：ecx=env（thiscall）。★被动裁决记录（零游戏函数调用、零分配）——SA-B/SA-C 裁决实验核心：
    - 面包屑（state=0xFF, obj=env, v0=env）
    - envdisp meta（marker+OFF_ED_*）：env / 表 cap,size,data / IsSpectator / [env+0x28064] / wrapper / st / [st+0x88] / [st+0x8c]
    - 真实记录（state=0, obj=env, v0=表data, v1=[st+0x8c], v2=IsSpectator）
    一次实机裁决「表空 vs 军队数组空」+ 崩点归属（0x2a0477 vs 0x2a0480）。"""
    m = lambda off: region + DATA_SCRATCH + off
    ed = lambda off: region + MARKER_OFF + off
    code = bytearray()
    emit(code, b"\x60")                                    # pushad
    emit(code, b"\x8B\x44\x24\x18")                        # mov eax,[esp+0x18] env(=ECX)
    emit(code, b"\xA3" + struct.pack("<I", m(0x10)))       # [scratch+0x10]=env
    # env 守卫：NULL / <0x10000 / >=0x80000000 → skip
    emit(code, b"\x85\xC0")                                # test eax,eax
    emit(code, b"\x75\x05")                                # jnz +5
    j1_at = len(code)
    emit(code, b"\xE9\x00\x00\x00\x00")                    # jmp skip
    emit(code, b"\x3D\x00\x00\x01\x00")                    # cmp eax,0x10000
    emit(code, b"\x73\x05")                                # jae +5
    j2_at = len(code)
    emit(code, b"\xE9\x00\x00\x00\x00")                    # jmp skip
    emit(code, b"\x3D\x00\x00\x00\x80")                    # cmp eax,0x80000000
    emit(code, b"\x72\x05")                                # jb +5
    j3_at = len(code)
    emit(code, b"\xE9\x00\x00\x00\x00")                    # jmp skip
    # ebp = marker（meta 写用）
    e_mov_eax_imm(code, region + MARKER_OFF)
    emit(code, b"\x89\xC5")                                # mov ebp,eax
    # 面包屑（state=0xFF, obj=env）
    emit(code, b"\x31\xDB")                                # xor ebx,ebx
    emit(code, b"\x89\xC6")                                # mov esi,eax  esi=env(obj)
    emit(code, b"\xB2\xFF")                                # mov dl,0xFF
    save_dl(code, region)
    ring_preamble(code, region, base)
    restore_dl(code, region)
    emit_rec_fields(code, region, TAG_ENVDISP)
    # 字段读取（env 已验证；单层解引用）
    e_mov_eax_abs(code, m(0x10))                           # eax = env
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_ED_ENV))  # [marker+ED_ENV]=env
    emit(code, b"\x8B\x90\xE4\x81\x02\x00")                # mov edx,[eax+0x281e4] 表data
    emit(code, b"\x89\x15" + struct.pack("<I", m(0x14)))   # [scratch+0x14]=v0(表data)
    emit(code, b"\x89\x95" + struct.pack("<I", OFF_ED_TABLE))
    emit(code, b"\x8B\x90\xDC\x81\x02\x00")                # mov edx,[eax+0x281dc] cap
    emit(code, b"\x89\x95" + struct.pack("<I", OFF_ED_CAP))
    emit(code, b"\x8B\x90\xE0\x81\x02\x00")                # mov edx,[eax+0x281e0] size
    emit(code, b"\x89\x95" + struct.pack("<I", OFF_ED_SIZE))
    emit(code, b"\x0F\xB6\x90\xE8\x81\x02\x00")            # movzx edx,byte[eax+0x281e8] IsSpectator
    emit(code, b"\x89\x15" + struct.pack("<I", m(0x1c)))   # [scratch+0x1c]=v2(issp)
    emit(code, b"\x89\x95" + struct.pack("<I", OFF_ED_ISSP))
    emit(code, b"\x8B\x90\x64\x80\x02\x00")                # mov edx,[eax+0x28064] env军队数
    emit(code, b"\x89\x95" + struct.pack("<I", OFF_ED_CNT64))
    emit(code, b"\x8B\x90\x1C\x83\x02\x00")                # mov edx,[eax+0x2831c] wrapper槽
    emit(code, b"\x89\x95" + struct.pack("<I", OFF_ED_WRAP))
    # st = [[env+8]+0xb4]（带守卫，短跳直连 st_fail 回填式：任一失败 → st_fail）；失败 → v1=0
    emit(code, b"\x8B\x40\x08")                            # mov eax,[env+8] 容器
    emit(code, b"\x85\xC0")
    jz_sf1_at = len(code)
    emit(code, b"\x74\x00")                                # jz st_fail（回填）
    emit(code, b"\x3D\x00\x00\x01\x00")                    # cmp eax,0x10000
    jb_sf1_at = len(code)
    emit(code, b"\x72\x00")                                # jb st_fail（回填）
    emit(code, b"\x3D\x00\x00\x00\x80")                    # cmp eax,0x80000000
    jae_sf1_at = len(code)
    emit(code, b"\x73\x00")                                # jae st_fail（回填）
    emit(code, b"\x8B\x40\xB4")                            # mov eax,[容器+0xb4] st
    emit(code, b"\x85\xC0")
    jz_sf2_at = len(code)
    emit(code, b"\x74\x00")                                # jz st_fail（回填）
    emit(code, b"\x3D\x00\x00\x01\x00")
    jb_sf2_at = len(code)
    emit(code, b"\x72\x00")                                # jb st_fail（回填）
    emit(code, b"\x3D\x00\x00\x00\x80")
    jae_sf2_at = len(code)
    emit(code, b"\x73\x00")                                # jae st_fail（回填）
    emit(code, b"\x89\x85" + struct.pack("<I", OFF_ED_ST))  # [marker+ED_ST]=st
    emit(code, b"\x8B\x50\x88")                            # mov edx,[st+0x88] 军队计数
    emit(code, b"\x89\x95" + struct.pack("<I", OFF_ED_ST88))
    emit(code, b"\x8B\x50\x8C")                            # mov edx,[st+0x8c] 军队数组base
    emit(code, b"\x89\x15" + struct.pack("<I", m(0x18)))   # [scratch+0x18]=v1(军队数组)
    emit(code, b"\x89\x95" + struct.pack("<I", OFF_ED_ST8C))
    emit(code, b"\xEB\x00")                                # jmp st_done（回填）
    st_jmp_at = len(code) - 1
    st_fail_at = len(code)
    emit(code, b"\xC7\x05" + struct.pack("<I", m(0x18)) + struct.pack("<I", 0))  # v1=0
    st_done_at = len(code)
    # 真实记录（state=0, obj=env）
    emit(code, b"\x31\xDB")                                # xor ebx,ebx
    e_mov_eax_abs(code, m(0x10))
    emit(code, b"\x89\xC6")                                # mov esi,eax  obj=env
    emit(code, b"\x31\xD2")                                # xor edx,edx  state=0
    save_dl(code, region)
    ring_preamble(code, region, base)
    restore_dl(code, region)
    emit_rec_fields(code, region, TAG_ENVDISP)
    skip_at = len(code)
    emit(code, b"\x61")                                    # popad
    emit(code, HOOKS["envdisp"]["orig"])                   # 重放 83 EC 38 53 8B D9
    e_jmp_rel(code, back_addr, stub_addr + len(code))
    # 回填
    backfill_jmp(code, j1_at, skip_at)
    backfill_jmp(code, j2_at, skip_at)
    backfill_jmp(code, j3_at, skip_at)
    code[jz_sf1_at + 1] = (st_fail_at - (jz_sf1_at + 2)) & 0xFF
    code[jb_sf1_at + 1] = (st_fail_at - (jb_sf1_at + 2)) & 0xFF
    code[jae_sf1_at + 1] = (st_fail_at - (jae_sf1_at + 2)) & 0xFF
    code[jz_sf2_at + 1] = (st_fail_at - (jz_sf2_at + 2)) & 0xFF
    code[jb_sf2_at + 1] = (st_fail_at - (jb_sf2_at + 2)) & 0xFF
    code[jae_sf2_at + 1] = (st_fail_at - (jae_sf2_at + 2)) & 0xFF
    code[st_jmp_at] = (st_done_at - (st_jmp_at + 1)) & 0xFF
    return bytes(code)


def build_inject_stub_writefile(stub_addr, region, base, back_addr):
    """备选：WriteFile 直写落盘变体（inject stub 专用，低频）。
    首调：CreateFileA(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, OPEN_ALWAYS) → SetFilePointer(FILE_END)。
    每调：WriteFile(记录)。宿主离线解析（v2 槽 = REC_MAGIC_WF 校验，容忍尾部半条）。"""
    m = lambda off: region + DATA_SCRATCH + off
    code = bytearray()
    emit(code, b"\x60")
    emit(code, b"\x8B\x44\x24\x34")
    emit(code, b"\xA3" + struct.pack("<I", m(0x14)))       # v0=attacker
    emit(code, b"\x8B\x44\x24\x1C")
    emit(code, b"\xA3" + struct.pack("<I", m(0x04)))       # pending
    emit(code, b"\x8B\x44\x24\x20")
    emit(code, b"\xA3" + struct.pack("<I", m(0x08)))       # retaddr
    emit(code, b"\x31\xD2")
    save_dl(code, region)
    # 句柄缓存（region+DATA_HMAP，0 = 未开）
    e_mov_eax_abs(code, region + DATA_HMAP)
    emit(code, b"\x85\xC0")
    jz_at = len(code)
    emit(code, b"\x74\x00")                                # jz need_open
    emit(code, b"\xEB\x00")                                # jmp have_handle（回填）
    jmp_at = len(code) - 1
    need_open_at = len(code)
    # CreateFileA(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL)
    e_push_imm(code, 0)
    e_push_imm(code, 0x80)                                 # FILE_ATTRIBUTE_NORMAL
    e_push_imm(code, 4)                                    # OPEN_ALWAYS
    e_push_imm(code, 0)
    e_push_imm(code, 1)                                    # FILE_SHARE_READ
    e_push_imm(code, 0x40000000)                           # GENERIC_WRITE
    e_push_imm(code, region + DATA_LOGPATH)
    e_mov_eax_abs(code, base + IAT_CreateFileA)
    e_call_eax(code)
    emit(code, b"\x83\xC4\x1C")                            # add esp,0x1c
    emit(code, b"\x83\xF8\xFF")                            # cmp eax,-1
    jz2_at = len(code)
    emit(code, b"\x74\x00")                                # jz skip_all
    emit(code, b"\xA3" + struct.pack("<I", region + DATA_HMAP))
    # SetFilePointer(h, 0, NULL, FILE_END)
    e_push_imm(code, 2)
    e_push_imm(code, 0)
    e_push_imm(code, 0)
    emit(code, b"\x50")                                    # push eax(h)
    e_mov_eax_abs(code, base + IAT_SetFilePointer)
    e_call_eax(code)
    emit(code, b"\x83\xC4\x10")
    have_handle_at = len(code)
    # 组记录到 scratch（REC_SIZE=0x20；v1 槽 = retaddr，v2 槽 = magic）
    emit(code, b"\x8B\x44\x24\x20")                        # mov eax,[esp+0x20] retaddr → v1
    emit(code, b"\xA3" + struct.pack("<I", m(0x18)))
    emit(code, b"\xC7\x05" + struct.pack("<I", m(0x1c)) + struct.pack("<I", REC_MAGIC_WF))
    # WriteFile(h, scratch, 0x20, &written, NULL)
    e_push_imm(code, 0)
    e_push_imm(code, region + DATA_SCRATCH + 0x24)         # &written（scratch 后 4 字节）
    e_push_imm(code, REC_SIZE)
    e_push_imm(code, region + DATA_SCRATCH)
    e_mov_eax_abs(code, region + DATA_HMAP)
    emit(code, b"\x50")                                    # push h
    e_mov_eax_abs(code, base + IAT_WriteFile)
    e_call_eax(code)
    emit(code, b"\x83\xC4\x14")
    skip_all_at = len(code)
    emit(code, b"\x61")
    emit(code, HOOKS["inject"]["orig"])
    e_jmp_rel(code, back_addr, stub_addr + len(code))
    code[jz_at + 1] = (need_open_at - (jz_at + 2)) & 0xFF
    code[jmp_at] = (have_handle_at - (jmp_at + 1)) & 0xFF
    code[jz2_at + 1] = (skip_all_at - (jz2_at + 2)) & 0xFF
    return bytes(code)


# ───────────────────────── offer 链观测 stub（★2026-08-18 新增，纯记录零注入） ─────────────────────────
def build_offer_stub(stub_addr, region, base, back_addr):
    """offer stub：hook FUN_1085b290 入口（0x85b2d0，sub esp,0x24; push ebx; push ebp）。
    ★解析层 offer 创建门（F 报告：0x85b309 cmp [faction+0x6a0],0 前、0x85b304 call getter 前）
    = 「AI 进攻 + 人类在场 → 战前选择界面」的决定性检测函数。
    入口：ecx=this（解析器上下文）、[esp]=返回地址、[esp+4]=arg1（参与者对象指针，
    游戏 0x85b2d5 mov ebp,[esp+0x30] 在 sub 0x24+push ebx+push ebp 后 = 原 arg1，+0x5c 经
    getter 0x3e5960 → faction；0x85b309 判 [faction+0x6a0]）。
    纯观测：记录 {obj=this, v0=arg1(参与者对象), v1=retaddr, v2=0}，不修改任何状态。"""
    m = lambda off: region + DATA_SCRATCH + off
    code = bytearray()
    emit(code, b"\x60")                                    # pushad
    emit(code, b"\x89\xCE")                                # mov esi,ecx  this(解析器)=obj
    emit(code, b"\x8B\x5C\x24\x20")                        # mov ebx,[esp+0x20] retaddr
    emit(code, b"\x31\xD2")                                # state=0
    save_dl(code, region)
    emit(code, b"\x8B\x44\x24\x24")                        # mov eax,[esp+0x24] arg1=参与者对象
    emit(code, b"\xA3" + struct.pack("<I", m(0x14)))       # v0=arg1
    emit(code, b"\x31\xC0")
    emit(code, b"\xA3" + struct.pack("<I", m(0x18)))       # v1=0
    emit(code, b"\x31\xC0")
    emit(code, b"\xA3" + struct.pack("<I", m(0x1c)))       # v2=0
    emit(code, b"\xE8" + struct.pack("<I", (region + OFF_INIT - (stub_addr + len(code) + 5)) & 0xFFFFFFFF))
    jmp_at = ring_preamble(code, region, base)
    restore_dl(code, region)
    emit_rec_fields(code, region, TAG_OFFER)
    skip_at = len(code)
    emit(code, b"\x61")                                    # popad
    emit(code, HOOKS["offer"]["orig"])                     # 重放 83 EC 24 53 55
    e_jmp_rel(code, back_addr, stub_addr + len(code))
    if jmp_at is not None:
        backfill_jmp(code, jmp_at, skip_at)
    return bytes(code)


def build_offerctor_stub(stub_addr, region, base, back_addr):
    """offerctor stub：hook FUN_106e50b0 入口（0x6e50b0，sub esp,0x10; push ebx; push ebp）。
    ★offer 构造器（F 报告：new(0x18) + vtable 0x1160bbbc + 注册事件 0x133）= 战前界面触发确认。
    入口：ecx=this、[esp]=返回地址、[esp+4]=arg1、[esp+8]=arg2、[esp+0xc]=arg3
    （构造器内 [ebx+4]=FUN_105d7f40(arg1,0) faction、[ebx+8]=arg3 aggressor）。
    纯观测：记录 {obj=this, v0=arg1, v1=arg2, v2=arg3}。"""
    m = lambda off: region + DATA_SCRATCH + off
    code = bytearray()
    emit(code, b"\x60")                                    # pushad
    emit(code, b"\x89\xCE")                                # mov esi,ecx  this=obj
    emit(code, b"\x8B\x5C\x24\x20")                        # mov ebx,[esp+0x20] retaddr
    emit(code, b"\x31\xD2")                                # state=0
    save_dl(code, region)
    emit(code, b"\x8B\x44\x24\x24")                        # eax=[esp+0x24] arg1
    emit(code, b"\xA3" + struct.pack("<I", m(0x14)))       # v0=arg1
    emit(code, b"\x8B\x44\x24\x28")                        # eax=[esp+0x28] arg2
    emit(code, b"\xA3" + struct.pack("<I", m(0x18)))       # v1=arg2
    emit(code, b"\x8B\x44\x24\x2C")                        # eax=[esp+0x2c] arg3
    emit(code, b"\xA3" + struct.pack("<I", m(0x1c)))       # v2=arg3
    emit(code, b"\xE8" + struct.pack("<I", (region + OFF_INIT - (stub_addr + len(code) + 5)) & 0xFFFFFFFF))
    jmp_at = ring_preamble(code, region, base)
    restore_dl(code, region)
    emit_rec_fields(code, region, TAG_OFFERCTOR)
    skip_at = len(code)
    emit(code, b"\x61")                                    # popad
    emit(code, HOOKS["offerctor"]["orig"])                 # 重放 83 EC 10 53 55
    e_jmp_rel(code, back_addr, stub_addr + len(code))
    if jmp_at is not None:
        backfill_jmp(code, jmp_at, skip_at)
    return bytes(code)


# ───────────────────────── 组装 + 数据段 ─────────────────────────
def build_data(region, logpath):
    """数据区 0x2800..0x2900（view/flag/inject/hmap/scratch 全零；K32/VEHN 字符串；默认注入开）。"""
    data = bytearray(0x100)
    struct.pack_into("<I", data, DATA_INJECT - 0x2800, 1)   # 默认注入开（宿主可改 0 纯观测）
    k = b"kernel32.dll\x00"
    data[DATA_K32 - 0x2800:DATA_K32 - 0x2800 + len(k)] = k
    v = b"AddVectoredExceptionHandler\x00"
    data[DATA_VEHN - 0x2800:DATA_VEHN - 0x2800 + len(v)] = v
    return bytes(data)


def build_marker_init(region):
    """marker 初始化（region+MARKER_OFF，大小 0x2200：header + 事件环）。"""
    m = bytearray(0x2200)
    struct.pack_into("<II", m, OFF_MAGIC, HDR_MAGIC, HDR_VERSION)
    return bytes(m)


def build_all(region, base, logpath):
    """返回 {name: (stub_addr, stub_bytes)} + init + veh + data + marker_init"""
    out = {}
    offs = {"inject": OFF_INJECT, "fork": OFF_FORK, "state": OFF_STATE, "factory": OFF_FACTORY,
            "envdisp": OFF_ENVDISP, "regconv": OFF_REGCONV, "offer": OFF_OFFER, "offerctor": OFF_OFFERCTOR}
    for name, off in offs.items():
        stub_addr = region + off
        back = base + HOOKS[name]["back"]
        if name == "inject":
            stub = build_inject_stub(stub_addr, region, base, back)
        elif name == "fork":
            stub = build_fork_stub(stub_addr, region, base, back)
        elif name == "state":
            stub = build_state_stub(stub_addr, region, base, back)
        elif name == "envdisp":
            stub = build_envdisp_stub(stub_addr, region, base, back)
        elif name == "regconv":
            stub = build_regconv_stub(stub_addr, region, base, back)
        elif name == "offer":
            stub = build_offer_stub(stub_addr, region, base, back)
        elif name == "offerctor":
            stub = build_offerctor_stub(stub_addr, region, base, back)
        else:
            stub = build_factory_stub(stub_addr, region, base, back)
        out[name] = (stub_addr, stub)
    init = build_init_sub(region + OFF_INIT, region, base)
    veh = build_veh_handler(region + OFF_VEH)
    data = build_data(region, logpath)
    marker = build_marker_init(region)
    return out, init, veh, data, marker


# ───────────────────────── 离线校验（capstone） ─────────────────────────
def validate(stubs, init, veh, region, base):
    try:
        from capstone import Cs, CS_ARCH_X86, CS_MODE_32
    except ImportError:
        print("capstone 不可用，跳过字节级校验")
        return True
    md = Cs(CS_ARCH_X86, CS_MODE_32)
    md.detail = True
    ok = True

    def check(label, blob, addr, expect_back=None):
        nonlocal ok
        insns = list(md.disasm(blob, addr))
        if not insns:
            print(f"  ❌ {label}: 无法反汇编"); ok = False; return
        if insns[0].mnemonic not in ("pushad", "pushal") and label != "init":
            print(f"  ❌ {label}: 首指令 {insns[0].mnemonic} 非 pushad"); ok = False
        last = insns[-1]
        if expect_back is not None:
            if last.mnemonic != "jmp":
                print(f"  ❌ {label}: 末指令 {last.mnemonic} 非 jmp"); ok = False
            else:
                e9 = blob.rfind(b"\xE9")
                rel = struct.unpack_from("<i", blob, e9 + 1)[0]
                tgt = (addr + e9 + 5 + rel) & 0xFFFFFFFF
                if tgt != expect_back:
                    print(f"  ❌ {label}: 末 jmp → {tgt:#x} 期望 back {expect_back:#x}"); ok = False
                else:
                    print(f"  ✅ {label}(len {len(blob)}): 末 jmp → back {expect_back:#x}")
        else:
            if last.mnemonic != "ret":
                print(f"  ❌ {label}: 末指令 {last.mnemonic} 非 ret"); ok = False
            else:
                print(f"  ✅ {label}(len {len(blob)}): 末指令 ret")
        # jcc 目标必须落在指令边界（★2026-08-17 实机教训：75 06 off-by-one 落到指令第二字节
        # 如 89 C3 的 C3=RET / 8B 40 10 的 40=inc eax → 崩；旧校验只查「范围内」漏过）
        starts = {ins.address for ins in insns}
        for ins in insns:
            if ins.mnemonic in ("jz", "je", "jne", "jnz", "jmp", "jb"):
                tgt = ins.operands[0].imm if ins.operands else None
                if tgt is None:
                    continue
                if tgt < addr or tgt >= addr + len(blob):
                    if expect_back is not None and tgt == expect_back:
                        continue
                    print(f"  ❌ {label}: {ins.mnemonic}@{ins.address:#x} 目标 {tgt:#x} 越界"); ok = False
                elif tgt not in starts:
                    print(f"  ❌ {label}: {ins.mnemonic}@{ins.address:#x} 目标 {tgt:#x} 不在指令边界（落到指令中间）"); ok = False
        # 重放字节存在性
        for name, hk in HOOKS.items():
            if name in stubs and hk["orig"] in blob:
                print(f"  ✅ {label}: 含 {name} 原字节重放 {hk['orig'].hex()}")
        return

    print("=== capstone 校验 ===")
    for name, (addr, blob) in stubs.items():
        check(name, blob, addr, base + HOOKS[name]["back"])
    check("init", init, region + OFF_INIT)
    check("veh", veh, region + OFF_VEH)

    # ★B4 修复（2026-08-17）：绝对地址/调用目标必须落在 stub 区或 DLL 镜像内。
    #   旧校验只查 jcc 边界/重放字节 → B1 double-add（call 0x6C5E0C20 超出镜像）全检通过。
    #   检查模式：BA imm32 FF D2（mov edx,imm; call edx）/ A1 imm32 FF D0（mov eax,[abs]; call eax）/
    #   A1 imm32 / 89 15 imm32 / A3 imm32 / C7 05 imm32 / 3B 05 imm32（绝对读/写/比较）。
    try:
        from re_lib import PE
        _pe = PE()
        _img_max = max(va + vsize for _, va, _, _, vsize in _pe.sections) - _pe.image_base
    except Exception:
        _pe, _img_max = None, 0
    if _pe:
        def _in_image(imm):
            if region <= imm < region + REGION_SIZE:
                return True
            return 0 <= imm - _pe.image_base < _img_max
        for _name, (_addr, _blob) in stubs.items():
            _b = _blob
            for _i in range(len(_b) - 6):
                _imm = None
                if _b[_i] == 0xBA and _b[_i + 5] == 0xFF and _b[_i + 6] == 0xD2:
                    _imm = struct.unpack_from("<I", _b, _i + 1)[0]   # call edx 目标
                elif _b[_i] == 0xA1 and _b[_i + 5] == 0xFF and _b[_i + 6] == 0xD0:
                    _imm = struct.unpack_from("<I", _b, _i + 1)[0]   # mov eax,[abs]; call eax
                elif _b[_i] == 0xA1:
                    _imm = struct.unpack_from("<I", _b, _i + 1)[0]   # mov eax,[abs]
                elif _b[_i] == 0x89 and _b[_i + 1] == 0x15:
                    _imm = struct.unpack_from("<I", _b, _i + 2)[0]   # mov [abs],edx
                elif _b[_i] == 0xA3:
                    _imm = struct.unpack_from("<I", _b, _i + 1)[0]   # mov [abs],eax
                elif _b[_i] == 0xC7 and _b[_i + 1] == 0x05:
                    _imm = struct.unpack_from("<I", _b, _i + 2)[0]   # mov dword[abs],imm
                elif _b[_i] == 0x3B and _b[_i + 1] == 0x05:
                    _imm = struct.unpack_from("<I", _b, _i + 2)[0]   # cmp eax,[abs]
                if _imm is not None and not _in_image(_imm):
                    print(f"  ❌ {_name}: 绝对地址 {_imm:#x}（RVA {_imm - _pe.image_base:#x}）超出镜像（SizeOfImage≈{_img_max:#x}）")
                    ok = False
    print("校验结论:", "✅ 全部通过" if ok else "❌ 存在失败项")
    return ok


def layout_dump(stubs, init, veh, data, region):
    print("\n=== 布局 dump ===")
    print(f"region={region:#x} size=0x{REGION_SIZE:x}")
    for name, (addr, blob) in stubs.items():
        print(f"  {name:9s} @ {addr:#x}  len=0x{len(blob):x} ({len(blob)})")
    print(f"  init     @ {region+OFF_INIT:#x}  len=0x{len(init):x}")
    print(f"  veh      @ {region+OFF_VEH:#x}  len=0x{len(veh):x}")
    print(f"  data     @ {region+OFF_DATA:#x}  len=0x{len(data):x}")
    total = sum(len(b) for _, b in stubs.values()) + len(init) + len(veh) + len(data)
    print(f"  合计 {total:#x} 字节 < region {REGION_SIZE:#x}: {'✅' if total < REGION_SIZE else '❌'}")


# ───────────────────────── dry-run：marker 环协议回环验证（无需游戏） ─────────────────────────
def dry_run_ring():
    """用本地 bytearray 模拟 marker，验证环协议（槽位计算/回绕/字段布局）与 stub 写入语义一致。"""
    arr = bytearray(0x2200)
    struct.pack_into("<II", arr, OFF_MAGIC, HDR_MAGIC, HDR_VERSION)

    def stub_append(tag, state, obj, retaddr, v0, v1, v2):
        count = struct.unpack_from("<I", arr, OFF_COUNT)[0]
        slot = count & (RING_SLOTS - 1)
        rec = RING_OFF + slot * REC_SIZE
        struct.pack_into("<IIBBHIIIII", arr, rec, count, 0x1234, tag, state, 0, retaddr, obj, v0, v1, v2)
        struct.pack_into("<I", arr, OFF_COUNT, count + 1)
        struct.pack_into("<B", arr, OFF_LAST_TAG, tag)

    stub_append(TAG_INJECT, 0, 0x12345000, 0xdeadbeef, 0x11111111, 0x22222222, 0x33333333)
    stub_append(TAG_FORK, 1, 0x12345000, 0, 1, 0, 2)
    stub_append(TAG_STATE, 4, 0x12345000, 0x5abf3d, 1, 0, 0)
    for i in range(RING_SLOTS + 2):                        # 回绕
        stub_append(TAG_FACTORY, 0, 0x99990000 + i, 0, 0, 0, 0)
    count = struct.unpack_from("<I", arr, OFF_COUNT)[0]
    ok = True
    if count != RING_SLOTS + 5:
        print(f"  ❌ count={count} 期望 {RING_SLOTS+5}"); ok = False
    # 回绕语义：槽 0 被 seq=256 覆盖（256 & 0xFF == 0）
    rec0 = struct.unpack_from("<IIBBHIIIII", arr, RING_OFF + 0)
    if rec0[0] != 256:
        print(f"  ❌ 槽0 seq={rec0[0]} 期望 256（回绕覆盖）"); ok = False
    # 末条 = seq 260（count-1）→ 槽 4
    slot = (count - 1) & (RING_SLOTS - 1)
    rec = struct.unpack_from("<IIBBHIIIII", arr, RING_OFF + slot * REC_SIZE)
    if rec[0] != count - 1 or rec[2] != TAG_FACTORY:
        print(f"  ❌ 末条 {rec}"); ok = False
    # 语义回读：seq=10 → 槽 10
    rec10 = struct.unpack_from("<IIBBHIIIII", arr, RING_OFF + 10 * REC_SIZE)
    if rec10[0] != 10 or rec10[2] != TAG_FACTORY:
        print(f"  ❌ seq10 {rec10}"); ok = False
    print(f"dry-run marker 环回环: count={count} 槽0seq={rec0[0]} 末条seq={rec[0]} tag={rec[2]} → {'✅' if ok else '❌'}")
    return ok


# ───────────────────────── main ─────────────────────────
def main():
    base = IMAGE_BASE
    region = 0x70000000                                   # 任意占位（安装时 = VirtualAllocEx 实际地址）
    logpath = r"D:\Projects\Python\AccessTest\shogun2_ai_battle\captures\h47a\f10_stub_wf.bin"
    stubs, init, veh, data, marker = build_all(region, base, logpath)
    layout_dump(stubs, init, veh, data, region)
    ok = validate(stubs, init, veh, region, base)
    if "--dry-run" in sys.argv:
        print("\n=== dry-run 环协议回环 ===")
        ok = dry_run_ring() and ok
    if "--writefile" in sys.argv:
        print("\n=== WriteFile 变体 inject stub ===")
        wf = build_inject_stub_writefile(region + OFF_INJECT, region, base, base + HOOKS["inject"]["back"])
        print(f"  len={len(wf)}")
        try:
            from capstone import Cs, CS_ARCH_X86, CS_MODE_32
            md = Cs(CS_ARCH_X86, CS_MODE_32)
            for ins in md.disasm(wf, region + OFF_INJECT):
                print(f"  {ins.address:#x}: {ins.mnemonic} {ins.op_str}")
        except ImportError:
            pass
        if b"\xE9" not in wf:
            ok = False
    print("\n总结果:", "✅ OK" if ok else "❌ FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
