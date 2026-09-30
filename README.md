# Shogun2 Observer v0.1.0 — 幕府将军2：AI 看海 / 观战 / 托管 全套实现
###### Shogun2 Observer（幕府2 观察者）— Total War: SHOGUN 2 AI Spectate / Autopilot Suite

> **产品名**：**Shogun2 Observer（幕府2 观察者）**，版本 **0.1.0**（程序内版本号以 `VERSION` 为唯一真源）。
> 旧名 **TW Observer** 已废弃，请以 **Shogun2 Observer** 为准。

---

## English

**Watch AI factions fight live in *Total War: SHOGUN 2* (single-player campaign).**

Three goals, all reached via **direct memory writes** to the 32-bit process (no DLL injection, no cracking, no DRM circumvention):

1. **Battle autopilot** — the *native* battle AI fully commands *your* army, on par with allied AI
2. **Campaign auto-play ("watching the sea")** — your faction is fully managed by the campaign AI
3. **AI civil-war spectator** — AI-vs-AI battles are force-loaded and auto-spectated (a single-byte `b9` write)

Plus a **pre-write filter** (type / scale / faction) so only the battles you care about get loaded.

Docs are primarily in Chinese (`docs/`, `reports/`). Tools are research-grade Python prototypes requiring **admin rights** (memory read/write). See `CONTRIBUTING.md` for deep-dive directions and dev conventions.

**Product**: **Shogun2 Observer** (幕府2 观察者) **v0.1.0** — formerly *TW Observer* (that name is retired). A **packaged `.exe`** exists, built with an embedded `requireAdministrator` manifest; **no GitHub Release has been published for this round yet**. **Administrator rights are mandatory**: the tools open the game process and read/write its memory. Runtime and crash logs are written to `%APPDATA%\Shogun2Observer\logs`.

> **Verification note**: the three goals were validated by the author on real gameplay (dates below). The current release is a **re-packaging of already-verified work** — it has **not** been re-tested live. Treat "works" claims as *author-verified at the stated date*, not *re-verified today*.

---

**《全面战争：幕府将军2》看海工具**，满足你在全面战争中看海梦，便于进行地图测试、mod 平衡性测试、也是广大电子斗蛐蛐爱好者的福音。

目前可以实现：

1. **部队转交 AI 控制**——自定义战斗或战役战斗，把你的部队交由**原生战斗 AI** 全权指挥（与友军、敌军同水平）。
2. **战役派系 AI 化（看海）**——战役中把你的派系交给 CAI 自主发展、自动过回合、自动扩张。建议搭配金钱修改提高存活率，否则被灭了就没有手操机会了（问题不大，死后游戏继续运行，可以看海看到结束）。
3. **AI 战斗捕捉**——捕捉战役中 AI 之间的战斗并旁观。不想错过 AI 的后期大战？看海中想盯住几个派系的决战？或是想研究 AI 最自然的形态？都可以。

> **声明（叠甲）**：本项目作者并非专业技术人员（仅非计算机专业基础水平），逆向工作主要依赖 DeepSeek AI Agent 执行，作者负责实机操作、视觉确认与路线纠错。工具均为**研究原型**，仅供单机学习使用；若遇 bug 欢迎 issue，但请理解代码质量与"专业 mod"存在差距。程序目前属于开发阶段，局限性见后半部分。

## 演示（视频待上传）

<!-- 上传 B站 演示视频后把链接贴这里：
[▶ 演示视频：AI 内战实时观战 + AI 托管 + 看海](你的视频链接)
-->

## 三个目标与原理（人话版）

| 目标 | 效果 | 核心原理 | 实测状态 |
|---|---|---|---|
| **① 战斗中 AI 托管** | 将你的部队交由原生战斗 AI 全权指挥 | 直写军队字段，让引擎认为"这支军队本来就是 AI 军队" | ✅ 已达成 |
| **② 看海（战役层）** | 你的派系由战役 AI 自主发展，你只旁观 | `faction+0x6a0=0`（非人类）+ manager 表该派系 `FULL_MANAGER`，**成对写** | ✅ 已达成（织田 AI 自主 34 回合） |
| **③ 观看 AI 内战** | 战役中 AI 势力互打时，战斗被加载出来供你旁观 | **`b9` 单字节直写**：伪造分叉输入让引擎走"人类加载链"，引擎发现本地玩家不在参战名单 → 自动旁观 | ✅ 已达成 |
| **③+ 写前过滤** | 只加载你想要的战斗 | A1 = `FUN_105caa60` 入口 hook，在**写 `b9` 之前**按类型/规模/阵营判定 | ✅ 已达成（拦截链路已验证） |

