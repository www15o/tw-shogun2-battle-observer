# -*- coding: utf-8 -*-
"""_run_elev.py — 通用提权启动器（发布版，路径自动定位）。

用法：Start-Process python _run_elev.py <script.py> [args...] -Verb RunAs（用户点一次 UAC）
内部：subprocess 拉起 tools/<script.py> + args，stdout/stderr 追加到
      tools/_elev_<script名>.log（PYTHONIOENCODING=utf-8 防 GBK 崩）；退出码透传。
"""
import subprocess, sys, os

TOOLS = os.path.dirname(os.path.abspath(__file__))   # 本文件所在目录（tools/）
BASE = os.path.dirname(TOOLS)                        # 仓库根
script = sys.argv[1]
args = sys.argv[2:]
LOG = os.path.join(TOOLS, f"_elev_{os.path.splitext(os.path.basename(script))[0]}.log")
PY = sys.executable
SCRIPT = os.path.join(TOOLS, script)

logf = open(LOG, "ab", buffering=0)
env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
p = subprocess.Popen([PY, "-u", SCRIPT] + args,
                     stdout=logf, stderr=logf, cwd=BASE, env=env)
rc = p.wait()
logf.close()
sys.exit(rc)
