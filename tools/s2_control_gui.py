# -*- coding: utf-8 -*-
"""s2_control_gui.py — 幕府2 控制台（加钱 + AI化/人控 + 战斗AI注入 + 看海捕捉）独立 GUI（2026-08-12）

功能（复用已验证工具：s2_watch / s2_money 机制 / s2_ai_ctl / s2_spectate）：
  1. 连接游戏（自动检测 shogun2.exe + 引擎 build）
  2. 扫描派系列表（名称 / 人类 / 国库）
  3. 加钱：选中派系国库写指定金额（faction+0x4fc，UI 即时生效）
  4. AI 化（看海）：+0x6a0=0 + manager=FULL_MANAGER（CAI 接管跑回合）
  5. 恢复人控：+0x6a0=1 + manager=HUMAN
  6. 战斗 AI 注入（s2_ai_ctl）：全员AI / 自动托管 / 切回人控（战斗场景内）
  7. 看海捕捉（s2_spectate）：类型/规模/阵营/PRE单位数 筛 → 写 b9 加载 AI 内战

打包：pyinstaller --noconfirm s2_control.spec
运行：需管理员权限（OpenProcess 写内存）+ 游戏运行中（战役/战斗）。
"""
import ctypes
import os
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb
import s2_watch as sw
import s2_ai_ctl as ctl
import re_b3_inject as b3
import battle_ai_ctl as ba
import s2_spectate as spec
import _g5_relation_write as g5


