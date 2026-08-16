# re_s15_battletype_report — 战斗类型识别字段定案（pending+0x58 = BATTLE_TYPE 枚举，野战/攻城/海战可分）（2026-08-19）

> 角色：目标3 精进 E1「按类型筛选（海战过滤=必加项）」静态分析子代理（S15）。
> 任务书：`work/re_route2_static_tasks_20260819.md` S15 条目（L24）。
> 方法：**纯只读静态**（capstone 反汇编 Empire.Retail.dll build 6262，Steam 版与 backup 版 MD5 一致 179A05D5BF9E09351BAE9C0450192C8E）+ 既有实机捕获/日志交叉复核。零注入、零写游戏文件。
> 证据分级：✅ 字节级（反汇编/写点/读点实锤）／🔶 静态推断／⚠️ 未核实（需实机）。
> 新产物：`work/_re_s15_static*.py`（1-20，全部只读）+ `work/re_s15_static*.txt`、`work/_re_s15_replay.py` + `work/re_s15_replay.txt`。

---

## 0. 三问速答

| 问 | 答案 | 置信度 |
|---|---|---|
| **战斗类型字段在哪** | **pending+0x58（int32，btype）= BATTLE_TYPE 枚举**——引擎在构造时写入（AI 路径默认 0xf=15 UNSPECIFIED；重建路径由 FUN_1069c7b0 判定写入 0-14），战斗启动检查器按具体值分支 | ✅ 字节级（枚举表+判定函数+构造器+消费者+序列化器五重证据） |
| **枚举值语义** | **野战=0(NORMAL)/1(AMBUSH)/2(BRIDGE)；攻城=3-10（FORT_*/FORTIFIED_*/UNFORTIFIED_SETTLEMENT/REGION_SLOT）；海战=11(NAVAL_NORMAL)/12(NAVAL_BLOCKADE_BREAKOUT)/13(NAVAL_BLOCKADE_RELIEF)/14(NAVAL_PORT_ASSAULT)；15=UNSPECIFIED**——权威表 0x11794478（索引=值，getter 0x95b00） | ✅ 表+判定函数返回值全集精确吻合（{0..14}） |
| **海战过滤可行性** | **可行且最便宜**：b9 watch 检测到新 AI 内战 pending 时（写 b9 前）读 **[pending+0x58]**，**11≤btype≤14 → 跳过**（不写 b9 不加载）；攻城/野战观赏性可再细分（3-10=攻城系 / 0-2=野战系） | ✅ 字段可用／⚠️ 海战值 11-14 的逐战实机对照待 1 轮验证（见 §6） |

**一句话**：战斗类型在 **pending+0x58**（btype），用 **BATTLE_TYPE 枚举**（11-14 = 海战），可在**写 b9 之前**零加载成本过滤海战。

---

## 1. 核心结论

**pending+0x58（btype，int32）= BATTLE_TYPE 枚举字段。**

- 陆战 army vtable（0x15bc860）不能区分野战/攻城（实机已证）——**但 pending+0x58 能**：它是引擎内部明确枚举的战斗类型字段，攻城=6（武田 pre-battle 实机确认）、野战=0、海战=11-14。
- 该字段在 **pending 构造/重建时即写入**（早于 b9 直写时机），因此 E1 watch 在写 b9 前读取即可过滤海战，**不需要加载战斗**。

---

## 2. 证据链（五重字节级 + 实机）

### 2.1 ★BATTLE_TYPE 枚举权威表（✅ 索引=值，getter 0x95b00）

- getter `0x95b00`：`mov eax,[esp+4]; mov eax,[eax*4+0x11794478]; ret 4` → **表 0x11794478 = 枚举值→名称**。
- 表 0x1794478 完整 dump（16 项，索引=枚举值，无空洞）：

