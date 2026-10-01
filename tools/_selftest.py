# -*- coding: utf-8 -*-
"""--selftest：打包产物自检（**不起 GUI、不连游戏**）。

目的：让 exe 能自证。当前完全没有这个能力——GUI 一起来就要游戏 + 管理员，
所以"这个包到底打全了没有"无法在没有游戏环境的机器上回答。

检查项：
  1. 版本 / 构建时间 / frozen 状态
  2. 依赖模块逐个 import      <- 等价于验证 hiddenimports 完整
  3. 内置资源可读（引擎 profile / VERSION）
  4. 第三方依赖（numpy 必需 / capstone 可选）
  5. 管理员权限               <- 仅报告，**不作为失败条件**

注意：`console=False` 的 exe 没有 stdout，因此结果同时写入日志文件，
并以**退出码**表达成败（0 = 通过）。
"""
import os
import sys

import _appinfo
import _applog

# 兜底清单：仅在 _build_manifest 缺失（源码直跑且从未构建过）时使用。
# 正常情况下模块清单来自打包时生成的闭包扫描结果 —— 不再手工维护。
FALLBACK_MODULES = [
    "probe_battle_env", "s2_watch", "s2_ai_ctl", "re_b3_inject", "battle_ai_ctl",
    "s2_spectate", "_g5_relation_write", "_probe_g5_relation_chain",
    "_prefilter_logic", "re_f10_stub_builder", "re_h46a", "re_a3_probe",
    "re_c2_faction",
]

# numpy 是运行托管/看海/观战所必需；capstone 只有静态反汇编模式（re_lib / re_b3_inject --dry）
# 才用得到 —— README 也这么写。所以它只警告、不算失败（否则一份"只用 numpy"的安装会被判 FAIL）。
THIRD_PARTY = ["numpy"]
OPTIONAL_THIRD_PARTY = ["capstone"]


def required_modules():
    """依赖清单来自构建时生成的 _build_manifest（= 闭包扫描结果）。"""
    try:
        import _build_manifest
        mods = [m for m in getattr(_build_manifest, "MODULES", []) if m != "_build_manifest"]
        if mods:
            return sorted(set(mods))
    except Exception:
        pass
    return sorted(FALLBACK_MODULES)


def _is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _check_modules():
    ok = True
    for name in required_modules():
        try:
            __import__(name)
            print(f"  [ok]   import {name}")
        except Exception as e:
            print(f"  [FAIL] import {name} -> {type(e).__name__}: {e}")
            ok = False
    return ok


def _check_third_party():
    ok = True
    for name in OPTIONAL_THIRD_PARTY:
        try:
            m = __import__(name)
            print(f"  [ok]   {name} {getattr(m, '__version__', '?')}")
        except Exception as e:
            print(f"  [warn] {name} 缺失（仅静态反汇编模式需要，不算失败）-> {type(e).__name__}: {e}")
    for name in THIRD_PARTY:
        try:
            m = __import__(name)
            print(f"  [ok]   {name} {getattr(m, '__version__', '?')}")
        except Exception as e:
            print(f"  [FAIL] {name} -> {type(e).__name__}: {e}")
            ok = False
    return ok


def _check_resources():
    """内置资源：引擎 profile（打包时进 datas）+ VERSION。"""
    ok = True
    try:
        import s2_spectate as spec
        for build in ("6118", "6115"):
            p = spec._old_profile_path(build)
            if p and os.path.exists(p):
                print(f"  [ok]   profile {build}: {p} ({os.path.getsize(p)} B)")
            else:
                print(f"  [FAIL] profile {build} 缺失（datas 没打进去？）-> {p}")
                ok = False
    except Exception as e:
        print(f"  [FAIL] 资源检查异常 -> {type(e).__name__}: {e}")
        ok = False
    return ok


def _check_app_services():
    """应用服务层：设置持久化 + 本地化。

    策略「存在才检查」：模块尚未落地 → [skip]（不算失败）；落地了但坏掉 → [FAIL]。
    这样重构期间与最终产物用同一套自检。
    """
    ok = True

    # --- 设置持久化 ---
    try:
        import _settings
        p = _settings.path()
        merged = _settings.all()
        got = _settings.get("ui.lang")
        if not isinstance(merged, dict) or got is None:
            print(f"  [FAIL] _settings 异常：all()={type(merged).__name__} get('ui.lang')={got!r}")
            ok = False
        else:
            print(f"  [ok]   _settings  path={p}")
            print(f"  [ok]   _settings  ui.lang={got!r}  已知键 {sorted(merged.keys())}")
    except ImportError:
        print("  [skip] _settings 未包含在本版本中")
    except Exception as e:
        print(f"  [FAIL] _settings -> {type(e).__name__}: {e}")
        ok = False

    # --- 本地化 ---
    try:
        import _i18n
        langs = [c for c, _ in _i18n.available()]
        print(f"  [ok]   _i18n 语言包: {langs}")
        if "zh_CN" not in langs or "en" not in langs:
            print(f"  [FAIL] _i18n 缺少语言包（需 zh_CN 与 en），实得 {langs}")
            ok = False
        else:
            orig = _i18n.lang()
            for code in ("zh_CN", "en"):
                _i18n.set_lang(code)
                sample = _i18n.t("about.title")
                if sample == "about.title":
                    sample = "(无 about.title 词条)"
                print(f"  [ok]   _i18n {code}: {sample}")
            _i18n.set_lang(orig)
    except ImportError:
        print("  [skip] _i18n 未包含在本版本中")
    except Exception as e:
        print(f"  [FAIL] _i18n -> {type(e).__name__}: {e}")
        ok = False

    return ok


def run():
    """返回退出码：0 = 通过。"""
    _applog.install()
    print("=" * 58)
    print(f"selftest  {_appinfo.title()}")
    print(f"frozen={getattr(sys, 'frozen', False)}  "
                 f"exe={sys.executable}")
    print(f"version={_appinfo.APP_VERSION}  build={_appinfo.BUILD_TIME}")

    results = []
    for title, fn in (("依赖模块", _check_modules),
                      ("第三方依赖", _check_third_party),
                      ("内置资源", _check_resources),
                      ("应用服务", _check_app_services)):
        print(f"-- {title} --")
        results.append((title, fn()))

    # 管理员：只报告
    admin = _is_admin()
    print(f"-- 权限 --\n  {'[ok]' if admin else '[warn]'} 管理员={admin}"
                 + ("" if admin else "（需要管理员才能写内存；自检不算失败）"))

    failed = [t for t, ok in results if not ok]
    print(f"RESULT: {'PASS' if not failed else 'FAIL'}"
                 + (f"  失败项={failed}" if failed else ""))
    print(f"日志: {_applog.log_path()}")
    print("=" * 58)
    return 1 if failed else 0
