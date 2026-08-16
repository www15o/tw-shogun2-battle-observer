# H47 描述符数据来源与注入目标判定（2026-08-11，subagent 静态产出）

> 任务：战斗加载对象描述符数据从哪来？写 pending 能否传导到 desc？正确注入目标/时机？
> 方法：Empire.Retail.dll build 6262 纯静态（capstone + Ghidra 桥），只读。
> 结论：**引擎无 pending→desc 拷贝路径，注入必须直接写加载对象描述符区（+0x10 与 +0xfc 两份）**。

## 一、描述符来源链图（对象 → 函数 → desc 字段）

```
[pending 对象 vtable 0x115fa8a4]           [model+0x14a0 战斗请求描述符]（独立对象）
        │  状态机 FUN_10604260 状态 9               │（FUN_10657120 写入，加载链不消费）
        │  RVA 0x6076e0..0x607740                   ▼
        ▼
  ① 新建两个 FUN_10562e10 对象（RVA 0x562e10，纯默认构造，全零，+0=2）
  ② obj#1 两处小写入：+0xd4/+0xd8（8字节，来自全局会话指针 FUN_105870e0）；+0x38 = -FUN_10698010 结果
     obj#2 不做任何写入（保持全零）
  ③ FUN_10560470 (RVA 0x560470) 组装 desc（栈缓冲 [esp+0x19c]，0x1e8 字节）：
     desc[0] = FUN_105eca90 结果（"s_promotion_post_battle"/"s_bos_…" 字符串查表）
     desc+4..+0xec = 深拷 obj#1；desc+0xf0..+0x1d8 = 深拷 obj#2
     desc+0x1dc/+0x1e0/+0x1e4 = 3 个标量（0 / 会话指针 / 0）
  ④ FUN_105cbee0 (RVA 0x5cbee0)：new(0x1f8) → FUN_10575110(加载对象, model, &desc) → FUN_105ea330 注册
  ⑤ FUN_10575110 薄拷入加载对象：+0x00=vtable 0x115fd168；+0x08=model；+0x0c=desc[0]；
     +0x10..+0xf8 = 深拷 desc+4..+0xec（=obj#1 拷贝）；+0xfc..+0x1e4 = 深拷 desc+0xf0..+0x1d8（=obj#2 拷贝）；
     +0x1f4 = 0（状态，决定消费端用哪个拷贝）
  ⑥ 消费端 FUN_105b6370 (slot1)：[加载对象+0x1f4]==1 → FUN_1059b680(ecx=+0x10, model)；==2 → (ecx=+0xfc, model)
```

## 二、关键结论

1. **FUN_10562e10（RVA 0x562e10）= 描述符源对象纯默认构造器**（全零，0xe8 字节，+0=2 计数）。**不读 pending/[model+0x14a0]/model**。
2. **FUN_10560470 不引用 pending（[model+0x14a4]）也不引用 [model+0x14a0]**；状态 9 里 pending 只作为状态机 gates（c2/c8/+0x58 等）被读，不进入 desc。
3. **FUN_10575110 除薄拷 desc 外无其他来源**；FUN_105cbee0 在构造/注册之间无其他数据写入；**[model+0x14a0] 不被加载链消费**。
4. **没有任何函数给描述符源对象填非零**（穷举 FUN_10562e10 的 18 个调用者，全是 new+default ctor+FUN_10560470 同类模式）。

## 三、注入目标判定（核心答案）

**写 pending 字段（参与者表 +0xb8/+0xbc、setup +0xf8..、参战方 +0x60/+0x64）不会传导到加载对象描述符。** 引擎每次用全新 FUN_10562e10 全零对象重建 desc。

**必须直接写加载对象描述符区**（`加载对象+0x10` 与 `加载对象+0xfc` 两份都写），字段集（对照 re_h47_crashpoint_report.md §3）：
- 参战方（军队）表：desc+0x5c=count、desc+0x60=指针数组（两处拷贝都写）
- 参与者表：desc+0xb8=数组、desc+0xbc=count
- setup 槽：desc+0xfc=count、+0x100/+0x104=槽表（8 字节项）
- local_38 门控：desc[0]∈{0,1} 或 [model+0x6e0] 有效（否则块 10/11/12 不可达）

**最佳注入时机**：
1. 首选：FUN_10575110 返回后、FUN_105ea330 注册前（FUN_105cbee0 内 RVA 0x5cbf56~0x5cbf62）——**函数内部微秒级窗口，外部轮询无法赶上，需代码补丁**。
2. 或注册后立即写（消费端在 [加载对象+0x1f4]==1/2 才触发，主循环下一 tick 前写可能来得及）。

## 四、开放疑点（追人类成功路径）

- 人类 pre-battle 加载成功，但状态 9 desc 组装也是 FUN_10562e10 全零——**人类路径为何不崩？**
- FUN_105cbee0 其他调用者（33 §2 记录，未追）：0x8881f2（CCQ pending 命令区）、FUN_105fd090 ×2（0x5fe3da/0x5fe6e7）、FUN_10a11600——**人类加载可能走这些调用者（不同数据源），而非主循环状态 9**。

## 五、关键地址（RVA）

| 项 | RVA | 说明 |
|---|---|---|
| FUN_10562e10 | 0x562e10 | 描述符源对象默认构造（全零）|
| FUN_10560470 | 0x560470 | 描述符组装 |
| FUN_105607a0 | 0x5607a0 | 0xe8 字节深拷 helper |
| FUN_105f6f50 / FUN_1054e740 | 0x5f6f50 / 0x54e740 | obj#1 小写入 |
| FUN_105eca90 | 0x5eca90 | desc[0] 来源（字符串查表）|
| FUN_105cbee0 | 0x5cbee0 | 加载入口：new(0x1f8)→构造→注册 |
| FUN_10575110 | 0x575110 | 加载对象构造（薄拷 desc）|
| FUN_105ea330 | 0x5ea330 | 注册进加载队列 |
| 状态 9 desc 组装点 | 0x6076e0..0x607740 | 两 FUN_10562e10 + FUN_10560470 |
| 注入点（构造后注册前）| 0x5cbf56..0x5cbf62 | 写加载对象 +0x10/+0xfc |
| 加载对象 vtable | 0x115fd168 | slot1=0x5b6370 |
| desc[0] 字符串 | 0x15ff324 / 0x15ff340 | "s_bos_promotion_post_battle"/"s_promotion_post_battle" |
