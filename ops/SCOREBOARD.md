# SCOREBOARD.md — 当前战况记分板

> 摘要：本板回答三个问题——**我们现在多快、杠杆在哪里、stable 是哪一版**。
> 表格区由 `bin/update-scoreboard` 从 paired 证据机械生成，**禁止手改**；
> 态势区由参谋在每次 decide / 上传后更新。口径：本地 `colima-t3-arm64`（4C/16GiB
> 容器）、developer profile、`rankable=false`、成对相对证据——**不是官方计时**。

## 当前态势（2026-09-23，EXP-0004 归档后）

- **官方 Development 读数（A1，2026-09-22 上传）**：primary **1,447,360.0444 eps**
  （71/72 scored；EXAMPLE container_crashed 按 0 计，修复价值 ≈ +20,385 eps/+1.41%）。
  逐单元分数 `out/exp-0004/dev-feedback-2026-09-22.json`，校准分析
  `out/exp-0004/calibration.md`。**口径教训**：官方 Dev 是 checked 自报吞吐
  （引擎内计时），比本地 host-wall 高 geomean 10.28×（固定开销摊薄效应，1.55×~45.15×）；
  两者排序 Kendall-τ=0.736。**Dev 数字用于验证语义链路与崩溃检测，本地 host-wall
  才是 Final（Runner 实测）口径的标尺**——优化决策继续以本地 paired 为准。
- **配额**：累计 4/20（历史 3 次仓外消耗已对账），**余 16 次**，上传冻结解除，
  恢复按 QUOTA.md 触发规则逐次签发。Development 关窗 2026-10-12 23:59 AoE。

- **当前 stable（hash 链正式身份）**：`ghcr.io/youxuanxue/track3-simulation-public@sha256:607bec51f29a941dd0ed701b3c154bd8b70715b962e02569e5be018f503f1f18`
  （HEAD `7d41ed5` = B2 代码 + #14 冷启动修剪 + #15 batch 计时修复 + #16 文档）。
  **链条创世**：`out/arm64-history/000000.json`，decide action=promote，事件 sha256
  `4fcd0923c1314fac721c03bcbaee7147d544893c2a892d13e124c4698fd60a92`，
  G1/G2 pass，432/432 单元×组全过（2026-09-22 窗，plan `7936da96…`）。
  旧参照 `track3-b2@sha256:ac2b7848…`（178,425 eps，2026-09-19）降级为历史锚点——
  其证据永久无 decide 资格（EXP-0001），仅作跨窗参考。
- **当前读数（本地口径）**：聚合算术平均中位 **244,673 eps**（432 runs 全绿）。
  跨窗参考：HEAD vs B2-B 72/72 单元全正，geomean 1.2488，算术 +37.1%——
  batch 单元 +71%~+110%（#15 主杠杆），全盘面 +5%~+10%（#14），
  mr-deep-book +52.2%（`out/exp-0002/noise-single-arm.md`）。
- **已注册候选镜像**：`ghcr.io/youxuanxue/track3-simulation-public@sha256:468ec20b…`
  （`submission/candidate.json`，team 23，phase=dev）。它与本地 stable 的对应关系
  未在仓内记录——属对账缺口，见 QUOTA.md。
- **基线对照**：Python ABIDES 公开 65 单元几何平均 13,793 eps（硬件未记录、仿真循环
  口径，`baselines/README.md` §1）。与本地口径**不可直接相除**，只作量级参照。
- **exemplar 警示（2026-09-23 升级）**：**EXAMPLE 已坐实在官方名册且计分**——
  A1 反馈中 `container_crashed` 按 0 计入 primary（损失 ≈1.41%）。崩溃主嫌排序：
  ①1h trace >10G 盘界 ②1800s launcher 超时（card 无 timeout 字段）③内存。
  **修复 = EXP-0005 候选**（先按官方容器设置本地复现确认绑定约束，再定向：
  parquet 序列化提速 / 盘界流式 / 引擎提速）。本地资格 roster 仍用 5s 覆盖版
  （PRIORS D3，PROTOCOL §6.1），5s 版 HEAD 读数 2.34M eps。
