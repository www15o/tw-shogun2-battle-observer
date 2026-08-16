# H47 人类 pre-battle 成功加载路径 vs AI 状态 9 崩溃路径（2026-08-11，subagent 静态产出）

> 任务：人类 pre-battle 战斗加载为什么成功？AI 状态 9 突破为什么崩？有没有可复用的加载触发路径/注入点？
> 方法：Empire.Retail.dll build 6262 纯静态（capstone 反汇编 + Ghidra 桥反编译），只读。
> 结论：**人类加载不走主循环状态 9 直接路径；人类走 CCQ 命令 handler（FUN_108880a0，CCQ_FINALISE_LOOT_OPTIONS），desc 数据来自加载队列中的 pending battle 对象（真实数据）。AI 内战 Q 为空 -> 状态 9 走分支 A 用全零 desc 直接调 FUN_105cbee0 -> 崩。**
> 修正 33_HANDOFF §2：状态 9 的 FUN_105cbee0 调用并非唯一触发点，且状态 9 自身有「空队列->直接加载 / 非空队列->append 进 pending battle 对象」双分支。

## 一、各调用者结论表（FUN_105cbee0 全部调用者）

| 调用者 | 调用点 RVA | 上下文 | 调 FUN_105cbee0 前 | desc 数据源 | 是否人类 pre-battle 路径 |
|---|---|---|---|---|---|
| **FUN_108880a0**（CCQ_FINALISE_LOOT_OPTIONS handler）| **0x8881f2** | CCQ 命令派发（玩家 pre-battle 最终化时）| 直接 new(0x1f8)，无 FUN_10560470 | **pending battle 对象（vtable 0x115fd0fc）条目拷贝**：+0x98 起 0x1e8 步长、count at +0x280，含真实参战方/参与者/setup | **是：人类 pre-battle 成功路径** |
| FUN_10604260（pending 状态机）状态 9 | 0x607779（分支A）/ 0x6077c6（分支B）| 主循环 pending 状态机 状态 9 | FUN_10560470(desc 组装) + FUN_10562e10 x2 | 两个全零默认对象 + 小写（FUN_105f6f50/1054e740）+ desc[0]=字符串查表 | **AI 内战路径**。空队列->分支 A 直接 FUN_105cbee0（全零 desc->崩）；非空队列->分支 B append 进 pending battle 对象 |
| FUN_105fd090 | 0x5fe3da / 0x5fe6e7 | realm-divide（幕府/帝国分裂）/ promotion 战后剧情战斗 | FUN_10560470(desc 组装) | FUN_10562e10 全零 + 字符串查表（p_bos_realm_divide_*/s_*_promotion_turn_start）| 否（剧情战后/剧情战斗，同款全零模式）|
| FUN_10a11600 | 0xa11600 | 角色数据战斗加载器（0xa11600/0xa11720/0xa117d0 家族；唯一 caller 0x9f1a5b）| FUN_10a077b0(param_1,1/2,本地缓冲) 填充 + FUN_10562e10 + FUN_10560470 | FUN_10a077b0 从角色数据列表（[param_1+0x15c]/[param_1+0x160]）填部分对象，其余仍全零 | 否（部分填充，非 pre-battle）|

## 二、核心破案：状态 9 双分支（RVA 0x607751~0x6077c6）

```
0x607751  mov esi, [[ebx+0x30]+8]+0x14a8   ; esi = 加载队列 Q
0x60775f  call 0x1be5d0                     ; al = (Q->head != 0)   [FUN_101be5d0 = cmp [ecx],0 / setne]
0x607766  jne 0x607780                      ; Q 非空 -> 分支 B
0x607768  mov ecx, edi; call 0x3e5960       ; 分支 A：model = FUN_103e5960(edi)
0x607779  call 0x5cbee0(model, &desc)       ;   -> 直接 FUN_105cbee0（H47 观测到的崩溃路径）
0x607780  mov ecx, [Q]; mov eax, [ecx]; call [eax+0x20]  ; 分支 B：iVar4 = Q[0]->slot8() = pending battle 对象
0x6077a2  mov edx, [iVar4+0x280]            ;   edx = 条目计数
0x6077ad  lea ecx, [edx+1]; mov [iVar4+0x98+0x1e8], ecx  ;   写回 [iVar4+0x280] = count+1
0x6077c6  call 0x5604c0([iVar4+0x98+edx*0x1e8], &desc)    ;   把状态9 desc 深拷 append 进 pending battle 对象
```

- **Q 为空**（AI 内战）-> 分支 A：状态 9 用 FUN_10560470 组装的全零 desc 直接调 FUN_105cbee0 -> 加载对象 desc 全零 -> 消费端 FUN_1059b680 空转 -> +0xc30 事件派发崩（H47a 观测吻合）。
- **Q 非空**（人类，pending battle 对象已在队列）-> 分支 B：状态 9 的 desc 被 append 进 pending battle 对象条目数组；真正的加载由 CCQ handler FUN_108880a0 从 pending battle 对象条目触发（真实数据）。

