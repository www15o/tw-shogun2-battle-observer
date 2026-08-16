# 幕府将军2：AI 看海 / 观战 / 托管 全套实现
###### Total War: SHOGUN 2 — AI Spectate / Autopilot / Campaign Automation Suite

---

## English (README in Chinese below)

**Watch AI factions fight live in *Total War: SHOGUN 2* (single-player campaign).**

Three goals, all achieved via **direct memory writes** to the 32-bit process (no DLL injection, no cracking):

1. **Battle autopilot** — the native battle AI fully commands *your* army (same level as allied AI)
2. **Campaign auto-play** — your faction is fully managed by the campaign AI (34 turns tested)
3. **AI civil-war spectator** — real-time spectating of AI-vs-AI battles (single-byte `b9` write → engine loads the battle → auto-spectator mode)

Docs are primarily in Chinese (`docs/`, `reports/`). Tools are research-grade Python prototypes requiring **admin rights** (memory read/write). See `CONTRIBUTING.md` for deep-dive directions and dev conventions.

---


**《全面战争：幕府将军2》看海工具**，满足你在全面战争中看海梦，便于进行地图测试、mod平衡性测试、也是广大电子斗蛐蛐爱好者的福音。

目前可以实现：
1、部队转交AI控制，包括自定义战斗或战役战斗。原生AI非脚本AI（现阶段存在命令仅一次性的风险，建议直接使用看海或battle_ai注入，存在一定bug）。
2、战役派系AI化。在战役中转交给AI进行发展、自动过回合、自动扩张，但建议搭配金钱修改等方式让存活率高一点，否则嘎了就没有手操机会了。（问题不大，死后游戏会继续运行，可以看海看到结束）
3、AI战斗捕捉。可捕捉战役中AI之间的战斗，不想错过AI的后期大战？或是看海中想要关注几个派系自然发生的决战？想要研究AI最自然的形态？快来帮忙测试bug吧。 

> **声明（叠甲）**：本项目作者并非专业技术人员（仅非计算机专业基础水平），逆向工作主要依赖 DeepSeek AI Agent 执行，作者负责实机操作、视觉确认与路线纠错。工具均为**研究原型**，仅供单机学习使用；若遇 bug 欢迎 issue，但请理解代码质量与"专业 mod"存在差距。后面可能懒得更新与维护，欢迎同好自由取用。程序目前属于开发阶段，相关局限性补充至后半部分。

## 演示（视频待上传）

<!-- 上传 B站 演示视频后把链接贴这里：
[▶ 演示视频：AI 内战实时观战 + AI 托管 + 看海](你的视频链接)
-->

## 三个目标与原理（人话版）

| 目标 | 效果 | 核心原理 |
|---|---|---|
| **① 战斗中 AI 托管** | 不同于现有三国、战锤类mod，将你的部队交由原生战斗 AI 全权指挥，与友军和敌军同水平 | 直写军队字段，让引擎认为"这支军队本来就是 AI 军队"（`s2_ai_ctl.py`） |
| **② 看海（战役层）** | 你的派系由 战役AI 自主发展，你只旁观 | 直写 `faction+0x6a0=0` + CAI manager，（`s2_watch.py`） |
| **③ 观看 AI 内战** | 战役中 AI 势力互打时，战斗被加载出来供你旁观 | **b9 单字节直写**：伪造分叉输入让引擎走"人类加载链"，引擎发现本地玩家不在参战名单 → 自动旁观（`_re_b9_forge.py`） |

## 快速开始

**前置**：Windows + Python 3 + Steam 版《幕府将军2》（最新版）
依赖：`pip install numpy`（必备）；`pip install capstone`（仅 `re_b3_inject` 的静态分析功能需要，运行时托管/看海/观战不需要）

```bash
# 0. 启动游戏（必须从 Steam 启动），进入任意战役/自定义战斗

# ① 战斗中把玩家军队交给 AI（战斗内运行，管理员权限）
python tools\_run_elev.py s2_ai_ctl.py auto

# ② 看海：把自己的派系交给 CAI（战役内运行）
python tools\_run_elev.py s2_watch.py --watch

# ③ 观战 AI 内战：等 AI 势力开战，战斗会被加载出来（战役内运行）
python tools\_run_elev.py _re_b9_forge.py watch
# 战斗加载后 = 自动旁观视角；Esc 随时退出
```

> 所有工具均需**管理员权限**（内存读写）。`_run_elev.py` 会弹 UAC；也可直接右键"以管理员身份运行"。
> 工具为研究原型，依赖本仓库 `tools/` 下的库（`probe_battle_env` 为公共基座），请勿单独拷走某个 .py。

## 工具清单

