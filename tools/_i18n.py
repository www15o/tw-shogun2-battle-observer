# -*- coding: utf-8 -*-
"""_i18n.py — Shogun2 Observer 界面本地化（简体中文 / English）。

**转换范围（本轮）＝ 界面壳**：
  - 窗口标题、按钮 / Label / LabelFrame / Treeview 表头等全部静态控件文本
  - 下拉选项显示名（引擎选项、语言选项）
  - 对话框文本：messagebox（管理员权限警告、崩溃提示）与「关于」对话框
  - 状态栏（lbl_status）文本
  - 新增「关于」对话框（产品名 / 版本 / 构建时间 / MIT 许可 / 仓库链接）
  - 新增语言切换下拉（简体中文 / English）：切换写 _settings 并提示重启后生效

**不转换（保持中文，故意如此）**：
  - self.log(...) / _applog.emit(...) 的日志诊断输出（含 ✗ ✓ ⚠️ 前缀、偏移量、机制说明）
  - 内部异常文本（异常对象自身 message、traceback 正文）
  - 研究性说明（代码注释、偏移常量来历、机制地图引用）
  理由：这些内容面向开发者与内存逆向排查，术语中英混排会让定位偏移 / 写入问题时更易误读；
  且它们大量出现在 f-string 中，纳入词条只会放大「缺 key → 界面显示成 key 名」的风险。

语言包查找顺序（源码直跑与 PyInstaller 打包都支持）：
  1. <本文件目录>/_locales/<code>.json      （源码直跑 / onedir 打包）
  2. <sys._MEIPASS>/_locales/<code>.json    （PyInstaller 解包目录，onefile 亦适用）

失败策略（安全第一，绝不抛异常）：
  语言包缺失 / 未找到 key / JSON 语法错误 / 编码错误 → t() 一律返回 key 本身。
  GUI 不会因少一条词条而崩溃；界面最多显示成 key 名，便于一眼看出缺哪条。

持久化：_settings.set("ui.lang", code) / _settings.get("ui.lang", "zh_CN")。
  _settings 不可用（文件缺失 / import 失败 / 读写异常）时降级为「仅本进程内生效」，
  同样不抛异常。
"""
import json
import os
import sys

DEFAULT_LANG = "zh_CN"

# 显示名用各自语言的原生写法（切到英文界面时也应看到「简体中文」）——故这里不做本地化。
_LANGS = (("zh_CN", "简体中文"), ("en", "English"))

_LANG = None    # 进程内当前语言码；首次访问时从 _settings 读取
_CACHE = {}     # code -> {key: str}；加载失败也缓存空 dict，避免反复读坏文件


def _here():
    """本文件所在目录（源码直跑 = tools/；打包后为 _MEIPASS 内解包目录）。"""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return ""


def _locale_dirs():
    """语言包候选目录，按优先级返回（源码目录 → PyInstaller 解包目录）。"""
    dirs = []
    h = _here()
    if h:
        dirs.append(os.path.join(h, "_locales"))
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        d = os.path.join(str(mp), "_locales")
        if d not in dirs:
            dirs.append(d)
    return dirs


def _load(code):
    """读取并解析 <code>.json；任何失败都返回 {}（已缓存）。"""
    if code in _CACHE:
        return _CACHE[code]
    data = {}
    for d in _locale_dirs():
        path = os.path.join(d, str(code) + ".json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            continue                      # 缺文件 / JSON 损坏 / 编码错误 → 试下一个目录
        if isinstance(obj, dict):
            data = {str(k): v for k, v in obj.items() if isinstance(v, str)}
            break
    _CACHE[code] = data
    return data


def _settings_get(key, default=None):
    try:
        import _settings
        v = _settings.get(key, default)
        return default if v is None else v
    except Exception:
        return default


def _settings_set(key, value):
    try:
        import _settings
        _settings.set(key, value)
        return True
    except Exception:
        return False


def _codes():
    return tuple(c for c, _n in _LANGS)


def lang():
    """当前语言码（首次调用从 _settings 读取；未知值回退 DEFAULT_LANG）。"""
    global _LANG
    if _LANG is None:
        try:
            v = str(_settings_get("ui.lang", DEFAULT_LANG) or DEFAULT_LANG)
        except Exception:
            v = DEFAULT_LANG
        _LANG = v if v in _codes() else DEFAULT_LANG
    return _LANG


def set_lang(code):
    """切换语言并持久化到 _settings（ui.lang）。永不抛异常。"""
    global _LANG
    try:
        c = str(code)
        if not c:
            return
        _LANG = c
        _settings_set("ui.lang", c)
    except Exception:
        pass


def t(key, **fmt):
    """取词条；缺 key / 缺文件 / JSON 损坏 → 返回 key 本身。占位符用 {name} 格式化。"""
    try:
        s = _load(lang()).get(key)
    except Exception:
        return str(key)
    if not isinstance(s, str):
        return str(key)
    if fmt:
        try:
            return s.format(**fmt)
        except Exception:
            return s                      # 占位符不匹配也返回原文，不抛
    return s


def available():
    """[(语言码, 显示名)] —— 语言下拉用（顺序 = 展示顺序）。"""
    return [(c, n) for c, n in _LANGS]
