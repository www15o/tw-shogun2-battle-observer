# 目标4 原生 AI 行为机制地图（41_GOAL4_AI_MECHANISM_MAP）

> 主题：**原生战斗 AI 行为机制研究**——为什么 AI 这样打（不用枪衾/三段击/火矢、不走城门、暴鸣声等），为「原生 AI 优化」（让 AI 更聪明/更符合预期）提供机制基础。
> 本文档只收录当前成立的结论；历史记录见 **Goal_4_LogBook.md**；选路/换路见 **15_GOAL4_EXPLORATION_MAP.md**。
> 来源：docs/54_HANDOFF_战斗细节调研 + 55 交接 + 8 份 work/re_* 报告（2026-08-16 十二轮调研）。**由 11 机制地图 §11 迁移而来（2026-08-16，与目标1「实现原生 AI 操控玩家部队」区分：本目标研究 AI 自身行为，目标1 研究操控通道）**。
> ★**任务集映射（2026-08-16）**：Goal4 = 多任务集（一任务一路线，见 15 探索地图）——T1 枪衾（§1.1/1.4）/ T2 三段击（§1.2）/ T3 火矢（§1.3/1.4）/ T4 走门攻门（§3）/ T5 暴鸣（§2）/ T6 攻城整体优化（§3.4）。

---

## 0. 一句话现状

**Q1-Q5 五问全部机制闭环**：AI 不用能力/不走门 = **引擎决策层行为**（能力命令不入计划链；攻城目标件打分门盲；门系统无 AI 入口），**改 db 无效（战术/能力选择 DLL 硬编码）**；Q3 暴鸣 = 声音事件→样本映射损坏（mod 诱发）；「AI 门盲」四环节定案（不选门/不看门状态/不开门/门开了也不进）。**下一步 = 动态验证 + AI 优化注入方案（hook 打分/目标覆盖）**。

---

## 1. 能力使用机制（Q1a 枪衾 / Q1b 三段击 / Q2 火矢）

### 1.1 S2 能力注册表（DLL RVA 0x10ec000-0x10ef000，能力 id 对）

| 能力键 | id 对 | 备注 |
|---|---|---|
| cantabrian_circle | 0x20/0x21 | 骑射 |
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

### 1.2 SquadTaskFireVolley 射击状态机（铁炮三段击，Q1b）

- 源码路径 `model\squad\squadtask\SquadTaskFireVolley.cpp`（@0x15b66a8）；tweaker 调试名「Squad Task Fire Volley」注册 @0x10df8。
- 状态名册（0x15b45b8-0x15b978c）：**逐排** CURRENT_ROW_FIRE / WAIT_FOR_CURRENT_ROW_LOADED_AND_READY / CURRENT_ROW_ADVANCE / INCREMENT_CURRENT_ROW / WAIT_FOR_FINAL_ROW_LOADED；**齐射** ORDER_GROUP_FIRE / WAIT_FOR_GROUP_FIRE_COMPLETED / ADVANCE_GROUP / WAIT_FOR_FIRST_GROUP_LOADED；**门控** WAIT_FOR_CAN_SHOOT_TEST / PERFORM_CAN_SHOOT_TEST / CANNOT_SHOOT / FACE_TARGET / REFORM / BUILD_REFORM_SPINE / FIRE_UNFORMED / DEFEND_AND_FIRE / FIRE_AND_ADVANCE；**打断** FIRE_AT_WILL / SKIRMISH / MELEE / NEW_ORDER / IDLE INTERRUPT 族。
- 状态机完整存在（✅）；**进入条件未解**（不触发候选：任务分配/门控失败/打断抢占，⚠️ 待 hook 或实机）。

### 1.3 火矢机制件（Q2）