| 值 | 名称 | 语义 |
|---|---|---|
| 0 | BATTLE_TYPE_NORMAL | 野战（普通） |
| 1 | BATTLE_TYPE_AMBUSH | 伏击 |
| 2 | BATTLE_TYPE_BRIDGE | 桥梁战 |
| 3 | BATTLE_TYPE_FORT_STANDARD | 攻城（要塞标准） |
| 4 | BATTLE_TYPE_FORT_SALLY_OUT | 攻城（出击） |
| 5 | BATTLE_TYPE_FORT_SIEGE_RELIEF | 攻城（解围） |
| 6 | BATTLE_TYPE_FORTIFIED_SETTLEMENT_STANDARD | **攻城（围城标准，武田 pre-battle 实机=6）** |
| 7 | BATTLE_TYPE_FORTIFIED_SETTLEMENT_SALLY_OUT | 攻城（出击） |
| 8 | BATTLE_TYPE_FORTIFIED_SETTLEMENT_SIEGE_RELIEF | 攻城（解围） |
| 9 | BATTLE_TYPE_UNFORTIFIED_SETTLEMENT_NORMAL | 无防御聚落战 |
| 10 | BATTLE_TYPE_REGION_SLOT_NORMAL | 区域格战 |
| **11** | **BATTLE_TYPE_NAVAL_NORMAL** | **海战（标准）** |
| **12** | **BATTLE_TYPE_NAVAL_BLOCKADE_BREAKOUT** | **海战（封锁突围）** |
| **13** | **BATTLE_TYPE_NAVAL_BLOCKADE_RELIEF** | **海战（封锁解围）** |
| **14** | **BATTLE_TYPE_NAVAL_PORT_ASSAULT** | **海战（港口突袭）** |
| 15 | BATTLE_TYPE_UNSPECIFIED | 未指定（AI 构造器默认） |

- 字符串块 @0x15aa448（与表一一对应）。全 .text 引用表者仅 3 处：0x8d895（SCALAR_FIELD 序列化器）、0x95b04（getter 自身）、0xc10337（序列化器）。

### 2.2 ★判定函数 FUN_1069c7b0 返回值 = BATTLE_TYPE 值全集（✅ 完整 0x3d0 字节反汇编）

- 全部 return 点：`1 / 3 / 6(0x7c4e20()≥0)/ 9(0x7c4e20()<0) / 0xc / 0xd / 0xe / 0xb(默认) / 7 / 4 / 8 / 9 / 5 / 0xa(×2) / 2 / arg4` → **返回值集 = {0,1,2,3,4,5,6,7,8,9,10,11,12,13,14} = BATTLE_TYPE 0-14 精确吻合**。
- 海战分支结构（arg3==0 或默认路径）：`0x6a0cd0(攻)/0x6a0cd0(守)` 双 faction 存在 → 查 `[faction+0xc4]`（→0xc 突围）/ `[faction+0xcc]`（→0xd 解围）/ `[faction+0xc4]+0x5cac10`（→0xe 港口）/ **默认 → 0xb（NAVAL_NORMAL）**——与枚举值一致。
- 陆战分支（arg3!=0）：arg4!=0→1(AMBUSH)；0x6b6110→vtable+0x54→3(FORT_STANDARD)；vtable+0x64→6/9；vtable+0x5c→0xa；0x85d0b0→2(BRIDGE)。
- **修正 re_h47_state9_report「FUN_1069c7b0 返回 1/3/6/9/0xc」「AI 实机 btype=11 未在返回值集」**：完整反汇编证明 0xb 是默认返回值之一（旧报告只追了部分路径）。

### 2.3 ★pending 构造器写入 +0x58（✅ 字节级）

- **FUN_10571520（pending 构造器，vtable 0x115fa8a4 写入 @0x57157e）**：`0x5715c9 mov [esi+0x58], 0xf`（=15 UNSPECIFIED）+ `[esi+0x5c]=0`。
- **FUN_10571950（另一构造器）**：`0x5719f7 mov [esi+0x58], [esp+0x50]`（= 调用方传入的 battle type 参数）。
- 重建 FUN_106e9f60（0x6ea276 起）：
  - `[esp+0x4c]==0` 路径：`push [esp+0x50](arg4); push [esp+0x1c](arg3); push [esp+0x4c](arg2); push ebp(arg1); call FUN_1069c7b0` → eax=btype；`cmp [esp+0x68],0; cmovne eax,0`（[esp+0x68]!=0 时强制 0）→ new(0x150) → **FUN_10571950(..., btype, ...)** → +0x58=判定结果。
  - `[esp+0x4c]!=0` 路径：new(0x150) → FUN_10571520 → +0x58=0xf。
  - → **AI 冲突常见路径（实机 btype=0/1/6/8/11/12 非 0xf）= FUN_1069c7b0 判定路径**。

