# 目标1 战斗 AI 机制地图（11_GOAL1_BATTLE_AI_MAP）

> 主题：**让玩家部队在战场上成为真正的 AI 部队**（目标1：战役战斗中玩家部队由原生战斗 AI 指挥）。
> 本文档只收录当前成立的结论，按游戏机制链组织。
> 历史记录见 **docs/12_GOAL1_LOGBOOK.md**。
> 最后更新：2026-08-13。来源：re_ba 系列报告 + 03_DISCOVERIES。

---

## 0. 机制链总览

```
根开关层     battle_ai 值字节 [base+0x18d2d88]+0x5c（引擎内 0 写路径，仅外部注入可置 1）
              消费者唯一 = 0x1adf50
                ▼
决策层       0x1adf50（军队 AI 控制决策，写 0x208 步长「记录」）
              三输入（battle_ai / test_ai_build / 全局字节恒0）+ invert
              输出 记录+0x12c=0/+0x148=1（AI 接管标志集）
              重跑：加载期（0x9a3fa）+ 事件驱动（FUN_10316ef0，门 env+0x281d4==2）
                ▼
拷贝层        army 构造器 0x10bb50（加载期构造时拷贝一次）
              记录+0x12c→army+0x270  +0x148→+0x28c  +0x14c→+0x290
              +0x150→+0x294  +0x13c/140/144→+0x280/284/288
                ▼
建立层        0x162b80（战斗初始化，0x9a3fa 之后）
              建组（0x38 条目→new(0x6c)）→ 建 army（new(0x344)）→
              建控制器判定：任一记录+0x12c!=0 → 必建；全 AI 且
              [st+4]+0x9f==0（恒0）→ 必建 → 【全 AI 组也建控制器】
                ▼
执行层        控制器 [group+0xc] = AI Script Controller（0x66c）
              Update 0x506ef0（运行时派发）：
                +0x634 早退门 → 收集器 0x51f670（ea8==1→+0x30）
                → 单位 tick 0x1e7c80/0x1e8790（表驱动）
                → 计划链 0x520140→0x506e00（AIBattleAnalyser 0/1/2/3）
                → 分发表 0x50f860 → order 单 → 0x509b10 每 tick 下令
                ▼
单位行动层     实体 AI 本能（锁最近敌冲锋）
              命令通道：0x1b3b70 gate（c01/ea8）→ 0x186c60 下令（cmd 0..3）
              单位 tick 目标数组 → 0x202080/0x201670 近距查询 → 0x4fa3d0 移动令
                ▼
副作用门控     等待门 FUN_10182100（NumHumans 计 a270!=0&&a28c==1）
              天气执行器 FUN_110e58a0（只服务 a270!=0 军队）
              变速锁（AI 军队 a270=0 锁变速）
              SWITCH_AI 残留消息周期重派发（~1Hz → 清 count 即关掉）
```

**两条有效激活路径（机制等价）**：

| 路径 | 记录 +0x12c | army 字段 | 控制器 | 单位行动 | 天气 UI |
|---|---|---|---|---|---|
| **battle_ai 战前触发**（值字节=1） | 0（引擎写） | 拷贝自记录（0x270=0 等） | 建（0x162b80） | 出生即 AI + 计划链 + 命令通道 | 卡/残留（无可靠解法，见 §7.3） |
| **直写**（RE-B3，部署后 8 字段） | 默认（人控语义） | 手动写（同值） | 建（组含人控军队必建） | 同左（部署后字段生效） | 无（部署后注入，天气阶段已过） |

两条路径在计划链与命令落地层逐位相同，与正常敌人 AI 完全同等待遇。

---

## 1. 根开关层：battle_ai 命令体系

### 1.1 命令对象

- battle_ai = tweaker 命令 BATTLE_AI_EXCLUSIVE（值 0xf8），对象 `[base+0x18d2d88]`，**值字节 +0x5c**，set 标志 +0x59；vtable 0x115a9cf8（通用命令对象族）
- getter `0xa0b70` = `mov al,[ecx+0x5c]; ret`（427 个调用点，按 ECX 立即数区分命令对象）
- **引擎内 0 写路径**（外部注入才可能=1）：0x18d2d88 全 DLL 仅 3 处引用（注册 0xf03b / 消费 0x1adf58 / BCQ thunk 0x156be90 死入口）；+0x5c 无直接内存访问；slot2 写回 0x8c0b0 无调用者
- **残留语义**：值字节是进程级内存态——一旦被外部注入 =1，读档/重进战役不重置（每次战斗加载 0x1adf50 都读到 1）

### 1.2 家族命令

| 命令 | 值 | 对象 RVA | 消费者 | 语义 |
|---|---|---|---|---|
| BATTLE_AI_EXCLUSIVE | 0xf8 | 0x18d2d88 | **仅 0x1adf50** | AI vs AI（军队 AI 化） |
| BATTLE_AI_INVERT | 0xf9 | 0x18d2468 | 0x1adf50 + 0x11de20 | 反转控制标志（仅 battle_ai=0 时生效） |
| AI_FORCE_ATTACK_PLAN | 0x33 | 0x1a69d70 | 0x5171e0 | 强制进攻计划（返回 0） |
| AI_FORCE_DEFENCE_PLAN | 0x34 | 0x1a69ef8 | 0x5171e0 | 强制防守计划（返回 1） |
| AI_FORCE_WITHDRAW_PLAN | 0x35 | 0x1a69f58 | 0x5171e0 | 强制撤退计划（返回 2） |

### 1.3 观感判别

- 同款观感的两根因：A=值字节=1（0x1adf50 全军 AI）；B=军队 a270=0 残留（等待门+天气执行器）
- 判别工具：`work/re_ba_probe.py`（只读：值字节/test_ai_build/全局字节 + 战斗军队字段）

---

## 2. 决策层：0x1adf50

### 2.1 决策链

```
FUN_101adf50(param_1=记录 0x208 步长, param_2=槽+0x10 指针)  __thiscall
  ① getter battle_ai 值字节（0x118d2d88+0x5c）→ 非 0 → AI 接管
  ② test_ai_build（0x1192d198 via 0x140150，dword）→ 非 0 → AI 接管
  ③ 全局字节 [0x118d1b5e] → 非 0 → AI 接管（无写入者，恒 0）
  ④ 全关 → local_35 = 槽+0x10（控制字节）+ invert 反转
  输出（写 param_1 记录）：
    +0x12c = local_35（byte 控制标志：0=AI）
    +0x148 = (local_35==0)（dword NOT 标志：1=AI）
    +0x13c/+0x140/+0x144 = 槽+0x20/+0x24/+0x28
    +0x14c = 1.0f、+0x150 = -1
  递归：for i in [param_1+0x15c]: 0x1adf50(this=[param_1+0x160]+i*0x208, param_2)
```

### 2.2 重跑与补写

- 0x1adf50 **无自身 gate**：每次调用无条件重写记录全字段
- 部署期重跑源 = **FUN_10316ef0**（env 更新虚方法，门控 `param_2!=0 && env+0x281d4==2`）：自拷贝路径读记录旧字节（保持现值）；批路径 FUN_10112950 按槽字节全量重写（临时槽表 slot[0] 0↔1 翻转，side-swap 语义）——只写记录会被玩家槽=1 翻回
- **战斗中稳写目标**：槽+0x10=0（主，防批路径翻转）+ 记录+0x12c=0（辅）；全部槽写 0 可无视 slot[0] 翻转；事件驱动非每帧

### 2.3 槽/记录定位链

