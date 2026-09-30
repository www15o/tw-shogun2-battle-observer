# -*- coding: utf-8 -*-
"""_run_elev.py — 提权启动器。

用法：
    python tools/_run_elev.py <script.py> [args...]

行为：
    - 当前**非管理员** → 用 ShellExecuteExW("runas") 重新拉起自己（弹一次 UAC），
      等子进程结束并把它的退出码透传出来；
    - 当前**已是管理员** → 直接运行 <script.py>，stdout/stderr 落到日志文件；
    - 用户取消 UAC → 明确提示并以 2 退出（不是静默失败）。

为什么要有这个文件（2026-10-01 修复）
    此前本文件只有 subprocess.Popen，**完全没有提权**；而 README 的三条快速开始
    命令都写成 python tools/_run_elev.py <目标>，于是三条入口在普通 shell 下都不
    提权、随后写内存必然失败。旧 docstring 把「请你自己 Start-Process -Verb RunAs」
    当成用法，但文档承诺的是「会自动弹 UAC」——承诺与实现不一致。现在实现补齐。
"""
import ctypes
import os
import subprocess
import sys
import tempfile

TOOLS = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(TOOLS)

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
INFINITE = 0xFFFFFFFF
ERROR_CANCELLED = 1223


def log_path(target):
    """日志放仓库外，避免弄脏 git 工作树（旧版写 tools/_elev_*.log）。"""
    name = "elev_%s.log" % os.path.splitext(os.path.basename(target))[0]
    appdata = os.environ.get("APPDATA")
    d = os.path.join(appdata, "Shogun2Observer", "logs") if appdata else tempfile.gettempdir()
    try:
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, name)
    except OSError:
        return os.path.join(tempfile.gettempdir(), name)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_direct(script, args):
    """已提权：直接跑目标，日志落盘，退出码透传。"""
    target = os.path.join(TOOLS, script)
    if not os.path.exists(target):
        sys.stderr.write("找不到目标脚本：%s" % target)
        sys.stderr.write(chr(10))
        return 2
    lp = log_path(script)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    with open(lp, "ab", buffering=0) as logf:
        p = subprocess.Popen([sys.executable, "-u", target] + list(args),
                             stdout=logf, stderr=logf, cwd=BASE, env=env)
        rc = p.wait()
    sys.stderr.write("日志: %s" % lp)
    sys.stderr.write(chr(10))
    return rc


def relaunch_elevated():
    """非管理员：runas 重启自己，等结束，透传退出码。返回 (ok, code)。"""
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # 子进程命令行 = 本文件绝对路径 + 原始参数；list2cmdline 负责引号与空格
    params = subprocess.list2cmdline([os.path.abspath(__file__)] + sys.argv[1:])

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    sei.lpVerb = "runas"
    sei.lpFile = sys.executable
    sei.lpParameters = params
    sei.lpDirectory = BASE
    sei.nShow = 1

    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        return False, ctypes.get_last_error()
    try:
        kernel32.WaitForSingleObject(sei.hProcess, INFINITE)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return True, code.value
    finally:
        kernel32.CloseHandle(sei.hProcess)


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("用法: python tools/_run_elev.py <script.py> [args...]")
        sys.stderr.write(chr(10))
        sys.stderr.write("  例: python tools/_run_elev.py s2_control_gui.py")
        sys.stderr.write(chr(10))
        return 2
    if is_admin():
        return run_direct(sys.argv[1], sys.argv[2:])
    sys.stderr.write("当前非管理员，正在请求提权（会弹一次 UAC）...")
    sys.stderr.write(chr(10))
    ok, code = relaunch_elevated()
    if not ok:
        if code == ERROR_CANCELLED:
            sys.stderr.write("提权被取消：本工具必须管理员权限才能读写游戏进程内存。")
        else:
            sys.stderr.write("提权失败（WinError %d）。请右键「以管理员身份运行」，"
                             "或用 Start-Process -Verb RunAs。" % code)
        sys.stderr.write(chr(10))
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())