## 三、人类成功加载路径链图

```
人类点 attack -> 战役 CCQ 命令序列：
  CCQ_SET_PENDING_BATTLE_READY_TO_START（handler RVA 0x88a580）
    -> FUN_106fe6c0 -> FUN_105f6b20 -> FUN_105b5140（写 0x20B player-setup 条目到 pending setup 槽）
  陆军对象 vtable slot32 方法 FUN_107e7590（RVA 0x7e7590）创建 pending battle：
    -> new(0x284) + FUN_10575410（vtable 0x115fd0fc，+0x280=0 计数清零）+ FUN_105ea330 注册进 Q=[campaign+0x14a8]
  主循环状态机 FUN_10604260 状态 9：Q 非空 -> 分支 B：FUN_105604c0 把状态9 desc append 进 pending battle 对象
  玩家最终化 -> CCQ_FINALISE_LOOT_OPTIONS -> handler FUN_108880a0（RVA 0x8880a0）：
    1) iVar4 = Q[0]->vtable[8](Q[0])            （slot8=0x100f3220 = mov eax,ecx;ret -> 返回 this = pending battle 对象）
    2) 循环拷 [iVar4+0x280] 个 0x1e8 条目 -> 栈 desc 数组（FUN_105607a0 深拷 x2）
    3) FUN_105b6ca0(count, iVar4)                 （按 0/1/2 最终化 pending 阶段）
    4) FUN_105b60e0(Q)                            （弹出头节点：vtable[1]() + vtable[0](1)，销毁）
    5) 若 count>0：model = FUN_103e5960([iVar4+8]+0x254)；FUN_105cbee0(model, &desc[0])
       -> new(0x1f8) + FUN_10575110（加载对象 vtable 0x115fd168，desc=真实数据）
       -> FUN_105ea330(Q, 加载对象) -> 消费端 FUN_105b6370（slot1）-> FUN_1059b680 正常处理
```

## 四、[model+0x14a8] 加载队列结构（供实机枚举加载对象）

- 队列 Q = `[campaign+0x14a8]`，campaign = `[[model+0x8c]+8]`（model = FUN_105cbee0 的 this；CCQ handler 侧 param_2 即 campaign）。
- Q = `new(8)` 对象（构造 FUN_10571110，RVA 0x571110，无 vtable）：
  - `Q+0`：head 指针（单链表头）
  - `Q+4`：flag（bool）
- 注册 FUN_105ea330（RVA 0x5ea330，__thiscall，this=Q，param=新对象）：
  - 新对象 +4 = 原 head；Q+0 = 新对象；Q+4 = (obj->vtable[2]()==0)
- 弹出 FUN_105b60e0（RVA 0x5b60e0，this=Q）：Q+0 = obj->next；调 obj->vtable[1]()；调 obj->vtable[0](1)；Q+4=0
- 节点布局：+0 = vtable，+4 = next
- **实机枚举加载对象**：读 `[[model+0x8c]+8]+0x14a8` 得 Q -> 沿 Q+0 -> node+4 -> ... 遍历；
  判型：`node+0==0x115fd168`（加载对象）、`==0x115fd0fc`（pending battle 对象）。

### pending battle 对象布局（vtable 0x115fd0fc，new(0x284)，构造 FUN_10575410）
| 偏移 | 字段 |
|---|---|
| +0 | vtable 0x115fd0fc |
| +4 | next（队列链接）|
| +8 | 上下文/拥有者（FUN_107e7590 的 edi）|
| +0x8c | 字节 1 |
| +0x94 | float |
| **+0x98** | **desc[0] 控制 dword（条目 0 起点）** |
| +0x9c..+0x184 | desc+4..+0xec（0xe8B 深拷区；参战方 count/array 在 desc+0x5c/+0x60 -> 对象+0xf4/+0xf8；参与者 array/count 在 desc+0xb8/+0xbc -> 对象+0x150/+0x154）|
| +0x188..+0x270 | desc+0xf0..+0x1d8（0xe8B 深拷区；setup 在 desc+0xfc/+0x100/+0x104 -> 对象+0x194/+0x198/+0x19c）|
| +0x274/+0x278/+0x27c | desc+0x1dc/+0x1e0/+0x1e4（3 标量）|
| **+0x280** | **条目计数**（新建=0；每 append/填充一次 +1）|
- 条目结构 = 0x1e8 字节 desc 模板（与 FUN_10560470/FUN_105604c0 输出同布局），深拷 helper = FUN_105607a0（0xe8B）/ FUN_105604c0（整条目 append）。

## 五、结论