```
env (e8)：
  env+0x28060 = 容器数组对象 {cap,count,组记录表 ptr}
  env+0x281d0 = 槽表数组对象 {cap,count,槽表 ptr}；env+0x281d4=2（重写门控值）
  记录[g][a] = [[env+0x28068] + g*0x38 + 8] + a*0x208
  槽[g]      = [env+0x281d0+8] + g*0x38（槽+0x10 = 源字节）
st：  [st+4] = 组容器；[st+4]+8 = 组记录表（0x38 步长）
group： [group+0xc] = AI 控制器（0x66c）；控制器+0x24 = e8
```
- 槽结构（0x38 步长，构造器 0x8ed30）：+0x00=组索引、+0x04=组内军队索引、**+0x10=控制字节（1=人类/0=AI）**、+0x14=侧名、+0x20/24/28=3 dword、+0x2c=!控制字节、+0x30=1.0f、+0x34=-1

---

## 3. 拷贝层：记录 → army

- 记录（0x208 步长军队控制块，0x1adf50 输出）与 battle army（new(0x344)，vtable 0x115bb2c0，存 [group+0x24]）是**两个不同对象**
- army 构造器 `0x10bb50` 逐字段拷贝（加载期构造时拷贝一次）：

| 记录 | → | army |
|---|---|---|
| +0x12c（byte 标志） | → | **+0x270（a270 人类标志）** |
| +0x148（NOT） | → | **+0x28c** |
| +0x14c（1.0f） | → | **+0x290（a290 变速值）** |
| +0x150（-1） | → | **+0x294** |
| +0x13c/+0x140/+0x144 | → | +0x280/+0x284/+0x288 |

- 与 SWITCH_AI handler 0x2abeb0 运行时重写字段逐项对应（handler 写 army 同字段同值）

---

## 4. 建立层：0x162b80（战斗初始化）

### 4.1 调用链与内容

```
FUN_10099d50（战斗模式分派器）
  → 0x9a336  组容器构造（+0x9f=[0x118d1751] 恒 0）
  → 0x9a3fa  FUN_10112950 → 0x1adf50（写全部记录，先于建 army）
  → 0x9a506  env 构造（new(0x28350)）
  → FUN_10122180 → 0x122c36 → 0x162b80
0x162b80（this=st）：
  ① 按 [st+4] 的 0x38 步长条目表逐条 new(0x6c) 组对象（构造 0x10b610）→ [st+0x88]/[st+0x8c] 组表
  ② 每组建 army（new(0x344)+0x10bb50 拷贝记录）→ [group+0x20]/[group+0x24]
  ③ 建控制器判定（0x162cf0-0x162d3e）：遍历组内记录 +0x12c——
     任一 !=0（人控）→ 必建；全部 ==0（全 AI）且 [st+4]+0x9f==0 → 建；+0x9f!=0 → 不建
  ④ 对有控制器组算 [st+0x379] 组间阵营关系
```

### 4.2 关键开关

- `[st+4]+0x9f` = 组容器构造器 0x11bb90 的 arg7 ← FUN_1014ef00 ← **全局字节 [0x118d1751]**（.data 未初始化、全 .text 无写入者、恒 0）→ **全 AI 组必建控制器**（含玩家派系组）
- 时序：0x9a3fa（0x1adf50 写记录）**先于** 0x162b80（建 army+控制器）→ 控制器判定必然看到决策后的 +0x12c

---

## 5. 执行层：AI Script Controller（[group+0xc]，0x66c）

### 5.1 结构

- 构造器 0x4958b0（调用者 0x162d2f 战斗初始化 / 0x2abf1d SWITCH_AI 懒建）；vtable 0x115af7fc（17 槽通用接口族）
- 内部：子管理器 +0x28/+0x2c/+0x30/+0x34（**+0x34=单位 AI tick 管理器**）；3×0xe8 子 AI 块 +0x3c/+0x124/+0x20c；计划对象 **+0x44c**（构造 0x4954c0）；近战分析 +0x468；**per-unit 注册表 +0x65c/+0x660 环形链表**（节点 [+0]=prev [+4]=next [+8]=key [+0xc]=per-unit 对象 0x545fb0）；**+0x634 全军皆人控标志**（0x4db700 重算：遍历 army 全 a270!=0→1）；+0x624/+0x628 计数器（每 tick +1）

### 5.2 Update 0x506ef0 全 tick 图

```
0x506ef0 Update（运行时函数指针派发：全文件 0 静态引用）
  ① 0x4db700 重算 +0x634
  ② 早退门：+0x634!=0（全人控）且 环空 → return（不计数！）
  ③ 0x51f670 收集器：groups→armies→units，unit+0xea8==1 → append 进 +0x30 注册
     （异侧组且身份命中 [e8+0x40] 我方名单——援军/盟军——→ +0x18/+0x1c/+0x20 数组+块2）
  ④ 节奏化单位 tick：0x1e7c70 → 0x1e8790（预扫）+ 0x1e7c80（推进目标）
  ⑤ 计划链 0x520140 → 0x506e00（计划对象 +0x44c，AIBattleAnalyser 0x5171e0 输出 0/1/2/3）
     → 按 [+0x45c] 分派：0→0x5041d0(计划码8/9) 1→0x504200(2/4) 2→0x228630(0xe) → 写 [控制器+0x5ec]
  ⑥ 0x50f860 分发表（plan 2→0x50f5b0 防守 / 4,8,0xa→0x50f3e0 意图码单 / 9→0x50f360 逐军进攻推进单
     / 7→0x50fa40 / 0xe→0x50ffe0 撤退单）→ order 单注册子管理器 A（+0x28）
     → 0x509b10 每 tick 更新（共同槽 0x50a4c0 写 unit+0xc04/sub+0x1163 认领标志 + 0x186c60 下令）
  ⑦ 0x4ce4a0 每 10 tick 直推 0x186300(unit,2,&pos) 防守姿态单 → unit+0x444（队列门 [u+0x9ec]<0xa）
  ⑧ 撤退/胜负 0x4fc1b0（伤亡比 +0x5e8*100/+0x5b0>50%）
```

### 5.3 per-unit 注册表（恒空是引擎设计）

- 填充唯一入口 = BCQ 命令族（全门控 [group+0xc]!=0）：BCQ_CREATE_AI_SCRIPT_CONTROLLER（0x2a6ca0→0x4d2640，内部引擎分配器 new(0x38)+0x545fb0）/ BCQ_ADD_UNIT_TO_AI_SCRIPT_CONTROLLER（0x2a5aa0→0x4b94e0→0x5465f0 置 unit+0xea8/+0xc01=1）/ BCQ_ADD_SHIP（0x2a5960→0x546560）/ BCQ_DESTROY（0x2a6e10→0x4da330）
- **正常战役/自定义战斗中注册表恒空**：引擎 DLL 无这些命令的发送器（注册表函数全文件零引用、hash getter thunk 零调用者、命令名仅注册处引用）；.pack 脚本层同样零引用（58 pack 明文 + 136 提取脚本全扫 0 命中）——**双层闭合**。该命令族只服务脚本化战斗
- per-unit 状态机（0x50fb70 逐节点更新 + 0x546770 订单链 0x54f970）只在注册表非空时运行，与战役战斗无关
- unit+0xea8/+0xc01 字段的语义根源 = 创建期绑定（见 §6.1）与 ADD_UNIT 注册副作用

---

## 6. 单位行动层

### 6.1 单位状态与行动驱动

