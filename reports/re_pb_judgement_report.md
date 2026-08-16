# re_pb_judgement_report — pending battle 判定机制调查（2026-08-09 深夜）

> 任务 = 用户指示（路线B 收口后）：调查 pending_battle 的判定机制。
> 方法：静态（Empire.Retail.dll build 6262，capstone + Ghidra bridge），基于路线 A 成果（ROUTE_A_DROPIN_20260809.md + re_a3_probe.py + 03_DISCOVERIES 十六节）。
> 地址约定：RVA。置信度：✅已确证 / 🔶推断 / ⚠️未完成。

## 0. 一句话结论

**"加载 vs 结算"判定 = 主循环 FUN_10703ea0 内多条件组合**：
- ① 速度门控：本地派系人类 || pending ready || 冲突列表非空等 → 正常速度，否则快速结算；
- ② **FUN_108b4ee0 遍历冲突列表 [model+0x149c] 逐个解析 faction 检查 +0x6a0** → 有人类=正常，无人类=速度上限 5（快速结算）——**"AI 内战自动结算"的直接引擎机制**；
- ③ pending battle 状态机：+0x4c/+0x50 状态（0/10=空，其他=待处理）→ 待处理时执行 FUN_109df0e0。
- **未定位（⚠️）**：冲突列表 [model+0x149c] 的**填充者**、pending 状态字段的**写入者**（激活点）、env 创建的**调用者**（加载执行）——这三处正是 H40 静态盲区，**实机 re_a3_probe --watch 是裁决手段**。

## 1. 主循环 FUN_10703ea0 判定结构（✅ 反编译 + 汇编确证）

### 1.0 对象身份澄清（术语，避免混淆）
- **pending battle 本身** = [model+0x14a4] 指向的对象（vtable 0x115fa8a4，方法 0x1058xxxx/0x105dxxxx）——**数据容器**（状态/参战方/参与者/ready），总是构造。
- **FUN_10703ea0（主循环）** = 战役 model 的**每 tick 更新函数**——**pending battle 的判定者/消费者**（读它的状态做判定），**不是** pending battle 的方法。
- **FUN_108b4ee0** = 主循环的辅助函数（遍历冲突列表查人类）——也不是 pending battle 的方法。
- **时序**：pending battle 总是存在 → 交战发生 → 冲突被记录（冲突列表 [model+0x149c]/pending 字段；**生产端未定位=盲区**）→ 主循环下一 tick 读到 → 判定"加载 vs 结算"。

### 1.1 完整伪代码（✅，含 uVar13 速度语义）
```
while ([model+0x14e0]+0xb4 == 0) {
  /* ---- 门控：正常速度 vs 快速 ---- */
  if (本地派系人类==1 || (状态∈{非0,非10} && [pending+0x55]!=0) || (FUN_101be5d0()!=0 && [[model+0x14a8]+4]==0))
    bVar14 = true;
  else bVar14 = (FUN_10858c20() != 0);

  /* ---- 速度步数上限 uVar13（每 tick 允许的模拟步数）---- */
  if (本地派系非人类) {                        // ← 无人类分支
    if (FUN_108b4ee0()==0) {                  // 冲突列表[model+0x149c]中无人类 faction
      if (5 < uVar13) uVar13 = 5;             // ⭐ 上限 5（快速结算）
    } else {                                  // 冲突列表有人类
      if (FUN_108b4f80()==0) { if (1 < uVar13) uVar13 = 1; }   // 上限 1
      else if (2 < uVar13) uVar13 = 2;                        // 上限 2
    }
  }

  /* ---- 每 tick 执行（uStack_5090 = 已执行步数）---- */
  if (FUN_106ff7b0()) { FUN_109df0e0(); uStack_5090 += 150; }        // pending 待处理 → 执行 +150
  else if (FUN_106ff700()) { "CAI do_cai_step"; FUN_10aeea90(); uStack_5090 += 150; }  // CAI 步骤 +150
  else uStack_5090 += 1;                                             // 无待处理 +1
  ...
  FUN_108ffb80(...);  // location_manager 更新
  FUN_106f5970(...);  // 每帧任务
  ...
  /* ---- 本 tick 结束条件 ---- */
  if (FUN_108bd240() || uVar13 <= uStack_5090 || FUN_106b6000()) break;
}
FUN_10609290(); FUN_107034d0();   // 循环外：标记管理器
```