- 能力三档 0x22-0x27 + enable 命令（`enable_ability_fire_arrows[_long_range|_extreme_range]` @0x16d9084 族）+ SPECIAL_ABILITY_FIRE_ARROWS 键（0x16902f0）。
- **tweaker 最小距离门控**：`LAND_GROUP_FIRE_ARROW_MIN_DISTANCE`（注册 0xcad61c）/ `NAVAL_GROUP_FIRE_ARROW_MIN_DISTANCE`（0xcad8ce）。
- 数据侧：projectiles 表含 flaming_arrow / arrow_flaming_naval / fire_arrow_trail → **火矢 = 独立弹药类型**（arrow↔flaming_arrow 切换）。
- **★引擎能力命令族（2026-08-16）**：`BCQ_UNIT_ORDER_SPECIAL_ABILITY`（handler **0x2ab840**）/ `BCQ_MULTIPLE_SELECTION_ORDER_CHANGE_SPECIAL_ABILITY`（**0x2a86f0**）/ **`...ON_AUTOTRIGGER`（**0x2a87b0**，自动触发标志）**——引擎有完整能力命令通道 + 自动触发机制；共用 seSpecialAbilityManager.cpp 链（0x284b60→0x285930）。
- **实机**：佐竹弓兵**部分单位自动切火矢**（AUTOTRIGGER 生效）→ **AI 会切火矢**，切换条件（目标类型偏好）未证。

### 1.5 ★三类能力通道对比（T1 枪衾 vs T2 三段击 vs T3 火矢，2026-08-16）

| 维度 | T1 枪衾（pike_wall_formation） | T2 三段击（rank_fire） | T3 火矢（flaming_arrows） |
|---|---|---|---|
| 能力类型 | 阵型/状态切换（近战防御阵） | 射击模式（铁炮逐排射击） | 弹药切换（arrow↔flaming_arrow） |
| 触发通道 | **能力命令**（BCQ_UNIT_ORDER_SPECIAL_ABILITY 0x2ab840） | **射击任务系统**（SquadTaskFireVolley 任务分配） | **AUTOTRIGGER 自动触发**（0x2a87b0 标志） |
| DLL 内发送器 | **无**（hash 全 .text 零引用，§1.4） | 任务系统存在（AI 有分配权） | **有**（引擎自动触发机制） |
| AI 可达性 | 🔴 通道关闭（无代码能发能力命令，与门命令同构） | 🟡 理论可达实际不触发（FireVolley 从未被分配：任务选择/CAN_SHOOT_TEST 门控/打断候选） | 🟢 通道打开（AI 会置自动触发标志，佐竹实证） |
| 实机 | AI 从不开阵 | AI 不三段击/不齐射 | 佐竹弓兵部分自动切火矢 |

**结论**：三类能力通道三态（关/阻/开）——T3 有 AI 自动通道（搁置合理）；T1 注入能力命令（外部发 BCQ）；T2 解任务分配条件（让 AI 自己选 FireVolley）。

### 1.4 ★能力数据完整性（2026-08-16 RPFM 提取 data.pack，Q1/Q2 关键实证）

- **`unit_abilities` 表**：`pike_wall_formation`（显示 "Yari Wall"）= 枪衾数据键；`rank_fire` ×2；`rapid_volley`；flaming 家族。**无 AI 使用开关列（无 Rome2 式 ai_usage）**。
- **`unit_to_unit_abilities_junctions` 表**（单位→能力分配，✅ 全部分配）：枪足轻全家族→`pike_wall_formation`；铁炮足轻全家族→`bamboo_wall`+`rank_fire`（武士+`rapid_volley`）；弓足轻/弓侍→`flaming_arrows_ability`；弓僧兵→`long_range`；弓英雄→`extreme_range`；骑射→`cantabrian_circle`。
- **`unit_special_abilities` 表**：flaming_arrows_ability 行含子条目 `flaming_arrow` / `arrow_flaming(_long_range/_exteme_range/_naval)` = 火矢弹药切换数据。
- **结论（Q1a/Q1b/Q2 定案）**：「AI 不用」**不是数据缺失**（能力全部分配）→ **引擎决策层问题**：AI 计划链（AIBattleAnalyser 产物）不含能力命令 → AI 不主动开阵型/射击能力。**★2026-08-16 证据升级（hash 校准确证）**：BCQ_UNIT_ORDER_SPECIAL_ABILITY（0x147ba8b3）/BCQ_MULTIPLE_SELECTION_ORDER_CHANGE_SPECIAL_ABILITY（0xc00bb50c）/enable_ability_fire_arrows（0x722cb90b）hash 在 .text **零引用**（hash 算法经 0xb8d940 反汇编 + GATE_STATUS=0xb0415d86 汇编校准 MATCH 确证可靠）→ **DLL 内物理无能力命令发送器**（能力命令只服务 UI/脚本/Lua 绑定，与门命令同构）。**Pack 分析收官（改 db 无效），优化必须走引擎层（hook 能力命令/任务分配）**。T1/T2 任务卡：work/re_t1t2_analysis.md。

---

