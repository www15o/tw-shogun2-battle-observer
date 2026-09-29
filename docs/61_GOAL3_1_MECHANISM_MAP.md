# Goal3.1 机制地图 — 写 b9 前规模/类型/阵营过滤（当前有效结论）

> **公开库说明**：本库只收录 **目标1 / 目标2 / 目标3 + 写 b9 前过滤** 的机制地图与探索地图。
> 修订日志（`12_GOAL1_LOGBOOK.md` / `22_GOAL2_LOGBOOK.md` / `62_GOAL3_1_LOGBOOK.md` / `Goal_3_LogBook.md`）
> 与目标4（AI 行为 / 攻城）研究文档、以及部分内部报告**不在本库发布**。
> 文中出现的 `work/` 路径与 `re_*_report.md` 指向作者私库工作目录，公开副本见 `reports/`。引用规则见 `docs/README.md`。


> 状态：**已实机验证**（2026-08-29，commit `4d04b3f` 对应形态）。
> 本文只收录**当前成立、有实机/字节级证据**的结论；错误与未定结论一律不进入本文，历史见 `62_GOAL3_1_LOGBOOK.md`。
> 与 Goal3 的关系：Goal3 解决“AI 内战能不能加载出来旁观”；Goal3.1 解决“在写 b9 前决定这场战斗值不值得加载”。本文从 Goal3 文档中抽出，Goal3 文档只保留指针。

---

## 0. 一句话结论

AI 内战在自动结算前，可以通过 A1（`FUN_105caa60` 入口 hook）读取 `[pending+0x90]` 数据块中的双方单位数，并在 `[pending+0xb9]` 被引擎读取前完成过滤；达标才写 `b9=1`，不达标保持原样自动结算。

---

## 1. 判定点与写 b9 时序

- 决定性 b9 读点：`FUN_105caa60` 内 `0x5caa6c` / `0x5caa8e`。
- A1 hook 位置：`FUN_105caa60` 入口 RVA `0x5caa60`。
- 因为 hook 在函数入口，stub 写 `[pending+0xb9]` 一定早于本次调用的 b9 读点。
- 真正需要保证的是：**判定数据要在本次调用的 b9 读点之前可读**，不要求 A1 入口时就可读。
- 已验证的 bridge 方案：stub 在入口向宿主请求 PRE 判定并自旋，宿主在自旋窗口内读 p90/hdr，写回 PASS/FAIL，stub 再决定是否写 b9。

---

## 2. btype 类型机制（S15，已确证）

### 2.1 字段与枚举

- 字段：`[pending+0x58]`（int32）= BATTLE_TYPE 枚举。
- 权威表：`0x11794478`（getter `0x95b00`）。
- 值域：
  - 0-2：野战（NORMAL / AMBUSH / BRIDGE）
  - 3-10：攻城（FORT_* / FORTIFIED_SETTLEMENT_* / UNFORTIFIED_SETTLEMENT / REGION_SLOT）
  - 11-14：海战（NAVAL_NORMAL / BLOCKADE_BREAKOUT / BLOCKADE_RELIEF / PORT_ASSAULT）
  - 15：UNSPECIFIED（AI 构造器默认 `0xf`）

### 2.2 过滤规则（A1 stub 已实装）

- GUI/CLI 映射：
  - 攻城 = `(3, 10)`
  - 野战 = `(0, 2)`
  - 海战 = `(11, 14)`
- 多范围语义：**OR**。任一范围命中即放行；全部范围未启用 = 全捕捉。
- 顺序：btype 过滤在 PRE 单位数过滤之前。
- 海战注意：PRE 单位数字段 `hdr+0x18/+0x78` 在海战固定为 `0/0`。若开启 PRE 阈值，应通过类型筛排除海战，否则海战会被 PRE 判 0 全部拦掉。

---

## 3. PRE 单位数数据源（L9，已实机验证）

### 3.1 读取链

```
[pending+0x90] = p90
[p90+0x1c]     = hdr
[hdr+0x18]     = 一方合计（含援军）
[hdr+0x78]     = 另一方合计（含援军）
```

- `[pending+0x90]` 是数据块指针，不是普通对象。
- 该数据源对应战役脚本 API `CampaignNumberOfUnitsInPlayerArmy/EnemyArmy/PlayerAlliance/EnemyAlliance`。
- 语义：**双方联盟合计，包含援军**，是“整场战斗最终规模”，不是单军规模。

### 3.2 已验证事实