### 1.2 ⭐ uVar13 语义（"速度上限 5"的确切含义，✅ 确证）
- **uVar13 = 战役模拟主循环"每 tick 允许执行的更新步数上限"**（uStack_5090 累计已执行步数，`uVar13 <= uStack_5090` 时本 tick 结束）。
- **不是战斗速度等级**（战斗层速度 = 0.1/1.0/2.0/4.0，cycle_battle_speed FUN_110dce90；两码事）。
- 值域与触发：
  | uVar13 上限 | 触发条件 | 语义 |
  |---|---|---|
  | 1 | 冲突列表有人类 + FUN_108b4f80()==0 | 正常节奏最慢（每 tick 1 步） |
  | 2 | 冲突列表有人类 + FUN_108b4f80()!=0 | 正常节奏（每 tick 2 步） |
  | 5 | **冲突列表无人类（AI 内战）** | **快速结算（每 tick ≤5 步）** |
  | 200 | bVar2（某特殊模式） | 极速 |
  | DAT_117a91b0 | 默认 | 全局默认速度 |
- 汇编证据：0x7044f9 `mov ebp,5`；0x70450a call 0x8b4ee0 → 0x704513-0x70451b `cmp ebp,5; cmova ebp,5`（ebp=uVar13 上限 5）。
- **结论：AI 内战自动结算 = 无人类分支把 uVar13 上限提到 5（每 tick 跑 5 个模拟步），战斗"快速结算"而非加载。**

### 1.3 每 tick 执行分派（✅）
| 条件 | 执行 | 步数 |
|---|---|---|
| FUN_106ff7b0()!=0（pending 待处理） | FUN_109df0e0（vtable 分发） | +150 |
| FUN_106ff700()!=0（CAI 步骤条件） | "CAI do_cai_step" + FUN_10aeea90 | +150 |
| 两者都空 | （无操作） | +1 |
- FUN_106ff7b0 = pending 状态检查：`[model+0x14a8]` 非空→返回；`[pending+0x4c/+0x50]` 状态 ∈{0,10}→继续；其他→待处理。
- FUN_106ff700 = session +0x7a8（已移交）+ 冲突列表 FUN_108bd240 + [session+0x2c]+0x895 检查 → CAI 步骤。

### 1.4 关键函数语义（✅）
| 函数 | 语义 |
|---|---|
| FUN_108b4ee0([model+0x149c]) | 遍历冲突列表（条目+8=对象，[+0x1c]→+0x254→FUN_103e5960→faction），检查 [faction+0x6a0]；有人类→1（正常），无→0（快速，uVar13 上限 5） |
| FUN_108bd240([model+0x149c]) | `[+0x20]==[+0x24]` 冲突列表是否空 |
| FUN_108bd250 | getter：`[x+4]→[+0x1c]→[+0x160]→[+8]→[+0x149c]` = 从子对象拿 model 冲突列表 |
| FUN_106ff7b0 | pending 状态检查：`[model+0x14a8]` 非空→返回；`[pending+0x4c/+0x50]` 状态 ∈{0,10}→继续处理；其他→待处理 |
| FUN_106ff700 | 检查 [model+0x1498] session +0x7a8（已移交）+ 冲突列表 + [session+0x2c]+0x895 + ... → CAI 步骤 |
| FUN_109df0e0 | 包装：调 [obj+4] vtable+0x3c → jmp FUN_109df850（pending 待处理执行） |
| FUN_10858c20 / FUN_108b5060 / FUN_108b4f80 / FUN_108b4fb0 | 门控/速度档辅助检查（列表标志） |
| FUN_100dbce0 | `mov eax,[ecx+4]; ret` = 状态 getter（pending +0x4c 实读 +0x50） |
| FUN_101be5d0 | `cmp [ecx],0; setne al` = 对象非空检查（[model+0x14a8]） |

