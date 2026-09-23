# PRIORS.md — 死路清单（禁止重复提议）

> 摘要：本文件登记**已被证据否决的方向**。军团任何成员提出优化方案前必须先查本表；
> 命中者直接驳回，除非带来能推翻原证据的新证据。每条注明否决证据。新增条目只能来自
> 已归档的 rejected 实验或一手文献/实测，禁止凭直觉登记。

## A. 路线级死路（调研已证伪，证据：`research/2026-09-21-speed-competition-landscape.md`）

| # | 方向 | 否决证据 |
|---|---|---|
| A1 | numpy 向量化订单簿 | JAX-LOB Table 5 实测：cancel/穿价慢 3–5x（调研 §2.2/§5.3） |
| A2 | Numba 作用于 Python 对象事件循环 | 只能碰已数组化的数值核，碰不了对象树（调研 §4.4） |
| A3 | 单世界撮合上 GPU | 单簿 GPU 慢 2–3 个数量级（JAX-LOB 实测，调研 §4.7.1） |
| A4 | GPU 用于主排名（batch 单元世界数不足） | 回本线 ≥1000 世界；本赛 batch 单元 N=3–8（调研 §4.7、§2.3）。GPU 专项奖另行决策，见 PROTOCOL §7 |
| A5 | 时间切片/事件聚合批处理 | 破坏 Tier-A 事件顺序（README Step 6） |
| A6 | Time Warp / GPU-PDES 乐观同步 | lookahead 不足 + 整簿状态回滚成本（调研 §2.3） |
| A7 | 集合竞价等定步长近似 | 语义与连续双边拍卖不互验（KineticSim 自述，调研 §2.2） |
| A8 | GNN/神经代理替代仿真 | 近似，过不了 Tier-A（调研 §2.3 DeepABM） |

## B. 工程级死路（确定性 / 正确性红线，违反即挂门）

| # | 做法 | 后果与证据 |
|---|---|---|
| B1 | `worker_seed = root + worker_id` 式 RNG 派生 | 流重叠风险，NumPy 官方明文警告；必须用 `(root_seed, scenario_id, agent_id, counter)` 派生（调研 §4.5） |
| B2 | `-ffast-math` / FMA contraction / rcp·rsqrt 近似指令 | 破坏逐字节确定性；必须 `-ffp-contract=off`（Bruce Dawson，调研 §4.5） |
| B3 | 每事件跨 Python↔编译核边界调用 | 边界税曾占 88% 样本；核心必须整段下沉（Ravid 案例，调研 §4.4） |
| B4 | vmap 大 tile / 静态展开大 kernel | 编译爆炸；EnderMio 全消融仅"小 tile+有界 chunk"存活（调研 §2.2） |
| B5 | 忽略冷启动 | 计时窗含容器启动；EnderMio 6.08x→含冷启动 1.25x；1BRC 前 8/10 靠 native image（调研 §5.9） |
| B6 | 为 GPU 而 GPU / 混合分工频繁往返 | CPU-GPU 往返吃光收益（调研 §2.3、§4.7.4） |
| B7 | 按 scenario ID 特判分发 | 违反 fast_sim 既定设计（参数化分发），sealed 场景直接翻车（`baselines/fast_sim/README.md`） |

## C. 反作弊红线（被追溯清零区，调研 §6.8，kernelguard 15 类模式对照）

| # | 做法 | 后果 |
|---|---|---|
| C1 | 计时器 monkey-patch / 伪造 stdout / 篡改评测函数 | 追溯清零（GPU MODE 官方 scrub 先例） |
| C2 | 按指针/调用次数指纹缓存结果、计时段切路径 | 同上（NVFP4 赛登顶提交几分钟内被 scrub） |
| C3 | `events_per_sec` 虚报（与 n_events÷wall_clock 偏差 >±5%） | g1 门判失格（README §events.json） |
| C4 | `n_events` 与 trace 行数不符 | g3 门判失格 |
| C5 | 为通过 repeat 校验而把遥测字段写成常量 | 官方明令禁止：必须报告真实计时（README §planned Final timing） |
| C6 | CUDA graph 重放滥用、未同步多流分发伪装吞吐 | kernelguard 检测模式 |

> 合法区：按评测**数据分布**设计（QuantCup 线性扫描先例）、公开单元特化 + 隐藏集稳定备胎
> 双轨。利用设定合法，利用测量非法。

## D. 本队实验死路（本地 rejected 实验登记处）

| # | 日期 | 方向 | 证据指针 |
|---|---|---|---|
| D1 | 2026-09-15 | t3-EXAMPLE 全尺寸运行在 B0 基线资格实验中 UnitExecutionError | `out/arm64-history/problems.jsonl` 首条；EXAMPLE 全尺寸运行至今 pending（`submission/validation.json` exemplar 字段）。**2026-09-23 补记：官方 Development 反馈坐实 EXAMPLE 在名册且计分（crash 按 0 计），修复立项 = EXP-0005 候选** |
| D2 | 2026-09-17~19 | opt-io 初版与 fix1 两轮均未达晋级线，迭代至 fix2 才进入 paired 确认 | `out/b2-qualify/b2-run-opt-io{,-fix,-fix2}/`（exitcode 与 evidence 留存）；教训：IO 优化的验收必须看 paired 中位而非单次 |
| D3 | 2026-09-22 | 本地资格 roster 使用 1h 版 EXAMPLE：单 run 24.5 min 写 9.73 GB trace 未完工即 ENOSPC（10 GiB scratch），全窗将 ~66 min×6 且有 OOM 前科（D1） | `out/exp-0002/baseline-run-failed-disk/`；本地惯例 = 5s 覆盖（PROTOCOL §6.1），1h 证据只作专项参考。**2026-09-23 补记：官方卡 `disk="10G"` + card 无 timeout 字段（1800s fallback）——D3 的盘/钟约束与官方 A1 crash 主嫌①②同构，1h 版不修复必 crash，已坐实在名册计分** |

（登记格式：编号 / 日期 / 一句话方向 / 证据路径。由参谋在 EXP 归档 rejected 时追加。）

## E. 待核实（未证伪也未证实，动手前先补证据）

- parquet 压缩档位（zstd 低级 / lz4 / none）在本容器环境的 CPU/体积取舍——无公开基准，需自测（调研 §8.5）。
- 容器内 THP/hugepage 影响——需自测（调研 §8.5）。
- amd64（舰队真实架构）与本地 arm64 的相对性能差——**通路已建成并经官方队列终验**（EXP-0003 建通路；EXP-0004 accepted：71/72 scored，官方 Dev vs 本地 Kendall-τ=0.736，比值 geomean 10.28× 系 Dev 引擎内计时与本地 host-wall 的**开销口径差**，非硬件因子，详见 `out/exp-0004/calibration.md`）。**仍缺**：官方 Final（Runner 实测、pinned idle 实例）口径与本地 host-wall 的绝对差——Dev 给不了此数，选项仍是云 Spot 单窗复测（<$1，EXP-0003 门 4 簿记）待统帅批凭证，或等 Final 官方读数。