- 陆战：PRE 判定与 POST 真值 `Σ[army+0x114]` 一致。
- 实机例：`PRE=20/21 → PASS → 写b9 → 战斗加载`，加载后单位数符合 PRE 预期。
- 海战：固定 `0/0`，不可用于海战规模判定。
- `[pending+0x90]` 构造初值为 0，非零写者尚未静态定位；但这不阻塞使用——host-mediated 自旋窗口内可等到它就绪。

---

## 4. A1 过滤管线（已验证形态）

### 4.1 返回地址白名单

A1 入口 hook 必须按返回地址过滤，排除 wrapper `0x5d94b9`（传入对象不是 pending）。有效返回地址：

| 返回地址 | 归属 | 作用 |
|---|---|---|
| `0x6045a4` | 状态1 第一次调用 | 通知路径，同输入同结果 |
| `0x6045c9` | 状态1 决策调用 | 写 `[pending+0x54]`，决定状态4/6 |
| `0x604936` | 状态6 结算重算 | 非决策读点，但可作为 p90 就绪后的 **PASS 预热点** |

> **0x604936 必须保留**：它本身不决策，但它能让同一 pending 先进入 sticky approval，后续状态1 决策调用会继承 PASS 并写 b9。把 0x604936 排除是已证伪的回归操作。

### 4.2 过滤顺序

1. 返回地址过滤
2. pending vtable 校验（`VT_PENDING_RVA`）
3. `[pending+0x55]` ready 检查（AI 内战 ready=0）
4. `[pending+0x155c]` 双人类模式检查
5. btype 双范围（§2）
6. 阵营白名单/黑名单（S18 链）
7. PRE 单位数（§3）
8. 全部通过 → 写 `[pending+0xb9]=1`

### 4.3 host-mediated 判定与 sticky approval（已验证核心）

- stub 写 pending 到共享区、`seq++`、decision=`0xFF`，然后自旋。
- 宿主线程 0.2ms 轮询，读 p90/hdr，计算阈值，先写 `ANSWERED_PENDING=本次 pending`，再写回 `DATA_PRE_UNIT=0/1`。
- stub 只接受 `DATA_PRE_UNIT != 0xFF` **且** `ANSWERED_PENDING == 本 pending` 的判定，防止跨 pending 误用；超时 fail-closed。
- stub 拿到 `1` → 放行；`0`/超时 → fail-closed。
- **sticky approval**：同一 `(pending, [pending+0x60], [pending+0x64], btype)` 一旦有真实 PASS，后续**数据未就绪**的 state-1 事件沿用该判定（写 b9）；具体数据在场时一律按当前数据重新判定（2026-08-30 实机修正：pending 指针在 battle_mgr 变化前会被复用，仅按 pending 会把上一场 PASS 污染到下一场）。**这是 state-6 预热点 → state-1 消费的核心桥，禁止删除。**
- 新 battle_mgr 出现时清空 approval，防止指针复用误放行。
- **AV 残余风险处置（2026-08-30）**：宿主每事件最多轮询 20ms（`PRE_POLL_MAX`）即写回，游戏线程卡顿有硬上界；`0x604936` 用 `SPIN_FULL=50M`、state-1 用 `SPIN_SHORT=5M` 自旋兜底，实际等待由宿主应答决定。
- **有界 defer（2026-08-30，强制本场消费；2026-09-01 修复安装条件）**：host 写回 PASS 后 arm `DEFER`（target=本 pending，budget=3）；`0x604954` 结算写点 hook 在 DEFER 生效时把 `state 5` 改写为 `state 1`，让下一 tick 重新进 state-1 消费 b9；stub 在 state-1 写 b9 后 disarm；budget 耗尽放行原结算（fail-closed）。`0x604936` 不再写 b9（只做预热点/日志），写 b9 只在 state-1（0x6045a4/0x6045c9）。**defer hook 安装条件必须与 `F_PRE_UNIT` 一致（`if flags & F_PRE_UNIT`）**；否则分类型/动态模式下 PRE 过滤生效但 defer 未装，state-6 PASS 会泄漏到下一场（0dd9403 曾漏改此条件）。

### 4.4 已知不接受的替代实现

- **dispatcher 缓存直读**（`0x6e9f60` 构造时缓存 `DATA_PRE_DISP_*`）：未验收，且实机导致 PRE 数据读不到、全 fail-closed。不能作为当前机制。
- **stub 内直读 p90/hdr**：在 p90 未就绪时 fail-closed；可作为后续优化方向，但必须解决“入口时未就绪、读点前就绪”的窗口问题。