### 1.5 冲突列表 [model+0x149c]（✅ 结构确证）
- model 上的 vector（+0x20=begin/+0x24=end 迭代）；条目 +8 = 对象指针，[对象+0x1c] → +0x254 → FUN_103e5960（`[[ecx+4]]` 链）→ faction 对象（+0x6a0 人类标志）
- **FUN_108b4ee0 用它做"无人类→快速结算"判定** = AI 内战自动结算的直接机制
- ⚠️ 填充者未定位（盲区）

## 2. pending battle 状态机（✅ 消费端确证，⚠️ 写入端未定位）
- **总是构造**（路线A）：[model+0x14a4]，new(0x150)→FUN_105712b0，vtable 0x115fa8a4
- **消费端**：FUN_106ff7b0 读状态（+0x4c/+0x50，0/10=空，其他=待处理）；主循环门控读 +0x55 ready；FUN_105cb870 读参与者 +0xb8/+0xbc；CCQ ready 链写 player-setup +0xf8
- **vtable 方法**（✅）：slot1=0x105d9070 遍历参战方双列表（+0x60/+0x64）逐元素 FUN_105d9b60；slot3=0x10587616 子对象 thunk；slot4=0x105d9050 清理（清 +0xa4）
- **⚠️ 未定位**：谁把 pending 状态从 0/10 改为激活值、谁填充参战方/参与者——**这是"冲突→pending 激活"的转变点，静态盲区（H40）**

## 3. 加载执行端（⚠️ 未定位）
- BATTLE_ENV 构造链（FUN_10099d50，含 0x9a40e/0x9a6dc 的 FUN_1011de20 调用）上游 = **FUN_1009d2e0（引擎启动大函数）**——不是每战加载触发；env 锚点 [base+0x1bc8180] 战役时=0，战斗时绑定（机制未追）
- 主循环待处理分支执行 = FUN_109df0e0（vtable 分发）/ FUN_10aeea90（大函数，CAI 步骤）——未确认哪个是 env 创建调用者

## 4. 对路线 A 的意义（🔶）
- "加载 vs 结算"判定 = 主循环多条件（门控 + FUN_108b4ee0 人类检查 + pending 状态机 + uVar13 速度档），**不是单点**
- 撬动点候选：① [pending+0x55] ready（路线A 已有，re_a3_probe --set-ready）② [model+0x149c] 冲突列表填充（填充者未定位）③ FUN_108b4ee0 的 +0x6a0 检查（改判定=伪人类风险 P42）
- **实机裁决优先**：re_a3_probe --scan/--watch（看海态下观察 pending 状态迁移）→ 若 AI 内战激活 pending → --set-ready 测试加载

## 5. 复盘 4 问
1. **多知道了什么**：主循环"无人类→快速"的**列表级判定**（FUN_108b4ee0 遍历 [model+0x149c] 冲突列表查 faction+0x6a0）；pending 状态机消费端（FUN_106ff7b0 状态 0/10=空）；**uVar13 = 每 tick 模拟步数上限（无人类=5，有人类=1/2，特殊=200，非战斗速度等级）**；冲突列表结构（条目+0x1c→faction）；pending vtable 方法（参战方遍历）。
2. **什么没按预期**：预期找到单点"加载/结算"判定——实际是多条件组合 + 速度档；预期找到 pending 激活写入者——未定位（静态盲区）；BATTLE_ENV 构造在引擎启动链（FUN_1009d2e0）非每战加载。
3. **假设还成立吗**：H40（AI vs AI 是否激活 pending）仍开放（静态不可定案，实机裁决）；"无人类→结算"机制 = FUN_108b4ee0 列表检查 + uVar13=5 快速档。
4. **下一步最小实验**：实机 re_a3_probe --watch（看海态）观测 pending 状态/冲突列表迁移 → 裁决 H40 → 若激活则 --set-ready 测试加载（路线A 实验 A/B）。