> 三个目标的机制细节见 `docs/`：`21`（看海）→ `11`（战斗托管）→ `61`（过滤）→ `40`（AI 内战，最深）。

## 快速开始

### 运行方式

| 方式 | 说明 |
|---|---|
| **打包 exe** | 已有打包好的 `Shogun2 Observer.exe`（内嵌 `requireAdministrator` 清单，双击即弹 UAC）。**本轮尚未建 GitHub Release**，后续版本会挂到 Releases |
| **源码直跑** | 需要 Windows + Python 3，按下文命令运行（同样需要管理员权限） |

> ⚠️ **必须使用管理员权限**：所有工具都要读写游戏进程内存（`OpenProcess` + `ReadProcessMemory` / `WriteProcessMemory`），非管理员运行会打不开进程。`_run_elev.py` 会弹 UAC 提权。
> 📄 **日志位置**：运行日志与崩溃栈写入 `%APPDATA%\Shogun2Observer\logs`（在资源管理器地址栏输入 `%APPDATA%` 即可直达）。

**前置**：Windows + Python 3 + Steam 版《幕府将军2》

依赖：`pip install numpy`（必备）；`pip install capstone`（仅静态分析模式需要，运行时托管/看海/观战不需要）

```bash
# 0. 从 Steam 启动游戏，进入任意战役 / 自定义战斗

# ① 战斗中把玩家军队交给 AI（战斗内运行，管理员权限）
python tools\_run_elev.py s2_ai_ctl.py auto

# ② 看海：把自己的派系交给 CAI（战役大地图运行）
python tools\_run_elev.py s2_watch.py --watch

# ③ 观战 AI 内战：等 AI 势力开战，战斗会被加载出来（战役大地图运行）
python tools\_run_elev.py _re_b9_forge.py watch

# ③+ 推荐：带过滤的图形控制台（整合全部功能）
python tools\_run_elev.py s2_control_gui.py
```

> 所有工具均需**管理员权限**（内存读写）。`_run_elev.py` 会弹 UAC；也可直接右键"以管理员身份运行"。
> ⚠️ **请勿单独拷走某个 .py**——入口脚本依赖同目录模块，详见 `tools/README.md`。
> ⚠️ **引擎偏移 profile 必须随库保留**：`tools/old_engine_support/profiles/` 下的 `Shogun2.dll_*.json` 是工具定位引擎所必需的偏移表，**删掉工具就跑不起来**。
> 游戏路径不硬编码：自动从 Steam 注册表探测，也可用 `SHOGUN2_DIR` / `SHOGUN2_DLL` 指定。

## 工具清单

完整说明（含依赖闭包图、参数表、已知限制）见 **[`tools/README.md`](tools/README.md)**。

| 工具 | 作用 | 目标 |
|---|---|---|
| `s2_control_gui.py` | **图形控制台**：加钱 / AI 化看海 / 战斗 AI 注入 / 看海捕捉 | ①②③ |
| `s2_ai_ctl.py` | 战斗 AI 托管控制台（auto 全托管 / status / watch 补写） | ① |
| `s2_watch.py` | 看海：faction 直写 + 回合监控（S2/FOTS/ROTS） | ② |
| `s2_spectate.py` | **看海捕捉器**：A1 hook + 类型/规模/阵营过滤 + 自动 ESC | ③ 过滤 |
| `_re_b9_forge.py` | `b9` 单字节直写（probe/write/watch 三模式 + 海战过滤） | ③ |
| `_re_battle_watcher.py` | 常驻战斗观察器：自动捕获每场战斗数据 | ③ 辅助 |
| `memscan.py` | 通用 32 位进程内存扫描器（定位字段用） | 工具链 |
| `probe_battle_env.py` 等 | 公共基座与内部实现库（供上面工具 import） | 依赖 |

