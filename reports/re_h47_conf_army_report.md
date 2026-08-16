# 冲突管理器 → 参战军队对象提取路径（2026-08-11，subagent 静态产出）

> 任务：从冲突管理器提取 AI 参战军队对象（供 desc 模板指针替换注入）。
> 方法：Empire.Retail.dll build 6262 纯静态（capstone + Ghidra），只读。
> 结论：**冲突条目直接内嵌军队对象**——entry[0x1c] + entry+0x58 链表，实机可复现遍历。

## 0. 关键结论速览
- **军队对象不在冲突条目内部字段直接保存单一指针**，两条路径：
  1. **entry[0x1c]** = 军队对象（faction = **(army+0x258)，人类标志 [faction+0x6a0]）
  2. **entry+0x58/+0x5c** 内嵌军队链表（节点 {prev@0, next@4, obj@8}），FUN_10875cb0(entry) 取尾/首节点 obj
- 事件处理 FUN_108c33e0 的军队**不来自冲突条目**，来自事件消息 param_2[0]（CCQ 命令军队）；冲突条目只做 faction 校验（entry[0x11c]）。
- 每条冲突 = 一个 AI 交战（攻/防两军）；冲突列表每个节点 = 独立交战条目。**攻/防双列表在 pending battle 对象（+0x60/+0x64），不在冲突条目**。

## 1. 提取路径链图（实机可复现）
```
model
  +0x149c → 冲突管理器 mgr   [FUN_108bd250 getter；mgr vtable 0x1620500]
mgr +0x20 = 链表 begin；+0x24 = end 哨兵
  节点: [node+4]=next，[node+8]=冲突条目 entry  (FUN_108b4ee0 / FUN_10102b50 证实)

entry (vtable RVA 0x16276d4 = runtime 0x7a7a76d4；大对象=entry-0x8c vtable 0x162768c)
  ├─ [entry+0x1c] → 军队对象 A
  │     faction = **(armyA+0x258)   (FUN_103e5960(armyA+0x254)，getter=*(*(x+4)))
  │     人类判定 [faction+0x6a0] != 0
  ├─ [entry+0x58] = 军队链表头(next)/[entry+0x5c] = 尾(prev)
  │     节点 { [0]=prev, [4]=next, [8]=军队对象 }
  │     FUN_10875cb0(entry)：取 [尾节点+8] 军队；[entry+0x74]!=0 时取 [头节点+8] (FUN_10875cf0)
  └─ 军队A/链表军队 → vtable[0x20](army, &out8)  out8={local_8(另一军队), piStack_4(对象)} ← 攻/防配对
```

**实机脚本伪码（只读遍历）：**
```
mgr = *(uint*)(model + 0x149c)
node = *(uint*)(mgr + 0x20);  end = *(uint*)(mgr + 0x24)
while node != end:
    entry = *(uint*)(node + 8)             # 校验 vtable == 0x7a7a76d4
    armyA = *(uint*)(entry + 0x1c)
    P = *(uint*)(armyA + 0x258); faction = *(uint*)P
    humanA = *(byte*)(faction + 0x6a0)
    head = *(uint*)(entry + 0x58); tail = *(uint*)(entry + 0x5c)
    if head != tail:
        armyB = *(uint*)(tail + 8)         # 或 [entry+0x74]!=0 用 *(uint*)(head+8)
    node = *(uint*)(node + 4)
```

## 2. 三个目标函数结论
- **FUN_108b4ee0**（0x8b4ee0，冲突列表遍历）：entry=[node+8]；[entry+0x1c]+0x254→FUN_103e5960→faction→[+0x6a0] 人类检测；army=FUN_10875cb0(entry)（链表军队）；位置门控 FUN_108c0f40（双方相邻才处理）。
- **FUN_10829b90**（0x829b90，登记/构造）：this[0]=param_3（登记对象，军队/阵营持有者）、this[0x18]=param_2（mgr）、内嵌双向链表头。**登记时军队指针没有固定在单一偏移**。
- **FUN_108c33e0**（0x8c33e0，entry slot1 事件处理）：军队来自事件消息 param_2[0]={army,code}（FUN_1085a820 构造），校验 [entry+0x11c]==faction。

## 3. 多军队遍历（攻/防列表）
- 冲突列表多节点 = 多场交战；每 entry 含参战军队（[0x1c] + +0x58 链表）。
- **攻/防双列表在 pending battle 对象（[model+0x14a4]）+0x60/+0x64**（vtable slot1=FUN_105d9070 遍历），不在冲突条目。
- 遍历全部 = 冲突列表全部节点 + 每节点 [0x1c] 与链表两节点，去重。

## 4. 军队对象特征字段（实机身份校验）
| 字段 | 含义 |
|---|---|
| army+0x258 | pointer-to-faction（faction=**(army+0x258)）|
| [faction+0x6a0] | 人类标志（≠0=人类）|
| army+0x170 | 决策槽位指针（FUN_108b4700 param_4 → +0x200 槽）|
| army+0x12c | AI 标志（战斗层，0=AI）|
| army+0x28c/290/294 | AI 字段（战斗层）|
| 军队步长 0x208 | 战斗层军队数组 |
| army vtable | **未知**（实机用 faction 链 + 特征字段定位）|

## 5. 关键地址（RVA）
| 项 | RVA |
|---|---|
| 冲突管理器 getter FUN_108bd250 | 0x8bd250 |
| 冲突列表遍历 FUN_108b4ee0 | 0x8b4ee0 |
| 冲突条目登记 FUN_10829b90 | 0x829b90 |
| 条目事件处理 FUN_108c33e0 | 0x8c33e0 |
| faction getter FUN_103e5960 | 0x3e5960（**x+4）|
| 链表取尾 FUN_10875cb0 / 取头 FUN_10875cf0 | 0x875cb0 / 0x875cf0 |
| 位置门控 FUN_108c0f40 | 0x8c0f40 |
| 决策分发 FUN_108b4700 | 0x8b4700 |
| 事件构造 FUN_1085a820 | 0x85a820（{army,code}）|
| 冲突条目 vtable | 0x16276d4（slot1=0x8c33e0）|
| 冲突管理器 vtable | 0x1620500 |

## 6. 对目标3 的意义
**AI 内战的参战军队可从冲突列表直接提取**（entry[0x1c] + entry+0x58 链表）——这正是「人类 desc 模板 + AI 真实军队替换指针」注入方案的数据源。实机验证：AI 内战冲突列表有内容（03 二十二节：5 次 conf 登记/12 次 pend 替换）。