### 2.4 ★战斗启动检查器按 btype 具体值分支（✅ 字节级，0x6060f0-0x606370）

函数（含 0x85ab90=CCQ 攻击创建族调用、[ebx+0xba]=人类/AI 构造差异标志，ebx=pending）对 **[pending+0x58]** 逐值检查：
- `cmp [ebx+0x58], 9` → 0x6b60e0+[vt+0x60] 检查（UNFORTIFIED_SETTLEMENT_NORMAL）
- `cmp [ebx+0x58], 4/5/7/8`（je 组）→ [vt+0x58] 检查（攻城 sally/siege-relief 系）
- `cmp [ebx+0x58], 0xc/0xd` → 0x6e51c0 检查（**海战封锁突围/解围**）
- `cmp [ebx+0x58], 0xa` → 0x6b60e0+[vt+0x58] 检查（REGION_SLOT_NORMAL）
- 0x606947 另处 `cmp [ebx+0x58], 0xe` → [ebx+0x90]+4==0 + 0x6a0cb0→[obj+0xcc] 检查（**海战港口突袭**）
- → **每个 BATTLE_TYPE 值有独立逻辑分支** = 引擎把它当正式枚举消费的铁证。

### 2.5 ★序列化器用 btype 索引枚举名称表（✅ 字节级，0xc10337）

- 0xc0fed0（ESF 序列化器，SCALAR_FIELD 记录）：`mov eax,[edi+0x5c]; push [eax*4+0x11794478]` → **把对象字段当 BATTLE_TYPE 枚举值直接索引名称表**。
- 同一对象 `[edi+0x58]` 作为 int 标量写入 → 该描述符对象 {+0x58=值, +0x5c=枚举} 双字段。
- pending+0x5c 有运行期写点（0x6062a1 `mov [ebx+0x5c],eax`，btype==0xa 分支内）→ pending 的 +0x5c 也可能承载枚举（⚠️ 字段间关系未全解，不影响 +0x58 主结论）。

### 2.6 ★攻城分支实机对照（✅ 既有实机）

- **武田 pre-battle（攻城，用户确认）**：`captures/h47a/activated_snapshot.json`「btype(+0x58): 0→6(攻城,FUN_1069c7b0)」→ **btype=6 = 围城 = FORTIFIED_SETTLEMENT_STANDARD ✓**。
- **0x59cfb1 攻城逻辑**：`cmp [esi+0x58],6` + `[[esi+0x90]+4]==1` + 守方 `[edi+0x64]` faction+0x6a0==1（守方人类）→ 攻城专属处理——与 6=围城语义一致。
- **H47a 看海（织田 AI 化，含沿海战争）btype 分布**：0×106 / 1×1 / 6×378 / 8×1 / 11×349 / 12×6（control+experiment+marker dumps 全量统计）——全部落在 BATTLE_TYPE 合法值域，**11/12 与沿海派系海战一致**（织田=尾张沿海）。

### 2.7 排除了什么（其他候选字段）