- **测量可信区**（EXP-0001 配对校准 + EXP-0002 单臂校准）：聚合比 95% 带 ±0.77%；
  单单元配对 median ±7.8% / p90 ±14.7%；单臂绝对 log sd median 1.38% / p90 5.52%。
  `t3-gbatch-hetero-mix`：EXP-0002 单臂 sd 仅 4.81%（五组 120.8K–136.1K）——
  B2 窗 54.5% 系该窗臂间交互产物，**"天生高噪"不成立，挂单关闭**；
  但 B2 窗该单元的 B 胜结论仍不可信，相关读数继续打折。
- **上轮死磕点**：opt-io 三迭代才过线（PRIORS D2）；冷启动/drop-cache 已有专项
  测量（`out/b2-qualify/b2-baseline-run-{coldstart,dropcache}/`）。
- **amd64 通路（EXP-0003/0004，校准完成）**：交叉构建/QEMU 语义/ghcr multi-arch
  全流程固化于 PROTOCOL §6.2。EXP-0004 经官方 Development 队列取得真实
  amd64+gVisor 反馈：71/72 scored，官方 vs 本地 Kendall-τ=0.736——语义链路合格、
  排序可信；比值 geomean 10.28× 系开销口径差（Dev 引擎内计时 vs 本地 host-wall），
  **非硬件校准因子**。Final 与本地 arm64 的绝对差仍未量化，云 Spot 复测簿记
  （EXP-0003 门 4 Option A，<$1）仍待统帅批凭证（PRIORS §E）。

## 杠杆读法（算术平均的含义）

贡献占比列 = 该单元对算术平均的权重。**提升收益按绝对增量论功**：头部
`t3-gb-*` / `t3-EXAMPLE` 的 10% 相对提升 ≈ 尾部单元翻几十倍。尾部
`t3-s001`（~1.4k eps）受固定开销主导，是冷启动类优化的天然探针，但对总分的
直接贡献微乎其微——修它是为了名册稳健性，不是为了涨分。

## 数据

<!-- scoreboard:meta:begin -->
- 数据源：`out/b2-qualify/b2-run-confirm` — b2-run-confirm (2026-09-19, colima-t3-arm64)
- 生成时刻：2026-09-21T12:36:25+00:00
- 单元数：72（双臂齐全）
- 算术平均：A = 157,683 → B = 178,425 eps（+13.2%）
- 几何平均 B/A：1.1644
<!-- scoreboard:meta:end -->

### 全单元表（按 B 中位降序 = 杠杆降序）