- **单位「出生即 AI」绑定**：单位/子对象创建初始化按 `[army+0x270]==0` 绑定 AI 标志——单位创建 0x12af31 写 ea8=1/c01=1、子对象 0x35e424 写 1168/1160=1；army+0x270 只在加载期由记录 +0x12c 拷贝（0x10bb50）
- **ea8 三值语义**：0=人控、1=AI、2=SCRIPTED（0x4d7bcd 日志字符串「SCRIPTED」@0x115eba80）；写入者全集 = 创建绑定（0x12a3c8=0/0x12af41=1）+ SWITCH_AI（0x2ac022=1）+ UNIT_CHANGE_CONTROL_STATUS（0x2aae09=参数）+ ADD_UNIT（0x5465fc=1）+ 解除（0x2d533a/0x54619a/0x546675=0）
- 新单位初始 ea8=0；**援军补 ea8 即被表驱动单位 tick 覆盖**（最低充分条件 = 出现在军队表 + ea8=1）

**三条行动驱动**：

| 驱动 | 输入 | 行为 | 依赖 |
|---|---|---|---|
| 实体 AI 本能（battle_entity 层） | 锁最近敌 | 冲锋/接敌 | unit+0xea8=1 等字段 |
| 命令通道 | 0x1b3b70 gate + 计划链 order 单 | 0x186c60(unit, cmd 0..3) 下令（0=HALT） | c01!=0 && ea8==1（gate） |
| 单位 tick | 表驱动三层循环 | 推进目标数组 → 0x202080/0x201670 近距查询 → 0x4fa3d0 移动令 | 控制器在跑（0x1e7c80 唯一调用链 0x506ff3） |

### 6.2 AI 部队的完整构成

- **0x1b3b70 是「单位可被 AI 下令」gate**：门控 c01!=0 && ea8==1 && 0x181e50 有效性 → 查 [unit+0xed4] 订单集（vtable 0x115cfeec，slot1=IsOrderEnabled）命令码 1/0/6
- **真下命令 = 0x186c60(unit, cmd 0..3)**（0=HALT，BCQ_UNIT_ISSUE_HALT_COMMAND handler 0x2ab450 直接调用）；6 调用者全 AI/per-unit 区运行时派发，0x50a4c0 门控通过后执行 0x186c60(unit,2)
- **命令落地字段**：[unit+0xeb0]/[+0x444] 状态对象/battle-orders 队列（[unit+0x14]→[+8]→[+0x98]→[+0x1014] slot2 {unit,0,9}）/unit+0xb64/+0xb80/sub+0x674——不查 ea8 与注册表；unit+0x1508 唯一写入在创建期（0x35de5a）；朝向 +0x6c4/+0x6dc 由实体层（0x3aae36/0x3aeae6）写
- **单位 tick 目标数组消费方 = 0x202080/0x201670**（对 {cnt@+0x14,data@+0x18,0x14B/条} 数组做单位↔条目近距查询 → 命中 → 0x4fa3d0([unit+0x114c],目标,8,0) 下移动令；0x4ef74e gate 命中→不可行动）
- **「真正 AI 部队」完整定义** = 单位字段（出生绑定 c01/ea8=1）+ 控制器（0x162b80 必建）+ 计划链（order 单 + 0x186c60 下令）+ 单位 tick（0x202080 消费移动令）+ 实体 AI——**战前触发与直写都达到，与正常敌人 AI 完全同等待遇**
- 「等待友军协同」的完整版（per-unit 状态机 0x50fb70 约束实体本能）只在脚本化战斗存在；战役战斗中 AI 军队的行为 = 计划链 order 单 + 实体 AI 本能 + 单位 tick 移动令（引擎对 AI 军队的标准行为）

---

## 7. 副作用与门控

### 7.1 阶段等待门

- `FUN_10182100`：NumHumansRequestingNextPhase = `FUN_10162a30` 计 `[army+0x270]!=0 && [army+0x28c]==1` → 计数==0 → 放行 → **battle_ai 0→3→5 自动跳 + 结算自动跳的统一开关**

### 7.2 部署判定

- `FUN_1017c2f0` 遍历读 0x208 记录 +0x12c（任一≠0 → 有人类军队）→ 全 0 → FUN_1014f0b0 自动路径 → state3 处理器 FUN_101ba420 自动推进

### 7.3 天气 UI

- 机制链：进攻战 → env+0x2819e=1（天气流程门控）→ 天气执行器 **FUN_110e58a0 只对 a270!=0（人类）军队发 BCQ_WEATHER_SELECTION_ATTACK** → 全军 AI → SELECTION 永不发送 → ws 天气状态对象 +8 永不置 1 → ws tick 永空闲 → pre_battle_wait 面板按钮每帧可见 → 叠加等待门跳过天气阶段 → 天气 UI 与 env 状态机脱节 → 点「进部署」黑屏（P23）
- 处理现状：
  - 游戏内隐藏（点窗外/隐藏 UI 快捷键）：无效（H20b' 用户实测）
  - ws 字段直写（+8/+0xb）：无效（P23-5 进攻战实机：窗口/按钮照常，点击仍黑屏）
  - 「天气界面观战模式」：可用（R2 实机：窗口遮挡但战斗可完整观看+操作）——唯一实机确认状态
- 规避：战斗部署后（阶段 1 已过）再 AI 化（直写）→ 不碰天气 UI；防守战 env+0x2819e=0 天然无天气 UI

### 7.4 变速锁

- BCQ_SET_TIME_MULTIPLIER handler=0x2a9a80 写 [army+0x290]；AI 激活军队（a270=0）被引擎锁变速（P25/P29 双向验证：a270 改回 1 → 单位停 AI + 变速恢复）——引擎对 AI 军队的设计约束

### 7.5 战斗 AI 代理关闭

- SWITCH_AI handler 0x2abeb0（函数边界 0x2abeb0-0x2ac0a6，含 ST_SWITCHED 写入）每次派发整体重写全套激活字段
- 重投源 = 残留消息周期重派发：派发器 0x320870 处理完**不清 count/cursor**，0x303380 门控含计数 + 浮点时间比较（~1Hz，与「1 秒内被改回」吻合）
- **解法**：写 count=0 清残留消息（队列 `[[0x118d8880]+0x3a4]+0x28048`，count@+0x28024）+ 写 army+0x270=1 触发引擎自带解除 AI 路径（0x2d5290/0x546160 族，被 a270 门控）+ battle_ai 值字节=0

---

## 8. 注入通道现状

| 路线 | 机制 | 状态 | 天气 UI | 战术质量 |
|---|---|---|---|---|
| **直写（RE-B3）** 8 字段 + st+0x31f0 | 部署后 WriteProcessMemory | ✅ 目标1 实机可用（可看整场）；记录标志缺位（判定链错位） | ✅ 无 | 与 battle_ai 等价（计划链/命令落地逐位相同） |
| **战前触发**（battle_ai 值字节=1） | 加载前写 → 引擎自行完成 记录+army+控制器 → 单位出生即 AI | ✅ 机制确证（全链），待实机验收 | 卡/残留（见 §7.3） | **与正常敌人 AI 完全同等待遇** |
| **补记录（槽源字节）** | 写 env+0x281d0 槽表槽+0x10=0 + 记录+0x12c=0 | ❌ 只让判定链自洽（FUN_1017c2f0 全 AI 语义），**不影响单位战术** | ✅ 无 | 不变（需配合直写 ea8） |
| **补注册表（BCQ/远程线程）** | 0x4d2640/0x4b94e0 序列 / 消息注入 | ❌ 关闭：注册表恒空是引擎设计（引擎+脚本双层无发送器），per-unit 状态机与战役战斗无关 | — | 补了也无效 |
| **AI_FORCE_*_PLAN 注入** | 值字节 0x33/0x34/0x35 | **战术倾向微调工具**：战斗内写 ≤10 tick 生效（0x5171e0 每次计划 tick 直读）；force defence=关闭进攻推进单（E1：AI 不攻城） | — | 战术倾向控制 |
| **行为控制台（数据写通道，2026-08-14 实机）** | WPM 直写 阈值 0x115c26ac / 计划码 [5ec]/[45c] / 撤退点 [3a8]/[3ac] / 状态码 | ✅ 阈值写（.rdata+VirtualProtectEx）与计划码 [45c]（33Hz 循环）实机通道通；**远程线程代码执行 ❌ 静默退出（通道废弃）** | ✅ 无 | 阈值防 hold / 撤退点 / 城墙回归（周期 0x186300）；计划码=速度调整非行为开关 |