| 候选 | 结论 |
|---|---|
| army vtable 0x15bc860 | ❌ 野战/攻城一致不可分（实机已证）；**海战 army vtable 未实机验证**（01:08 海战 dump 时旧脚本未录 vtable）——若海战 army vtable ≠ 0x15bc860 可作加载后二次判据（⚠️ 待验） |
| 战场环境 [e8+0x68] 字节（[controller+0x24]=e8，投票 0x4bcd40 消费：0=野战/非0=攻城系） | 🔶 战斗内指示器；写入者未钉死（全 .text 仅 2 处 [reg+0x68] 字节写：0x4a0e9c=1、0x50ba50=0，均属其他对象）；**加载后才可用**，非首选 |
| group 布局差异（陆战组 +0x3c/+0x40；海战组 +0xb0/+0xb4=10、+0x20c/+0x230=16） | ✅ 实机差异存在（re_battle_scale_data #2）——加载后海战特征签名，可作二次确认 |
| BATTLE_SETUP_INFO（回放 52 字段） | ⚠️ 顶层叶子**无显式战斗类型枚举**（实测两个 .replay 解析：43/45 字段=地图/天气/坐标/建筑/同盟，无 0-15 类字段）——战斗类型经 AUTO_GENERATOR_INPUT 地形/种子隐式表达，不适合筛选 |
| db battle_types.xml（Tutorial/campaign_battle/capture_point/**classic**/**naval**/**siege**/historic/napoleon_historic） | 另一层（UI/模式层，env ctor 0x247a22-0x247a50 按 campaign_battle/historic/napoleon_historic 字符串分支）——与 BATTLE_TYPE 枚举不同维（模式 vs 类型），非首选 |

---

## 3. 字段速查

| 项 | 值 |
|---|---|
| **战斗类型字段** | **pending+0x58（int32 btype）** |
| 枚举权威表 | 0x11794478（索引=值；getter 0x95b00；字符串块 0x15aa448） |
| 判定函数 | FUN_1069c7b0（0x69c7b0，返回 0-14） |
| 构造器写入 | FUN_10571520 @0x5715c9（=0xf）；FUN_10571950 @0x5719f7（=param） |
| 重建路径 | FUN_106e9f60 @0x6ea2b7（call FUN_1069c7b0）→ @0x6ea2f4（call FUN_10571950 带 btype） |
| 消费点（值分支） | 0x606124(9)/0x606151(4,5,7,8)/0x60618f(0xc,0xd)/0x606277(0xa)/0x606947(0xe)/0x59cfb1(6)/0x6052be(0xe)/0x5bcc17(0xe) |
| 序列化（名称表索引） | 0xc10337（[obj+0x5c]→表）；0xc0fed0 = SCALAR_FIELD 记录器 |
| **海战值域** | **11 ≤ btype ≤ 14** |
| **攻城值域** | 3 ≤ btype ≤ 10 |
| **野战值域** | 0 ≤ btype ≤ 2 |
| 未知 | 15（0xf，AI 构造器默认 UNSPECIFIED） |

---

## 4. 筛选实现建议（E1：海战过滤 + 攻城/野战观赏性）

### 4.1 推荐落点：b9 watch 写 b9 之前（零加载成本）

b9 forge watch（`work/_re_b9_forge.py` 现役）检测到新 AI 内战 pending（state<10 且 vt 匹配）时，**在写 [pending+0xb9]=1 之前读 [pending+0x58]**：

```python
btype = read_int32(pending + 0x58)
if 11 <= btype <= 14:        # 海战（NAVAL_NORMAL/BLOCKADE_BREAKOUT/BLOCKADE_RELIEF/PORT_ASSAULT）
    SKIP   # 不写 b9，不加载 → 海战过滤
elif btype == 15:            # 0xf UNSPECIFIED（AI 简单构造器路径）
    FALLBACK  # 按策略：默认加载 or 默认跳过（建议默认加载，宁多勿漏；见 §6 验证点 ③）
elif 3 <= btype <= 10:       # 攻城系（FORT_*/FORTIFIED_*/UNFORTIFIED_SETTLEMENT/REGION_SLOT）
    LOAD（攻城观赏性单独标记，可选）
else:                        # 0/1/2 野战/伏击/桥梁
    LOAD
```

- **时机优势**：pending+0x58 在构造/重建时已写入（§2.3），watch 轮询到 pending 时必然可读；无需加载、无需触碰任何引擎数据。
- **与现有单位数筛选组合**：pending+0x58（类型，先）→ 冲突军队数粗筛（S14）→ 加载后 [army+0x114] 单位数精筛（S13）——完整 E1 筛选管线。

### 4.2 备选（加载后二次确认）