<!-- scoreboard:table:begin -->
| # | 单元 | A 中位 eps | B 中位 eps | B/A | B 贡献占比 |
|---|---|---|---|---|---|
| 1 | t3-EXAMPLE-vectorized-matching | 1,645,953 | 1,790,730 | 1.088 | 13.94% |
| 2 | t3-gb-mega-throughput | 714,780 | 741,204 | 1.037 | 5.77% |
| 3 | t3-gb-pop-horizon-scale | 578,248 | 648,730 | 1.122 | 5.05% |
| 4 | t3-gb-horizon-240s | 527,268 | 584,133 | 1.108 | 4.55% |
| 5 | t3-gb-highfreq-40hz-60s | 498,189 | 534,717 | 1.073 | 4.16% |
| 6 | t3-mr-cancel-replace-churn | 382,627 | 415,505 | 1.086 | 3.23% |
| 7 | t3-mr-deep-book-state-size | 378,100 | 402,005 | 1.063 | 3.13% |
| 8 | t3-gb-pop-128-agents | 361,819 | 400,851 | 1.108 | 3.12% |
| 9 | t3-mp05-cancel-churn-newest | 310,046 | 352,669 | 1.137 | 2.75% |
| 10 | t3-s012-partial-fill-cancel-race | 275,243 | 313,825 | 1.140 | 2.44% |
| 11 | t3-cancelmodify-lifecycle | 222,372 | 255,396 | 1.149 | 1.99% |
| 12 | t3-mr-checkpoint-cadence-fine | 222,500 | 253,862 | 1.141 | 1.98% |
| 13 | t3-mp03-mm-heavy-newest | 192,225 | 228,603 | 1.189 | 1.78% |
| 14 | t3-mp04-mm-heavy-oldest | 192,765 | 228,225 | 1.184 | 1.78% |
| 15 | t3-sf-05-deep-book-liquidity | 141,749 | 157,617 | 1.112 | 1.23% |
| 16 | t3-mr-long-horizon-replay | 140,712 | 156,984 | 1.116 | 1.22% |
| 17 | t3-mp06-tight-spread-selfcross | 140,760 | 154,983 | 1.101 | 1.21% |
| 18 | t3-mp01-stp-newest-baseline | 124,476 | 152,191 | 1.223 | 1.18% |
| 19 | t3-st02-informed-entry-burst | 122,938 | 151,884 | 1.235 | 1.18% |
| 20 | t3-st01-liquidity-churn-baseline | 125,605 | 151,728 | 1.208 | 1.18% |
| 21 | t3-sf-06-high-fundamental-vol | 123,728 | 150,615 | 1.217 | 1.17% |
| 22 | t3-sf-02-heavy-jump-tails | 125,338 | 150,097 | 1.198 | 1.17% |
| 23 | t3-partialfill-atomicity | 124,649 | 149,823 | 1.202 | 1.17% |
| 24 | t3-multilevel-crossing | 115,462 | 149,482 | 1.295 | 1.16% |
| 25 | t3-stp-cancel-newest | 115,456 | 149,355 | 1.294 | 1.16% |
| 26 | t3-fastlob-tight-book | 124,287 | 149,128 | 1.200 | 1.16% |
| 27 | t3-as06-throughput-fast | 119,045 | 139,822 | 1.175 | 1.09% |
| 28 | t3-mp07-heavy-flow-oldest | 116,186 | 138,953 | 1.196 | 1.08% |
| 29 | t3-st05-latency-spike-pareto | 125,876 | 138,746 | 1.102 | 1.08% |
| 30 | t3-coarse-tick-ties | 116,214 | 138,646 | 1.193 | 1.08% |
| 31 | t3-sf-01-baseline-regime | 115,635 | 138,475 | 1.198 | 1.08% |
| 32 | t3-gbatch-homog-8 | 113,017 | 138,347 | 1.224 | 1.08% |
| 33 | t3-st03-volatility-burst | 125,544 | 138,285 | 1.101 | 1.08% |
| 34 | t3-mp02-stp-oldest-baseline | 123,976 | 137,103 | 1.106 | 1.07% |
| 35 | t3-fastlob-core | 124,728 | 136,950 | 1.098 | 1.07% |
| 36 | t3-sf-07-coarse-agent-batch | 123,396 | 136,892 | 1.109 | 1.07% |
| 37 | t3-momentum-mix-priority | 124,540 | 136,779 | 1.098 | 1.06% |
| 38 | t3-sf-04-thin-book-depth | 123,991 | 136,683 | 1.102 | 1.06% |
| 39 | t3-sf-03-vol-clustering-momentum | 124,302 | 135,927 | 1.094 | 1.06% |
| 40 | t3-gb-base-30agent-30s | 117,911 | 129,360 | 1.097 | 1.01% |
| 41 | t3-gbatch-hetero-mix | 98,011 | 118,756 | 1.212 | 0.92% |
| 42 | t3-as03-mm-liquidity-heavy | 86,856 | 107,691 | 1.240 | 0.84% |
| 43 | t3-gbatch-many-6 | 85,346 | 102,882 | 1.205 | 0.80% |
| 44 | t3-as02-scale-up-noise | 67,716 | 88,605 | 1.308 | 0.69% |
| 45 | t3-ca-fat-tail-jumps | 69,315 | 85,088 | 1.228 | 0.66% |
| 46 | t3-ca-fast-mean-reversion | 70,688 | 84,363 | 1.193 | 0.66% |
| 47 | t3-ca-baseline-calm | 70,695 | 83,777 | 1.185 | 0.65% |
| 48 | t3-ca-high-volatility | 70,116 | 77,116 | 1.100 | 0.60% |
| 49 | t3-ra03-large-shock | 63,581 | 76,776 | 1.208 | 0.60% |
| 50 | t3-ra01-fundamental-shock-mid | 64,132 | 76,416 | 1.192 | 0.59% |
| 51 | t3-st04-thin-book-mm-strain | 62,629 | 76,341 | 1.219 | 0.59% |
| 52 | t3-ra05-shock-momentum-heavy | 62,226 | 75,314 | 1.210 | 0.59% |
| 53 | t3-ra06-shock-value-heavy | 56,433 | 74,857 | 1.326 | 0.58% |
| 54 | t3-ra04-shock-late | 61,552 | 74,383 | 1.208 | 0.58% |
| 55 | t3-ra02-shock-early | 67,443 | 74,276 | 1.101 | 0.58% |
| 56 | t3-gbatch-varsize | 60,348 | 72,736 | 1.205 | 0.57% |
| 57 | t3-gbatch-homog-4 | 56,757 | 67,811 | 1.195 | 0.53% |
| 58 | t3-ca-thin-book-depth | 45,013 | 54,589 | 1.213 | 0.42% |
| 59 | t3-gbatch-dense-3 | 42,980 | 52,892 | 1.231 | 0.41% |
| 60 | t3-as01-base-mix | 46,860 | 51,627 | 1.102 | 0.40% |
| 61 | t3-as04-value-heavy | 43,244 | 51,260 | 1.185 | 0.40% |
| 62 | t3-as05-oracle-variant | 40,766 | 49,079 | 1.204 | 0.38% |
| 63 | t3-eq-pareto-heavytail | 27,926 | 34,547 | 1.237 | 0.27% |
| 64 | t3-eq001-pareto-latency-tail | 31,075 | 34,350 | 1.105 | 0.27% |
| 65 | t3-eq-lognormal-highsigma | 28,025 | 31,565 | 1.126 | 0.25% |
| 66 | t3-eq-uniform-wide | 25,912 | 31,170 | 1.203 | 0.24% |
| 67 | t3-eq-lognormal-highmag | 25,881 | 31,135 | 1.203 | 0.24% |
| 68 | t3-eq-uniform-tight | 27,829 | 30,888 | 1.110 | 0.24% |
| 69 | t3-eq-lognormal-lowsigma | 27,362 | 30,780 | 1.125 | 0.24% |
| 70 | t3-s019-latency-jitter-kendall | 23,485 | 26,130 | 1.113 | 0.20% |
| 71 | t3-eq-deterministic-baseline | 20,184 | 22,662 | 1.123 | 0.18% |
| 72 | t3-s001-price-time-priority | 1,164 | 1,425 | 1.224 | 0.01% |
<!-- scoreboard:table:end -->