---

## 9. 关键地址速查

| 机制 | RVA/地址 | 说明 |
|---|---|---|
| battle_ai 命令对象 | 0x18d2d88（+0x5c 值字节） | 根开关（引擎内 0 写路径） |
| getter | 0xa0b70 | mov al,[ecx+0x5c]; ret（427 调用点） |
| 决策函数 | 0x1adf50 | 军队 AI 控制决策（写 0x208 记录） |
| 槽表 | env+0x281d0（槽+0x10=控制字节） | 0x1adf50 的 param_2 源 |
| 记录 | 0x208 步长 | 军队控制块（[[env+0x28068]+g*0x38+8]+a*0x208） |
| army 构造器 | 0x10bb50 | 记录→army 拷贝（+0x12c→+0x270 等 6 字段） |
| 战斗初始化 | 0x162b80 | 建组/army/控制器（调用者 0x122c36） |
| +0x9f 源 | 0x118d1751 | 全局字节恒 0（0x11bb90 arg7 ← 0x14ef00） |
| 控制器 | 0x66c 对象（[group+0xc]） | AI Script Controller，构造 0x4958b0，vtable 0x115af7fc |
| 控制器 Update | 0x506ef0 | 运行时派发（全文件 0 静态引用） |
| 收集器 | 0x51f670 | ea8==1 → +0x30 注册 |
| 单位 tick | 0x1e7c70/0x1e7c80/0x1e8790 | 表驱动推进目标 |
| 计划链 | 0x520140/0x506e00/0x5171e0 | AIBattleAnalyser 0/1/2/3 |
| 分发表 | 0x50f860（0x50f5b0/0x50f3e0/0x50f360/0x50fa40/0x50ffe0） | order 单构建 |
| 下令 | 0x186c60（cmd 0..3）/ 0x1b3b70 gate | 0=HALT（BCQ_UNIT_ISSUE_HALT_COMMAND 0x2ab450） |
| tick 消费 | 0x202080/0x201670 → 0x4fa3d0 | 移动令（推断高置信） |
| per-unit 对象 | 0x545fb0（vtable 0x115eeef4） | 注册表节点（0x546770→订单 0x54f970，脚本化战斗用） |
| BCQ CREATE/ADD_UNIT | 0x2a6ca0/0x2a5aa0 | 注册表唯一入口（引擎+脚本双层无发送器） |
| SWITCH_AI handler | 0x2abeb0（含 ST_SWITCHED 写） | 懒建控制器+写全套字段 |
| UNIT handler | 0x2aadd0 | 只写 unit+0xea8 |
| 消息队列 | [[0x118d8880]+0x3a4]+0x28048（count@+0x28024） | 残留消息 → 清 count 关闭 AI 代理 |
| 等待门 | 0x182100/0x162a30 | NumHumans（a270!=0&&a28c==1） |
| 天气执行器 | 0x10e58a0 | 只服务 a270!=0 军队 |
| 变速 | 0x2a9a80 | 写 [army+0x290] |
| 解除 AI 路径 | 0x2d5290/0x546160 族 | 被 a270 门控（引擎自带） |
| 脚本层 unit_controller | 绑定 0x1c2b20/0x1c4a60/0x1c4b80（vtable 0x115c704c） | battle.unit_controller（create/add_units/take_control） |

---

## 10. 当前前沿疑点

- [x] **战前触发实机验收（2026-08-17 自定义野战，battle_ai=1）**：✅ 通过——4 军全 a270=0/ea8=1、控制器 tick 递增（405→1226）、双方活动单各 1（阵营级 80 单位）、用户肉眼「全程自主行动、无发呆、无溃散卡住、交战无差异」。
- [x] **1-D 完整度定案（同场实机）**：战术等价、无降智；差异仅初始化残留（见下）。
- [x] **a290=0 判定修正（实机）**：我军军0 a290=0.000、友军/敌军 1.0f；40s 高频采样无引擎回写=**一次性写入**；记录+0x14c=1.0f 全正常、ctor 0x10c0b9 无条件拷贝 → **拷贝后改写**（候选=BCQ_SET_TIME_MULTIPLIER=0，0x2a9b2e 分支倍率=0 直写）——**良性**（AI 军锁变速忽略此字段，无行为影响）。
- [x] **槽+0x10=1 人类语义残留实锤（实机）**：我军组槽控制字节=1、敌军组=0（13 §8 2-A「判定链错位」）；记录层全 AI（+0x12c=0）不受影响；部署判定 FUN_1017c2f0 读记录非槽。
- [ ] **部署异常（实机新发现，待根因）**：battle_ai=1 我军部署到战场中间而非己方部署区（友军/敌军正常铺开）→ 我军首先接敌/被夹击，兵力剩半（~10 队被歼），**战术级问题**。判定层（记录）正常 → 差异在**军级放置逻辑**（候选 a290=0 / 军级放置门控）；下一战 deploy_fix.py --preview 部署期（状态1-3）采样位置验证。
- [ ] 待实机：清 count 关闭 AI 代理（队列 [[0x118d8880]+0x3a4]+0x28024）
- [ ] 待实机：判别探针 re_ba_probe.py（值字节残留 vs 军队字段残留）
- [ ] 未核实：单位 tick 消费方 0x202080/0x201670 的对象同一性铁证（推断高置信）；FUN_10316ef0 部署期重跑频率
- [ ] **待实机（行为控制台，2026-08-14 新增）**：①阈值 0.0 vs 1.5 攻城战对照（守城 AI 发呆差异，场景已修正：发呆只在攻城战）②撤退点直写 --withdraw（[3a8]/[3ac] 采样恒 0 → 直写 → 观察 AI 撤退）③城墙回归周期 0x186300（墙槽单位 [2c+0x20]==0xf）
- [ ] **观测通道（2026-08-14 修复完成，待实机验证）**：位置链 = `[unit+0xb9c]+0x6dc`（0x399f80 的 ecx，0x15d5d0=lea +0xb4）；hold 码权威链 = **orderset [unit+0xed4]→[+0x40]→[entry]→order**（[unit+0xf1c] 只是兜底，大片 None 根因）；工具 work/_diag_obs_fixed.py（--dry PASS）。留待实机：矩阵分支命中率、orderset vs f1c 一致性、断点 0x1ac0ab/0x2de3d0
- [ ] **🔑 发呆根因（2026-08-14 实机冻结快照，battle_ai=1 攻城战）**：非交战单位全体静止（含溃散）+ 玩家移动命令无效 = **引擎对某军队生成 0 个 order 单**（我方控制器活动单列表 [ctrl+0x28]+0xc4=0，敌方非 0）；我方异常 = **a290=0x0**（正常 1.0f）+ **st+0x31f0=0**。阈值 0.0 对已静止单位无效（不是 hold17 死锁，是订单生成缺失）。**下一步**：①确认 a290/st+0x31f0 我方特有（对照敌方）②追踪 0x50f360 选军数组 [0x60c]/[0x610]（我方是否被选）③定位活动单=0 的确切生成点（0x50f860 分发表为何对组0 空）
- [ ] **🔑🔑 发呆根因实锤（2026-08-14 实机恢复期，L18）**：**选军数组 [ctrl+0x60c]：敌方=2（选中 2 军）、我方=0（从未被选）**——「0x50f360 随机选军一次选定固定」下**玩家派系军队整体被遗漏** → 永不收推进单（活动单=0）→ 全静止（含溃散）。发呆会「陆续恢复」（部分单位经单位 tick 本能通道恢复）。**修复候选：直写选军数组 [0x60c]=1+[0x610]=我军军队表（人工选中）**——数据写通道待验证；或查选军重选条件。
- [ ] **🔑🔑🔑 发呆根因静态全解（2026-08-14，L19 / re_ba_army_select_report.md）**：选军**无重选**（[60c] 写者=ctor+0x4dab30）；活动单=0 ⟹ **0x50f360 从未运行** ⟹ 我方计划码恒≠9（[5ec]=4 → 0x50f3e0 敌群数组空 → 0 单）；**a290=0 + st+0x31f0=0 同缺 = 玩家派系军队没走完整 SWITCH_AI/初始化路径**（SWITCH_AI 0x2abff2 写 a290=1.0f、0x2ac093 写 st+0x31f0=1）。**修复 P0**=AI_FORCE_ATTACK_PLAN 值字节=1（最低风险）→ **P2**=补写 [army+0x290]=1.0f → **P1**=直写选军数组（最高风险，描述符指针须从 [mgr+0x40] 读，无效指针崩溃）。观测工具 re_ba_armyselect_obs.py（--dry PASS）。
- [ ] **🔑🔑🔑🔑 发呆=引擎普遍行为（2026-08-15 实机 AI vs AI 内战，L20）**：**原生 AI 攻城战第一轮进攻后同样对峙**——攻方 5/8 单位（3足轻+2骑）8 秒完全不动在后方待命、仅 1 弓箭手交战、守方缩城。原生 AI 攻方 a290=1.0f/选军 count=2/活动单 3 个（完整）仍部分单位停 → **发呆非 battle_ai 特有，是引擎设计（选军只驱动部分单位，无重选）**；battle_ai 玩家派系 count=0 是极端（全军无驱动）。**若目标=观战完整（路线1）：原生 AI 行为已可看完整场；若目标=全军持续进攻（行为质量 §5）：需 P1 选军数组扩展驱动全部**。
- [ ] **★发呆与部署异常的关系（2026-08-17 新观察）**：本场野战我军无发呆（有订单）但部署中位+伤亡惨重；L17 攻城战我军 0 订单发呆。两者可能同源（玩家派系军初始化缺失）但表现不同（野战订单链仍跑、攻城订单生成断）。攻城战重测待做。
- [x] **🔑🔑🔑🔑🔑 发呆根因闭环 + 修复（2026-08-15/16 实机定案，L24c/L24d）**：**发呆直接原因 = 推进单目标军队描述符状态字节 `[order+0x4c]→[0]→[0x44]`=4（不可驱动/忙态）** → 完结门 0x4ec290（要求 `[0x44]<4`）返回 0 → 0x4eebd0 检查子对象数=0 返回 flag → 0x509b45 跳过认领段 → **c04 认领永不写入 → 0x186c60 永不下令 → 单位静止（含溃散不跑）**。守方缩城时目标被置 4（引擎调度状态机 bug/边界，可逆）。**修复实机验证成功**：直写 `[0x44]=0` → 引擎重写 3（可驱动）→ 组0 从 0动7停 → **7动0停持续 10s**。**修复通道 = 周期写 [0x44]=0（数据写零代码）**，待工具化 s2_ai_unstall.py。⚠️ 此结论**取代/深化** L17-L21 的「选军遗漏/计划码/活动单=0」各解释（那些是现象链，最终断点=目标状态 [0x44]=4）。

