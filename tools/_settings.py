# -*- coding: utf-8 -*-
r"""设置持久化 —— Shogun2 Observer 的唯一设置真源。

**为什么单独一个模块**：GUI、命令行工具（s2_ai_ctl / s2_spectate ...）与打包后的 exe
都要读写同一份用户设置。各写各的 JSON 迟早会摊上「半截文件打不开」这类把工具带崩的
事故 —— 位置、格式、容错在本模块一次性收口。

文件位置：%APPDATA%\APP_ID\settings.json（APP_ID 见 _appinfo.py）
目录一律由 _appinfo.data_dir() 决定，本模块**不自己拼 APPDATA**。

公开 API（冻结，GUI 已按此调用）::

    DEFAULTS            默认值表（只读，请勿修改）
    path()              设置文件绝对路径
    get(key, default)   取单项；未知 key 返回 default
    set(key, value)     写单项并立即原子落盘
    update(mapping)     批量写，一次落盘
    all()               DEFAULTS 合并后的全量
    reset()             恢复默认并落盘（按语义丢弃未知 key）

失败策略（本模块存在的意义）：**绝不抛异常**。文件缺失 / JSON 损坏 / 目录不可写 /
磁盘错误一律回退 DEFAULTS —— 这是内存读写工具，设置文件坏了也绝不能让它起不来。
set / update / reset 返回 True/False 表示是否成功落盘，调用方可忽略。

未知 key：调用方可以写 DEFAULTS 里没有的 key（后续版本项、临时状态），能存能取、
跨进程不丢；只有 reset() 会按「恢复默认」的语义把它们清掉。

内存语义：首次访问懒加载进缓存，之后以内存为准（GUI 已有单实例锁）。get / all 命中
缓存时返回缓存里的对象本身（这样 lst = get(k); lst.append(x) 这类就地修改用得住），
命中 DEFAULTS 时返回深拷贝 —— **DEFAULTS 永不被调用方改坏**。需要强制重读磁盘用
reload()。落盘内容是 DEFAULTS 合并缓存后的完整快照（便于手改与排查）。

线程安全：公开函数全部持可重入锁。
"""
import copy
import json
import os
import tempfile
import threading

import _appinfo

__all__ = ["DEFAULTS", "FILE_NAME", "path", "get", "set", "update", "all", "reset", "reload"]

FILE_NAME = "settings.json"

# 默认值表（对齐 GUI 现有硬编码默认值）。只读：不要就地修改，也不要假设调用方拿到的
# 是这里的对象（get/all 命中默认时给的是深拷贝）。
DEFAULTS = {
    "ui.lang": "zh_CN",
    "ui.geometry": "1100x700",
    "engine.choice": "自动",
    "money.amount": "50000",
    "spectate.whitelist": [],
    "spectate.blacklist": [],
}

_LOCK = threading.RLock()
_CACHE = None            # None = 尚未从磁盘加载；加载后为 dict（只装实际存在的键）


# ---------------------------------------------------------------- 路径

def path():
    """设置文件绝对路径。任何情况下都不抛异常、不创建文件。

    每次调用都问一次 _appinfo.data_dir()（这样测试可以 monkeypatch 它，
    也保证打包 / 换目录后立即生效）。
    """
    try:
        base = _appinfo.data_dir()
    except Exception:
        base = os.path.join(os.path.expanduser("~"),
                            getattr(_appinfo, "APP_ID", "Shogun2Observer"))
    try:
        return os.path.join(base, FILE_NAME)
    except Exception:
        return FILE_NAME


# ---------------------------------------------------------------- 读写盘

def _norm_key(key):
    """key 一律按字符串处理（JSON 对象的键必须可序列化）。"""
    if isinstance(key, str):
        return key
    try:
        return str(key)
    except Exception:
        return ""


def _read_file():
    """读磁盘 -> dict。缺失 / 损坏 / 权限不足 / 顶层不是对象 一律返回 {}（不抛）。"""
    try:
        # utf-8-sig：顺手兼容记事本等工具写出的 BOM
        with open(path(), "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        out[_norm_key(k)] = v
    return out


def _cache():
    """懒加载缓存（调用方需持锁）。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = _read_file()
    return _CACHE


def _merged():
    """DEFAULTS 合并缓存后的全量（调用方需持锁）。"""
    out = copy.deepcopy(DEFAULTS)
    out.update(_CACHE or {})
    return out


def _atomic_write(data):
    """临时文件 + os.replace 原子落盘。成功 True；任何失败 False（不抛、不留 .tmp）。"""
    target = path()
    d = os.path.dirname(target) or "."
    tmp = None
    try:
        os.makedirs(d, exist_ok=True)
        # 临时文件必须与目标同目录（同一卷），os.replace 才是原子替换
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            # ensure_ascii=False：中文（引擎名 / 语言）以 UTF-8 明文落盘，便于手改
            # default=str：个别值不可序列化时也不至于整份设置写不进去
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, target)
        tmp = None
        return True
    except Exception:
        return False
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------- 公开 API

def get(key, default=None):
    """取单项。命中缓存返回原对象；命中 DEFAULTS 返回深拷贝；未知 key 返回 default。"""
    k = _norm_key(key)
    try:
        with _LOCK:
            c = _cache()
            if k in c:
                return c[k]
            if k in DEFAULTS:
                return copy.deepcopy(DEFAULTS[k])
    except Exception:
        pass
    return default


def set(key, value):
    """写单项并立即原子落盘。返回是否成功落盘。"""
    try:
        return update({key: value})
    except Exception:          # 例如 key 不可哈希这类调用方笔误：也不抛
        return False


def update(mapping):
    """批量写，一次落盘（不产生中间态）。返回是否成功落盘。

    注意：落盘失败时值仍留在内存里（本次会话可用），只是没持久化。
    """
    try:
        items = dict(mapping)
    except Exception:
        return False
    try:
        with _LOCK:
            c = _cache()
            for k, v in items.items():
                c[_norm_key(k)] = v
            return _atomic_write(_merged())
    except Exception:
        return False


def all():
    """DEFAULTS 合并后的全量（含未知 key）。任何情况下都返回 dict。"""
    try:
        with _LOCK:
            return _merged()
    except Exception:
        return copy.deepcopy(DEFAULTS)


def reset():
    """恢复默认并落盘（未知 key 一并丢弃）。返回是否成功落盘。"""
    global _CACHE
    try:
        with _LOCK:
            _CACHE = {}
            return _atomic_write(_merged())
    except Exception:
        return False


def reload():
    """丢弃内存缓存、强制重读磁盘，返回合并后的全量（多实例 / 手改文件后用）。"""
    global _CACHE
    try:
        with _LOCK:
            _CACHE = _read_file()
            return _merged()
    except Exception:
        return copy.deepcopy(DEFAULTS)