### 前缀组汇总

<!-- scoreboard:rollup:begin -->
| 前缀组 | 单元数 | A 合计 eps | B 合计 eps | B/A | B 贡献占比 |
|---|---|---|---|---|---|
| gb | 6 | 2,798,215 | 3,038,995 | 1.086 | 23.66% |
| EXAMPLE | 1 | 1,645,953 | 1,790,730 | 1.088 | 13.94% |
| mp | 7 | 1,200,434 | 1,392,726 | 1.160 | 10.84% |
| mr | 4 | 1,123,939 | 1,228,356 | 1.093 | 9.56% |
| sf | 7 | 878,139 | 1,006,307 | 1.146 | 7.83% |
| st | 5 | 562,593 | 656,984 | 1.168 | 5.11% |
| gbatch | 6 | 456,459 | 553,424 | 1.212 | 4.31% |
| as | 6 | 404,486 | 488,083 | 1.207 | 3.80% |
| ra | 6 | 375,367 | 452,023 | 1.204 | 3.52% |
| ca | 5 | 325,826 | 384,934 | 1.181 | 3.00% |
| s | 3 | 299,892 | 341,380 | 1.138 | 2.66% |
| fastlob | 2 | 249,015 | 286,078 | 1.149 | 2.23% |
| cancelmodify | 1 | 222,372 | 255,396 | 1.149 | 1.99% |
| eq | 8 | 214,194 | 247,097 | 1.154 | 1.92% |
| partialfill | 1 | 124,649 | 149,823 | 1.202 | 1.17% |
| multilevel | 1 | 115,462 | 149,482 | 1.295 | 1.16% |
| stp | 1 | 115,456 | 149,355 | 1.294 | 1.16% |
| coarse | 1 | 116,214 | 138,646 | 1.193 | 1.08% |
| momentum | 1 | 124,540 | 136,779 | 1.098 | 1.06% |
<!-- scoreboard:rollup:end -->

## 更新规则

1. 每次 `decide` 晋级 / 回滚后：`ops/bin/update-scoreboard --run-dir <确认实验目录>
   --label "<轮次说明>"`，并同步"当前态势"区的 stable digest 与日期。
2. 每次上传后：更新"已注册候选镜像"与 QUOTA.md 台账（同一笔动作，不分两次提交）。
3. 表格区只由工具重写；发现表格与证据不一致时，以证据为准重跑工具，不手改数字。