---

## 11. 战斗细节机制补充（54_HANDOFF 调研第一轮，2026-08-19）

> 承接 docs/54_HANDOFF_战斗细节调研。仅收录已确证（✅ 二进制/数据侧铁证）；推断见 work/re_battle_details_report.md。报告 = 第一轮静态+网搜+环境；实机观察待第二轮。

### 11.1 S2 特殊能力注册表（DLL 0x10ec000-0x10ef000，能力 id 对）

| 能力键 | id 对 | 备注 |
|---|---|---|
| cantabrian_circle | 0x20/0x21 | |
| **flaming_arrows_ability** | **0x22/0x23** | 火矢（Q2） |
| flaming_arrows_long_range_ability | 0x24/0x25 | 火矢-远程档 |
| flaming_arrows_extreme_range_ability | 0x26/0x27 | 火矢-极远档 |
| whistling_arrows_ability | 0x28-0x2d | 鸣镝箭三档 |
| banzai | 0x30/0x31 | |
| blinding_grenade | 0x32/0x33 | |
| **rank_fire** | **0x3a/0x3b** | 三段击（Q1b） |
| rapid_volley | 0x3c/0x3d | 速射 |
| stand_firm | 0x40/0x41 | 坚守 |
| we_stand_and_fight | 0x42/0x43 | |
| **spear_wall（枪衾）** | **0x44/0x45** | DLL 内码键；文案/语音键 = yari_wall（Q1a） |
| rapid_advance | 0x46/0x47 | |
| heroic_assault / kill_zone / suppression_fire | 0x4a-0x4f | |
| kneel_fire_ability | 0x50/0x51 | FOTS 跪射 |
| pike_wall_formation | 0x58/0x59 | |
| fire_and_advance | 0x6e/0x6f | |
| tower_flaming_arrows | 0xa2 | 哨塔火矢（固定行为） |
| flaming_arrows_naval | 0x98/0x99 | 海战火矢 |

- 能力 = 阵型成型/状态切换型（spear_wall 语音 `special_ability_yari_wall_formed`）；**AI 计划链（§5.2）产物不含能力命令 → AI 不主动开阵型能力 = 决策层行为（🔶）**。

### 11.2 SquadTaskFireVolley 射击状态机（铁炮三段击，Q1b）

- 源码路径 `model\squad\squadtask\SquadTaskFireVolley.cpp`（@0x15b66a8）；tweaker 调试名「Squad Task Fire Volley」注册 @0x10df8。
- 状态名册（0x15b45b8-0x15b978c）：**逐排** CURRENT_ROW_FIRE / WAIT_FOR_CURRENT_ROW_LOADED_AND_READY / CURRENT_ROW_ADVANCE / INCREMENT_CURRENT_ROW / WAIT_FOR_FINAL_ROW_LOADED；**齐射** ORDER_GROUP_FIRE / WAIT_FOR_GROUP_FIRE_COMPLETED / ADVANCE_GROUP / WAIT_FOR_FIRST_GROUP_LOADED；**门控** WAIT_FOR_CAN_SHOOT_TEST / PERFORM_CAN_SHOOT_TEST / CANNOT_SHOOT / FACE_TARGET / REFORM / BUILD_REFORM_SPINE / FIRE_UNFORMED / DEFEND_AND_FIRE / FIRE_AND_ADVANCE；**打断** FIRE_AT_WILL / SKIRMISH / MELEE / NEW_ORDER / IDLE INTERRUPT 族。
- 状态机完整存在（✅）；**进入条件未解**（不触发候选：任务分配/门控失败/打断抢占，⚠️ 待 hook 或实机）。

### 11.3 火矢机制件（Q2）