## 2. 音效机制（Q3 弓箭暴鸣声）

### 2.1 根因（2026-08-16 实证）

- **暴鸣 = 声音事件→样本映射损坏**：Sinfonia_Semplice.pack（PFH2）= 替换 `sounds_packed/sound_events` + `sounds_packed/sound_bank_database`（事件→银行→样本映射数据）；修复 = 覆盖映射数据。用户已装但 AI vs AI 场仍听到（修复未覆盖/顺序问题）。
- **★分族差异 = 「单位键」模式**：织田弓足轻（专属键 `_Oda`）=正常音；今川/佐竹（通用键）=炮声；units_tables 通用行 vs `_Oda` 行**字节级一致**（数据排除）；H-A 火焰箭假设**证伪**（普通箭也炮声）。

### 2.2 引擎音效体系

- sound.pack 箭矢命中音效 = `ntw\projectiles\ntw_arrow_hitstreecanopy_*.wav`（ntw\ = 继承拿破仑/帝国音频资产）；tweaker `AUDIO_DISTANCE_LAND/NAVAL_PROJECTILES_ARROW_CLOSE/MEDIUM`；UnitSoundTracker.cpp / NavalSoundTracker.cpp；BOW_DRAW/BOW_RELEASE 事件；Miles（mss32.dll）。
- **未决**：精确触发链（哪个事件→哪个样本；UnitSoundTracker BOW_RELEASE 事件选择代码 0x166fb0c 族）；单位键如何影响事件→样本映射。

---

## 3. 攻城战术机制（Q4/Q5：AI 不走门）

### 3.1 战术名册与 tactics_log 机制

- **战术名册**：RVA 0x15b4940-0x15b4c18 内联 ASCII 串数组（43 名）：move_to_point / withdraw / scout / support_group / attack_enemy_battlegroup / attack_enemy_battlegroup_naval / defend_line / defend_crossing / block_crossing / assault_crossing / seize_crossing / skirmish / outflank / double_envelopment / stop_and_shoot / warcry / limbered_artillery / special_tactic / general_support / fort_assault / fort_reinforcement / attack_building / **assault_gate** / assault_wall_ladders / assault_wall_siege_tower / sap / capture_plaza / capture_buildings / general_attack_settlement / attack_settlement_surplus / defend_walls / defend_breaches / defend_junctions / defend_plaza / general_defend_settlement / defend_settlement_surplus / support_defend_settlement / support_defend_settlement_surplus / sally_out。
- **★名册指针表**：.data VA 0x1179c218（RVA 0x179c218）43 项；idx：22=fort_assault、23=fort_reinforcement、24=attack_building、**25=assault_gate**、26=assault_wall_ladders、27=assault_wall_siege_tower、28=sap。唯一 xref = 日志写函数 0x506440。
- **★tactics_log 机制（确证，可复用）**：格式串 RVA 0x15eb508 = `%s,\t%u,\t%u,`；写函数 **0x506440** = `fprintf(FILE*, fmt, 名册表[idx], rec[+8]=X, rec[+0xc]=Y)`；记录 16B 容器内联 **AI SC+0x63c**；fopen_s 门控 [VA 0x11a6a082]≠0（构造 0x496cf0）；AI SC 析构 0x4ad340→0x4adf00 flush 写盘（战斗结束）。**X/Y = 编队 id / 单位实体 id**（海战 X==Y 8..2153 连续缺 9；攻城 fort_assault 恒 (8,各单位) 先于 assault_gate/fort_reinforcement (突击组,目标)）。分析脚本：work/_tactics_log_analyze.py（改 DUMP 路径复用）。

### 3.2 assault_gate 语义（= 攻城门建筑件，三重证据）

1. **名册族谱**：夹在 attack_building 与 assault_wall_ladders/siege_tower/sap 之间（要塞攻城战术族）。
2. **全 DLL "gate" 字符串清点**：全部是城门概念（BCQ_BUILDING_PIECE_CHANGE_GATE_STATUS 0x15cf160 / FORCE_GATES_OPEN 0x15e3658 / gate_open_time / _gate_open/_gate_ajar 动画 / adc_fort_gates_*），**无任何地形 gate 串**（反证「地形隘口」假设）。
3. **攻城 AI 0x430cc0 族**显式按件名 "gate"(0x15d5b14)/"gate_ajar"(0x15e835c) 识别城门建筑件送 0x44c0a0 几何分类。

