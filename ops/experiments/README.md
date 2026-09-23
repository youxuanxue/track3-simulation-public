# experiments/ — 编号实验台账

> 摘要：一实验一档，编号 `EXP-NNNN` 递增不重用。没有编号就没有改动。建档用
> `../bin/new-experiment "假设一句话"`，填写模板各栏。本目录是军团对抗
> "重复踩死路"的核心资产（参照 ml-mike 1900 次编号实验 + 2400 行死路清单打法）。

## 生命周期

```
proposed ──► running ──► accepted（晋级 stable，SCOREBOARD 更新）
                  │
                  └────► rejected（回滚，死路登记 PRIORS.md §D）
                  │
                  └────► parked（证据不足/优先级让位，可复活）
```

- 同一时刻 `running` 状态的实验**最多两个**：一个计算型（可并行改代码），一个测量型
  （独占 bench-lock）。测量型实验排队，持锁顺序 = 队列顺序。
- 任何状态迁移由参谋执行，并同步更新台账首页（本文件的"当前在役"表）。

## 必填纪律

1. **假设必须可证伪**：写成"改 X 后，单元集合 Y 的 paired 中位 events/sec 提升 ≥Z%"，
   禁止"提升性能"式空话（GPU MODE 教训：目标是合同式约束，不是愿望）。
2. **改动范围先圈定**：列出允许触碰的文件/目录；范围外改动一律新开 EXP。
3. **验收线先写好再动手**：四门验收（PROTOCOL §4）的本实验阈值，含"其他单元零回归"
   的具体容忍度。
4. **结果栏只贴复核后的数字**：工兵自报数字写在"原始记录"，参谋复核后的数字才进
   "结论"。两级分离，不许合并。
5. **归档不删除**：rejected 也是资产。死路信息浓缩进 PRIORS.md，全文留档。

## 命名与对应物

| 物 | 规则 |
|---|---|
| 实验档 | `EXP-NNNN-short-slug.md`（`bin/new-experiment` 生成） |
| 分支 | `exp/EXP-NNNN-short-slug` |
| worktree | `../.worktrees/EXP-NNNN-short-slug/`（不进主仓，见 PROTOCOL §5） |
| Docker tag | `track3-exp-NNNN:<arch>`，严禁覆盖 `latest` 与 stable 引用 |
| 证据目录 | `out/exp-NNNN-*/`（gitignored；台账只记路径 + sha256） |

## 当前在役

| EXP | 状态 | 假设摘要 | 承办 |
|---|---|---|---|
| —（置空，2026-09-23） | — | EXP-0005 候选待签发：EXAMPLE 1h 可接纳性攻关（本地复现绑定约束→定向修复，预估 +1.41% 且消除 Final crash 风险） | 参谋 |

## 已归档

| EXP | 结果 | 一句话结论 | 证据指针 |
|---|---|---|---|
| [EXP-0001](EXP-0001-decide-noise-calibration.md) | accepted | decide 不可事后补登（证据随代码变更过期，B2 证据 freeze 后 90 分钟失效）→ PROTOCOL §5.5 代码冻结窗；门2 阈值实测校准：聚合 ≥2%（噪声带 ±0.77%）、单元 ≤0.85 确认回归（p90 带 ±14.7%）；发现 gbatch-hetero-mix 单元级结论不可信（sd 54.5%，口径符号相反） | `out/exp-0001-assess-confirm.err`、`out/exp-0001-noise-report.md` |
| [EXP-0002](EXP-0002-head-baseline-chain.md) | accepted | hash 链创世：stable=`…@sha256:607bec51…`（HEAD 7d41ed5），432/432 全绿，G1/G2 pass，聚合 244,673 eps（跨窗参考 vs B2-B +37.1%，72/72 全正，#15 为 batch 主杠杆）；考古坐实 B2 本地 5s exemplar 惯例并条令化；venv 数值库版本属验证者身份（pandas 钉 3.0.5）；hetero-mix"天生高噪"证伪 | `out/exp-0002/`、`out/arm64-history/000000.json`（事件 `4fcd0923…`）、`out/exp-0002/noise-single-arm.md` |
| [EXP-0003](EXP-0003-amd64-pathway.md) | accepted（门4 parked） | amd64 交付通路建成：根 Dockerfile 零改动交叉构建（基镜像 multi-arch），QEMU 语义冒烟两单元全绿，ghcr amd64+multi-arch tag 推送并匿名验证；流程固化 PROTOCOL §6.2；ghcr 凭证在 macOS 侧、冒烟必须 `--run-as-host-user` 两条教训入 §6.1/§6.2；真实硬件校准簿记移交统帅 | `out/exp-0003/`、ghcr `:exp-0003-amd64-7d41ed5`（`96dcbeea…`）、`:exp-0003-multi-7d41ed5`（`2f3de103…`） |
| [EXP-0004](EXP-0004-dev-queue-amd64-calibration.md) | accepted | 官方 Development 终验（A1）：primary 1,447,360.0444（71/72，EXAMPLE crash 计 0）；语义链路在真实 amd64+gVisor 全过；官方 Dev vs 本地 Kendall-τ=0.736 过线，比值 geomean 10.28× 系开销口径差（Dev 引擎内计时 vs 本地 host-wall），**非硬件因子**——本地 host-wall 仍是 Final 口径标尺；配额对账关闭（4/20，余 16，冻结解除）；EXAMPLE 名册坐实，crash 攻关拆 EXP-0005 候选 | `out/exp-0004/`（`dev-feedback-2026-09-22.json`、`calibration.md`、上传截图） |