---

## 5. POST 真值链（用于验收，已确证）

```
battle_mgr = [base+0x1bc8180]
env        = battle_mgr+0x110
st         = [[env+8]+0xb4]
gcnt/gtbl  = [st+0x88] / [st+0x8c]
group      = gtbl[i]
acnt/atbl  = [group+0x20] / [group+0x24]
army       = atbl[j]
unit_count = [army+0x114]   ; FotS 回退 [army+0x12c]
```

- POST 总数 = `Σ[army+0x114]`。
- PRE/POST 配对必须按双方 persistent faction 指针一一对应，禁止 FIFO/名字 OR 猜配。
- 注意：POST 在 battle_mgr 创建瞬间采样可能漏援军；PRE 是含援军合计，两者对照时要以最终稳定 POST 为准。

---

## 6. 已排除 / 证伪（不得再当结论）

| 旧说法 | 处置 |
|---|---|
| `[army+0x294]` 是 PRE 单位数/规模阈值 | ❌ 证伪：它是规模代理/静态模板值，不是当前单位总数；GUI 已删除该阈值 |
| `side+0x88/+0x90` 是加载前单位数候选 | ❌ 证伪：位于 nested sub-object，不是单位数/军队向量 |
| battle setup/factory 链 `FUN_106bba80 → [battle_obj+0x140]` 是 AI 内战 PRE 数据源 | ❌ 证伪：AI 内战从不调工厂/env 链，factory ctor 0 触发 |
| dispatcher 缓存 `DATA_PRE_DISP_*` 是可用优化 | ❌ 未验收/回归：实机导致 PRE 全 fail-closed |
| 排除 `0x604936` 可解决“写 b9 不加载” | ❌ 证伪：它破坏 sticky approval 预热，导致状态1 拿不到 PASS |
| “Goal3.1 未达成 / p90 在决定性读点前不可用” | ❌ 错误：已实机达成；p90 可在自旋窗口内就绪 |

---

## 7. 当前状态与下一步

- 已知可用基线：commit `4d04b3f` 的 host-mediated + sticky approval + 保留 `0x604936`。
- GUI 已集成 PRE 单位数（总≥/每方≥）并移除旧 `+0x294` 规模阈值。
- 当前代码 = 基线 + AV 残余风险处置（按 pending 应答 + 宿主 20ms 必答 + 分级自旋，见 §8）+ **有界 defer（0x604954 结算改写为 state-1，强制本场消费）**。
- **已实机验证（2026-08-30）**：`ret=0x604936 PASS（19/20，不写 b9，arm DEFER）→ 下一 tick ret=0x6045a4/0x6045c9（同 side/btype 19/20）写b9 → ★新战斗 mgr`，加载后单位数 `[19,20]` 与 PRE 一致。这是首次观察到**同场正确消费**（非 pending 复用污染）。
- 下一步：继续看海 ≥30 分钟无 `0xC0000005`，并采集更多达标样本确认捕获稳定。
- **新待查（2026-08-30）**：长会话结束后“恢复人控”写 `+0x6a0=1 + HUMAN` 后游戏 `0xC0000005` 退出（与看海/自旋 AV 不同路径）；且上一轮日志实际跑的是旧 dist（无 `⚙` 诊断），已替换为带诊断的 dist。恢复人控崩溃需复现取证。

---

## 8. 稳定性（2026-08-30 AV 残余风险处置后）

- 当前代码：host-mediated 阻塞自旋 + `_preunit_thread` 启动 + sticky approval（battle 身份键）+ 无条件写回 + 三个返回地址 + `ret=` 日志 + **有界 defer**，全部保留。
- **新增 AV 处置**（不破坏骨架）：
  - `DATA_PRE_UNIT_ANSWERED_PENDING=0x1A18`：宿主先标“本判定属于哪个 pending”，stub 只消费归属匹配的判定；
  - 宿主轮询上限 `PRE_POLL_MAX=0.02s`（20ms 级硬上界），不再存在“宿主无人应答 → 50M 自旋到底”；
  - 分级自旋：`0x604936` 用 `SPIN_FULL=50M`，state-1 用 `SPIN_SHORT=5M`，仅作宿主彻底失联时的兜底；
  - `_preunit_thread` 设 `THREAD_PRIORITY_ABOVE_NORMAL`，缩短应答延迟。