### 3.3 ★AI 门盲（Q4/Q5 终极闭环，2026-08-16 十二轮）

- **AI 选择层会选 assault_gate**（✅ tactics_log 实证：3 场攻城战分配块全部含 `assault_gate,X,Y` 共 6 行）——「AI 从不选攻门」旧结论**证伪**。
- **但执行层四环节全断（字节级确证）**：
  1. **不选门**：fort planner 0x50fba0 目标件 = 0x4e8d90 挑 **0x514000 防守密度最高分件**（纯单位强度求和 [unit+0xcac]+[unit+0xca8] 加权×2，**无门状态读取/无件类型过滤**）→ 门件无守军分数≈0 永远不被选 → 突击组攻墙段。
  2. **不看门状态**：门状态（门对象 +0x68/+0x8c/+0xec/+0xf1；门条目状态字节 [+0x6c]）**读取者全在视觉/表现层**（0x30a4c0 动画状态机/0x4780c0 运动学）→ AI 决策层零可见；layout+0x222 死标志；门件+0x167f=已毁标志非开关。
  3. **不开门/不夺门**：门开关 = 门件 update 0x47f990→0x487438 **直接调 0x477f40/0x48a4c0**（非 BCQ，0x477f40 调用者 5 处：步进/带动画/超时自动关←fort tick 0x426ba0/0x487535）；BCQ 门命令（0x475e40）只是外部注入通道（DLL 内无 AI 发送器）；**门=「单位在场计数驱动」**（门件 [+0x84]/[+0x88] 两军计数>0 即开，无计时）；**AI 不主动夺门**（夺旗任务系统对 AI 开放——任务类型 6/0xc0cb0/fort planner 0x50fc3c 相位 0x13/0x52eec0 进度比，但无「站门旗」指派命令）。
  4. **门开了也不进**：门开/关不改变任何件分数 → **门开前后 AI 决策逐字节不变，照样爬墙**（用户实机「门开了 AI 照样爬墙」完美解释）；「用门」逻辑不存在（门状态函数零 AI 决策调用者）。
- **夺门/控制点系统（用户洞察部分证实）**：`gate_capture_time`（0x16d0bdc 夺区参数族，注册 0xe6fb28）/`gate_opening_chance`（0x16d8574，0xecce8d 运行时表）**消费者未定位**（KV-tweaker 间接访问，诚实）；门是否注册为 capture point 待查 pack（capture_location_list.xml 0x1658260/夺区对象 0x4a84b0/[ctx+0x23f4]）；`adc_enemy_gates_captured`（提示 id 9）存在 = 夺门是设计内概念。
- **★战术选择 = DLL 硬编码（Pack 定案）**：战术名册串 data.pack 871 张 db 表 + 1245 张 schema 定义**零出现**；battle_ai_abilities_usage_params 等 7 张 AI 调参表 data.pack 缺席（全走 DLL 默认值）→ **改 db 无效**。
- **溃兵走门（用户观察，补寻路缺口）**：崩溃单位从门撤离 = **寻路层认门实机证据**（门=可行通道，碰撞类型 collision3d_gate=ID 3 独立处理）；进攻不走门 ≠ 寻路不认门，而是**目标决策层从不把门当目标**（目标=墙段）→ **重复寻路不能解决**（重算不改目标）；**走门正解 = 寻路/移动层注入「移动到门内点」命令**（绕过 fort planner 硬编码决策）。

### 3.4 AI 战术选择决策链（结构确证）

AI SC Update 0x506ef0 → 每 10 tick AIBattleAnalyser0 0x520140（0x506e00/0x5171e0 → 0x5041d0/0x504200/0x228630 设 [AI_SC+0x5ec] 战斗状态）→ 分发表 0x50f860（状态 2..0xe → 0x50f360/0x50f5b0/0x50fa40/0x50ffe0/0x50f3e0）→ 组管理器 [AI_SC+0x28] 行动码 0xa/0x46/0x64（0x4d19f0/0x4d1980/0x4d1a60/0x4d1dc0 入队→0x4fce30 登记）→ 组链表非空走 fort planner 包装 0x50fb70 → 要塞战规划器 0x50fba0（**+0x518!=0 才主流程**）+ 攻城 AI 0x489620←0x485080←战场更新 0x3a39cb → 0x430cc0 族（门件分析）+ Fort Analysis 0x42c6e0/0x42cb50/0x43dc80。**+0x518 实机=0（自定义攻城战 fort planner 未启用？待确认启用条件）**。