- 能力三档 0x22-0x27 + enable 命令（`enable_ability_fire_arrows[_long_range|_extreme_range]` @0x16d9084 族）+ SPECIAL_ABILITY_FIRE_ARROWS 键（0x16902f0）。
- **tweaker 最小距离门控**：`LAND_GROUP_FIRE_ARROW_MIN_DISTANCE`（注册 0xcad61c）/ `NAVAL_GROUP_FIRE_ARROW_MIN_DISTANCE`（0xcad8ce）——火矢有最小距离限制。
- 数据侧：projectiles 表含 flaming_arrow / arrow_flaming_naval / fire_arrow_trail（2025_fix_bundle 内可见）→ **火矢 = 独立弹药类型**（arrow↔flaming_arrow 切换）。
- **★引擎能力命令族（2026-08-19）**：`BCQ_UNIT_ORDER_SPECIAL_ABILITY`（handler **0x2ab840**，命令单位执行能力）/ `BCQ_MULTIPLE_SELECTION_ORDER_CHANGE_SPECIAL_ABILITY`（**0x2a86f0**，切换选中能力）/ **`...ON_AUTOTRIGGER`（**0x2a87b0**，设置能力自动触发标志）**——引擎有完整能力命令通道 + **自动触发机制**（火矢"自动模式"开关即此通道）；共用 seSpecialAbilityManager.cpp 链（0x284b60→0x285930）。
- AI 何时切火矢（目标类型偏好）：⚠️ 未证，待实机；**AI 是否置 AUTOTRIGGER = 逆向发送方目标**。

### 11.4 攻城战术类型与状态机（Q4/Q5）

- **AI 战术类型名册（0x15b4940-0x15b4c18 内联 ASCII 串，全 43 名）**：`move_to_point / withdraw / scout / support_group / attack_enemy_battlegroup / attack_enemy_battlegroup_naval / defend_line / defend_crossing / block_crossing / assault_crossing / seize_crossing / skirmish / outflank / double_envelopment / stop_and_shoot / warcry / limbered_artillery / special_tactic / general_support / fort_assault / fort_reinforcement / attack_building / **assault_gate** / assault_wall_ladders / assault_wall_siege_tower / sap / capture_plaza / capture_buildings / general_attack_settlement / attack_settlement_surplus / defend_walls / defend_breaches / defend_junctions / defend_plaza / general_defend_settlement / defend_settlement_surplus / support_defend_settlement / support_defend_settlement_surplus / sally_out` → **独立「攻门」战术存在（✅），只攻门不爬墙引擎支持**。第八轮实证见 §11.8（★AI 选择层会选 assault_gate，执行层断链）。
- 单位级攻城状态机：ERECT_LADDERS / CLIMB_LADDERS / CLIMB_UP_LADDER（云梯）；INTERCEPT_WALL / FIND_SPINE_ON_WALLS / BUILD_SPINE / BUILD_FORMATION_SPLINE / TAKE_UP_POSITIONS（城墙阵型）；PICK_UP_ENGINES / MOVE_TO_GRAPPLING_POSITIONS / GRAPPLING（攻城武器）。
- 命令面：`BCQ_MULTIPLE_SELECTION_ERECT_LADDERS`（0x15ca7c8）；`BCQ_BUILDING_PIECE_CHANGE_GATE_STATUS`（0x15cf160）；**`FORCE_GATES_OPEN`** tweaker（0x15e3658）；`CASTLE_GATE_OPEN/CLOSE/LOCK/UNLOCK`（0x1670b60 族）。
- 战斗类型：fort_standard / fort_sally / fort_relief / settlement_standard / town_normal 等（0x15b5428 文档串）。

### 11.5 音效体系（Q3 暴鸣声）

- 社区：知名 bug「bows sound like cannons」（2012 至今，多源）；修复 mod = Arrowcannon_sound_fix / Sinfonia Semplice（「修复用单位 mod 时遇到的 corrupted sounds」）/ DarthMod soundfix。**用户环境已激活 Sinfonia_Semplice（user.script.txt）+ 2025_fix_bundle**。
- **★根因实证（2026-08-16）**：Sinfonia_Semplice.pack = **PFH2 格式，内容 = 替换 `sounds_packed/sound_events` + `sounds_packed/sound_bank_database`**（声音事件→音效银行→样本映射数据）→ **暴鸣 = 事件→样本映射损坏（unit mod 诱发），修复 = 覆盖映射数据**。用户实测 AI vs AI 场次仍能听到（修复未覆盖/顺序问题）。
- **★分族差异实证（2026-08-16）**：野战（织田攻今川守）今川弓足轻发射音=炮声、织田=正常；**units_tables 字节级对比通用行（今川用）vs `_Oda` 行完全一致**（entity/variant/class 全同）+ 共享 land unit/弹药 + 2025_patch 只改海战 → **单位数据 100% 相同，分族差异 = 运行时现象**；最强候选 = **H-A 火焰箭实际激活**（引擎 AUTOTRIGGER 机制 + 火焰箭发射音=炮声感，用户"没射火箭"未验证）；验证 = 下战斗观察今川弓兵箭矢是否带火 + 探针读弹药状态（battle_mgr 链已可读）。
- 引擎侧：sound.pack 箭矢命中音效 = `ntw\projectiles\ntw_arrow_hitstreecanopy_*.wav`（**ntw\ 前缀 = 继承拿破仑/帝国音频资产**）；tweaker `AUDIO_DISTANCE_LAND/NAVAL_PROJECTILES_ARROW_CLOSE/MEDIUM`；UnitSoundTracker.cpp / NavalSoundTracker.cpp（音效追踪器）；BOW_DRAW/BOW_RELEASE 事件；Miles（mss32.dll）驱动。

### 11.6 ★能力数据完整性（2026-08-19 RPFM 提取 data.pack，Q1/Q2 关键实证）

- **`db\unit_abilities_tables\unit_abilities`**（能力定义，字段流 = key/显示名/描述/图标/次数…）：`pike_wall_formation`（显示 "Yari Wall"）= 枪衾数据键；`rank_fire` ×2；`rapid_volley`；flaming 家族。**无 AI 使用开关列（无 Rome2 式 ai_usage）**。
- **`db\unit_to_unit_abilities_junctions_tables\unit_to_unit_abilities_junctions`**（单位→能力分配，✅ 全部分配）：
  - 枪足轻全家族（Citizenry/Hattori/Ikko/Oda/MP/Tutorial）→ `pike_wall_formation`
  - 铁炮足轻全家族（含 Oda/Hattori/Ikko/Otomo/Imported）→ `bamboo_wall` + `rank_fire`；铁炮武士 → + `rapid_volley`
  - 弓足轻/弓侍/浪人 → `flaming_arrows_ability`；弓僧兵 → `flaming_arrows_long_range_ability`；弓英雄 → `flaming_arrows_extreme_range_ability`；骑射 → `cantabrian_circle`
- **`db\unit_special_abilities_tables\unit_special_abilities`**：flaming_arrows_ability 行含子条目 `flaming_arrow` / `arrow_flaming(_long_range/_exteme_range/_naval)` = 火矢弹药类型切换数据。
- **结论**：Q1a/Q1b/Q2 的「AI 不用」**不是数据缺失**（能力全部分配）→ **引擎决策层问题，必须逆向**（或实机行为证据收窄范围）。

### 11.7 环境（2026-08-19 实测，影响所有战斗细节解读）

- 激活 mod = launcher：2025_fix_bundle（非官方修复，改单位数值/士气/弹药/autoresolve，**无能力逻辑**）+ 日语语音 + FOTS 制服；user.script.txt：城寨 mod ×5 + **Sinfonia Semplice**。
- 游戏运行中（PID 9744），battle_ai=0（原生 AI）；当前野战，s2_ai_cmd --probe 报 st 链不可读（battle_mgr 全局读失败）→ **实机观测工具需先校准**。

### 11.8 ★Q4/Q5 战术日志实证（2026-08-16 第八轮：tactics_log 读取 + 三子代理，Q4/Q5 结案）

