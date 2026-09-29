# tools/ — 工具集使用说明

> 全部为**研究原型**，仅用于**单机**游戏进程的内存读写。请勿用于任何在线/对战场景。
> 本目录只含脚本源码，**不含任何游戏文件**（游戏二进制、.pack、提取数据一律不入库）。

---

## 0. 前置条件

| 项 | 要求 |
|---|---|
| 系统 | Windows（工具依赖 Win32 API 读写进程内存） |
| Python | 3.x（开发环境 3.14） |
| 权限 | **管理员**——读写游戏进程内存必需。用 `_run_elev.py` 可弹 UAC 提权启动 |
| 游戏 | Steam 版《幕府将军2》，**运行中**（除静态分析类工具外） |
| 依赖 | `pip install numpy`（必需）；`pip install capstone`（仅静态反汇编模式需要） |

### 游戏路径配置

脚本**不硬编码游戏路径**。`re_lib.py` 按以下顺序解析 `Empire.Retail.dll`：

1. 环境变量 `SHOGUN2_DLL`（完整文件路径）
2. 环境变量 `SHOGUN2_DIR`（游戏根目录）
3. 自动探测：Steam 注册表安装位置 + `libraryfolders.vdf` 中的库目录

```powershell
# 一般无需手动设置（自动探测即可）；仅在探测失败、或想静态分析时才需要
$env:SHOGUN2_DIR = "<你的游戏安装目录>"
```

> 未配置且自动探测失败时，静态分析工具会给出明确提示后退出——不会猜测路径。

---

## 1. 工具分层：入口 vs 依赖库

**⚠️ 禁止单独拷走某个 `.py`**：入口脚本靠同目录模块互相 import，缺一个就 `ImportError`。

### 入口（可直接运行）

| 工具 | 作用 | 目标 |
|---|---|---|
| `s2_control_gui.py` | **图形控制台**（整合版）：加钱 / AI 化看海 / 战斗 AI 注入 / 看海捕捉 | ①②③ |
| `s2_ai_ctl.py` | 战斗中把玩家军队交给**原生**战斗 AI（auto 全托管 / status / watch 补写） | ① |
| `s2_watch.py` | 看海：玩家派系交 CAI 自主发展（S2 / FOTS / ROTS 通用） | ② |
| `s2_spectate.py` | **看海捕捉器**：A1 hook + 类型/规模/阵营过滤 + 自动 ESC | ③ + 过滤 |
| `_re_b9_forge.py` | 目标3 原始实现：b9 单字节直写（probe / write / watch 三模式 + 海战过滤） | ③ |
| `_re_battle_watcher.py` | 常驻战斗观察器：自动捕获每场战斗数据（攒数据用） | ③ 辅助 |
| `s2_money.py` | 改国库（配合看海提高存活率） | ② 辅助 |
| `s2_autowatch.py` | 自动看海监控（无人值守推进） | ② 辅助 |
| `s2_ai_cmd.py` | 战斗 AI 命令层探针 | ① 辅助 |
| `memscan.py` | 通用 32 位进程内存扫描器（定位未知字段用） | 工具链 |
| `battle_ai_ctl.py` | battle_ai 标志位只读/注入/取消（带回读验证） | ① 辅助 |
| `re_lib.py` | 静态反汇编分析库（capstone；也可当 CLI 跑） | 工具链 |
| `_g5_relation_write.py` | 外交关系「已知」标志直写（地图全开/全接触，`--apply`/`--restore`/`--status`） | 附加 |
| `_run_elev.py` | UAC 提权启动器（`python _run_elev.py <脚本> <参数...>`） | 工具链 |

### 依赖库（供上面的入口 import）

`probe_battle_env.py`（公共基座：进程锚点/对象定位/读写封装）、`re_b3_inject.py`、`re_f10_stub_builder.py`、`re_c2_faction.py`、`re_h46a.py`、`re_a3_probe.py`、`_prefilter_logic.py`（写 b9 前的过滤判定）、`_probe_g5_relation_chain.py`（外交关系链定位）。

---

## 2. 依赖闭包

```
s2_control_gui   → s2_watch, s2_ai_ctl, s2_spectate, _g5_relation_write,
                   probe_battle_env, re_b3_inject, battle_ai_ctl
s2_spectate      → re_f10_stub_builder, probe_battle_env, _prefilter_logic
_g5_relation_write → _probe_g5_relation_chain
s2_ai_ctl        → probe_battle_env, re_b3_inject, battle_ai_ctl
s2_watch         → probe_battle_env, re_c2_faction
s2_money         → probe_battle_env, re_c2_faction
s2_autowatch     → probe_battle_env, re_c2_faction, s2_watch,
                   re_h46a, re_b3_inject, re_a3_probe
s2_ai_cmd        → probe_battle_env, re_b3_inject
_re_b9_forge     → probe_battle_env（watch 模式）
_re_battle_watcher → probe_battle_env

第三方：numpy（必需）；capstone（仅 re_lib / re_b3_inject 静态模式）
```

---

## 3. 快速开始

```bash
# ① 战斗中把玩家军队交给原生 AI（战斗内运行）
python _run_elev.py s2_ai_ctl.py auto

# ② 看海：把自己的派系交给 CAI（战役大地图运行）
python _run_elev.py s2_watch.py --watch

# ③ 观看 AI 内战
python _run_elev.py _re_b9_forge.py watch

# ③+ 带过滤的看海捕捉（GUI）
python _run_elev.py s2_control_gui.py
```

GUI 的「看海捕捉」区参数：

| 参数 | 说明 | 默认 |
|---|---|---|
| 类型 | `siege`(攻城) / `field`(野战) / `naval`(海战) | siege |
| 规模 | PRE 精确单位数（双方合计，含援军）≥ N 才保留 | — |
| 阵营白名单 | 只保留这些派系参与的战斗（逗号分隔，空 = 全部） | 空 |
| 阵营黑名单 | 排除这些派系（与白名单为 OR 语义） | 空 |
| 自动 ESC | 判定跳过时自动退出（旁观态安全；玩家参战态不退，避免战役层投降损失） | 开 |

命令行等价形式：

```bash
python s2_spectate.py --type siege --factions 织田 --auto-esc --observe 3600
```

---

## 4. 已知限制

- **捕捉原理**：A1 = `FUN_105caa60` 入口 hook（游戏内同 tick 必经点），把过滤前移到写 `[pending+0xb9]=1` 之前。已验证的是**拦截链路**；PRE 单位数取 `[pending+0x90]+0x1c → hdr+0x18/+0x78`（含援军）。
- 规模阈值历史上曾用 `[army+0x294]`，该字段**不是单位总数**（PRE=12 vs POST≈19-20），**已删除**，不要再用。
- 白名单/黑名单的派系名走 `side+0xc` 回退链（旧版只查 `+0x64` 会漏判）；两者为 **OR** 语义。
- **投降保护**：玩家参战（`issp=0`）的低价值局不自动 ESC——ESC 等同投降，会造成战役层损失。
- A1 需要管理员权限；**游戏重启后 hook 失效**，需重新「开始捕捉」。
- 战斗退出靠 ESC 键（PostMessage 扫描码）。
- 工具为研究原型，未做 mod / 武家崛起（ROTS）与武家之殇（FOTS）的全面兼容验证。