---

## 4. 关键地址速查

| 项 | RVA/VA | 说明 |
|---|---|---|
| 能力注册表 | 0x10ec000-0x10ef000 | 能力 id 对（§1.1） |
| SquadTaskFireVolley | 0x15b66a8 / 状态名册 0x15b45b8-0x15b978c | 三段击状态机 |
| 能力命令族 | 0x2ab840 / 0x2a86f0 / 0x2a87b0 | BCQ 能力/自动触发 handler |
| 战术名册 | 0x15b4940-0x15b4c18（内联串）；指针表 0x179c218 | 43 名，assault_gate=25 |
| tactics_log | 格式串 0x15eb508 / 写函数 0x506440 / fopen 0x496cf0 / flush 0x4ad340 | 门控 [0x11A6A082] |
| fort planner | 0x50fba0（+0x518!=0 主流程）/ 0x50fb70 包装 / 选件 0x4e8d90 / 打分 0x514000 | 目标件=防守密度最高件 |
| 门状态 | 门对象 +0x68/+0x8c/+0xec/+0xf1；门条目 [+0x6c]；setter 0x477f40 | 读取者全在表现层 |
| 门开关链 | 0x47f990→0x487438→0x477f40/0x48a4c0 | 非 BCQ；BCQ handler 0x475e40 |
| 夺门系统 | gate_capture_time 0x16d0bdc / gate_opening_chance 0x16d8574 / capture_point 0x16e7c6c | 消费者未定位 |
| 攻城 AI | 0x430cc0 族（门件识别）/ 0x489620 / Fort Analysis 0x42c6e0 | 件名 "gate" 0x15d5b14 |
| 音效 | BOW_DRAW/BOW_RELEASE 事件；UnitSoundTracker 0x166fb0c 族 | 单位键模式 |
| 寻路/碰撞 | collision3d_gate=ID 3（0x16a31a4，查找表 0xd5b9cf）；PathSearch.cpp 0x15da660 | 溃兵走门=寻路认门 |

---

## 5. 当前前沿疑点（待验证）

- [ ] **fort planner +0x518 启用条件**（自定义攻城战实机=0，fort planner 未跑；hook 0x514000 探针两次崩溃已修——movss 重定位/recptr 位置/EFLAGS，待重试确认 0x514000 是否被调）
- [ ] 动态断点 7 项：0x50fe94（目标件=墙段?）/0x430c60+0x222（死标志）/注入「移动到门内点」（寻路认门?）/0x487438（开门触发语义）/0x12a7790（tweaker get 回栈找 gate_capture_time 消费者）/0x52eec0（AI 夺旗使用）/rpfm 查 capture_location_list.xml（门是否夺区）
- [ ] Q3 精确链：UnitSoundTracker BOW_RELEASE 事件选择代码（0x166fb0c 族）+ Sinfonia 映射 vs data.pack 差异
- [ ] Q3 单位键预测验证：专属键家族（长宗我部/服部/一向）=正常音 vs 通用键（武田/岛津/上杉）=炮声
- [ ] Q1b SquadTaskFireVolley 进入条件（任务分配/CANNOT_SHOOT 门控/打断抢占）
- [ ] Q2 AUTOTRIGGER 发送方（AI 何时置自动触发标志）
- [ ] land_units / unit_variants 定位（movies/models 包，禁 mmap 全映射，用 RPFM extract）
- [ ] ⚠️ 2025_fix_bundle.pack 文件缺失确认（launcher 引用但 data/ 无文件 → mod 可能未生效，影响全部 mod 影响解读）
- [ ] **★AI 防守固守族（2026-08-17 fork 会话挂起，未展开）**：玩家需求「AI 防守野战原地固守被僧弓白嫖（山上不动）」。机制定位（已确证）：固守执行器 = 0x4ce4a0 每 10 tick 直推防守单 0x186300 → hold 原地；不反击 = 交战触发距离（0x202080/0x3bfa50）< 远程射程。已证伪：impetuous 数据通道（引擎消费点 = 士气状态机 `morale_impetuous`/`ums_impetuous_threshold_lower=36`，vanilla 0 兵种用它）；kv 三表无触发距离键（enemy_effect_range=80 是士气半径）。**待办（与 T4 同族：AI 战术层反应性不足）**：静态定位 0x4ce4a0 防守单推送的触发条件（读哪些输入：距离/被远程攻击标志/周期）→ 判断改哪里能让被射防守单位动起来；候选通道 = 引擎判定点 patch / battle.ai_unit_controller clear_objective（脚本加载路径同 T4 卡点）
- [ ] **★battle_entities / kv 表数据层记录（2026-08-17 fork 会话，已完成）**：见 41 §6「数据层速查」；xhold 受击反应机制已确证（引擎调试串 0x1645CEF）