> 触发：用户实机 3 场 AI 攻城战「无人走门」vs 日志显示每场选 assault_gate → 语义矛盾 → 三路并行（DLL RE / Pack / 日志数据）。报告：work/re_tactic_log_mechanism.md / re_battle_personality_report.md / re_tactics_log_analysis.md。

- **★tactics_log 机制（确证）**：格式串 RVA 0x15eb508 = `%s,\t%u,\t%u,`（两个 %u）；写函数 **0x506440** = `fprintf(FILE*, fmt, 名册表[idx], rec[+8]=X, rec[+0xc]=Y)`；名册指针表 .data VA 0x1179c218（RVA 0x179c218）43 项 → 名册串（idx：22=fort_assault、23=fort_reinforcement、24=attack_building、**25=assault_gate**、26=assault_wall_ladders、27=assault_wall_siege_tower、28=sap）；记录 16B 容器内联 **AI SC+0x63c**；fopen_s 门控 [VA 0x11a6a082]≠0（构造 0x496cf0）；AI SC 析构 0x4ad340→0x4adf00 flush 写盘（战斗结束）。**X/Y 语义（高置信）：X=编队 id（=指挥官单位实体 id），Y=单位实体 id**——海战每船独立编队故 X==Y（实体 id 8..2153 连续仅缺 9）；攻城 fort_assault 恒 (8, 各单位)、assault_gate/fort_reinforcement 的 X=派过 fort_assault 的单位、Y=目标单位。
- **★assault_gate 语义 = 攻城门建筑件（三重证据，高置信）**：① 名册族谱夹在 attack_building 与 assault_wall_ladders/siege_tower/sap 之间（要塞攻城战术族）② 全 DLL "gate" 字符串清点全部是城门概念（BCQ_BUILDING_PIECE_CHANGE_GATE_STATUS 0x15cf160 / FORCE_GATES_OPEN 0x15e3658 / gate_open_time / _gate_open/_gate_ajar 动画 / adc_fort_gates_*），**无任何地形 gate 串**（反证"地形隘口"假设）③ 攻城 AI 0x430cc0 族显式按件名 "gate"/"gate_ajar" 识别城门建筑件送 0x44c0a0 几何分类。**用户"assault_gate 可能不是攻门"的假设 → 反证**。
- **★AI 选择层实证（tactics_log 3 场攻城战）**：每场分配块全部含 `assault_gate, X, Y`（共 6 行）；模式 = `(fort_assault,8,G)` 恒先于 `(fort_reinforcement/assault_gate, G, D)`（X=突击组、Y=守军组/目标，守军可被多突击组共享）；**「AI 从不选攻门」旧结论被日志证伪 → 修正为「选择层会选 assault_gate，执行层未见攻门」**。
- **★执行层断链（首选解释，置信度中→高，2026-08-16 第九轮 RE-BCQ 修正推理并强化证据）**：门状态变更唯一途径 BCQ_BUILDING_PIECE_CHANGE_GATE_STATUS（注册 0x18640，handler VA 0x102a6380→0x475e40），命令对象 **[0x1192fc90] 全 DLL 无其他引用者**。⚠️ **旧表述「命令注册表 [0x1192fc90] 无引用者 → 无发起者」推理不成立**：0x1192fc90 是命令对象/hash 缓冲而非注册表（注册表本体 0x118d9000，派发器 0x3043c0/0x320870 按 hash 线性探测后 **call [entry+0x14] 间接调用** handler——间接分发下对象无直接 xref 是正常的）。**但结论本身被强化确证**（work/re_bcq_dispatch_report.md）：门命令无 getter thunk（148 个 thunk 全映射，BCQ_BUILDING_PIECE_* 全族均无）、hash imm32 全 .text 0 命中、handler 0x102a6380/0x100475e40 各仅注册处 1/0 引用 → **DLL 内物理无门命令发送器**；FORCE_GATES_OPEN（tweaker 0x11a65d90）thunk 零调用者+值字节零读取（注册即死）；CASTLE_GATE_OPEN/CLOSE/LOCK/UNLOCK（0x1670b60 族）= 音效/动画事件名非命令（旧「命令面」表述修正）；assault_gate 执行走订单系统（0x50fba0 内 0 个 BCQ 发送器调用），ATTACK_BUILDING 发送器 0x1f5720 仅被零调用者的控制台包装 0x1c3cc0 引用 → **AI 既不开门也不发攻门件命令，门系统在 DLL 内无任何 AI 入口**。附带推翻旧「DLL 无 BCQ 发送器」结论（04 P-14/L4/L5）：thunk 地址差 +4，修正后 SWITCH_AI/UNIT_CHANGE/AI_SCRIPT_CONTROLLER 族均有 DLL 内发送器，其中 AI 区 0x532xxx 族 = Lua 绑定实现（add_units/attack_unit/capture_settlement/defend_position/clear_objective，绑定表 0x117a02f8）。
- **★Pack 侧定案**：战术名册串在 data.pack 871 张 db 表 + 1245 张 schema 定义**零出现**；battle_ai_abilities_usage_params / _kv_battle_ai_ability_usage_variables / battle_difficulty_modifiers / battle_siege_vehicle_permissions 等 7 张 AI 调参表 schema 有而 data.pack 无（全走 DLL 默认值）；battle_types 8 行无 fort_standard 类；battle_type_setup_limits（weighting_type 配兵上限）是唯一数据驱动成分但与战术无关 → **AI 战术选择 = DLL 硬编码，改 db 表无效**（要走引擎层战术状态机，或战役层 CAI 间接改配兵）。
- **★AI 战术选择决策链（结构确证，末端 idx 分配点未定位）**：AI SC Update 0x506ef0 → 每 10 tick AIBattleAnalyser0 0x520140（0x506e00/0x5171e0 → 0x5041d0/0x504200/0x228630 设 [AI_SC+0x5ec]）→ 分发表 0x50f860（状态 2..0xe → 0x50f360/0x50f5b0/0x50fa40/0x50ffe0/0x50f3e0）→ 组管理器 [AI_SC+0x28] 行动码 0xa/0x46/0x64（0x4d19f0/0x4d1980/0x4d1a60/0x4d1dc0 入队→0x4fce30 登记）→ 要塞战规划器 0x50fba0（[AI_SC+0x518]!=0）+ 攻城 AI 0x489620←0x485080←战场更新 0x3a39cb → 0x430cc0 族（门件分析）+ Fort Analysis 0x42c6e0/0x42cb50/0x43dc80。idx 分配点候选：组 vtable 方法（0x509b10 调 vtable[0x28]/[0x2c]）或 0x50fba0/门件分析函数内部（需动态断点 0x506440 回栈确认）。
- **⚠️ 独立发现**：**2025_fix_bundle.pack 文件缺失**（data/ 目录无此文件，launcher moddata 仍引用，pack list os error 2）——该 mod 可能从未生效，影响 Q1-Q5 全部"mod 影响"解读（数值改动实际没进游戏）。
- **Q4/Q5 结案表述**：引擎支持只攻门（✅ assault_gate 战术 + 门状态系统 + 攻城 AI 识别门件）；AI 战术选择层会选 assault_gate（✅ 日志实证）；「AI 不走门」= 执行层断链（✅ 静态定案：门系统 DLL 内无任何 AI 入口——门命令无发送器 + FORCE_GATES_OPEN 无消费者 + assault_gate 不发攻门件命令；动态断点 0x475e40/0x43dc80 可最终确认）；玩家侧手动攻门在**本 build 无入口**（控制台无按键、FORCE_GATES_OPEN 值字节无人读、BCQ 门命令无 thunk/无发送器——需外部注入协议，见 work/re_bcq_dispatch_report.md §2/§4）。
- **★第十轮 assault_gate 执行链深挖（2026-08，报告 work/re_assault_gate_exec_report.md）**：① fort planner 0x50fba0 全链拆解——组行动对象 = GTA（ctor 0x495dd0，vtable 0x115eb0ec，"AI BATTLE GTA" 0x15eb474），单位被指派行动 = `[unit+0xbec]`（0x5172e0），随后 0x50a500→0x186c60(unit,2) 下令；**目标件 = 0x4e8d90 挑 0x514000 分数（防守密度）最高件**——门件无防守故分数≈0 → 单位被派攻墙而非门（断裂点候选②，首选解释，中置信）。② settlement AI（0x489620）门件分配（0x430ed0/0x432a20/0x432c90 入队码含 0x19=25）**消费方已确证 = 0x4896ee 求和进 [settleAI+0x1c]**（只做强度预算，不产生单位命令；0x19 与 idx 25 为数值巧合）。③ 单位侧攻建筑任务类确证：状态名表 .data 0x1796574（MOVE_TO_BUILDING→ASSAULT_BUILDING→…），任务 ctor 0x18b4f0/0x18b5e0（断点建议）。④ **日志 Y 字段修正**：flush 0x4adf17-0x4adf1f 逐条回填 `[rec+0xc]=[[容器[0]]+8]+0x50`（同批同值）→ prior「Y=目标单位 id」降为存疑；add 函数仍静态不可达（全 .text 无 0x63c..0x668 绝对位移写、0x494bb0 仅 flush 调用）——动态断点 0x506440 回栈定位。

