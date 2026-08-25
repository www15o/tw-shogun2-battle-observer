# -*- coding: utf-8 -*-
"""s2_control_gui.py — 幕府2 控制台（加钱 + AI化/人控 + 战斗AI注入）独立 GUI（2026-08-12）

功能（复用已验证工具：s2_watch / s2_money 机制 / s2_ai_ctl）：
  1. 连接游戏（自动检测 shogun2.exe + 引擎 build）
  2. 扫描派系列表（名称 / 人类 / 国库）
  3. 加钱：选中派系国库写指定金额（faction+0x4fc，UI 即时生效）
  4. AI 化（看海）：+0x6a0=0 + manager=FULL_MANAGER（CAI 接管跑回合）
  5. 恢复人控：+0x6a0=1 + manager=HUMAN
  6. 战斗 AI 注入（s2_ai_ctl）：全员AI / 自动托管 / 切回人控（战斗场景内）

打包：pyinstaller --onefile --noconsole --name s2_control s2_control_gui.py
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
import re_c2_faction as fc
import s2_watch as sw
import s2_ai_ctl as ctl
import battle_ai_ctl as ba
import s2_spectate as spec


class App:
    def __init__(self, root):
        self.root = root
        root.title("幕府2 控制台 — 加钱 / AI化 / 战斗注入 / 看海捕捉")
        root.geometry("1100x660")
        root.minsize(900, 560)   # ★2026-08-19 最小尺寸（缩放不截断控件）
        self.h = None
        self.base = None
        self.facs = []          # [(addr, human, name, treasury)]
        self.sel = None         # 选中派系 (addr, human, name, treasury)
        self._mgr_cache = {}    # faction_addr → manager 写点（同会话缓存，AI化/恢复提速）

        # --- 顶部：连接 + 扫描 ---
        top = ttk.Frame(root, padding=6)
        top.pack(fill=tk.X)
        ttk.Button(top, text="连接游戏", command=self.cmd_connect).pack(side=tk.LEFT)
        ttk.Button(top, text="刷新派系", command=self.cmd_scan).pack(side=tk.LEFT, padx=4)
        self.lbl_status = ttk.Label(top, text="未连接")
        self.lbl_status.pack(side=tk.LEFT, padx=8)

        # --- 派系列表（★白名单 / 黑名单列） ---
        mid = ttk.Frame(root, padding=6)
        mid.pack(fill=tk.BOTH, expand=True)
        cols = ("name", "human", "treasury", "wl", "bl")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=10,
                                 selectmode="extended")   # ★多选（批量白/黑名单）
        self.tree.heading("name", text="派系")
        self.tree.heading("human", text="人类")
        self.tree.heading("treasury", text="国库")
        self.tree.heading("wl", text="白名单")
        self.tree.heading("bl", text="黑名单")
        self.tree.column("name", width=130)
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
        ttk.Label(ops, text="（战斗场景内生效）").grid(row=2, column=4)

        # row3 battle_ai
        ttk.Label(ops, text="battle_ai:").grid(row=3, column=0, sticky=tk.W)
        ttk.Button(ops, text="注入", command=lambda: self.cmd_ba("inject")).grid(row=3, column=1)
        ttk.Button(ops, text="取消", command=lambda: self.cmd_ba("cancel")).grid(row=3, column=2, padx=4)
        ttk.Button(ops, text="状态", command=lambda: self.cmd_ba("status")).grid(row=3, column=3, padx=4)
        ttk.Label(ops, text="（战前/战役地图写，战斗加载时消费；重启游戏需重写）").grid(row=3, column=4)

        # --- ★看海捕捉（4 行：类型 / 阈值 / 白名单 / 操作） ---
        # row4 类型多选
        ttk.Label(ops, text="看海捕捉-类型:").grid(row=4, column=0, sticky=tk.W)
        self.var_ct = {"siege": tk.BooleanVar(value=True), "field": tk.BooleanVar(value=False),
                       "naval": tk.BooleanVar(value=False)}
        ttk.Checkbutton(ops, text="攻城", variable=self.var_ct["siege"]).grid(row=4, column=1)
        ttk.Checkbutton(ops, text="野战", variable=self.var_ct["field"]).grid(row=4, column=2)
        ttk.Checkbutton(ops, text="海战", variable=self.var_ct["naval"]).grid(row=4, column=3)
        ttk.Label(ops, text="（全不选/全选=全捕捉）").grid(row=4, column=4, columnspan=3, sticky=tk.W)

        # row5 分类型规模阈值 + 自动ESC
        # ★2026-08-28 确认：S16 [army+0x294] 只是规模代理，不是单位总数（PRE=12 vs POST≈19-20）
        # → 默认 0=关闭；>0 仍会按 +0x294 预筛，仅当用户明确接受该语义时再填。
        ttk.Label(ops, text="规模阈值:").grid(row=5, column=0, sticky=tk.W)
        self.var_scale = {}
        ttk.Label(ops, text="海≥").grid(row=5, column=1, sticky=tk.E)
        self.var_scale["naval"] = tk.StringVar(value="0")
        ttk.Entry(ops, textvariable=self.var_scale["naval"], width=4).grid(row=5, column=2, sticky=tk.W)
        ttk.Label(ops, text="野≥").grid(row=5, column=3, sticky=tk.E)
        self.var_scale["field"] = tk.StringVar(value="0")
        ttk.Entry(ops, textvariable=self.var_scale["field"], width=4).grid(row=5, column=4, sticky=tk.W)
        ttk.Label(ops, text="攻≥").grid(row=5, column=5, sticky=tk.E)
        self.var_scale["siege"] = tk.StringVar(value="0")
        ttk.Entry(ops, textvariable=self.var_scale["siege"], width=4).grid(row=5, column=6, sticky=tk.W)
        ttk.Label(ops, text="（+0x294 规模代理 ⚠️非单位数；0=关）").grid(row=5, column=7, columnspan=2, sticky=tk.W)
        self.var_esc = tk.BooleanVar(value=False)   # ★默认关（用户否决后置 ESC）
        ttk.Checkbutton(ops, text="自动ESC", variable=self.var_esc).grid(row=5, column=9, padx=6)

        # row6 白名单
        ttk.Label(ops, text="白名单:").grid(row=6, column=0, sticky=tk.W)
        ttk.Button(ops, text="标记选中", command=self.mark_whitelist_sel).grid(row=6, column=1)
        ttk.Button(ops, text="全选", command=self.whitelist_all).grid(row=6, column=2, padx=2)
        ttk.Button(ops, text="清空", command=self.clear_whitelist).grid(row=6, column=3, padx=2)
        ttk.Label(ops, text="（列表多选/Ctrl/Shift + 标记选中；无白名单=全部捕捉）").grid(row=6, column=4, columnspan=4, sticky=tk.W)

        # row7 黑名单
        ttk.Label(ops, text="黑名单:").grid(row=7, column=0, sticky=tk.W)
        ttk.Button(ops, text="标记选中", command=self.mark_blacklist_sel).grid(row=7, column=1)
        ttk.Button(ops, text="全选", command=self.blacklist_all).grid(row=7, column=2, padx=2)
        ttk.Button(ops, text="清空", command=self.clear_blacklist).grid(row=7, column=3, padx=2)
        ttk.Label(ops, text="（黑名单派系参与的战斗不捕捉；白名单与黑名单同时存在时黑名单优先）").grid(row=7, column=4, columnspan=4, sticky=tk.W)

        # row8 操作
        ttk.Label(ops, text="操作:").grid(row=8, column=0, sticky=tk.W)
        ttk.Button(ops, text="开始捕捉", command=self.cmd_spectate_start).grid(row=8, column=1, padx=4)
        ttk.Button(ops, text="停止", command=self.cmd_spectate_stop).grid(row=8, column=2)

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

    # ---------- 连接 / 扫描 ----------
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
                self.log("✗ 未找到引擎模块（empire.retail.dll / shogun2.dll）")
                return
            self.h, self.base, self.build = h, base, build
            self.set_status(f"PID={pid} {build} base=0x{base:08x}")
            self.log(f"✓ 已连接 PID={pid} 引擎={build} base=0x{base:08x}")
        except Exception as e:
            self.log(f"✗ 连接异常: {e}")

    def cmd_scan(self):
        if not self._require_game():
            return
        self.thread(self._scan)

    def _scan(self):
        try:
            facs = sw.scan_factions(self.h, self.base)
            self.facs = facs
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
                                 values=(name or "?", "★" if h6 else "-", tr, wl, bl))
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

    # ---------- AI化 / 恢复（定位 manager 表） ----------
    def _locate_manager(self, fa):
        """定位 manager 写点（scan_objA + find_manager_entry），返回 mgr_target 或 None。
        同会话缓存：首次全内存扫描定位后缓存，AI化/恢复连续操作毫秒级。
        缓存复用前校验 faction 对象仍有效（vtable 匹配），失效则重新定位。"""
        # 缓存优先
        cached = self._mgr_cache.get(fa)
        if cached:
            if pb.read_u32(self.h, fa) == self.base + sw.VTABLE_RVA:
                self.log("⚡ 缓存命中 manager 写点（免扫描）")
                return cached
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

    def cmd_ai_ify(self):
        if not self._require_game() or not self._require_sel():
            return
        self.thread(self._ai_ify)

    def _ai_ify(self):
        try:
            fa, h6, name, tr = self.sel
            self.log(f"▶ AI化 {name!r} 定位 manager 表…")
            mgr_tgt = self._locate_manager(fa)
            if mgr_tgt is None:
                # 不做降级（缺 FULL_MANAGER 无看海意义），明确报错。
                # 2026-08-13 已确证读档后 manager 表存在；此错误仅表示本次扫描未命中。
                self.log("✗ AI化失败：manager 表未定位（可能不在战役，或内存扫描未命中）——"
                         "请确认已进入战役后重试（若仍失败，先刷新派系/重新连接）")
                return
            ok = sw.do_watch(self.h, self.base, fa, mgr_tgt)
            self.log(f"{'✅' if ok else '⚠️'} AI化(看海) {name!r}：+0x6a0=0 + FULL_MANAGER")
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
            self.log(f"▶ 恢复人控 {name!r} 定位 manager 表…")
            mgr_tgt = self._locate_manager(fa)
            if mgr_tgt is None:
                # 降级：只写 +0x6a0=1（manager 表扫描未命中时）
                self.log("⚠️ manager 表未定位（可能不在战役）——降级只写 +0x6a0=1")
                ok = fc.write_byte(self.h, fa + 0x6a0, 1)
                back = pb.read_u8(self.h, fa + 0x6a0)
                self.log(f"{'✅' if ok and back == 1 else '✗'} 降级恢复 +0x6a0=1 回读={back}")
                return
            ok = sw.do_restore(self.h, self.base, fa, mgr_tgt)
            self.log(f"{'✅' if ok else '⚠️'} 恢复人控 {name!r}：+0x6a0=1 + HUMAN")
            self.set_status(f"{name} 已恢复 human=1")
        except Exception as e:
            self.log(f"✗ 恢复异常: {e}（游戏重启/进程变更会失效，请重新连接）")

    # ---------- 战斗 AI 注入 ----------
    def cmd_battle(self, mode):
        if not self._require_game():
            return
        self.thread(self._battle, mode)

    def _battle(self, mode):
        try:
            if mode == "all-ai":
                self.log("全员 AI 托管 + 援军监控（Ctrl+C 停止——GUI 中请点窗口关闭/等待）")
                rc = ctl.cmd_all_ai(self.h, self.base)
            elif mode == "auto":
                self.log("自动托管：监测战斗状态自动接管（后台持续）")
                rc = ctl.cmd_auto(self.h, self.base)
            elif mode == "human":
                rc = ctl.cmd_human(self.h, self.base)
            else:
                return
            self.log(f"战斗注入返回 {rc}")
        except Exception as e:
            self.log(f"✗ 战斗注入异常: {e}")

    # ---------- battle_ai 注入 / 取消 / 状态 ----------
    def cmd_ba(self, mode):
        if not self._require_game():
            return
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
            # ★分类型规模阈值（var_scale 是 dict：naval/field/siege 三个 StringVar）
            scale = {}
            for k in ("naval", "field", "siege"):
                try:
                    scale[k] = int(self.var_scale[k].get() or 0)
                except (ValueError, KeyError):
                    scale[k] = 0
            facs = list(self.whitelist) or None   # ★白名单 = 列表双击标记集合（空=全部）
            excls = list(self.blacklist) or None  # ★黑名单 = 列表标记集合（空=不排除）
            sc = spec.SpectateCapture(self.h, self.base, logfn=self.log)
            if not sc.install(btype_ranges, scale, facs, excls, self.var_esc.get()):
                return
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