class App:
    def __init__(self, root):
        self.root = root
        root.title("幕府2 控制台 — 加钱 / AI化 / 战斗注入 / 看海捕捉")
        root.geometry("1100x700")
        root.minsize(900, 600)   # ★2026-08-19 最小尺寸（缩放不截断控件）
        self.h = None
        self.base = None
        self.build = None           # 模块名：empire(6262)/shogun2(6118/6115)
        self.engine = None          # 生效引擎 '6262'/'6118'/'6115'（自动检测 or 手动选择）
        self.facs = []          # [(addr, human, name, treasury)]
        self.religions = {}     # addr → 阵营/宗教显示名（扫描时读 faction+0x6d4）
        self.sel = None         # 选中派系 (addr, human, name, treasury)
        self._mgr_cache = {}    # faction_addr → manager 写点（同会话缓存，AI化/恢复提速）
        self._aiified = set()   # 本会话 GUI AI化 过的 faction 地址（恢复人控唯一许可集；P42 纪律）
        self._battle_stop = None    # 全员AI/自动托管 的停止事件
        self._battle_thread = None  # 当前战斗 AI 监控线程
        self._dyn_max_total = 0     # 动态决战：已捕捉最大 PRE 总单位数（不区分类型）
        self._dyn_max_field = 0     # 野战动态基准
        self._dyn_max_siege = 0     # 攻城动态基准

        # --- 顶部：连接 + 扫描 + 引擎 ---
        top = ttk.Frame(root, padding=6)
        top.pack(fill=tk.X)
        ttk.Button(top, text="连接游戏", command=self.cmd_connect).pack(side=tk.LEFT)
        ttk.Button(top, text="刷新派系", command=self.cmd_scan).pack(side=tk.LEFT, padx=4)
        self.lbl_status = ttk.Label(top, text="未连接")
        self.lbl_status.pack(side=tk.LEFT, padx=8)
        # ★引擎选择（自动 = 按运行模块检测；手动 = 覆盖/无法识别时备用）
        ttk.Label(top, text="引擎:").pack(side=tk.LEFT, padx=(12, 2))
        self.var_engine = tk.StringVar(value="自动")
        self.cmb_engine = ttk.Combobox(top, textvariable=self.var_engine, width=10, state="readonly",
                                       values=("自动", "6262 新引擎", "6118 旧引擎", "6115 旧引擎"))
        self.cmb_engine.pack(side=tk.LEFT)
        self.cmb_engine.bind("<<ComboboxSelected>>", self._on_engine_pick)

        # --- 派系列表（★白名单 / 黑名单列） ---
        mid = ttk.Frame(root, padding=6)
        mid.pack(fill=tk.BOTH, expand=True)
        cols = ("name", "religion", "human", "treasury", "wl", "bl")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=10,
                                 selectmode="extended")   # ★多选（批量白/黑名单）
        self.tree.heading("name", text="派系")
        self.tree.heading("religion", text="阵营/宗教")
        self.tree.heading("human", text="人类")
        self.tree.heading("treasury", text="国库")
        self.tree.heading("wl", text="白名单")
        self.tree.heading("bl", text="黑名单")
        self.tree.column("name", width=130)
        self.tree.column("religion", width=90, anchor=tk.CENTER)
        self.tree.column("human", width=50)
        self.tree.column("treasury", width=80)
        self.tree.column("wl", width=50, anchor=tk.CENTER)
        self.tree.column("bl", width=50, anchor=tk.CENTER)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", self.toggle_whitelist)   # ★双击 = 快速标记白名单
        self.whitelist = set()                                  # 白名单派系名集合（空=全部捕捉）
        self.blacklist = set()                                  # 黑名单派系名集合（空=不排除）

        # --- 操作区（每功能一行） ---
        ops = ttk.Frame(root, padding=6)
        ops.pack(fill=tk.X)

        # row0 金钱
        ttk.Label(ops, text="国库金额:").grid(row=0, column=0, sticky=tk.W)
        self.var_money = tk.StringVar(value="50000")
        ttk.Entry(ops, textvariable=self.var_money, width=12).grid(row=0, column=1)
        ttk.Button(ops, text="设为国库", command=self.cmd_set_money).grid(row=0, column=2, padx=4)

        # row1 AI化
        ttk.Button(ops, text="AI 化(看海)", command=self.cmd_ai_ify).grid(row=1, column=1, padx=4)
        ttk.Button(ops, text="恢复人控", command=self.cmd_restore).grid(row=1, column=2, padx=4)

        # row2 战斗AI注入
        ttk.Label(ops, text="战斗AI注入:").grid(row=2, column=0, sticky=tk.W)
        ttk.Button(ops, text="全员AI", command=lambda: self.cmd_battle("all-ai")).grid(row=2, column=1)
        ttk.Button(ops, text="自动托管", command=lambda: self.cmd_battle("auto")).grid(row=2, column=2, padx=4)
        ttk.Button(ops, text="切回人控", command=lambda: self.cmd_battle("human")).grid(row=2, column=3, padx=4)
        ttk.Button(ops, text="停止监控", command=self.cmd_battle_stop).grid(row=2, column=5, padx=4)
        ttk.Label(ops, text="（战斗场景内生效）").grid(row=2, column=4)

        # row3 battle_ai
        ttk.Label(ops, text="battle_ai:").grid(row=3, column=0, sticky=tk.W)
        ttk.Button(ops, text="注入", command=lambda: self.cmd_ba("inject")).grid(row=3, column=1)
        ttk.Button(ops, text="取消", command=lambda: self.cmd_ba("cancel")).grid(row=3, column=2, padx=4)
        ttk.Button(ops, text="状态", command=lambda: self.cmd_ba("status")).grid(row=3, column=3, padx=4)
        ttk.Label(ops, text="（战前/战役地图写，战斗加载时消费；重启游戏需重写）").grid(row=3, column=4)

        # --- ★看海捕捉（类型 / PRE单位数 / 白名单/黑名单 / 操作） ---
        # row4 类型多选
        ttk.Label(ops, text="看海捕捉-类型:").grid(row=4, column=0, sticky=tk.W)
        self.var_ct = {"siege": tk.BooleanVar(value=True), "field": tk.BooleanVar(value=False),
                       "naval": tk.BooleanVar(value=False)}
        ttk.Checkbutton(ops, text="攻城", variable=self.var_ct["siege"]).grid(row=4, column=1)
        ttk.Checkbutton(ops, text="野战", variable=self.var_ct["field"]).grid(row=4, column=2)
        ttk.Checkbutton(ops, text="海战", variable=self.var_ct["naval"]).grid(row=4, column=3)
        ttk.Label(ops, text="（全不选/全选=全捕捉）").grid(row=4, column=4, columnspan=3, sticky=tk.W)

        # row5 PRE 单位数精确筛（Goal3.1；hdr+0x18/+0x78 含援军）
        # ★2026-08-29 实机验证：PRE 判定与 POST Σ[army+0x114] 一致；海战字段为 0/0，需配海战类型筛跳过
        ttk.Label(ops, text="PRE单位数:").grid(row=5, column=0, sticky=tk.W)
        ttk.Label(ops, text="总≥").grid(row=5, column=1, sticky=tk.E)
        self.var_pre_total = tk.StringVar(value="0")
        ttk.Entry(ops, textvariable=self.var_pre_total, width=5).grid(row=5, column=2, sticky=tk.W)
        ttk.Label(ops, text="每方≥").grid(row=5, column=3, sticky=tk.E)
        self.var_pre_per_side = tk.StringVar(value="0")
        ttk.Entry(ops, textvariable=self.var_pre_per_side, width=5).grid(row=5, column=4, sticky=tk.W)
        self.var_dyn = tk.BooleanVar(value=False)   # ★动态决战：80% 最大规模下限 + 双方 1:2
        ttk.Checkbutton(ops, text="动态决战", variable=self.var_dyn).grid(row=5, column=5, padx=6)
        self.lbl_dyn = ttk.Label(ops, text="基准:0 下限:0")
        self.lbl_dyn.grid(row=5, column=6, sticky=tk.W)
        ttk.Button(ops, text="重置基准", command=self.cmd_dyn_reset).grid(row=5, column=7, padx=4)
        self.var_split = tk.BooleanVar(value=False)  # ★按类型区分野战/攻城
        ttk.Checkbutton(ops, text="按类型区分", variable=self.var_split,
                        command=self._toggle_split).grid(row=5, column=8, padx=6)
        ttk.Label(ops, text="（80%+1:2；海战0/0用类型筛）").grid(row=5, column=9, columnspan=2, sticky=tk.W)

        # row6 分类型筛选（默认隐藏；勾选“按类型区分”后显示）
        self._split_widgets = []
        _sw = self._split_widgets
        _w = ttk.Label(ops, text="分类型:")
        _w.grid(row=6, column=0, sticky=tk.W); _sw.append(_w)
        _w = ttk.Label(ops, text="野总≥")
        _w.grid(row=6, column=1, sticky=tk.E); _sw.append(_w)
        self.var_pre_total_field = tk.StringVar(value="0")
        _w = ttk.Entry(ops, textvariable=self.var_pre_total_field, width=5)
        _w.grid(row=6, column=2, sticky=tk.W); _sw.append(_w)
        _w = ttk.Label(ops, text="野每方≥")
        _w.grid(row=6, column=3, sticky=tk.E); _sw.append(_w)
        self.var_pre_per_side_field = tk.StringVar(value="0")
        _w = ttk.Entry(ops, textvariable=self.var_pre_per_side_field, width=5)
        _w.grid(row=6, column=4, sticky=tk.W); _sw.append(_w)
        _w = ttk.Label(ops, text="城总≥")
        _w.grid(row=6, column=5, sticky=tk.E); _sw.append(_w)
        self.var_pre_total_siege = tk.StringVar(value="0")
        _w = ttk.Entry(ops, textvariable=self.var_pre_total_siege, width=5)
        _w.grid(row=6, column=6, sticky=tk.W); _sw.append(_w)
        _w = ttk.Label(ops, text="城每方≥")
        _w.grid(row=6, column=7, sticky=tk.E); _sw.append(_w)
        self.var_pre_per_side_siege = tk.StringVar(value="0")
        _w = ttk.Entry(ops, textvariable=self.var_pre_per_side_siege, width=5)
        _w.grid(row=6, column=8, sticky=tk.W); _sw.append(_w)
        _w = ttk.Label(ops, text="（勾选后野战/攻城分开筛选与动态基准）")
        _w.grid(row=6, column=9, columnspan=2, sticky=tk.W); _sw.append(_w)
        for _w in self._split_widgets:
            _w.grid_remove()

        # row7 白名单
        ttk.Label(ops, text="白名单:").grid(row=7, column=0, sticky=tk.W)
        ttk.Button(ops, text="标记选中", command=self.mark_whitelist_sel).grid(row=7, column=1)
        ttk.Button(ops, text="全选", command=self.whitelist_all).grid(row=7, column=2, padx=2)
        ttk.Button(ops, text="清空", command=self.clear_whitelist).grid(row=7, column=3, padx=2)
        ttk.Label(ops, text="（列表多选/Ctrl/Shift + 标记选中；无白名单=全部捕捉）").grid(row=7, column=4, columnspan=4, sticky=tk.W)

        # row8 黑名单
        ttk.Label(ops, text="黑名单:").grid(row=8, column=0, sticky=tk.W)
        ttk.Button(ops, text="标记选中", command=self.mark_blacklist_sel).grid(row=8, column=1)
        ttk.Button(ops, text="全选", command=self.blacklist_all).grid(row=8, column=2, padx=2)
        ttk.Button(ops, text="清空", command=self.clear_blacklist).grid(row=8, column=3, padx=2)
        ttk.Label(ops, text="（黑名单派系参与的战斗不捕捉；白名单与黑名单同时存在时黑名单优先）").grid(row=8, column=4, columnspan=4, sticky=tk.W)

        # row9 操作 + 自动ESC
        ttk.Label(ops, text="操作:").grid(row=9, column=0, sticky=tk.W)
        ttk.Button(ops, text="开始捕捉", command=self.cmd_spectate_start).grid(row=9, column=1, padx=4)
        ttk.Button(ops, text="停止", command=self.cmd_spectate_stop).grid(row=9, column=2)
        self.var_esc = tk.BooleanVar(value=False)   # ★默认关（用户否决后置 ESC）
        ttk.Checkbutton(ops, text="自动ESC", variable=self.var_esc).grid(row=9, column=3, padx=6)

        # row10 小地图全开（Goal5 L28）
        ttk.Label(ops, text="小地图全开:").grid(row=10, column=0, sticky=tk.W)
        ttk.Button(ops, text="全量known开图", command=lambda: self.cmd_g5("apply")).grid(row=10, column=1)
        ttk.Button(ops, text="恢复known", command=lambda: self.cmd_g5("restore")).grid(row=10, column=2, padx=4)
        ttk.Button(ops, text="状态", command=lambda: self.cmd_g5("status")).grid(row=10, column=3, padx=4)
        ttk.Label(ops, text="（写全量 relation+0x7e0=1；写后需手动切一次小地图标签触发重建）").grid(row=10, column=4, columnspan=6, sticky=tk.W)

        # --- 日志 ---
        self.logbox = scrolledtext.ScrolledText(root, height=12, state="disabled",
                                                font=("Consolas", 9))
        self.logbox.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    # ---------- 工具 ----------
    def log(self, msg):
        def _w():
            self.logbox.config(state="normal")
            self.logbox.insert(tk.END, msg + "\n")
            self.logbox.see(tk.END)
            self.logbox.config(state="disabled")
        self.root.after(0, _w)

    def set_status(self, s):
        self.root.after(0, lambda: self.lbl_status.config(text=s))

    def thread(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _require_game(self):
        if not self.h:
            self.log("✗ 未连接游戏（先点「连接游戏」）")
            return False
        return True

    def _require_sel(self):
        if not self.sel:
            self.log("✗ 未选中派系（先在列表选择）")
            return False
        return True

    def _engine_choice(self):
        """手动选择的引擎（'6262'/'6118'/'6115'）；'自动' → None。"""
        v = self.var_engine.get()
        if v.startswith("6262"):
            return "6262"
        if v.startswith("6118"):
            return "6118"
        if v.startswith("6115"):
            return "6115"
        return None

    def _detected_engine(self):
        """按运行进程识别的引擎（empire=6262；shogun2=读 BUILD 串区分 6115/6118）。"""
        if not self.h or not self.build:
            return None
        try:
            det, err = spec.resolve_engine_for_module(self.h, self.build)
            return det
        except Exception:
            return None

    def _on_engine_pick(self, _evt=None):
        """手动改引擎：若已连接且与当前检测冲突 → 提示重连（自动模式始终以检测为准）。"""
        eng = self._engine_choice()
        det = self._detected_engine() if self.build else None
        if eng and det and eng != det:
            self.log(f"⚠️ 手动引擎 {eng} ≠ 检测 {det}——请重连应用；冲突时连接会被拒绝（防错偏移写坏）")
        elif eng:
            self.log(f"▶ 引擎已设为 {eng}，点「连接游戏」应用")
        else:
            self.log("▶ 引擎已设为自动（连接时按运行模块+BUILD 串识别）")

    def _resolve_engine(self, build):
        """返回 (engine, note)。note 非空=需展示给用户的提示。
        规则：自动 → 按运行进程识别；手动 → 必须与检测一致，否则拒绝（防错偏移）。
        无法识别且未手动选择 → 拒绝并提示手动选择。"""
        det = self._detected_engine()
        manual = self._engine_choice()
        if det is None:
            if manual is None:
                return None, f"无法识别引擎模块 build={build!r}——请在上方「引擎」手动选 6262/6118/6115 后重连"
            return manual, f"模块识别失败（build={build!r}），按手动引擎 {manual} 继续（请确认版本）"
        if manual is not None and manual != det:
            return None, f"检测引擎 {det}({build}) 与你手动选的 {manual} 冲突——已拒绝连接（避免按错偏移写坏内存）。请设「自动」或选 {det}"
        if manual == det:
            return det, f"手动引擎 {manual} 与检测一致"
        return det, None

    def cmd_connect(self):
        self.thread(self._connect)

    def _connect(self):
        try:
            pid = pb.find_pid()
            if pid is None:
                self.log("✗ shogun2.exe 未运行（请先启动游戏）")
                return
            h = pb.K32.OpenProcess(pb.PROCESS_QUERY_INFORMATION | pb.PROCESS_VM_READ |
                                   pb.PROCESS_VM_WRITE | pb.PROCESS_VM_OPERATION, False, pid)
            if not h:
                self.log(f"✗ OpenProcess 失败 err={ctypes.get_last_error()}（需管理员权限）")
                return
            build, base, prof = pb.detect_build(h)
            if base is None:
                pb.K32.CloseHandle(h)
                self.log("✗ 未找到引擎模块（empire.retail.dll / shogun2.dll）")
                return
            self.h = h                    # ★先挂句柄：_resolve_engine 的检测（BUILD 串/磁盘）需要它
            self.base = base
            self.build = build
            # ★引擎裁决：自动检测为主；手动须与检测一致；模块无法识别 → 提示手动
            eng, note = self._resolve_engine(build)
            if eng is None:
                pb.K32.CloseHandle(h)
                self.h = None
                self.log(f"✗ 引擎冲突/未识别：{note}")
                self.set_status("未连接（引擎待定）")
                return
            self.engine = eng
            self._mgr_cache = {}
            self._aiified = set()
            # ★引擎装配：按裁决引擎切换 spec（s2_spectate）全局常量（6262 默认 / 6118 读 profile）
            try:
                ename, eerr = spec.apply_engine(eng)
                if eerr:
                    self.log(f"⚠️ 引擎常量装配部分失败（{eerr}），后续 6118 专属功能可能不可用")
                else:
                    self.log(f"✓ 引擎常量已装配：{ename}")
            except Exception as e:
                self.log(f"⚠️ 引擎装配异常: {e}")
            # ★战斗托管链锚点装配（battle_mgr 槽 / battle_ai 锚点按引擎区分）
            try:
                b3.set_engine(eng)
                self.log(f"✓ 战斗锚点已装配：引擎 {eng} battle_mgr槽=0x{b3.RVA_MGR:x}")
            except Exception as e:
                self.log(f"⚠️ 战斗锚点装配异常: {e}")
            self.set_status(f"PID={pid} 引擎={eng} ({build}) base=0x{base:08x}")
            self.log(f"✓ 已连接 PID={pid} 引擎={eng}（模块 {build}）base=0x{base:08x}" + (f"｜{note}" if note else ""))
            if eng in ("6118", "6115"):
                self.log(f"ℹ️ 旧引擎 {eng} 路径：AI化/恢复走 cm→obj_A 链；小地图全开走 faction 池→cm→relation；"
                         "战斗托管（全员AI/自动托管/切回人控）走各自 battle_mgr 槽（6115=静态候选，进真实战斗后确认）")
        except Exception as e:
            self.log(f"✗ 连接异常: {e}")

    def cmd_scan(self):
        if not self._require_game():
            return
        self.thread(self._scan)

    def _scan(self):
        try:
            # ★引擎 faction vtable 参数化：6118 主基 0x1594178（spec.apply_engine 已按 build 重绑）
            fvt = spec.FACTION_VTABLE_RVA
            facs = sw.scan_factions(self.h, self.base, faction_vtable_rva=fvt)
            self.facs = facs
            self.religions = {fa: spec.faction_religion_display(self.h, fa) for fa, _, _, _ in facs}
            self.root.after(0, self._fill_tree)
            self.log(f"✓ 扫描到 {len(facs)} 个派系")
        except Exception as e:
            self.log(f"✗ 扫描异常: {e}")

    def _fill_tree(self):
        try:
            self.tree.delete(*self.tree.get_children())
            seen = set()
            for addr, h6, name, tr in self.facs:
                if addr in seen:      # ★2026-08-19 去重（扫描窗口重叠可能重复 addr → iid 冲突闪退）
                    continue
                seen.add(addr)
                tag = "human" if h6 == 1 else ""
                wl = "★" if name in self.whitelist else ""
                bl = "✕" if name in self.blacklist else ""
                self.tree.insert("", tk.END, iid=str(addr), tags=(tag,),
                                 values=(name or "?", self.religions.get(addr, "?"),
                                         "★" if h6 else "-", tr, wl, bl))
            self.tree.tag_configure("human", background="#ffe9c9")
        except Exception as e:
            self.log(f"✗ 填充派系列表异常: {e}")

    def toggle_whitelist(self, _evt=None):
        """双击列表行 = 快速标记/取消白名单（看海捕捉阵营筛选用）"""
        sel = self.tree.selection()
        if not sel:
            return
        for fa, h6, name, tr in self.facs:
            if int(sel[0]) == fa and name:
                if name in self.whitelist:
                    self.whitelist.discard(name)
                    self.log(f"✕ 白名单移除 {name}")
                else:
                    self.whitelist.add(name)
                    self.log(f"★ 白名单添加 {name}")
                self._fill_tree()
                return

    def clear_whitelist(self):
        self.whitelist.clear()
        self._fill_tree()
        self.log("白名单已清空（=全部捕捉）")

    def mark_whitelist_sel(self):
        """★批量白名单：多选列表行 → 全部加入白名单"""
        sels = self.tree.selection()
        if not sels:
            self.log("⚠️ 未选中行（可 Ctrl/Shift 多选）")
            return
        n = 0
        for sid in sels:
            for fa, h6, name, tr in self.facs:
                if int(sid) == fa and name:
                    if name not in self.whitelist:
                        self.whitelist.add(name)
                        n += 1
                    break
        self._fill_tree()
        self.log(f"★ 白名单批量添加 {n} 个（当前 {len(self.whitelist)} 个：{'/'.join(sorted(self.whitelist))}）")

    def whitelist_all(self):
        """全选白名单（所有有名 faction）"""
        self.whitelist = {n for _, _, n, _ in self.facs if n}
        self._fill_tree()
        self.log(f"★ 白名单全选（{len(self.whitelist)} 个，即全部捕捉）")

    def clear_blacklist(self):
        self.blacklist.clear()
        self._fill_tree()
        self.log("黑名单已清空（=不排除任何派系）")

    def mark_blacklist_sel(self):
        """批量黑名单：多选列表行 → 全部加入黑名单"""
        sels = self.tree.selection()
        if not sels:
            self.log("⚠️ 未选中行（可 Ctrl/Shift 多选）")
            return
        n = 0
        for sid in sels:
            for fa, h6, name, tr in self.facs:
                if int(sid) == fa and name:
                    if name not in self.blacklist:
                        self.blacklist.add(name)
                        n += 1
                    break
        self._fill_tree()
        self.log(f"✕ 黑名单批量添加 {n} 个（当前 {len(self.blacklist)} 个：{'/'.join(sorted(self.blacklist))}）")

    def blacklist_all(self):
        """全选黑名单（所有有名 faction）"""
        self.blacklist = {n for _, _, n, _ in self.facs if n}
        self._fill_tree()
        self.log(f"✕ 黑名单全选（{len(self.blacklist)} 个，即全部排除）")

    def toggle_blacklist(self, _evt=None):
        """双击列表行 = 快速标记/取消黑名单（当前未绑定，保留备用）"""
        sel = self.tree.selection()
        if not sel:
            return
        for fa, h6, name, tr in self.facs:
            if int(sel[0]) == fa and name:
                if name in self.blacklist:
                    self.blacklist.discard(name)
                    self.log(f"○ 黑名单移除 {name}")
                else:
                    self.blacklist.add(name)
                    self.log(f"✕ 黑名单添加 {name}")
                self._fill_tree()
                return

    def on_select(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        addr = int(sel[0])
        for fa, h6, name, tr in self.facs:
            if fa == addr:
                self.sel = (fa, h6, name, tr)
                self.log(f"▶ 选中 {name!r} 0x{fa:08x} human={h6} 国库={tr}")
                return

    # ---------- 加钱 ----------
    def cmd_set_money(self):
        if not self._require_game() or not self._require_sel():
            return
        try:
            amt = int(self.var_money.get())
        except ValueError:
            self.log("✗ 金额无效")
            return
        self.thread(self._set_money, amt)

    def _set_money(self, amt):
        try:
            fa, h6, name, tr = self.sel
            buf = ctypes.create_string_buffer(struct.pack("<I", amt))
            got = ctypes.c_size_t()
            ok = pb.K32.WriteProcessMemory(self.h, ctypes.c_void_p(fa + 0x4fc), buf, 4,
                                           ctypes.byref(got))
            back = pb.read_u32(self.h, fa + 0x4fc)
            ok = ok and got.value == 4 and back == amt
            self.log(f"国库 {name!r}: {tr} → {amt} 回读={back} {'✅' if ok else '✗'}")
            self.set_status(f"{name} 国库={back}")
        except Exception as e:
            self.log(f"✗ 加钱异常: {e}（游戏重启/进程变更会失效，请重新连接）")

    # ---------- AI化 / 恢复（定位 manager 表；引擎分流） ----------
    def _locate_manager(self, fa):
        """引擎分流：6262 = 全堆 scan_objA（现役已验证）；6118 = cm 槽链 + 修正版全堆兜底。"""
        if getattr(self, "engine", None) in ("6118", "6115"):
            return self._locate_manager_6118(fa)
        return self._locate_manager_6262(fa)

    def _locate_manager_6262(self, fa):
        """6262：定位 manager 写点（scan_objA + find_manager_entry），返回 mgr_target 或 None。
        同会话缓存：首次全内存扫描定位后缓存，AI化/恢复连续操作毫秒级。
        缓存复用前校验 faction 对象仍有效（vtable 匹配），失效则重新定位。"""
        # 缓存优先
        cached = self._mgr_cache.get(fa)
        if cached:
            # ★2026-08-30 修复：缓存不能只看 faction vtable，还要验证 manager 条目本身仍有效。
            # 长会话多次强制加载后 manager 表可能重分配，旧 mgr_tgt 写入会 0xC0000005。
            if pb.read_u32(self.h, fa) == self.base + sw.VTABLE_RVA:
                try:
                    key = pb.read_u32(self.h, cached - 4)          # 条目 key 指针
                    key_ok = bool(key) and pb.read_u32(self.h, key + 0x164) == fa
                    mval = pb.read_u32(self.h, cached)             # 当前 manager 值
                    mval_ok = mval is not None and 0 <= mval <= 7
                    if key_ok and mval_ok:
                        self.log("⚡ 缓存命中 manager 写点（条目校验通过，免扫描）")
                        return cached
                    self.log("⚠️ manager 缓存条目已失效（key/值校验失败），重新定位")
                except Exception:
                    self.log("⚠️ manager 缓存读取异常，重新定位")
            self._mgr_cache.pop(fa, None)
        objs = sw.scan_objA(self.h, self.base, max_cands=10)
        if not objs:
            self.log("✗ manager 表未定位（可能不在战役）")
            return None
        objs.sort(key=lambda x: -x[3])
        objA, cnt, tbl, nh = objs[0]
        entry = sw.find_manager_entry(self.h, objA, tbl, cnt, fa)
        if not entry:
            self.log("✗ 未找到该派系 manager 条目")
            return None
        idx, key, m = entry
        mgr_tgt = tbl + idx * 8 + 4
        self._mgr_cache[fa] = mgr_tgt
        self.log(f"✓ manager 定位成功（缓存，下次免扫描）")
        return mgr_tgt

    def _cache_ok_6118(self, fa, tgt):
        """6118 缓存校验：faction +0x6a0 合法 + 条目 key+0x164→fa + m∈[0,9]。"""
        try:
            if pb.read_u8(self.h, fa + sw.OFF_HUMAN) not in (0, 1):
                return False
            key = pb.read_u32(self.h, tgt - 4)
            if not key or pb.read_u32(self.h, key + sw.KEY_BACK) != fa:
                return False
            m = pb.read_u32(self.h, tgt)
            if m is None or not (0 <= m <= 9):
                return False
            return True
        except Exception:
            return False

    def _locate_manager_6118(self, fa):
        """6118：cm 槽链 [cm+0x14b0] → obj_A（快，audit §I 实机定案）；失败 → 修正版全堆兜底
        （cnt∈[20,2000]@+0x648 + 真实 faction 集合行验证，修 6262 scan_objA 的假阴性源）。"""
        cached = self._mgr_cache.get(fa)
        if cached and self._cache_ok_6118(fa, cached):
            self.log("⚡ 6118 缓存命中 manager 写点（条目校验通过）")
            return cached
        fvt = spec.FACTION_VTABLE_RVA
        try:
            r = sw.locate_manager_table_cm(self.h, self.base, fvt)
            cands = [r] if r else sw.scan_manager_table_heap(self.h, self.base, fvt)
        except Exception as e:
            self.log(f"✗ 6118 manager 定位异常: {e}")
            return None
        if not cands:
            self.log("✗ 6118 manager 表未定位（cm 槽链 + 全堆兜底均未命中）——确认在战役地图后重试")
            return None
        for c in cands:
            entry = sw.find_manager_entry(self.h, c["objA"], c["tbl"], c["cnt"], fa)
            if entry:
                idx, key, m = entry
                mgr_tgt = c["tbl"] + idx * 8 + 4
                self._mgr_cache[fa] = mgr_tgt
                self.log(f"✓ 6118 manager[{idx}] 定位（obj_A=0x{c['objA']:08x} cnt={c['cnt']} "
                         f"hit={c.get('hit_ratio')} m={m}）缓存命中点=0x{mgr_tgt:08x}")
                return mgr_tgt
        self.log("✗ 表中未找到该派系条目（对象已 churn？刷新派系后重试）")
        return None

    def cmd_ai_ify(self):
        if not self._require_game() or not self._require_sel():
            return
        self.thread(self._ai_ify)

    def _ai_ify(self):
        try:
            fa, h6, name, tr = self.sel
            if h6 != 1:
                # ★6118 同名歧义教训：AI 副本/同名派系禁止 AI化（无意义且会污染 _aiified → 误判可恢复）
                self.log(f"✗ AI化 {name!r} 拒绝：human={h6}（AI化只对人类派系有意义；"
                         f"同名/重复派系请选列表里 ★ 高亮的人类那行）")
                return
            self.log(f"▶ AI化 {name!r} 定位 manager 表…")
            mgr_tgt = self._locate_manager(fa)
            if mgr_tgt is None:
                self.log("✗ AI化失败：manager 表未定位——请确认已进入战役后重试（刷新派系/重新连接）")
                return
            ok = sw.do_watch(self.h, self.base, fa, mgr_tgt)
            if ok:
                self._aiified.add(fa)
                self.log(f"✅ AI化(看海) {name!r}：+0x6a0=0 + FULL_MANAGER（可点「恢复人控」还原）")
            else:
                self.log(f"⚠️ AI化 {name!r} 部分失败，检查回读")
            self.set_status(f"{name} 已 AI化 human=0")
        except Exception as e:
            self.log(f"✗ AI化异常: {e}（游戏重启/进程变更会失效，请重新连接）")

    def cmd_restore(self):
        if not self._require_game() or not self._require_sel():
            return
        self.thread(self._restore)

    def _restore(self):
        try:
            fa, h6, name, tr = self.sel
            if h6 == 1:
                self.log(f"ℹ️ {name!r} 已是人控（human=1），无需恢复")
                return
            if fa not in self._aiified:
                # ★P42 纪律：给从未人控的 AI 派系伪造 HUMAN 完整状态集会崩（local player 冲突）
                self.log(f"✗ 恢复人控 {name!r} 拒绝：该派系不是本会话 GUI AI化 的（human=0）。"
                         f"P42：AI→人类伪造非法会崩。若要用 CLI 工具 AI化 过，请用该工具 --restore。")
                return
            self.log(f"▶ 恢复人控 {name!r} 定位 manager 表…")
            mgr_tgt = self._locate_manager(fa)
            if mgr_tgt is None:
                self.log("✗ manager 表未定位——请确认在战役地图后重试")
                return
            ok = sw.do_restore(self.h, self.base, fa, mgr_tgt)
            if ok:
                self._aiified.discard(fa)
                self.log(f"✅ 恢复人控 {name!r}：+0x6a0=1 + HUMAN")
            else:
                self.log(f"⚠️ 恢复 {name!r} 部分失败，检查回读")
            self.set_status(f"{name} 已恢复 human=1")
        except Exception as e:
            self.log(f"✗ 恢复异常: {e}（游戏重启/进程变更会失效，请重新连接）")

    # ---------- 战斗 AI 注入 ----------
    def cmd_battle(self, mode):
        if not self._require_game():
            return
        if getattr(self, "engine", None) in ("6118", "6115"):
            # ★2026-09-05 迁移：战斗托管链锚点已按引擎装配（b3.set_engine，_connect 时执行）。
            # 6115/6118 battle_mgr 槽各自的写入安全性由 resolve_e8 链 fail-closed 保证：
            # 槽错/非战斗 → 链解析失败 → 拒绝写（不会误写）。
            self.log(f"▶ 旧引擎 {self.engine} 战斗托管：走 b3 引擎化 battle_mgr 槽 "
                     f"(0x{b3.RVA_MGR:x})——若在战斗内仍提示未进入，则该 build 槽未实机确认")
        if mode in ("all-ai", "auto"):
            if self._battle_thread and self._battle_thread.is_alive():
                self.log("⚠️ 战斗 AI 监控已在运行（先点「停止监控」或「切回人控」）")
                return
            self._battle_stop = threading.Event()
            self._battle_thread = threading.Thread(
                target=self._battle_run, args=(mode, self._battle_stop), daemon=True)
            self._battle_thread.start()
        elif mode == "human":
            self.log("▶ 切回人控：先停止监控，再执行完整关闭序列…")
            self._stop_battle_monitor()
            self.thread(self._battle_run, "human", None)
        else:
            self.log(f"✗ 未知战斗模式 {mode}")

    def _battle_run(self, mode, stop_event):
        try:
            if mode == "all-ai":
                self.log("全员 AI 托管 + 援军监控（点「停止监控」或「切回人控」停止）")
                rc = ctl.cmd_all_ai(self.h, self.base, stop_event=stop_event)
            elif mode == "auto":
                self.log("自动托管：监测战斗状态自动接管（点「停止监控」停止）")
                rc = ctl.cmd_auto(self.h, self.base, stop_event=stop_event)
            elif mode == "human":
                self.log("执行切回人控完整关闭序列…")
                rc = ctl.cmd_human(self.h, self.base)
            else:
                return
            self.log(f"战斗注入返回 {rc}")
        except Exception as e:
            self.log(f"✗ 战斗注入异常: {e}")
        finally:
            if mode in ("all-ai", "auto") and self._battle_thread is threading.current_thread():
                self._battle_thread = None

    def _stop_battle_monitor(self, wait=2.0):
        """停止当前全员AI/自动托管监控线程（有运行中才置事件并 join）。"""
        t, e = self._battle_thread, self._battle_stop
        if t is not None and t.is_alive():
            if e is not None:
                e.set()
            t.join(timeout=wait)
            if t.is_alive():
                self.log("⚠️ 监控线程未在超时内退出（可能正阻塞在 sleep，稍后会自动退出）")
            else:
                self.log("⏹ 战斗 AI 监控已停止")
        elif e is not None:
            e.set()
        self._battle_thread = None
        self._battle_stop = None

    def cmd_battle_stop(self):
        if not self._battle_thread or not self._battle_thread.is_alive():
            self.log("当前没有运行中的战斗 AI 监控")
            return
        self._stop_battle_monitor()

    # ---------- battle_ai 注入 / 取消 / 状态 ----------
    def cmd_ba(self, mode):
        if not self._require_game():
            return
        if getattr(self, "engine", None) in ("6118", "6115"):
            # ★2026-09-05 迁移：battle_ai 对象锚点已引擎化（battle_ai_ctl._anchors 按 build 区分；
            # 6115 obj 0x19865a0 / vt 0x1460ab8 实机校准通过）。写入前仍双重校准（vtable+value_id）。
            self.log(f"▶ 旧引擎 {self.engine} battle_ai：走引擎化锚点，写前校准 vtable/value_id")
        self.thread(self._ba, mode)

    def _ba(self, mode):
        try:
            if mode == "status":
                d = ba.describe(self.h, self.base)
                if not d["ok"]:
                    self.log("✗ battle_ai 校准失败（vtable/value_id 不符）——检查引擎 build / 锚点")
                    return
                self.log(f"battle_ai 状态: {ba.fmt_status(d)} → "
                         f"当前 {'注入中' if d['value'] else '未注入'}")
                return
            on = (mode == "inject")
            ok, d = ba.set_battle_ai(self.h, self.base, on)
            if not d["ok"]:
                self.log("✗ battle_ai 校准失败（vtable/value_id 不符）——拒绝写入")
                return
            act = "注入 battle_ai=1" if on else "取消 battle_ai=0"
            if ok:
                self.log(f"✅ {act}: set=0x{d['rb_set']:02x} value=0x{d['rb_value']:02x} 回读验证通过")
            else:
                self.log(f"✗ {act} 写入失败/回读不符: set=0x{d['rb_set']:02x} "
                         f"value=0x{d['rb_value']:02x} (w1={d.get('w1')} w2={d.get('w2')})")
        except Exception as e:
            self.log(f"✗ battle_ai 异常: {e}")

    # ---------- ★小地图全开（Goal5 L28；引擎分流：6118 走 faction 池→cm→relation 链） ----------
    def cmd_g5(self, mode):
        if not self._require_game():
            return
        self.thread(self._g5, mode)

    def _g5_6118_local(self):
        """6118 local faction：faction 池扫 + human==1（无 6262 session 锚，cm+0xb0=0）。"""
        facs = sw.scan_factions(self.h, self.base, faction_vtable_rva=spec.FACTION_VTABLE_RVA)
        local = next((a for a, h6, _n, _t in facs if h6 == 1), None)
        if local is None:
            self.log("✗ 未找到 human==1 faction（看海纯 AI 态无 local？）")
        return local

    def _g5_6118(self, mode):
        try:
            if mode == "apply":
                local = self._g5_6118_local()
                if not local:
                    return
                self.log("▶ 6118 小地图全开：faction 池→cm→全 faction relation known=1（含备份）…")
                entries = g5.collect_all_entries(self.h, self.base, local)
                if not entries:
                    self.log("✗ relation 枚举失败（cm+0x1498 链）")
                    return
                rc = g5.write_entries(self.h, entries, "all-dm-6118-gui",
                                      local=local, dm=None, base=self.base)
                self.log("✅ 6118 全量 known 已写入（自动备份已落盘）。请手动切换一次小地图标签触发重建。"
                         if rc == 0 else f"✗ 6118 写入失败 rc={rc}")
            elif mode == "restore":
                self.log("▶ 6118 恢复 known：从最新备份还原…")
                rc = g5.restore(self.h, self.base, force=False)
                self.log("✅ 6118 已恢复。请切一次小地图标签确认。" if rc == 0
                         else f"✗ 6118 恢复失败 rc={rc}（若会话已变，需 --force 或忽略旧备份）")
            elif mode == "status":
                local = self._g5_6118_local()
                if not local:
                    return
                entries = g5.collect_all_entries(self.h, self.base, local)
                if not entries:
                    self.log("✗ relation 枚举失败")
                    return
                known = sum(1 for e in entries if e["known"] == 1)
                self.log(f"6118 known 状态：{known}/{len(entries)}（覆盖 {len({e.get('name') for e in entries if e.get('name')})} 派系名）")
            else:
                self.log(f"✗ 未知模式 {mode}")
        except Exception as e:
            self.log(f"✗ 6118 小地图全开异常: {e}")

    def _g5(self, mode):
        try:
            if getattr(self, "engine", None) in ("6118", "6115"):
                return self._g5_6118(mode)
            if mode == "apply":
                self.log("▶ 小地图全开：写全部 faction relation+0x7e0=1（2256 条，含备份）…")
                rc = g5.apply(self.h, self.base, all_dm=True)
                if rc == 0:
                    self.log("✅ 全量 known 已写入。请手动切换一次小地图标签触发重建（之后应全开）。")
                else:
                    self.log(f"✗ 全量 known 写入失败 rc={rc}")
            elif mode == "restore":
                self.log("▶ 恢复 known：从最新备份还原…")
                rc = g5.restore(self.h, self.base, force=False)
                if rc == 0:
                    self.log("✅ 已恢复。请手动切换一次小地图标签确认缩回。")
                else:
                    self.log(f"✗ 恢复失败 rc={rc}（若会话已变，需 --force 或忽略旧备份）")
            elif mode == "status":
                rc = g5.status(self.h, self.base)
                if rc != 0:
                    self.log(f"✗ known 状态查询失败 rc={rc}")
            else:
                self.log(f"✗ 未知模式 {mode}")
        except Exception as e:
            self.log(f"✗ 小地图全开异常: {e}")

    # ---------- ★动态决战（80% 最大规模下限 + 双方 1:2；可按野战/攻城分桶） ----------
    def _toggle_split(self):
        for w in self._split_widgets:
            if self.var_split.get():
                w.grid()
            else:
                w.grid_remove()
        # 切换后刷新动态标签显示格式
        if self.var_split.get():
            self._update_dyn_label_split(self._dyn_max_field, self._dyn_max_siege)
        else:
            self._update_dyn_label(self._dyn_max_total, int(self._dyn_max_total * 0.8))

    def cmd_dyn_reset(self):
        self._dyn_max_total = 0
        self._dyn_max_field = 0
        self._dyn_max_siege = 0
        sc = getattr(self, "_sc", None)
        if sc is not None:
            sc._dyn_max_total = 0
            sc._dyn_max_field = 0
            sc._dyn_max_siege = 0
        if self.var_split.get():
            self._update_dyn_label_split(0, 0)
        else:
            self._update_dyn_label(0, 0)
        self.log("动态决战基准已重置（野战/攻城/总计）")

    def _update_dyn_label(self, max_total, floor):
        self.root.after(0, lambda: self.lbl_dyn.config(text=f"基准:{max_total} 下限:{floor}"))

    def _update_dyn_label_split(self, field_max, siege_max):
        self.root.after(0, lambda: self.lbl_dyn.config(
            text=f"野基准:{field_max} 城基准:{siege_max} 野下限:{int(field_max * 0.8)} 城下限:{int(siege_max * 0.8)}"))

    def _on_dyn_update(self, data, floor=None):
        if isinstance(data, dict):
            self._dyn_max_field = data.get("field", 0)
            self._dyn_max_siege = data.get("siege", 0)
            self._update_dyn_label_split(self._dyn_max_field, self._dyn_max_siege)
            self.log(f"⚔ 动态决战基准更新：野战最大={self._dyn_max_field}→下限={int(self._dyn_max_field * 0.8)}；"
                     f"攻城最大={self._dyn_max_siege}→下限={int(self._dyn_max_siege * 0.8)}")
        else:
            self._dyn_max_total = data
            self._update_dyn_label(data, floor)
            self.log(f"⚔ 动态决战基准更新：最大规模={data} → 下限={floor}")

    # ---------- ★看海捕捉（A1 + 筛选） ----------
    def cmd_spectate_start(self):
        if not self._require_game():
            return
        self.thread(self._spectate_start)

    def _spectate_start(self):
        try:
            if getattr(self, "_sc", None) and self._sc.region:
                self.log("⚠️ 看海捕捉已在运行（先停止）")
                return
            # ★类型多选（全不选/全选 = 全捕捉）
            btype_ranges = []
            for k, r in (("siege", (3, 10)), ("field", (0, 2)), ("naval", (11, 14))):
                if self.var_ct[k].get():
                    btype_ranges.append(r)
            # ★PRE 单位数精确筛（Goal3.1；hdr+0x18/+0x78 含援军）
            def _pre_int(var):
                try:
                    return int(var.get() or 0)
                except ValueError:
                    return 0
            split = bool(self.var_split.get())
            pre_min_total = _pre_int(self.var_pre_total)
            pre_min_per_side = _pre_int(self.var_pre_per_side)
            pre_min_total_field = _pre_int(self.var_pre_total_field)
            pre_min_per_side_field = _pre_int(self.var_pre_per_side_field)
            pre_min_total_siege = _pre_int(self.var_pre_total_siege)
            pre_min_per_side_siege = _pre_int(self.var_pre_per_side_siege)
            pre_unit_cfg = {}
            if split:
                pre_unit_cfg = {
                    "split_type": True,
                    "min_total_field": pre_min_total_field,
                    "min_per_side_field": pre_min_per_side_field,
                    "min_total_siege": pre_min_total_siege,
                    "min_per_side_siege": pre_min_per_side_siege,
                }
            elif pre_min_total or pre_min_per_side:
                pre_unit_cfg = {"min_total": pre_min_total, "min_per_side": pre_min_per_side}
            dyn = bool(self.var_dyn.get())
            if dyn:
                pre_unit_cfg.setdefault("min_ratio", 0.5)   # 双方最不平衡 1:2
                if split:
                    pre_unit_cfg.setdefault("min_total_field", pre_min_total_field)
                    pre_unit_cfg.setdefault("min_per_side_field", pre_min_per_side_field)
                    pre_unit_cfg.setdefault("min_total_siege", pre_min_total_siege)
                    pre_unit_cfg.setdefault("min_per_side_siege", pre_min_per_side_siege)
                else:
                    pre_unit_cfg.setdefault("min_total", pre_min_total)
                    pre_unit_cfg.setdefault("min_per_side", pre_min_per_side)
            facs = list(self.whitelist) or None   # ★白名单 = 列表双击标记集合（空=全部）
            excls = list(self.blacklist) or None  # ★黑名单 = 列表标记集合（空=不排除）
            sc = spec.SpectateCapture(self.h, self.base, logfn=self.log)
            if not sc.install(btype_ranges=btype_ranges, factions=facs, exclude=excls,
                              auto_esc=self.var_esc.get(), pre_unit_cfg=pre_unit_cfg or None,
                              dyn_decisive=dyn, dyn_cb=self._on_dyn_update):
                return
            if dyn:
                if split:
                    sc._dyn_max_field = self._dyn_max_field
                    sc._dyn_max_siege = self._dyn_max_siege
                    if self._dyn_max_field or self._dyn_max_siege:
                        self.log(f"动态决战沿用已有基准：野战={self._dyn_max_field}→下限={int(self._dyn_max_field * 0.8)}；"
                                 f"攻城={self._dyn_max_siege}→下限={int(self._dyn_max_siege * 0.8)}")
                else:
                    sc._dyn_max_total = self._dyn_max_total
                    if self._dyn_max_total:
                        self.log(f"动态决战沿用已有基准：最大={self._dyn_max_total} → 下限={int(self._dyn_max_total * 0.8)}")
            if pre_unit_cfg:
                if split:
                    self.log(f"★ PRE单位数筛已启用（分类型）：野战总≥{pre_min_total_field} 每方≥{pre_min_per_side_field}；"
                             f"攻城总≥{pre_min_total_siege} 每方≥{pre_min_per_side_siege}"
                             + (" 动态决战:各自80%+双方1:2" if dyn else "")
                             + "（含援军；海战字段为0/0）")
                else:
                    self.log(f"★ PRE单位数筛已启用：总≥{pre_unit_cfg.get('min_total', 0)} "
                             f"每方≥{pre_unit_cfg.get('min_per_side', 0)}"
                             + (" 动态决战:下限=最大×80% 双方1:2" if dyn else "")
                             + "（含援军；海战字段为0/0）")
            self._sc = sc
            sc.start_observe()
        except Exception as e:
            self.log(f"✗ 捕捉异常: {e}")

    def cmd_spectate_stop(self):
        def _w():
            sc = getattr(self, "_sc", None)
            if not sc:
                self.log("⚠️ 未在捕捉")
                return
            sc.stop()
            sc.uninstall()
            self._sc = None
            self.log("⏹ 看海捕捉已停止（A1 卸载）")
        self.thread(_w)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
