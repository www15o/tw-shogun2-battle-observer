# -*- coding: utf-8 -*-
"""统一日志 —— 文件 + GUI 面板 + stdout/stderr 捕获 + 崩溃落盘。

**为什么需要**：打包后的 EXE 是 `console=False`，没有 stdout。
而工具链（probe_battle_env / s2_spectate / s2_ai_ctl ...）全部用 `print()`
报告关键结论——「结构校验失败，禁止写」「回读不一致」「A1 未安装」这类信息
会**全部静默丢失**，用户只看到"点了没反应"。
本模块把 stdio 重定向到日志文件与 GUI 面板，保证关键诊断不会消失。

用法：
    import _applog
    _applog.install()                  # 进程启动时调一次
    _applog.subscribe(gui.append)      # GUI 面板订阅（可选）
    _applog.emit("已连接")             # 主动记录
"""
import os
import sys
import threading
import time
import traceback

import _appinfo

_LOCK = threading.RLock()
_SUBS = []
_LOG_PATH = None
_INSTALLED = False
_KEEP_LOGS = 20


# ---------------------------------------------------------------- 路径

def log_path():
    """当前会话日志文件路径（未 install 时为 None）。"""
    return _LOG_PATH


def log_dir():
    d = os.path.join(_appinfo.data_dir(), "logs")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _prune(keep=_KEEP_LOGS):
    """只保留最近 keep 个日志，避免长期使用后堆积。"""
    try:
        files = [os.path.join(log_dir(), n) for n in os.listdir(log_dir()) if n.endswith(".log")]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in files[keep:]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


# ---------------------------------------------------------------- 订阅 / 输出

def subscribe(fn):
    """订阅日志行（GUI 面板用）。fn(line: str)"""
    with _LOCK:
        if fn not in _SUBS:
            _SUBS.append(fn)


def unsubscribe(fn):
    with _LOCK:
        if fn in _SUBS:
            _SUBS.remove(fn)


def emit(msg, tag=""):
    """记录一行：写文件 + 通知订阅者。任何异常都不向上抛。"""
    line = time.strftime("[%H:%M:%S]") + (f"[{tag}]" if tag else "") + " " + str(msg)
    with _LOCK:
        path = _LOG_PATH
        subs = list(_SUBS)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    for fn in subs:
        try:
            fn(line)
        except Exception:
            pass


# ---------------------------------------------------------------- stdio 捕获

class _Tee:
    """sys.stdout / sys.stderr 替身：原样转发（开发期控制台还在），并逐行进日志。"""

    def __init__(self, stream, tag):
        self._stream = stream
        self._tag = tag
        self._buf = ""

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        try:
            if self._stream is not None:
                self._stream.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                emit(line, self._tag)
        return len(s)

    def flush(self):
        try:
            if self._stream is not None:
                self._stream.flush()
        except Exception:
            pass

    def isatty(self):
        return False


# ---------------------------------------------------------------- 崩溃

def format_exc(exc_info=None):
    return "".join(traceback.format_exception(*(exc_info or sys.exc_info())))


def crash(where="", exc_info=None):
    """记录崩溃现场，返回格式化后的文本（调用方可拿去弹窗）。"""
    text = format_exc(exc_info)
    emit(f"!!! 未处理异常 @ {where or 'unknown'}", "CRASH")
    for ln in text.rstrip().splitlines():
        emit(ln, "CRASH")
    try:
        with open(os.path.join(log_dir(), "crash.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  {where} =====\n")
            f.write(text)
    except Exception:
        pass
    return text


def _excepthook(etype, value, tb):
    crash("main-thread", (etype, value, tb))
    try:
        sys.__excepthook__(etype, value, tb)
    except Exception:
        pass


def _thread_excepthook(args):
    crash(f"thread:{getattr(args.thread, 'name', '?')}",
          (args.exc_type, args.exc_value, args.exc_traceback))


# ---------------------------------------------------------------- 安装

def install(capture_stdio=True, banner=True):
    """进程启动时调用一次。返回日志文件路径。"""
    global _LOG_PATH, _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return _LOG_PATH
        _INSTALLED = True
    _prune()
    _LOG_PATH = os.path.join(log_dir(), f"run-{time.strftime('%Y%m%d-%H%M%S')}.log")

    if capture_stdio:
        out = sys.__stdout__ if sys.stdout is None else sys.stdout
        err = sys.__stderr__ if sys.stderr is None else sys.stderr
        sys.stdout = _Tee(out, "out")
        sys.stderr = _Tee(err, "err")

    sys.excepthook = _excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_excepthook

    if banner:
        emit(f"{_appinfo.title()}  |  {_appinfo.APP_TAGLINE}")
        emit(f"python {sys.version.split()[0]}  frozen={getattr(sys, 'frozen', False)}")
        emit(f"log = {_LOG_PATH}")
    return _LOG_PATH