## 6. 数据层速查（2026-08-17 fork 会话新增）

> 全部经 RPFM extract data.pack → TSV，产物在 `work/tsv_extract/db/`。pack 禁令合规（list+extract，无 mmap）。

### 6.1 引用链（已确证）

```
units（兵种身份/成本，37 列）
  → unit_stats_land（战斗属性 113 列 + man_entity/mount_entity 引用）
    → battle_entities（实体物理/移动参数，21 列）
```

**注意**：S2 无 land_units_tables（Rome2 式拆分），实体引用直接在 `unit_stats_land` 的 `man_entity`/`mount_entity` 列。**一个 entity 被大量兵种共用**：shogun_inf_samurai=133 兵种 / boshin_infantry_medium=74 / shogun_inf_missile_ashigaru=39 / shogun_inf_hero=34……（341 兵种仅引用 12 个步兵 entity）。改 entity 一行 = 改全部引用兵种；单兵种需新造 key + 改引用。

### 6.2 battle_entities 21 列（全可改，实体级物理/移动，不含攻击/防御/士气）

移动：walk/run/crawl_speed、acceleration、deceleration、turn_speed；冲锋：charge_speed + charge_distance_commence_run/adopt_charge_pose/pick_target；碰撞：radius、shape(circle/ellipse)、radii_ratio、mass、height；射击：fire_arc_close/loose；生存：hit_points（步兵=1 英雄=2）；标识：key/class_validation/type。**表内无 gate/门实体**（门碰撞在引擎 collision3d 层，type ID 3）。

### 6.3 kv 表（S2 全带 `_` 前缀，三张主表）

| 表 | 冲锋相关 | 其他 |
|---|---|---|
| `_kv_rules`（147 键） | charge_cool_down_time=10、melee_charge_factor_power_divisor=1 | **pike_wall_move_speed_modifier=0.75（T1 枪衾减速）**、fire_volley_* 系列（T2 齐射阈值/瞄准延迟）、firing_drill_rank_fire_reload_modifier=0 |
| `_kv_morale`（93 键） | charge_bonus=5、charge_timeout=80 | defensive_fort=6、ums_*_threshold_* 士气档、ume_concerned_* 系列 |
| `_kv_fatigue`（37 键） | charging=15 | running=6、threshold_* 疲劳档 |

**三段冲锋距离不在任何 kv 表**（battle_entities 3 列是唯一数据源）；交战触发距离也不在 kv 表（enemy_effect_range=80 是士气半径）。

### 6.4 ★xhold 受击反应机制（已确证，引擎调试串 DLL 0x1645CEF）

近战命中结算后第二套判定（与伤害独立）：

```
Using xhold index = hn < melee_hn_to_xholds_N_max(...) = %d   ← hn 命中数值分档 0-4
Result = roll < kc            → HIT（击杀）
Result = roll < kc+xholds[档][0] → KNOCKDOWN（击倒）
Result = roll < kc+xholds[档][1] → KNOCKBACK（击退）
Result = roll < kc+xholds[档][2] → STEPBACK（退步）
Result = roll > kc+xholds[档][2] → MISS（无反应）
```

- 档位映射：hn ≤ -6→档0；-6~0→档1；0~6→档2；6~12→档3；>12→档4（阈值 -6/0/6/12）
- 档位阈值（累积）：knockdown 30/50/60/80/100、knockback 70/90/110/150/200、stepback 70/90/130/185/270；档0/1 的 knockback=stepback（无独立退步区间）
- 远程：missile_xholds_knockdown 35→65 仅击倒（无击退/退步）
- **错归因案例**：玩家称「knockdown_0=30 经测试=枪足轻防守时敌方进 30 码冲锋」→ 证伪：30 是 0-100 骰的概率阈值非距离；"30 码冲锋"实为实体本能交战触发（0x202080/0x3bfa50）+ charge_distance_commence_run=30 的巧合。可证伪实验：改 knockdown_0 只影响击倒频率不影响冲锋距离。
