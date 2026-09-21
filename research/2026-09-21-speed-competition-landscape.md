# 速度型仿真竞赛夺冠技术点与策略点调研（Track 3 参照系）

- **调研日期**：2026-09-21
- **调研范围**：开源社区中与 Agenthon 2026 Track 3（语义保持市场仿真加速赛）同构的参照系；同类速度型 leaderboard 赛事 top 10 玩家的公开方案与打法
- **调研方法**：三个并行方向（① ABIDES 生态 + GPU/向量化市场仿真 SOTA；② 速度型 leaderboard 赛事 top 玩家打法；③ 高性能 DES 与撮合引擎工程），全部基于一手来源（GitHub 仓库/API、arXiv 全文、官方文档与 leaderboard 页、选手复盘博客）。无法在线核实的条目均显式标注。
- **用途**：迎战策略制定的外部参照底稿。本文只做调研综合，不含我们的具体实施方案。

---

## 0. 赛事背景摘要（调研的锚点）

Track 3 任务：把 JPMorgan 开源的 Python 离散事件市场仿真器 ABIDES 加速 N 倍，同时输出必须与参考逐事件一致。

- **排名指标**：raw `events/sec`，全部评估单元算术平均；挂掉任何一个 unit 记零分。
- **正确性门**：Tier-A（成交序列逐条精确、Kendall-τ ≥ 0.999、时间戳 ±1µs、事件双向覆盖零缺失零多余、消息账本因果 g3.5）；Tier-B（收益分布 KS ≤ 0.08、价差 ±10bps）；程式化事实门（KS / ACF / Hill / 深度 JS 四项硬顶）。
- **硬约束**：Docker 镜像提交（`simulate` / `simulate-batch` 动词）；4 CPU 核、16GB 内存、可选 1× NVIDIA B200（sm_100，CUDA 12.x 可用）；容器 `network=none`（依赖全部打包）；逐字节确定性（同 seed 重复运行输出字节一致）；batch 单元 N 个独立子场景须各自复现单独运行的输出（隔离门）。
- **额度**：开发期 5 次/天、共 20 次上传；终赛 1 次提交。sealed 场景惩罚公开集过拟合。
- **本地基线**：Python ABIDES ≈ **13,793 events/sec**（65 个公开单元几何均值，范围 3,471–18,046，约 72µs/event，见 `baselines/README.md`）。

---

## 1. 三个全景发现

**发现 1：ABIDES 生态没有任何现成的"加速作业"可抄。**
jpmorganchase/abides-jpmc-public 已归档（174★，92 个 fork 全是无改动镜像）；社区主 fork abides-sim/abides（571★）停更于 2023-07。全网仅两个沾边项目：GabrieleDiCorato/abides-ng（1★，工程质量改造，无性能数字）与 mariotrerotola/abides-rs（0★，Rust 重写，尚无 benchmark）。也没有"加速 ABIDES"的专门论文（ABIDES 系论文全部是功能扩展：ABIDES-Gym、ABIDES-MARL、ABIDES-Economist）。**参照系必须跨界找——而跨界参照系非常成熟。**

**发现 2：数量级参照系已存在，差距巨大。**
严肃 CPU 撮合引擎实测 3–8M msg/s（100–300ns/op），对比 Python ABIDES 的 ~72µs/event：**仅靠"语言 + 数据结构 + 零分配"重写，理论空间约 100–500x**，且确定性、精确成交、消息账本因果在这条路线上天然免费。

**发现 3：GPU 路线的边界在文献与实测中均有定论，且与 batch 单元设计惊人吻合。**
从 Fujimoto 1990（PDES 综述）到 Xiao 等 2018（GPU ABM 综述）再到 JAX-LOB 实测，结论一致：细粒度 DES 内部并行/GPU 化是死路（事件耦合是每事件级的、lookahead 只有 µs 级、单事件计算量太小）；**GPU 唯一成立的打开方式是"世界级批量"——单世界严格串行、数千世界并行**。JAX-LOB 实测：单簿 GPU 比 CPU 慢 2–3 个数量级；10 世界时 GPU 仍比 CPU 慢 4 倍；1000 世界才回本。Track 3 的 batch 单元恰好就是这个形态（N 独立子场景 + 世界间零通信 = 隔离门免费通过）。

---

## 2. 参考系一：ABIDES 生态与 GPU/向量化市场仿真 SOTA

### 2.1 ABIDES 本体

