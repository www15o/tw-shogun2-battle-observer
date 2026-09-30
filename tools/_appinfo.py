# -*- coding: utf-8 -*-
"""应用标识与版本 —— `单一真源`。

版本解析优先级：
  1. `_version.py`：打包时由 `packaging/build.ps1` 生成（含构建时间）
  2. 仓库 `VERSION` 文件：源码直跑时使用
  3. `dev`：兜底

名称：Shogun2 Observer（幕府2 观察者）——以「观察」为原点：把部队 / 国政交给 AI，
你旁观 AI 对 AI 的战役与战斗。与仓库名 tw-shogun2-battle-observer 同系列。

**命名规则：<游戏缩写> Observer**。各游戏是独立仓库、独立产品，不共用产品名与配置目录：
幕府2 = Shogun2 Observer；后续阿提拉 = Attila Observer、三国 = ThreeKingdoms Observer、
战锤3 = Warhammer3 Observer。后续系列复用 packaging/ 模板，只改本文件的名称常量。
"""
import os
import sys

APP_NAME = "Shogun2 Observer"
APP_NAME_ZH = "幕府2 观察者"
APP_ID = "Shogun2Observer"               # %APPDATA% 目录名 / 单实例锁名
APP_TAGLINE = "战斗托管 / 看海 / AI 内战观战"

_HERE = os.path.dirname(os.path.abspath(__file__))


def _from_generated():
    """打包时注入的 _version.py（优先）。"""
    try:
        import _version  # noqa: F401
        v = getattr(_version, "VERSION", None)
        if v:
            return str(v), getattr(_version, "BUILD_TIME", None)
    except Exception:
        pass
    return None, None


def _from_file():
    """仓库 VERSION 文件（源码直跑）。"""
    bases = [_HERE, os.path.dirname(_HERE), getattr(sys, "_MEIPASS", "") or ""]
    for base in bases:
        if not base:
            continue
        try:
            with open(os.path.join(base, "VERSION"), "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            continue
    return None


def _resolve():
    v, t = _from_generated()
    if v:
        return v, t
    return (_from_file() or "dev"), None


APP_VERSION, BUILD_TIME = _resolve()
__version__ = APP_VERSION


def title():
    """窗口标题 / About 用的完整标识。"""
    s = f"{APP_NAME} v{APP_VERSION}"
    return f"{s}  (build {BUILD_TIME})" if BUILD_TIME else s


def data_dir():
    """%APPDATA%/<APP_ID> —— 设置与日志的根（不存在则创建）。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_ID)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d