## 6. 地址速查
| 项 | RVA | 说明 |
|---|---|---|
| 主循环 | 0x703ea0 | 门控+判定+uVar13 速度档+标记管理器 |
| 冲突列表人类检查 | 0x8b4ee0 | 遍历 [model+0x149c] 查 faction+0x6a0 |
| 冲突列表空检查 | 0x8bd240 | [model+0x149c] [+0x20]==[+0x24] |
| 冲突列表 getter | 0x8bd250 | 子对象→[+0x149c] |
| pending 状态检查 | 0x6ff7b0 | [pending+0x4c/+0x50]∈{0,10}=空 |
| 待处理执行包装 | 0x9df0e0 | [obj+4] vtable+0x3c → 0x9df850 |
| CAI 步骤路径 | 0x6ff700 / 0xaeea90 | session 移交+冲突列表 → "CAI do_cai_step" |
| 门控/速度档辅助 | 0x858c20 / 0x8b5060 / 0x8b4f80 / 0x8b4fb0 | 列表标志检查 |
| 状态 getter | 0xdbce0 | mov eax,[ecx+4];ret |
| 非空检查 | 0x1be5d0 | [model+0x14a8] |
| pending vtable | 0x15fa8a4 | slot1=参战方遍历 0x5d9070 |
| 冲突列表字段 | model+0x149c | vector（+0x20/+0x24） |
| pending 字段 | model+0x14a4 | +0x4c/+0x50 状态、+0x55 ready、+0x60/+0x64 参战方、+0xb8/+0xbc 参与者、+0xf8 player-setup |

## 7. 尝试1（2026-08-09 深夜）：pending 激活写入者追踪——进展与盲区

### 7.1 ⭐ pending 状态机值域（FUN_106ebf70 反汇编确证，新）
```
[ebp+0x14a4]（pending）→ +0x4c/+0x50 状态：
  状态==0 → 跳过（空）
  状态==10 → 跳过（终态）
  状态∈{非0,非10} → 继续
  [esi+0x160]→[+8]→[+0x14a4]（另一 model 的 pending）状态==9 → 检查参与者 FUN_105cb300
    → 含此 faction → [esi+0x99]=1（标记）
```
- **值域**：0=空、9=战斗加载/进行相关、10=终态；≠0/10 = "有待处理"
- **联动**：[esi+0x99] 标记 → 主循环门控 FUN_108b5060 读 [entry+0x99] → 走正常速度。即 **pending 状态==9 = 战斗正在加载/进行**，冲突条目被标记 → 主循环不快速结算。

### 7.2 参与者检查链（✅）
- FUN_105cb300(pending, faction) → FUN_105cb330（参战方列表 +0x60/+0x64 的对象：+0xc=key、+0xb8/+0xbc=参与者表）→ FUN_105870b0 比对 → 是否已参与
- 调用者：0x6d31de/0x6d350d/0x6d365a/0x6d3668（FUN_106d31c0 等）、0x6ec025（FUN_106ebf70）、0x6fab90/0x6fabda（FUN_106fab40/FUN_106fabb0 已参战标记，观战会话全局 0x11a7d77a 门控）

### 7.3 pending 重置/初始化（✅）
- FUN_106fabf0（0x6fabf0，124B）：写 +0xc=0/+0x14=arg/+0x18=0/+0x1c..+0x30/+0x64=0（清参战方）/+0x6c word=0/+0x70=2/+0x74=2/+0x78/+0x7c/+0x80=0 —— 参战方双列表初始化/重置

### 7.4 ⚠️ 盲区（尝试1 未突破）
- **[pending+0x4c/+0x50] 状态的直接写入者仍未定位**：279 个 [reg+0x14a4] 读取点后续 250B 无 +0x4c/+0x50/+0x55/+0x60/+0x64/+0xb8/+0xbc 写；猜测状态写可能经**间接链**（[x+0x160]→[+8]→[+0x14a4]）或 pending 构造/重置（FUN_105712b0 初始化）+ 外部 setter。
- 下一步候选：a) 追 FUN_105712b0（构造）看 +0x50 初值 & 是否有运行时 setter 引用；b) 扫间接链 [x+0x160]→[+8]→[+0x14a4] 的写；c) 实机 re_a3_probe --watch 看状态迁移（裁决优先，静态盲区成本高）。