## 方法论（逆向怎么做的）

**总体路线**：官方通道优先 → 动态定位 → 静态精读 → 实机验证。全程可复现、可回滚，结论按置信度三档标注（**已确证 / 推断 / 未核实**）。

### 静态分析工具
| 工具 | 用途 |
|---|---|
| **Ghidra + 自研 bridge** | 反编译 `Empire.Retail.dll`（RVA 级函数地图、vtable 表；bridge 支持"强制建函数"模式） |
| **capstone + `re_lib.py`** | 命令行反汇编（批量字节扫描、调用树追踪） |
| **RPFM** | 读取 `.pack`（提取脚本 / 表 / 文本） |

### 动态分析工具（运行中的游戏）
| 工具 | 用途 |
|---|---|
| `memscan.py` | 全进程内存扫描 + 差分收敛 + 读写（字段定位） |
| `probe_battle_env.py` | 公共基座：进程锚点（find_model）、对象定位、RWPM 封装 |
| `_re_battle_watcher.py` | 常驻观测：每场战斗自动 dump 数据 |
| `s2_spectate.py` / `_re_b9_forge.py` / `s2_watch.py` / `s2_ai_ctl.py` | 直写工具（probe / write / watch 三模式） |

**动态方法**：锚点差分（士兵数 / 国库金币 → 收敛到对象结构）→ 结构解剖（dump 找字段 / 指针链）→ 写实验验证（可回写、崩溃可取证）。

> 一套血泪教训：**反编译偏移 ≠ 内存布局**。多函数偏移拼接前必须先验证**对象同一性**，否则会把不同对象的字段当成一条链。

### 主要依赖 Agent 执行
- 逆向的大部分工作——静态反编译阅读、调用链追踪、跨函数结论拼装、内存观测脚本编写——由 **AI Agent（DeepSeek，含子代理）执行**
- **文档体系**：机制地图 + 探索地图 + 报告——支撑多 Agent 会话接力、新会话零损耗续接

## 文档与报告

- `docs/README.md` —— **文档地图、证据等级约定与引用规则**（建议先读这份）
- `docs/` —— 机制地图（目标1 `11` / 目标2 `21` / 目标3 `40` / 过滤 `61`）、探索地图（`13`/`14`）
- `reports/` —— 关键逆向报告（加载崩溃点、desc 数据源、人类加载路径、pending battle 判定、战斗类型识别、`b9` 常驻化）

## 现有局限性与深挖方向

三个目标（**战斗内托管、战略地图看海、AI 内战观看**）均已完成。但深挖空间仍然很大，详见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

**已知局限**
- **Mod 通用性**：尚未确认对大型 mod 与武家崛起（ROTS）/ 武家之殇（FOTS）是否通用，欢迎测试。
- **工具未复测**：本版是已通过实机验证成果的**重新打包**，未做逐项回归测试。
- **规模过滤的边界**：PRE 精确单位数已可用（含援军），但多 force / 援军合计的极端编成仍待实机对照。

**深挖方向**
- **捕捉链路精进**：A1 已把过滤前移到写 `b9` 之前；海战 / 攻城 / 野战类型判据可继续细化。
- **FOTS / ROTS 差异重验**。
- **AI 行为质量研究**：枪衾 / 三段击 / 火矢 / 攻门时机等——**目前仍在研究中，结论未定，文档暂不发布**。
- **迁移到更多全战**：本人 Steam 仅有中世纪2 / 三国 / 战锤3；其余全战系列如有需求欢迎协助。

**想一起深挖游戏看海的欢迎加群**：QQ 群 `1104022796`（或 Bilibili 同名 / issue 联系）。群管理开放认领——我会把深挖方向列清楚，一起玩的人接手维护。

## 免责与合规

- 本仓库仅含**工具脚本 + 方法文档**，不含任何游戏二进制、不包含游戏提取资产、不涉及破解/DRM。
- 内存读写仅作用于**单机游戏进程**，属 mod 性质的个人研究用途；请勿用于任何在线/对战场景。
- 对逆向过程中涉及的 CA（Creative Assembly）版权内容，仅保留方法描述，不保留游戏文件。
- 工具为研究原型，**使用风险自负**；请先备份存档。