### 11.9 ★夺门机制 + 门开状态 AI 行为（2026-08-16 第十二轮：用户领域洞察驱动，Q4/Q5 终极闭环）

> 触发：用户洞察「攻门与开门是两码事，开门=站旗夺控制权」+「门开了 AI 也不冲门照样爬墙」。报告：work/re_gate_capture_report.md（夺门）+ work/re_gate_open_ai_report.md（门开行为）。

- **★门开关的真实通道（修正第十轮绝对化表述）**：门件 per-tick update 链 **0x47f990→0x47fb40→0x486ec0→0x487438** 状态变化时**直接调 0x477f40（门状态 setter）+ 0x48a4c0 开关门（非 BCQ）**；关门带随机掷骰（0xa0b60，阈值=(他军-己军)*0.1）。0x477f40 直接调用者共 5 处：步进开关 0x4427a0/0x455810、带动画开关 0x4758cd 族、超时自动关 0x477ff0/0x4757c7（fort tick 0x426ba0 驱动）、0x487535。**BCQ_BUILDING_PIECE_CHANGE_GATE_STATUS（handler 0x475e40）只是外部注入通道之一，非唯一**。prior「引擎无代码能开门」表述作废（当时只排查 BCQ 通道）。
- **★门=「单位在场驱动」非「计时夺满」**：门件 [+0x84]/[+0x88] = 两军单位在场计数、[+0x178]=攻方引用，**计数>0 即开门（无 capture_time 计时/进度条）**；`gate_capture_time`（0x16d0bdc，注册 0xe6fb28 与 keep/key_building/tower_capture_time 同族=夺区参数族）/ `gate_opening_chance`（0x16d8574，0xecce8d 运行时表查找）**消费者静态未定位**（KV-tweaker 经注册表 [0x11bd4938] 间接访问，诚实未决项）。门是否注册为 capture point：夺区系统=capture_location_list.xml 数据驱动（0x1658260 加载/0xbd2f2e 解析，属性 cp_type 0x1658b38/cp_tickets 0x1658b40/capture_point_type 0x1658b24；夺区对象 ctor 0x4a84b0 vtable 0x115eac2c 0x22c，列表 [ctx+0x23f4]），**静态不可裁决需查 pack**；但 `adc_enemy_gates_captured`（提示 id 9）存在 = 夺门是设计内概念。**用户洞察部分证实**（在场驱动存在、夺旗任务系统存在），「计时夺满→开门」未证实。
- **★AI 不参与夺门**：夺旗任务系统对 AI 开放（任务类型 6，getter 0xc0cb0；fort planner 0x50fba0 相位 0x13 夺旗检查 0x50fc3c：任务类型 6 → 0x2199c0 点-矩形判断；0x52eec0 夺旗进度比 [军+0x30]/[区+0x48] 限幅 0.03-0.97），**但未发现 AI 给单位下「去站门旗」命令** → AI 不主动夺门（与实机「AI 从不开门/不走门」一致）。
- **★门开状态 AI 行为（字节级确证，完美解释用户观察）**：
  - 门状态存储：门对象 +0x68 门id/+0x8c 超时/+0xec 时间/+0xf1 标志；门条目状态字节 [+0x6c]（0x30a555 动画状态机读取：非0=开/0=关）；layout 通知标志 [+0x222] 全 .text 无读取者=死标志；门件 [+0x167f] 实为「已毁」标志（0x432c90）。
  - **门状态读取者全部在视觉/表现层**（0x30a4c0-0x30c500 门扇动画状态机、0x4780c0 运动学）——**AI 决策层零读取**。
  - **fort planner 目标件打分是门盲的**：0x514000 防守密度分 = 纯单位强度求和（[unit+0xcac]+[unit+0xca8]，[unit+0xb9c]→[+0x2781] 加权×2），**无门状态读取、无件类型过滤**；0x4e8d90 选分最高件无门过滤分支；0x50fba0 目标件链（0x50fe94→0x4e8d90→0x4c4210→0x514000→+0x46→0x4d1980→0x5172e0）全程零门状态读取。⇒ **门开/关不改变任何件分数 → 门件分数≈0（无守军）永远不被选 → 突击组继续攻墙；门开前后 AI 决策逐字节不变**。
  - **「用门」逻辑不存在**：门状态函数（0x477f40/0x477ff0/0x4757c7/0x48a4c0/0x430c60）调用者全集 = 门类自身方法 + fort tick 超时自动关 + BCQ handler，**零 AI 决策调用**；settlement AI 唯一门相关分支（0x4331e0-0x4334a0 'gate_ajar' 件名检查）= 防御强度分析筛选（结果只求和进相位预算，不产生单位命令）；寻路层「门开→路径/碰撞更新」静态未定位（移动命令链 0x4fa3d0 不查询门件 0xbe2a00；barrier=地图 esf 数据+campaign 脚本命令，诚实标注）。
- **★Q4/Q5 终极结论（AI 攻城行为完全「门盲」）**：AI **不选门**（目标件打分门盲，门 0 分被墙段取代）→ **不看门状态**（读取者全在表现层）→ **不开门/不夺门**（无站旗指派、门命令 AI 无发送器；门只被门类自身/tick 超时/外部注入改状态）→ **门开了也不进**（决策逐字节不变，照样爬墙）。**这是引擎设计**（门系统面向玩家/脚本交互，AI 攻城走墙段/云梯/攻城塔体系），**非 bug**。玩家侧手动攻门 = 外部注入通道（BCQ 门命令无 thunk/无发送器，需注入协议）。
- **可验证预测（动态）**：①断 0x50fe94 开门前后选中件 id 不变（预测=墙段）②断 0x430c60 后 layout+0x222 读断点 0 命中（死标志）③观战侧注入「移动到门内点」命令验证寻路层认不认门 ④断 0x487438 验证开门触发（在场计数语义）⑤断 0x12a7790（tweaker get）回栈定位 gate_capture_time/gate_opening_chance 消费者 ⑥断 0x52eec0 验证 AI 夺旗任务实际使用 ⑦rpfm 查 fort 图 pack 的 capture_location_list.xml 裁决门是否注册为夺区。