| 工具 | 作用 | 目标 |
|---|---|---|
| `s2_ai_ctl.py` | 战斗 AI 托管控制台（auto 全托管 / status / watch 补写） | ① |
| `s2_watch.py` | 看海：faction 直写 + 回合监控（S2/FOTS/ROTS 通用） | ② |
| `_re_b9_forge.py` | b9 单字节直写 → AI 内战加载+旁观（probe/write/watch 三模式 + 海战过滤） | ③ |
| `_re_battle_watcher.py` | 常驻战斗观察器：自动捕获每场战斗数据（攒数据用） | ③ 辅助 |
| `memscan.py` | 通用 32 位进程内存扫描器（定位字段用） | 工具链 |
| `probe_battle_env.py` | 公共基座：进程锚点/对象定位/读写封装 | 依赖 |
| `re_b3_inject.py` / `re_h46a.py` / `re_a3_probe.py` / `re_c2_faction.py` | 内部实现库（供上述工具 import） | 依赖 |
| `re_lib.py` | 静态反汇编分析库（capstone；仅 re_b3_inject 静态模式用） | 依赖 |

## 方法论（逆向怎么做的）

**总体路线**：官方通道优先 → 动态定位 → 静态精读 → 实机验证。全程可复现、可回滚，结论按置信度三档标注（确证 / 推断 / 未核实）。

### 静态分析工具
| 工具 | 用途 |
|---|---|
| **Ghidra + 自研 bridge** | 反编译 `Empire.Retail.dll`（RVA 级函数地图、vtable 表；bridge 支持"强制建函数"模式） |
| **capstone + re_lib.py** | 命令行反汇编（批量字节扫描、调用树追踪） |
| **RPFM** | 读取 `.pack`（提取脚本 / 表 / 文本） |

### 动态分析工具（运行中的游戏）
| 工具 | 用途 |
|---|---|
| **memscan.py** | 全进程内存扫描 + 差分收敛 + 读写（字段定位） |
| **probe_battle_env.py** | 公共基座：进程锚点（find_model）、对象定位、RWPM 封装 |
| **_re_battle_watcher.py** | 常驻观测：每场战斗自动 dump 数据 |
| **_re_b9_forge.py / s2_watch.py / s2_ai_ctl.py** | 直写工具（probe / write / watch 三模式） |

**动态方法**：锚点差分（士兵数 / 国库金币 → 收敛到对象结构）→ 结构解剖（dump 找字段 / 指针链）→ 写实验验证（可回写、崩溃可取证）。

### 主要依赖 Agent 执行
- 逆向的大部分工作——静态反编译阅读、调用链追踪、跨函数结论拼装、内存观测脚本编写——由 **AI Agent（DeepSeek，含子代理）执行**
- **文档体系**：机制地图 + 探索地图 + 报告——支撑多 Agent 会话接力、新会话零损耗续接

## 文档与报告

- `docs/` —— 机制地图（目标1 `11_` / 目标3 `40_`）、探索地图（`13_`/`14_`）
- `reports/` —— 关键逆向报告（加载崩溃点、desc 数据源、人类加载路径、pending battle 判定、战斗类型识别、b9 常驻化）


## 深挖方向/ 现有局限性 / 招募同好

本项目前期只有三个目标，1、完成战斗内的看海。2、完成战略地图的看海。3、完成AI内战的看海。现已完成。



但实际确认下来，深挖空间还很大、局限性也很多（详见 `CONTRIBUTING.md`）也可能会作为后续目标：
- **Mod通用**：当前版本暂未确认是否对mod和武家崛起通用，欢迎测试。
- **战斗AI机制不明朗**：现有AI可能在初期行为符合原生AI，但评估下来存在指挥链断裂的情况。建议先采用战役AI战斗捕捉器嫁接，这种情况下调用的是完整的战斗AI。或采用battle_ai这种意思官方实际测试用的接口（但战役进攻方中会存在跳出天气UI的问题，建议自定义战斗中使用。

- **b9 捕捉率 100%**：外部轮询结构上 <100%，主推引擎 hook（A1 = FUN_105caa60 入口）
- **FOTS / ROTS 差异重验**、**战斗筛选管线精进**（规模/类型/海战过滤）
- **AI战斗行为优化**：枪衾 / 三段击 / 火矢 / 攻门时机等 AI 行为研究
- **其他游戏机制深挖**：可中场切换派系。
- **迁移到更多全战**：本人Steam仅有中世纪2全面战争 / 三国全面战争 / 战锤3（官方通道下限 + 引擎层思路已成型），其余全战系列如有需求还请各位大佬协助。

**想一起深挖游戏看海的欢迎加群**：[QQ 群号待填]（或 Bilibili同名/issue 联系）。群管理开放认领——我会把深挖方向列清楚，一起玩的人接手维护。

## 免责与合规

- 本仓库仅含**工具脚本 + 方法文档**，不含任何游戏二进制、不包含游戏提取资产、不涉及破解/DRM。
- 内存读写仅作用于**单机游戏进程**，属 mod 性质的个人研究用途；请勿用于任何在线/对战场景。
- 对逆向过程中涉及的 CA（Creative Assembly）版权内容，仅保留方法描述，不保留游戏文件。