- army vtable ≠ 0x15bc860 → 海战（⚠️ 海战 vtable 待实机验证后启用）
- 组布局签名：组 +0xb0/+0xb4 满向量（{10,10}）而无 +0x40 值 → 海战（re_battle_scale_data #2 实机差异）
- 战场环境 [e8+0x68] 字节（0=野战 / 非0=攻城系；🔶 写入者未钉死）

### 4.3 不建议

- BATTLE_SETUP_INFO（无显式类型字段）；db battle_types（模式层，维度不同）。

---

## 5. 置信度汇总

- ✅ **字节级**：枚举表 0x11794478（16 项索引=值）；FUN_1069c7b0 返回值全集=0-14；构造器 +0x58 写点×2；重建路径 btype 计算→构造器；战斗启动检查器按 {4,5,7,8,9,10,12,13,14} 值分支；序列化器 [obj+0x5c]→名称表索引；攻城分支 0x59cfb1（6+守方人类）。
- ✅ **实机对照**：btype=6=围城（武田 pre-battle）；btype 全量统计落在合法值域 {0,1,6,8,11,12}。
- 🔶 **静态推断**：11=NAVAL_NORMAL 语义（表+判定函数吻合，但 0xb 同时是 FUN_1069c7b0 默认返回值 → **逐战对照需 1 轮实机**，见 §6 ①）。
- ⚠️ **未核实**：海战 army vtable；[e8+0x68] 写入者；pending+0x5c 与 +0x58 的精确关系；btype=15 出现频率（哪些 AI 冲突走 0xf 路径）。

---

## 6. 实机验证清单（1 轮，配合 E1 数据采集）

- [ ] **① 海战 AI 内战**：watch 检测到海战 pending 时记录 [pending+0x58]（预期 11-14，重点确认 11）→ 对照用户视觉「海战」。
- [ ] **② 野战 AI 内战**（5v4/5v2 型）：记录 [pending+0x58]（预期 0=NORMAL）。
- [ ] **③ 攻城 AI 内战**（10v1 型）：记录 [pending+0x58]（预期 3-10，最可能 6）。
- [ ] **④ btype=15 频率**：看海一段时间的 btype 分布，统计 0xf 占比（决定 FALLBACK 策略）。
- [ ] **⑤ 海战 army vtable**：**常驻观察器 `_re_battle_watcher.py`（已就绪，commit 2a55ccc）已自动逐场采集 army vtable**——攒几场海战即可对照 0x15bc860（判据：海战 army vtable ≠ 0x15bc860 → vtable 可作加载后二次判据；= 同 → 只能靠 pending+0x58）。

> 判定：① 若海战 btype ∈ {11,12,13,14} → 海战过滤规则直接可用（11≤b≤14）；若 11 混入陆战（0xb 默认路径误标）→ 海战过滤改用 {12,13,14} 或叠加 army vtable/组布局二次判据。

---

## 7. 复盘 4 问（AGENTS §1）

1. **多知道了什么**：pending+0x58=btype=BATTLE_TYPE 枚举（表 0x11794478 索引=值，16 项，11-14=海战）；FUN_1069c7b0 完整返回值=0-14（修正旧报告「1/3/6/9/0xc」与「11 不在返回值集」）；战斗启动检查器/序列化器按值消费=引擎正式枚举；db battle_types=模式层（不同维）。
2. **什么没按预期**：BATTLE_SETUP_INFO 无显式类型字段（地图生成隐式表达）；[e8+0x68] 战场类型字节写入者未钉死；海战 army vtable 旧 dump 未录（无法静态定海/陆 vtable 差异）。
3. **假设修正**：「btype=11 未在 FUN_1069c7b0 返回值集」→ **证伪**（0xb 是默认返回值）；「state9 条件 ⑥ [pending+0x58]!=1」→ 实际消费点更丰富（0x606xxx 按 9/10/12/13/14 等分支），state9 报告为部分路径。
4. **下一步最小实验**：实机跑 §6 清单①-③（1 轮海战+1 轮野战+1 轮攻城记录 btype）→ 定案 11-14=海战过滤规则；顺带⑤ 录海战 army vtable。