- **新增有界 defer**（2026-08-30，解决"同场自然循环消费从未被观察到"）：
  - `DATA_PRE_UNIT_DEFER=0x1A1C / DEFER_TARGET=0x1A20 / DEFER_BUDGET=0x1A24`；
  - host 写 PASS 后 arm DEFER（budget=3）；`0x604954` hook 把结算写 state 5 改写为 state 1；
  - 下一 tick 重新 state-1 → sticky PASS → 写 b9 → 本场加载；stub 写 b9 确认后 disarm；
  - budget 耗尽 → 放行原结算（fail-closed）；不冻结线程（每场最多多 N 个 tick）。
- `0x604936` 不再写 b9（只做预热点/日志）；写 b9 只在 state-1。
- 相比 dispatcher 缓存回归版：功能链路完整，稳定性改善（不再“无人应答/缓存为空”必败）。
- 相比原始 `4d04b3f`：AV 残余风险显著缩小（卡游戏线程时长有硬上界 + 判定按 pending 隔离）；且不再依赖 state-6 写 b9 的跨战斗污染来"碰运气加载"。
- 实机复验要求：PRE 阈值开，看海 ≥30 分钟无 `0xC0000005`；达标战斗本场加载（同 side/btype 的 state-6 PASS → 下一 tick state-1 写 b9）；不达标战斗不被带飞。**2026-08-30 已首验通过**（19/20 本场加载、[19,20] 一致）；继续长稳观察。

---

## 9. 动态决战捕捉（2026-08-30 新增功能）

- **触发**：GUI「动态决战」勾选 / CLI `--dyn-decisive`。
- **总下限动态化**：以“本会话已捕捉（PASS）的最大 PRE 总单位数”为基准，总下限自动 = `max(手动总≥, 基准×80%)`；初始无基准时仍尊重手动总≥（默认 0=不筛）。
- **双方平衡条件**：`min_ratio=0.5`，即 `小方/大方 ≥ 0.5`，双方最不平衡不超过 `1:2`；任一方为 0 直接不通过。
- **基准只由会写 b9 的 state-1 PASS 更新**：state-6 预热点 PASS 只记入 sticky 候选，不计入“已捕捉最大规模”，避免被后续 155c/数据未就绪拦截的未加载战斗抬高阈值。
- **UI**：动态基准/下限实时显示；「重置基准」清零本会话基准（含正在运行的捕捉对象）。
- **分类型（2026-08-31 新增）**：勾选「按类型区分」后，野战（btype 0-2）与攻城（btype 3-10）分别维护手动阈值与动态基准 `dyn_max_field/dyn_max_siege`，各自按 80% 计算下限；海战/未知不套用规模阈值。
- **实现位置**：`s2_spectate.SpectateCapture._preunit_loop` + `s2_control_gui` 动态控件。

---

## 10. 参战双方“阵营/宗教”显示（2026-08-31 新增）

- **需求**：随机阵营 mod 下希望旁观/列表能看到“东军/西军/佐幕派/保皇派”这类派系所属阵营/宗教，增强带入感。
- **数据源**：持久 faction `+0x6d4`（getter `0x5ff820` = `mov eax,[ecx+0x6d4]`）。
- **实机确认**：`+0x6d4` 不是裸 C 串，而是 **UTF-16 string 对象**：
  ```
  [p+0] = 长度
  [p+4] = 容量
  [p+8] = 数据指针（UTF-16，如 rel_catholic）
  ```
- **key → 显示名**（内置映射，未知 key 回退显示原始 key）：
  - `rel_buddhist` → 东军（模组）
  - `rel_catholic` → 西军（模组）
  - `rel_ikko` → 第三军（模组）
  - `inf_bos_shogunate` → 佐幕派、`inf_bos_imperial` → 保皇派、`inf_bos_self` → 独立
  - `inf_gem_fujiwara` → 藤原氏、`inf_gem_minamoto` → 源氏、`inf_gem_taira` → 平氏、`inf_gem_neutral` → 中立
- **接入位置**：
  - GUI 派系列表新增「阵营/宗教」列（`s2_control_gui` 扫描时填充）；
  - `[拦截 #N]` 日志与 `★新战斗` 日志显示 `阵营=织田(东军)/今川(西军)`；
  - `⚙` 军队诊断每军显示 `faction=织田(东军)`。
- **性质**：纯显示用途，不参与白名单/黑名单过滤（过滤仍按派系名）。