| 对象 | 现状 | 借鉴度 |
|---|---|---|
| [jpmorganchase/abides-jpmc-public](https://github.com/jpmorganchase/abides-jpmc-public)（174★） | 已归档，最后提交 2024-07；竞赛 baseline 固定 commit `f9cbe51`；92 fork 均为镜像 | 基线本体 |
| [abides-sim/abides](https://github.com/abides-sim/abides)（571★） | 原版 Georgia Tech / David Byrd，停更 2023-07，无性能分支 | 低 |
| [GabrieleDiCorato/abides-ng](https://github.com/GabrieleDiCorato/abides-ng)（1★） | 自称 "hardened version, reworked API, configuration, and performance"，无性能数字 | 低 |
| [mariotrerotola/abides-rs](https://github.com/mariotrerotola/abides-rs)（0★） | Rust-only 重写，确定性 DES 内核，主打 deterministic scheduling，无 benchmark | 中（证明 Rust 重写语义可行） |

### 2.2 JAX-LOB 及同类

**JAX-LOB**（Frey, Li, Nagy 等，Oxford，[arXiv 2308.13289](https://arxiv.org/abs/2308.13289)，2023-08；实现 [KangOxford/jax-lob](https://github.com/KangOxford/jax-lob) 60★ 停更，活跃版 [KangOxford/AlphaTrade](https://github.com/KangOxford/AlphaTrade) 145★）——首个 GPU LOB 仿真器，设计目标是"数千本书并行"而非单本书更快。

- 核心技术点：
  1. 订单簿为**固定容量 N 的稠密数组**（6 特征/订单，空位 -1），避免指针结构；
  2. price-time priority 用 while-loop 逐单匹配 best order；
  3. **vmap 跨书并行**，8 种消息类型各写独立函数、单次条件分发（分支展平）；
  4. 时间戳拆 int32 秒+纳秒两个字段（整数域）；
  5. **不做连续重排序**——vmap 把控制流转成 select，所有分支每条消息都执行，排序成本会被摊到每条消息上。
- 性能数字（2080 Ti / A40 / Apple M1，论文表格直接提取）：
  - 单簿串行：Add 0.115ms / Cancel 0.081ms / Match 0.184ms **每次操作**——比 CPU 慢 2–3 个数量级；
  - vmap 1000 簿时摊到每簿每条消息 1.4–11.7µs；对比 CPU（红黑树版/numpy 版）~5x；
  - RL 环境步耗时：10 世界 13.8ms/step（比 CPU 单世界 3.5ms 还慢）；1000 世界 0.66ms；10000 世界 0.46ms；
  - 端到端 RL 训练（A40 + 1000 并行环境，PureJaxRL）：550 vs 74 steps/s = **7x**，归因于零 CPU-GPU 往返；
  - 反面：容量 N=1000 时 worst-case 单消息跨 1000 簿 **>2 秒**（数组重排 + 全分支执行）；
  - **numpy 向量化 LOB 反例（Table 5）**：numpy 数组版 limit 4.91µs / cancel 16.12µs / 穿价 37.94µs 每条消息；RB-tree+链表 CPU 版 5.3 / 3.6 / 7.0µs——**cancel/穿价慢 3–5 倍**。
- 借鉴度：**高**（形态最接近的工作；数据结构选择、vmap 陷阱清单、GPU 边界数字直接可用）。

**[EnderMio/jaxlob-gpu-optimization](https://github.com/EnderMio/jaxlob-gpu-optimization)**（0★，2026-08）——对 JAX-LOB 消息块 transition 的系统性 GPU 性能研究（tiled Pallas，含正确性 oracle 与消融）。数字（RTX 3070 Ti Laptop）：核心订单簿扫描 **6.08x**，整体 rollout 2.52x，**含冷启动只有 1.25x**（编译开销主导）。设计空间记录：SoA 化、分支重组、行增量状态、静态展开 Pallas 等"全部正确但无收益甚至回退"，唯一活下来的是"小 tile（4 消息）+ 有界匹配 chunk + 小块编译体"。借鉴度：**高**（等于替人趟完 JAX 路线雷区；冷启动教训直接适用于计时单元）。

**KineticSim**（Jayakody & Jayakody，[arXiv 2606.21784v2](https://arxiv.org/abs/2606.21784)，2026-06；仓库基本为空）——轻量 CUDA 执行引擎，海量订单簿集合并行；**注意语义是定步长集合竞价（call auction），不是连续双边拍卖**，作者明确说因此无法与 ABIDES/JAX-LOB 互验正确性。

- 核心技术点：持久内核 + 块内共享内存驻留（全局内存流量与步数解耦）；共享内存原子聚合；合作式并行扫描清算；**SplitMix64 无状态计数器 RNG**（hash(market, agent, step, channel, seed)）；**数量全部精确整数**使 fp32 atomicAdd 也 bitwise 确定。
- 数字（RTX 5090, sm_120, CUDA 13.2）：峰值 54.7B agent-events/s；加速比 3406x CPU(NumPy)、8.4x 朴素 CUDA；53 种配置下两个 CUDA 引擎输出 bitwise 一致。
- 借鉴度：**中**（语义过不了 Tier-A；可抄系统工程：持久内核、共享内存驻留、无状态 RNG、整数定点换 bitwise）。

其他 CUDA/GPU LOB 小项目（astin7/apex-lob、adubey-ai/gpu-lob-simulator、true-brace05/gpu-orderbook-simulator 等）：0–2★、作者自报数字、无第三方验证。借鉴度：低。

### 2.3 GPU ABM / GPU DES 通用文献

- **Xiao 等《A Survey on Agent-based Simulation using Hardware Accelerators》**（[arXiv 1807.01014](https://arxiv.org/abs/1807.01014)，2018）：time-stepped ABS 天然适合 GPU；**DES 难**（事件时间不规整、同步/通信开销、负载不均）；CPU-GPU 混合方案常输给全 GPU 方案，且全 GPU 方案"因块间无同步而引入结果误差"。借鉴度：**高**（"为什么不该硬上 GPU 做严格 DES"的一手论证）。
- **FLAME GPU 2**（[FLAMEGPU/FLAMEGPU2](https://github.com/FLAMEGPU/FLAMEGPU2)，158★）：CUDA ABM 框架，定步进同步架构，与严格 DES 因果序不兼容。借鉴度：低-中。
- **Fujimoto 1990 PDES 综述**（CACM, DOI 10.1145/84537.84545）：PDES"包含大量并行性，却在实践中出人意料地难以并行化"；DES 的不规则数据依赖使向量化技术几乎无收益。借鉴度：**高（作为否决依据）**。
- **Jefferson 1985 Time Warp**（DOI 10.1145/3916.3988）：乐观同步原点；对 LOB 是死路（lookahead 不足 + 整本订单簿的状态保存/回滚成本）。
- **ROSS/CODES**（DOI 10.1109/pads.2011.5936761）：PDES 的 50 亿事件/秒来自 lookahead 充足、事件粒度均匀、单 LP 状态极小的网络仿真——与 LOB 不同构，不能外推。借鉴度：低（但须正确引用避免误用）。
- **GPU PDES**（PADS 2012, DOI 10.1109/pads.2012.27）：25x 提升来自**外部并行**（参数研究=大量独立仿真）+ 事件聚合；单仿真内部并行只是辅助。借鉴度：中高。
- **DeepABM**（arXiv 2110.04421）：GNN 可微 ABM，本质是近似，过不了 Tier-A。借鉴度：低。

### 2.4 多世界批处理（RL 社区工程）

| 对象 | 关键数字/技术 | 借鉴度 |
|---|---|---|
| [EnvPool](https://github.com/sail-sg/envpool)（1520★，arXiv 2206.10558） | C++ 线程池异步向量化环境：Atari 1M FPS、MuJoCo 3M FPS；教训：环境执行常是系统瓶颈 | 中-高 |
| [PureJaxRL](https://github.com/luchris429/PureJaxRL)（1107★） | 全 JAX 端到端，并行后 >1000x；核心=环境与学习同设备、零 CPU-GPU 往返 | 中-高 |
| [Brax](https://github.com/google/brax)（3237★） | JAX 物理引擎，vmap 万级并行 | 中 |
| Isaac Gym（NVIDIA，arXiv 2108.10470） | 物理+PPO 全 GPU，2–3 个数量级提速 | 中 |
| [Madrona](https://github.com/shacklettbp/madrona)（523★）+ GPUDrive（620★，arXiv 2408.01584） | GPU 批量仿真引擎（ECS 架构），单 GPU 数千世界、1M FPS 多智能体驾驶 | 中-高（ECS/SoA 布局） |

共同结论：场景都是定步进 MDP（与严格 DES 不同），但**"单世界串行、万世界并行 + 全链路同设备"的总架构**对 batch 单元直接适用。

---

## 3. 参考系二：速度型 leaderboard 赛事 top 玩家打法

### 3.1 GPU MODE（原 CUDA MODE）kernel 竞赛

- 生态（已核实）：官方题集 [gpu-mode/reference-kernels](https://github.com/gpu-mode/reference-kernels)（308★）。赛事：AMD $100K（2025 上）、AMD $100K+ 分布式（2025.8–10，$150K）、**NVIDIA Blackwell NVFP4（2025.11–2026.2，B200）**、AMD $1.1M（2026.2）、B200 线性代数赛（2026）。首场 AMD 赛 2 个月 163+ 队、3 万+ 提交；KernelBot 累计 ~40 万提交。
- 评测器机制（eval.py 源码已核实）：`POPCORN_SEED` 秘密种子 + Cantor 配对生成输入；正确性与计时都在 spawn 子进程；每次调用前深拷贝输入；**leaderboard 模式每次计时迭代换 seed 且每轮重新校验正确性**；CPU 侧 perf_counter_ns + 前后 synchronize。计分 geomean；大奖给最接近 roofline 的 kernel；shape 公开、输入随机采样。

**冠军/Top 选手手段（一手复盘）**：

- **RadeonFlow（AMD 2025 总冠军）**：[TechnicalReport.md](https://github.com/RadeonFlow/RadeonFlow_Kernels/blob/main/TechnicalReport.md)。FP8-GEMM 按重要性：转置提 L2/HBM 利用率 → 批量 GDS→LDS → shared memory padding 消 bank conflict → Split-K → 双缓冲 → fast-but-unsafe cast → block swizzling。**快版 kernel 只支持 benchmark shape**（公开承认的特化），另保留全形状 legacy 版。MLA 题用结合律重排（matrix absorption）单变换最高省 125.6x 计算；**CUDA Graphs 只为消 launch 开销**（评测机 CPU 计时，~170µs launch bubble），且发现全图捕获反而变慢、只捕获受益段。**本地完整复刻官方 eval.py** + 自动生成官方单文件提交格式 + 自写 C++ checker 对拍。
- **Akash Karnatak**：[FP8 GEMM for AMD MI300x](https://akashkarnatak.github.io/amd-challenge/)。**StreamK 仅对特定 shape 启用**——参考实现 fp32 累加校验使 StreamK 的原子加精度失配，必须走 fp32 中间缓冲，"negates most of the performance gains… only for those specific cases"。正确性门直接决定优化空间边界。
- **ml-mike（2026 QR 赛第 5）**：[QR Decomp at the Speed of Light](https://ml-mike.com/writing/qr_v2/)——信息密度最高的复盘：
  - 15 天 ~1900 次编号实验，419ms → 1499µs geomean，280x 超 torch.geqrf；
  - "**everyone at the top specialized to the benchmark shapes. The organizers accepted that and policed correctness instead**"；
  - **隐藏分与公开分可严重背离**："one route passed 6/6 runs with a public score of 2.59 ms and a secret score of 9.81 ms"——快但不稳的路线被主动撤回，常备稳定备胎；
  - **评测时间预算是优化变量**：ranked 评测限 240 秒，267 个 Triton 编译需 354 秒冷启动 → 压到 100 编译/96 秒；
  - "Improve the score is a bad objective"——目标写成"n=1024 提升且其他 case 零回归"的合同式约束；
  - 领奖台手段：第 1 名纯 Triton 12.6k 行 + CholeskyQR + 三 fp16-MMA 模拟 fp32；第 3 名 14.5k 行机器生成 CUDA + tcgen05；第 4 名 25.8k 行 NVRTC 源码级 JIT + 全图 CUDA graph；
  - 工程基础设施：`research/` vs `sota/` 目录分离、2400 行 `PRIORS.md` 记录死路、每次 eval 落 `performance.jsonl` 自动出图。
- **gau-nernst**：[All-to-All worklog](https://gau-nernst.github.io/amd-a2a/)：纯 PyTorch 重写先吃"参考实现很烂"的红利（1311µs vs 参考 93540µs），再上 P2P/symmetric heap。

**Reward hacking 与反作弊（红线地图）**：

- 官方复盘 [Anatomy of a Reward Hack](https://www.gpumode.com/news/reward-hacking)（NVFP4 赛）：某提交发现评测恰好 15 次校验 + 15 次计时，用 `id(data)` 指纹计数切路径——计时段第 1 次调用算完全部 15 题、之后查 dict 返回缓存指针，成绩虚高 ~2µs 登顶，**赛后几分钟内被 scrub**；且备有不依赖 id() 的第二路径。修复演进：每次计时前 clone + 打乱顺序 → 对输出 buffer 做 GPU 端指纹。
- [gpu-mode/kernelguard](https://github.com/gpu-mode/kernelguard)：15 类作弊模式检测——计时器 monkey-patch、伪造 stdout、篡改评测函数、按指针缓存结果、CUDA graph 重放滥用、except 静默回退、硬编码 benchmark shape 配置表、未同步多流分发等。
- KernelBench EVAL.md：对抗性单测 + 启发式名言 "**if you beat cudnn by more than 10%, think again**"。

对 Track 3 借鉴度：**高**。同构点：GPU 可选、正确性硬门、秘密 seed + 隐藏评测 ≈ sealed scenarios、"shape 特化合法、只管正确性" ≈ 公开单元可特化但隐藏集惩罚。反作弊清单同时是选手的禁区地图。

### 3.2 QuantCup（撮合引擎速度竞赛）

- 2011 冠军 **voyager** 的 [engine.c](https://gist.github.com/druska/d6ce3f2bac74db08ee9007cdf98106ef)（含完整设计注释）：
  - **Flat array 按价格直接索引整个订单簿**（`pricePoints[MAX_PRICE+1]`），全局 askMin/bidMax 游标——牺牲理论复杂度换常数；
  - **静态 arena 预分配全部订单条目**，分配=递增计数器，零 malloc；
  - **惰性撤单：只把 size 置 0**（"one memory store instruction on x86_64"）；
  - 针对 STRINGLEN=5 特化的手写展开 strcpy；结构体布局为 cache 优化；
  - **对评测数据分布的显式赌注**：承认算法线性、退化差，但"on the simulated data feed 绝大多数订单检查不超过 2 个价位"——用评测器自带 feed 的统计特性证明线性扫描可行。
- [ajtulloch/quantcup-orderbook](https://github.com/ajtulloch/quantcup-orderbook)（213★）：整洁 C++/Boost 重写与冠军 C 实现无统计显著差异；概括冠军实现为 "hand-coded intrusive linked lists, global variables"。
- 借鉴度：**高**——与 ABIDES 订单簿内核最直接同构；"承认最坏情况、按评测数据分布设计并用实测背书"正是"公开单元特化 + 隐藏集兜底"的原始版本。

### 3.3 1BRC（One Billion Row Challenge，2024.1）

- 主办仓 [gunnarmorling/1brc](https://github.com/gunnarmorling/1brc)（8.1k★），164 份提交；[官方结果文](https://www.morling.dev/blog/1brc-results-are-in/)：冠军 1.535s（8 核），基线两个月被压快两个数量级。
- Top 手段：**Top 10 全部用 sun.misc.Unsafe；8/10 用 GraalVM native image——"成绩进 2 秒后，启动速度成为决定性差异"**；分段并行、SIMD/SWAR、消分支误预测、process forking trick。
- [Ben Hoyt 的 Go 九连迭代](https://benhoyt.com/writings/go-1brc/)：1m45s→3.4s，**每步先 pprof 定位再动手**；手写定点解析、自写哈希表；坦承 "assume valid input"。
- 组织者侧：中途换评测机后**全量重测历史提交**保持可比；另有 32 核全开与 10K key 集的 bonus 评测防单一分布过拟合。
- 借鉴度：**高**——唯一与 Track 3 同样"排名只认墙钟"的大样本速度榜；证明消灭冷启动本身就是排名武器。

### 3.4 其他赛事参照

- **Google Hash Code**（目标分+4 小时）：[Nanored4498/HashCode2022](https://github.com/Nanored4498/HashCode2022)——赛时第 31、赛后继续调参到第 1；**每个输入数据集用不同策略/参数**是标准形态。借鉴度：中（per-instance 路由对应 per-scenario 策略分发）。
- **Halite**（机器人对战，回合时限）：[FakePsyho/halite2](https://github.com/FakePsyho/halite2)（第 2 超长复盘）：①黄金法则"focus on the things that have the biggest influence… while keeping everything simple"，看回放找最弱环节→最小改动→提交；②单一 evaluation function；③完全无状态降 bug 面；④**对齐排名构成**（"focused almost exclusively on 4-player games, because they had much higher impact on the final ranking (twice as many)"）；⑤**自建分布式评测**（EC2 48 核每小时 2.5 万局，复刻官方 matchmaking）。借鉴度：中-高。
- **Al Zimmermann's Programming Contests**（[azspcs.com](http://azspcs.com)）：只提交解不提交程序，无公开代码传统。借鉴度：低-中。
- **IMC Prosperity**（交易竞赛，提炼"对齐评测器"元打法）：[ericcccsliu/imc-prosperity-2](https://github.com/ericcccsliu/imc-prosperity-2)（254★，2024 全球第 2）、[chrispyroberts/imc-prosperity-3](https://github.com/chrispyroberts/imc-prosperity-3)（169★，2025 全球第 7，逐轮复盘）；回测器生态 [jmerle/imc-prosperity-3-backtester](https://github.com/jmerle/imc-prosperity-2-backtester)（203★）——**逐条逆向官方撮合语义并本地复刻**，输出格式与官方逐字节兼容；chrispyroberts 用样本数据逆向隐藏做市商行为。借鉴度：中（对应 Track 3：用公开单元参考 trace 校准 ABIDES 语义与容差边界）。

---

## 4. 参考系三：高性能 DES 与撮合引擎工程

### 4.1 高吞吐 CPU 撮合引擎（对照基准：Python ABIDES ~72µs/event）

| 对象 | 关键数字 | 核心技术点 |
|---|---|---|
| [exchange-core](https://github.com/exchange-core/exchange-core)（Java，mzheravin） | 单簿 **5M ops/s**（2010 Xeon X5690）；大市价单撮合 150ns；p50 延迟 1.5µs | 全程无浮点；对象池+单 ring buffer；Disruptor 分核；symbol 分片；线程亲和；ART 价层索引 |
| [chronoxor/CppTrader](https://github.com/chronoxor/CppTrader)（1071★） | ITCH 解析 41.5M msg/s；含完整簿维护 **3.2M msg/s，激进版 8.3M msg/s**；单消息 ~120ns | C++ 全栈 |
| [AsthaMishra/matching-engine](https://github.com/AsthaMishra/matching-engine)（Rust） | 回放 1.04 亿条真实 ITCH 5.0，簿操作 ~100ns/op（进程内微基准） | 每簿严格单线程；symbol 按 id%workers 分片；lock-free channel + 响应槽对象池 |
| [liquibook](https://github.com/enewhuis/liquibook)（C++） | 2.0–2.5M 笔/秒持续插入（作者自报） | header-only；price-level depth book |
| [joaquinbejar/OrderBook-rs](https://github.com/joaquinbejar/OrderBook-rs)（532★） | lock-free 线程安全簿 | Rust |

### 4.2 LMAX 架构线（性能归因清单）

- [LMAX Disruptor 论文](https://lmax-exchange.github.io/disruptor/disruptor.html)：Disruptor vs ArrayBlockingQueue 单播 1P-1C **26M vs 5.3M ops/s**（2011 Nehalem），新硬件 134–160M ops/s；锁成本实验：单线程 300ms → 加锁 10s → 双线程争锁 224s。技术点：ring buffer 预分配、2 的幂取模、单写者、缓存行对齐防 false sharing、批量追赶。
- [Martin Fowler《The LMAX Architecture》](https://martinfowler.com/articles/lmax.html)：全部业务逻辑单线程 **6M TPS**；进阶路径：顺序化+全内存 10K TPS → 良好结构 100K → 自定义缓存友好集合+GC 纪律 6M TPS。**收益主要来自数据结构与内存布局，而不是并发**——这条三级跳就是 Track 3 CPU 路线的直接路线图。

### 4.3 DES 内核优化文献

| 文献 | 结论 | 借鉴度 |
|---|---|---|
| Brown 1988, Calendar Queue（DOI 10.1145/63039.63045） | 实测 O(1)；1 万事件时比 splay tree **快 3 倍**；桶宽自适应 | 高（事件队列是 DES 内核第一热点之一；适配 ABIDES 近常数延迟增量） |
| Tang, Goh & Thng 2005, LadderQueue（DOI 10.1145/1103323.1103324） | 摊还 O(1)（仅要求增量均值有限非零，方差可无限，如 Pareto）；实测 100 万–1000 万事件稳定 O(1) | 中高（二期实验） |
| Gupta 等 2017, 2-tier/3-tier 优先队列（DOI 10.1145/3064911.3064921） | 2500 组 PHOLD 配置上比 LadderQ 再快 **10–50%**（分层：顶层无序粗桶+底层精确排序） | 中 |
| Fujimoto 1990 PDES 综述（DOI 10.1145/84537.84545） | DES 不规则数据依赖使向量化几乎无收益；并行出人意料地难 | 高（否决依据） |

综合结论：ABIDES 每个订单事件都可能改变 BBO 并立即唤醒一批智能体——耦合是**每事件级**的；唯一充足的并行轴是"世界"（独立场景/独立 seed），与 batch 单元设计完全同构。这应作为架构决策第一原则。

### 4.4 Python 加速路线实证

- **[Ohad Ravid《Making Python 100x faster with <100 lines of Rust》(2023)](https://ohadravid.github.io/posts/2023-03-rusty-python/)**（与 ABIDES 形态高度相似的 Python 对象+numpy 负载）：

  | 版本 | 每轮耗时 | 加速比 |
  |---|---|---|
  | Python 基线 | 293.4ms | 1x |
  | Rust 逐行直译 | 23.4ms | 12.5x |
  | 数据结构下沉为 Rust pyclass | 6.29ms | 46.5x |
  | 去分配 | 2.90ms | **101x** |

  PyPy 反而慢 5 倍；numba/PyPy 类 JIT 对对象图负载收益很小；py-spy 火焰图显示跨边界 `getattr/extract` 一度占 88% 样本——**边界必须按整段循环下沉，不能按单事件下沉**。
- **Numba**（nopython 模式）：只对已数组化的数值核有效（oracle 路径采样等），不能触碰 Python 对象树——对事件循环本身无能为力。
- **numpy 向量化 LOB 是负收益反例**（JAX-LOB Table 5，见 §2.2）。

### 4.5 确定性高速随机数与浮点确定性

- **JAX PRNG 设计文档（[JEP 263](https://github.com/jax-ml/jax/blob/main/docs/jep/263-prng.md)）**：Threefry 计数器型 PRNG + 可分裂 key 模型；用 counter 维做向量化采样；坚持软件 PRNG 保证跨后端逐位可复现。其 API 设计（key 派生树）可直接照搬到 `(root_seed, scenario_id, agent_id, counter)` 派生方案。
- **[NumPy 官方并行 RNG 文档](https://numpy.org/doc/stable/reference/random/parallel.html)**：`SeedSequence.spawn()`；Philox 用 128-bit key 产生独立流；`jumped()` 跳 2¹²⁸；**明确警告 `worker_seed = root + worker_id` 旧做法不安全**（流重叠风险）。
- **Salmon, Moraes, Dror & Shaw (SC'11)《Parallel Random Numbers: As Easy as 1, 2, 3》**（DOI 10.1145/2063384.2063405）：Philox/Threefry 原始论文。
- **[Bruce Dawson《Floating-Point Determinism》](https://randomascii.wordpress.com/2013/07/16/floating-point-determinism/)**：IEEE 只保证 +−×÷ 与 sqrt 正确舍入；确定性杀手清单——FMA/fp-contract（须 `-ffp-contract=off`）、x87 中间精度、`-ffast-math`、rcpss/rsqrtss 近似指令、transcendental 跨 libm 不一致、FTZ/DAZ、按 CPU 特性分派的库代码路径。关键安心剂：**同一二进制+同一 CPU，浮点确定性基本免费**。实践结论：撮合/成交判定/时间戳整体迁入整数域（int64 价格 tick 与 ns），浮点只留 oracle/统计计算并锁死编译选项。

### 4.6 输出 IO 与基础设施

- **PyArrow Parquet**：热循环内只做 SoA 列缓冲追加（int64 数组），结束一次性 `pa.Table.from_arrays` → 单次 `write_table`；默认 snappy，压缩档位（zstd 低级 vs lz4 vs none）的 CPU/体积取舍**无可信公开基准，需自测**。
- **绑核/NUMA**：`sched_setaffinity(2)` / numactl `--physcpubind`；容器 4 核下 batch 单元"每核一进程一世界、内存本地分配"是标准模型；子进程继承同一二进制与同一 RNG 派生方案以保证"与单独跑字节一致"。hugepage/THP/isolcpus 容器内多半不可控，列低优先级。

### 4.7 GPU 路线真实边界（结论性）

1. 单簿 GPU 慢得离谱（JAX-LOB 实测 0.08–0.25ms/操作，慢 2–3 个数量级）；**世界数 <1000 不开 GPU**。
2. vmap 语义决定性能：所有分支都算（select 化），耗时 = 最慢分支；要么均衡分支要么下沉小 tile。
3. 精确门下 GPU 浮点语义（FTZ、libm 差异、warp 归约序）是硬风险。可行折中：**撮合顺序、成交判定、时间戳、账本全部留 CPU 整数域；GPU 只批量计算纯函数性质的部分**（多世界 oracle 路径预采样、agent 决策批量数学），并验证 GPU 结果不影响任何进入 trace 的值。
4. 混合分工在 survey 中常输给全 GPU，但在"交换必须严格串行"的本竞赛约束下是合理中间路线。
5. 不做 Time Warp/GPU-PDES：lookahead 不足 + 大状态回滚。

---

## 5. 核心夺冠技术点（分层蒸馏，按杠杆排序）

### A. 架构与语言层（10–100x 级）

1. **单线程事件循环 + 整数域 + 零分配**是黄金三角（LMAX 10K→100K→6M TPS 路径；exchange-core 5M ops/s）。
2. **Rust(PyO3)/C++ 整核下沉**；混合路线的边界税是致命伤（Ravid 案例：逐行直译 12.5x → 全下沉 46.5x → 去分配 101x；跨语言调用按整段循环下沉）。
3. **numpy 向量化订单簿已被证伪**（cancel/穿价慢 3–5x）；Numba 只用于已数组化的数值核。

### B. 数据结构层（2–10x 级）

4. 订单簿：价格 tick 整数化后 flat array/位图索引 + level 内侵入式 FIFO 链表 + 订单 ID→节点哈希表 + 全对象池/arena + 惰性删除（QuantCup 全套）。
5. 事件队列：binary heap 起步 → calendar queue（实测 O(1)，3x over splay）→ LadderQueue / 3-tier（再快 10–50%）。

### C. 确定性工程层（入场券）

6. 计数器型 RNG（Philox/Threefry）+ 按 `(root_seed, scenario_id, agent_id, counter)` 派生独立流；禁 `root + worker_id` 式派生。
7. 撮合/成交/时间戳全整数域；浮点仅统计用途；禁 fast-math、`-ffp-contract=off`、禁 rcp/rsqrt、libm 静态打包。
8. 整数定点换 bitwise 一致（KineticSim：数量全整数后连 atomicAdd 都确定）。

### D. 系统层（计入成绩的固定开销，常是胜负手）

9. **冷启动即排名武器**（1BRC GraalVM；EnderMio 6.08x→1.25x；GPU MODE 在 240 秒预算内压编译数）。Track 3 计时窗含容器内启动：解释器/JIT 预热必须工程化。
10. batch 单元：每核一进程一世界 + 绑核 + SoA 列缓冲一次性写 parquet。
11. 若上 GPU：CUDA graphs 消 launch 开销，只捕获受益段。

### E. GPU 层（只作 batch 单元加餐）

12. 世界级批量是唯一成立形态：单世界严格串行、≥1000 世界并行；稠密定容数组 + 不排序线性扫描 + 分支展平（JAX-LOB 妥协集）。
13. 精确门下的折中：撮合/时间戳/账本留 CPU 整数域，GPU 只做 oracle 预采样与 agent 决策批量纯函数。
14. 反面清单：集合竞价等定步近似不可互验（KineticSim 自述）；GNN/神经代理过不了 Tier-A；静态展开大 kernel 编译爆炸；为 GPU 而 GPU。

---

## 6. 核心夺冠策略点（top 玩家元打法，按重要性排序）

1. **评测器本地化是第一基础设施。** RadeonFlow 直接本地跑官方 eval.py 并自动生成提交格式；Prosperity top 队逐条逆向官方撮合语义写回测器；Halite 自建每小时 2.5 万局分布式评测。**迭代带宽 = 成绩。** → Track 3：本地回归套件 + 逐事件 diff + Kendall-τ 工具先于任何优化建好。
2. **先算上界再动手（roofline 先行）。** seb-v 先算理论 FLOPS/带宽上界；ml-mike 先算"领奖台需要多少累计 geomean 提升"再决定是否架构级转向。→ Track 3：先建簿记（CPU 路线上界 ~1–5M events/s；batch 4 进程 ×4；GPU 批量 ≥1000 世界回本）再定投入。
3. **针对评测分布特化 + per-case 路由，但配隐藏集备胎。** "Everyone at the top specialized to the benchmark shapes"（官方默许、只管正确性）；但公开 2.59ms / 隐藏 9.81ms 的背离案例真实存在，快而不稳的路线被主动撤回。→ **激进版/稳定版双轨**。
4. **正确性门的精确语义就是优化空间的边界。** StreamK 因 fp32 校验只能部分启用；→ 逐条吃透 Tier-A（精确成交、τ≥0.999、双向覆盖、账本因果）后可知：Tier-A 下"近似换速度"几乎全禁，**速度只能来自常数因子**（语言、数据结构、分配、IO、启动）——形态上更接近 1BRC/QuantCup 而非 GPU kernel 赛的"数学等价变换"。
5. **聚合指标下不许单点回归。** "Improve the score is a bad objective"；目标写成"某 case 提升且其他 case 零回归"的合同式约束。→ Track 3 全场算术平均、挂一个 unit 记零分：65 个公开单元全绿是不可谈判的底线。
6. **提交额度稀缺 → 以最坏情况而非公开榜最好成绩决策。** → Track 3 每天 5 次/共 20 次/终赛 1 次：每次提交按"sealed 集最坏情况"评估，常备 stability reference。
7. **profiler 驱动单变量实验 + 失败结论资产化。** ml-mike 1900 次编号实验 + 2400 行 PRIORS.md 死路清单；benhoyt 每步先 pprof。优化赛中后期瓶颈是"不重复踩死路"。
8. **红线：利用设定合法，利用测量被追溯清零。** 按数据分布设计（QuantCup 赌线性扫描、1BRC assume valid input）合法；计时盲区、结果缓存、伪造输出会被 scrub 并追溯。kernelguard 15 类模式清单值得对照自查（按指针缓存结果、CUDA graph 重放滥用、计时器 monkey-patch、伪造 stdout 等）。
9. **工程卫生即速度**：无状态核心（Halite）、提交格式自动化（gen_submission.py）、强制可读性重写（让后续补丁更准）、性能数据落盘自动出图。
10. **公开复盘是社区常态**——GPU MODE 全部提交开源成数据集、Prosperity 亚军仓库数百星。先研究别人的方案；同时预期自己的方案最终被研究。

---

## 7. 与 Track 3 约束的映射要点

| Track 3 特征 | 对应参照 | 含义 |
|---|---|---|
| 排名 = 全场算术平均，挂一 unit 记零分 | GPU MODE geomean 纪律 | 不允许单点回归；全单元全绿是底线 |
| 逐字节确定性 + repeat 一致 | KineticSim / JAX JEP-263 / Dawson | 整数域 + 确定性编译 + counter RNG 是入场券 |
| Tier-A 精确门（成交序列/τ/覆盖/账本） | QuantCup/1BRC 形态 | 近似换速度全禁；速度只来自常数因子 |
| batch 单元隔离门 | JAX-LOB/Madrona/EnvPool | 世界并行是唯一安全的并行轴；世界间零通信 = 隔离门免费 |
| GPU 可选 + B200 | GPU MODE NVFP4（同在 B200） | GPU 奖（utilization 门槛）与主排名分开决策；单场景不上 GPU |
| network=none + 计时含启动 | 1BRC GraalVM / EnderMio 冷启动 | AOT 编译、镜像瘦身、预热是排名武器 |
| sealed 场景惩罚过拟合 | GPU MODE 隐藏 seed/秘密分 | 特化须配稳定备胎；按最坏情况提交 |
| 5 次/天、20 次、终赛 1 次 | GPU MODE 提交经济学 | 本地评测带宽决定迭代速度；提交按最坏情况决策 |

---

## 8. 不确定性与待核实项

1. "无 ABIDES 加速先例"置信度中高（GitHub + arXiv 均无命中，但调研环境搜索引擎受限）。
2. GPU PDES 经典文献个别条目（Fujimoto/Perumalla 线）未能在线逐字核实，方向性结论可靠。
3. 各小项目（apex-lob、AsthaMishra、KineticSim 等）性能数字为作者自报，无第三方复现；AsthaMishra 的 100ns 为进程内微基准（不含 IO 端到端）。
4. JAX-LOB 论文中的"CPU 对比实现"本身是 Python 级（5µs/op），远慢于 exchange-core 级（0.2µs/op）——其 GPU vs CPU 倍数对优化过的 CPU 引擎会进一步缩水。
5. parquet 压缩档位、容器内 THP/hugepage 效果：无可信公开数字，需自测。
6. gpumode.com 榜单逐名细节未直接核实（转引自官方 news markdown 与选手复盘）；AMD $1.1M 与 NVFP4 最终获奖名单未找到公开材料；QuantCup 2011 之后届次、Hash Code 各届冠军 write-up 未找到一手来源。
7. Prosperity 4 冠军（Seven Deuce）仓库截至调研日仅为占位符。

---

## 9. 下一步衔接（不是策略，只是衔接）

所有参照系的共同第 0 步：**用 `throughput/timer.py` + 65 个公开单元建立可复现本地基准与逐事件 diff/Kendall-τ 工具**——正确性对齐工具先于任何优化。之后按 §5 的数量级簿记做一次路线级 roofline 分析（纯 CPU 重写 / Python+编译扩展混合 / GPU 批量世界三条线的期望值与风险），再定迎战策略。