### 1. 人类 vs AI 加载路径差异表
| | 人类 pre-battle | AI 内战（状态 9 突破）|
|---|---|---|
| pending battle 对象（Q 中，vtable 0x115fd0fc）| 存在（FUN_107e7590 创建 + setup 命令填充）| 无（Q 空）|
| 状态 9 分支 | 分支 B（append desc 进 pending battle 对象）| 分支 A（直接 FUN_105cbee0）|
| FUN_105cbee0 的 desc | 来自 pending battle 对象条目（真实参战方/参与者/setup）| FUN_10560470 全零对象组装 |
| 结果 | 加载对象 desc 非空 -> 正常加载 | desc 全零 -> FUN_1059b680 空转 -> +0xc30 事件派发崩 |

### 2. AI 内战可复用的加载触发路径 / 注入点（按可行性）
1. **首选（无需 CCQ，最直接）**：在 FUN_105cbee0 内「构造后、注册前」（RVA 0x5cbf56~0x5cbf62）或状态 9 分支 A 调 FUN_105cbee0 前，直接写加载对象 desc 区（`加载对象+0x10` 与 `+0xfc` 两份），字段集见 re_h47_crashpoint_report §3（参战方 +0x5c/+0x60、参与者 +0xb8/+0xbc、setup +0xfc/+0x100/+0x104、local_38 门控 desc[0] 属于 {0,1}）。模板结构 = 上表 pending battle 对象条目布局。
2. **引擎自带排队路径复用（分支 B + CCQ）**：若向 Q 注入一个**已填好条目 0 的 pending battle 对象**（vtable 0x115fd0fc，+0x98 起写 0x1e8 desc 模板，+0x280=1），状态 9 自动改走分支 B（append），随后 CCQ handler FUN_108880a0 处理队列时会用真实条目 0 调 FUN_105cbee0。**约束**：需要 CCQ_FINALISE_LOOT_OPTIONS 命令被触发（campaign 脚本发送），且注入的 desc 模板仍需包含真实参战方/参与者/setup 指针——数据工作量与方案 1 相当，还多一个命令触发依赖，故仅当希望利用引擎原生加载流程时选用。
3. **不推荐**：让 AI 走 FUN_105fd090/FUN_10a11600（剧情战斗路径，同样全零 desc，且业务语义不符）。

### 3. desc 数据源结构（注入模板）
- 人类 desc 非零，数据源 = pending battle 对象条目（+0x98 起，0x1e8 布局）-> FUN_105cbee0 -> FUN_10575110 薄拷到加载对象 +0x10（状态1拷贝）与 +0xfc（状态2拷贝）。
- 关键字段偏移（desc 相对起点）：
  - desc[0]：控制/冲突引用（属于 {0,1} 才解锁军队/参与者块 10/11/12，否则 local_38=0 空转）
  - desc+0x5c/+0x60：参战方 count / 指针数组（每元素须为合法军队对象 vtable、+0xe0/+0xe4、+0x1c8 链）
  - desc+0xb8/+0xbc：参与者 array / count（每元素 vtable、+0x2c、+0x30）
  - desc+0xfc/+0x100/+0x104：setup count / 槽表（8B 项）
  - desc+0x1dc/+0x1e0/+0x1e4：3 标量（0 / 会话指针 / 0）

## 六、关键地址表（RVA）
| 项 | RVA | 说明 |
|---|---|---|
| CCQ_FINALISE_LOOT_OPTIONS handler | 0x8880a0 | 人类加载 handler；0x8881f2 调 FUN_105cbee0 |
| handler 注册点 | 0x4181b | mov [esp+0x18],0x108880a0 + 命令名 CCQ_FINALISE_LOOT_OPTIONS(VA 0x1161cfb0) |
| CCQ_SET_PENDING_BATTLE_READY_TO_START handler | 0x88a580 | -> FUN_106fe6c0 -> 0x5f6b20 -> FUN_105b5140（写 player-setup 条目）|
| pending battle 创建 | 0x7e7590 / 0x7e7c10 | 陆军 vtable slot32/40；new(0x284)+FUN_10575410+FUN_105ea330 |
| pending battle 构造器 | 0x575410 | vtable 0x115fd0fc，+0x280=0 |
| pending battle vtable | 0x115fd0fc | slot8=0x100f3220（返回 this）；slot28=0x5b6370 |
| 状态 9 分支 A（直接加载）| 0x607779 | FUN_105cbee0(model,&desc) |
| 状态 9 分支 B（append）| 0x6077c6 | FUN_105604c0(pending+0x98+count*0x1e8,&desc) |
| 队列 gate | 0x1be5d0 | [Q]!=0 |
| 队列构造 | 0x571110 | new(8)：+0=head、+4=flag |
| 队列注册/弹出 | 0x5ea330 / 0x5b60e0 | 见第四节 |
| desc 深拷 | 0x5607a0（0xe8B）/ 0x5604c0（整条目）| |
| 人类模型获取 | 0x3e5960 | **(param+4)；CCQ handler 传 [pending+8]+0x254 |
