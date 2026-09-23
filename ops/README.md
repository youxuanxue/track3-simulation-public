# ops/ — chilli 战队 Agent 军团指挥部

> 摘要：本目录是 Track 3 夺冠战役的**共享大脑与作战条令**。军团成员（人类统帅 +
> 主会话参谋 + 按需派遣的 subagent 工兵）零上下文启动，一切共识以本目录文件为准。
> 对话会遗忘，文件不会。开工前必读：`PROTOCOL.md` → `PRIORS.md` → `SCOREBOARD.md`。

## 使命

把 Agenthon 2026 Track 3 的**全场算术平均 events/sec** 推到物理上界之内、对手之上。
约束不可谈判：任一单元挂掉记零分，因此 **72/72 门全绿 + 逐字节确定性** 是一切优化的前提。

## 指挥结构

```
统帅（人）          决策点所有者：上传触发、路线转向、终赛选型、额度使用
  │
参谋（主会话 agent）  统筹：读记分板 → 立项 EXP → 派工兵 → 亲自复核验收 → 更新台账
  │
  ├─ 工兵（coder subagent）    单实验实施：改代码、跑检查、产出证据文件
  ├─ 侦察（explore subagent）  只读调查：代码结构、瓶颈定位、文献核实
  └─ 门卫（验收流程）          机器验收四门，见 PROTOCOL.md §4；不凭报告，只凭重跑
```

## 闭环

```
SCOREBOARD.md 找最大杠杆
      │
      ▼
experiments/EXP-NNNN 立项（假设 + 验收线 + 改动范围）
      │
      ▼
worktree 隔离实施（bin/new-experiment 建档）
      │
      ▼
bench-lock 独占测量主机，paired A/B（scripts/benchmark_candidates.py 协议）
      │
      ▼
验收四门（PROTOCOL.md §4）：回归全绿 / 提升达线 / holdouts 过 / 双跑字节一致
      │
      ├── 过 → decide 晋级 stable，更新 SCOREBOARD，EXP 归档 accepted
      └── 否 → 回滚，EXP 归档 rejected，死路登记 PRIORS.md
      │
      ▼
统帅按 QUOTA.md 触发规则决定是否消耗上传额度
```

## 文件地图

| 文件 | 作用 | 更新者 |
|---|---|---|
| `SCOREBOARD.md` | 当前战况：stable 镜像、72 单元最新速率、杠杆排序 | 参谋（每次 decide 后） |
| `PRIORS.md` | 死路清单：已证伪方向，禁止重复提议 | 参谋（每次 rejected 后） |
| `PROTOCOL.md` | 作战条令：编制、简报模板、隔离互斥、验收四门、红线、决策点 | 统帅（变更即全军团生效） |
| `QUOTA.md` | 提交配额账本与上传触发规则 | 统帅 + 参谋共同记账 |
| `experiments/` | 编号实验台账（EXP-NNNN，一实验一档） | 承办工兵起草，参谋定稿 |
| `bin/bench-lock` | 测量主机互斥锁（计时实验必须持锁） | — |
| `bin/new-experiment` | 实验建档（取号 + 从模板生成 EXP 档） | — |
| `bin/update-scoreboard` | 从 paired 证据重算记分板表格 | — |

## 与仓库规则的关系

- `../AGENTS.md` 的防火墙与评分库规则**优先于**本目录任何文件。禁止把 sealed
  traces / answer keys 带入本仓库；评分函数只从 `qfbench2_common` 导入，禁止重实现。
- `../out/` 是本地证据区（gitignored，不提交）；本目录只提交**结论与指针**
  （证据路径 + sha256），不提交证据本体。
- 事件类型字符串、`simulate`/`simulate-batch` 动词、容差常数以 `AGENTS.md` 与
  `templates/trace_column_registry.json` 为准。

## 铁律三条

1. **没有 EXP 编号，不动代码。** 一切改动归属某个编号实验，可追溯、可回滚。
2. **没有持锁，不计时。** 任何 events/sec 数字必须产自 bench-lock 独占的主机，否则作废。
3. **没有重跑，不验收。** subagent 的自报结论一律由参谋亲自重跑关键命令复核后才入账。
