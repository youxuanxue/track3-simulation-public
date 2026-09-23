# EXP-0002 — HEAD 冻结新鲜 baseline：同窗完成 freeze→run→assess→noise→decide，建立 hash 链 stable 身份，并计量 #14/#15 收益

- **编号**：EXP-0002
- **立项日期**：2026-09-21
- **状态**：running
- **承办**：参谋（主会话），测量主机 colima-t3-arm64
- **关联**：EXP-0001 后续 1/2/3；SCOREBOARD "stable 未经正式链条"；PROTOCOL §5.5 代码冻结窗首次实战

## 假设（可证伪）

1. 在 HEAD（`7d41ed5`，含 #14 冷启动修剪、#15 batch 计时修复）构建镜像，单镜像
   baseline 用途 freeze → run → assess → decide 同窗完成，decide action = promote，
   hash 链首环建立（此前链为空）。
2. HEAD 镜像相对 B2 stable（`track3-b2@ac2b7848`，算术平均 178.4k eps）不差
   （#14/#15 为优化提交，预期持平或更好；跨窗对照仅作参考，不作晋级依据）。
3. `t3-gbatch-hetero-mix` 在 HEAD 单臂 5 组下的组间散射可报告其测量可信度
   （EXP-0001 发现 3 的跟进）。

## 依据

- EXP-0001 发现 1：旧证据永久失去 decide 资格，链只能以新鲜证据重建。
- 未命中 PRIORS 条目；本实验不改模拟器代码。

## 改动范围（白名单）

- `out/exp-0002*`、`out/arm64-history/`（decide 写入链事件）
- `ops/`（本档、PROTOCOL §6、SCOREBOARD、experiments/README、QUOTA 如需）
- **代码冻结窗生效中**：窗内禁碰 `identity()` 覆盖的代码文件
  （baselines/ qfbench2_track_simulation/ throughput/ scripts/ 的 .py/.pyx/.patch、
  两个 Dockerfile、simulate、simulate-batch、trace_column_registry.json）

## 验收线（先写死，后动手）

- [ ] 门 1：assess G2 = pass（含 65/65 语义与字节级 repeat 一致，由评估器复核）
- [ ] 门 2：本实验为链路建立，门 2 变形为——decide action = **promote**，
  stable = HEAD 镜像 ghcr digest
- [ ] 门 3：holdouts 新鲜参数集通过（generate 新 seed → run → index → assess 复核）
- [ ] 门 4：assess 内 byte-repeat-failure 检查为空（5 组字节一致）
- [ ] 附：噪声报告落盘（单臂组间散射 + hetero-mix 专项读数）

## 测量协议

- 持锁：`ops/bin/bench-lock exec exp-0002 <wait> -- …` 覆盖 holdout run 与 baseline run
- 镜像：`track3-exp-0002:arm64`（HEAD `7d41ed5`）→ 推送 ghcr 后以 digest 引用
- scratch：**10 GiB**（`/mnt/t3-scratch`，loop ext4 -m0）。
  **偏离 runbook 64 GiB 的说明**：VM 数据盘经清理后可用 ~57G，放不下 64GiB 文件；
  且官方单元卡 `disk="10G"`、`CAPS` 默认 10 GiB——10GiB 更贴近官方口径；
  b2 证据显示单 run scratch 峰值 ≪1MiB（如 831,488 B），容量不构成约束。
- history：`out/arm64-history`（主线链；`out/b2-qualify/arm64-history` 为 b2 时代副链，
  同样为空，不混用）
- 命令序列：
  1. `docker build -t track3-exp-0002:arm64`（平台 linux/arm64，t3-arm64 VM）
  2. `docker tag … ghcr.io/youxuanxue/track3-simulation-public:exp-0002-7d41ed5` + push
  3. `prepare_submission.py verify-delivery` + `package --team-key-file ~/.config/agenthon/team23.key`
     → G1（zip + .g1.json）
  4. `validate_candidate_parameters.py generate`（新 seed 20260921，参考镜像
     `track3-reference-arm64:latest`）→ `run --image <ghcr digest>` → `benchmark_candidates.index`
  5. bench-lock 内：`freeze --purpose baseline --round EXP-0002 --disk-gib 10 …`
     → `run --scratch-volume /mnt/t3-scratch` → `assess` → 噪声分析 → `decide`

## 原始记录（工兵自报，未复核）

- 2026-09-21：VM t3-arm64 启动（4C/16G，/dev/vdb1 126G）；清理已死 track3 v1–v3 镜像
  与可回拉 ghcr 标签、prune 挂载卷 17.96GB、删除 b2 时代遗留 scratch.img 33G；
  保留 track4/* 与 `track3-b2:opt-batch-fork`（现行 stable 本地副本，ghcr 可回拉）。
- 2026-09-21：scratch 重建为 10 GiB loop ext4（容量 10,464,022,528 B ≤ 10 GiB 预算 ✓）。
- 2026-09-21：docker.io 被 DNS 污染 → dockerd 代理固化
  （`/etc/systemd/system/docker.service.d/proxy.conf` → `host.lima.internal:7890`）；
  构建容器内域名不可解析 → 构建代理改用裸 IP `192.168.5.2:7890`（pypi/github 容器内 200 ✓）。
- 2026-09-21：构建 `track3-exp-0002:arm64` 成功（第三次尝试）；冒烟 s001 通过
  （rc=0，trace/ledger/events 三件套落盘，sim-loop 89.5k eps / n=604）。
- 2026-09-21：推送 `ghcr.io/youxuanxue/track3-simulation-public:exp-0002-7d41ed5`，
  digest `sha256:607bec51f29a941dd0ed701b3c154bd8b70715b962e02569e5be018f503f1f18`
  （与本地 manifest list 一致；首次 EOF 中断，续传成功）。
- 2026-09-21：macOS 侧 urlopen 需显式 `HTTPS_PROXY=127.0.0.1:7890`（系统代理不被
  该 Python 读取，SSL EOF）。
- 2026-09-21：**verify-delivery 通过**（匿名拉取必须 `DOCKER_HOST` 指向带代理的
  t3-arm64 VM——`anonymous_pull` 读 DOCKER_HOST 否则回落默认 context，
  `prepare_submission.py:159`）。
- 2026-09-21：**G1 通过**：`out/exp-0002/development.zip` + `development.g1.json`
  （scope G1 / status passed / rankable false，team23.key 签名）。
- 2026-09-21：holdout 生成（seed 20260921，27 案例 / 7 家族 / 6 batch，参考镜像
  `track3-reference-arm64@sha256:222266fcd5f9…`——生成器只收 digest 引用）；
  **holdout run 通过**（bench-lock 持锁，`out/exp-0002/parameters/heldout-result.json`）。
- 2026-09-21：holdout-index 建立（镜像→证据映射）；
  **plan 冻结** `out/exp-0002/plan-baseline.json`
  （plan_sha256 `641528579218a86e…`，purpose baseline，disk-gib 10，
  history `out/arm64-history`）。
- 2026-09-21：baseline 测量运行启动（bench-lock 持锁，432 容器运行）——结果待补。

## 结论（参谋复核后填写）

（待测量窗完成后填写：decide 事件 sha256、stable digest、四门状态、噪声读数、
与 B2 的参考性对照）

## 死路登记（仅 rejected 必填）

（如发生）

### 2026-09-21 23:49 +0800 — baseline 运行重启
- 故障修复：`/mnt/t3-scratch` ext4 默认 root 755 → `chmod 1777`（PROTOCOL §6.1 配方已同步）。失败证据留存 `out/exp-0002/baseline-run-failed-perm`。
- 重启方式：VM 内 `bench-lock exec exp-0002-baseline` + nohup（防 ssh 中断杀进程），日志 `out/exp-0002/baseline-run.log`，输出 `out/exp-0002/baseline-run`。本地任务 bash-to65pvy4。

### 2026-09-22 00:14–01:30 +0800 — 运行 v1 失败分析与三项修正（重大方法论发现）

**故障**：EXAMPLE warmup 运行 1467s 后以 ENOSPC 死亡（`OSError: Errno 28`，pyarrow 写 trace 时 scratch 满）。单 run 已写 **9.73 GB** 且未完工。

**根因链（逐层证实）**：
1. 我此前"单 run scratch 峰值 ≪1MiB"的校准是在 s001 小单元上观测的，外推到 EXAMPLE 是错的；10 GiB scratch + plan disk-gib 10 双重不够。
2. B2 时代 864 条记录全盘峰值仅 0.05 GiB，EXAMPLE 在 B2 是 10,227,367 事件 / 6.2s / eps≈1.63M——而 git 中 scenario.json（1h horizon）自 release 起逐字节未变。矛盾。
3. `out/b2-qualify/PHASE1-ANALYSIS.md` 揭开答案：**B2 本地 roster 用的是 5s 缩减版 exemplar**（未提交的工作区改动，"恢复 1h 后 ~66 min×6"）。git 历史自然看不到。
4. 5s 真迹找到：`out/b2-qualify/smoke-io/bench2/.../in/scenario.json`，4 份独立暂存 md5 全同（`9c77997023ed…`），`_comment` 自述 "LOCAL SHORTENING for fork qualify speed… restore before 1h evidence"。与上游 1h 版仅 _comment/description/horizon_ns 差异。
5. 哈希考古：B2 计划 EXAMPLE input `1af68ffd…` 无法从现存 artifact 精确复现（32 组合暴力匹配未果——freeze 时工作区另有已失传的未提交差异）。**语义层面 workload 由 S1 字节锁定**，引擎不读 README/manifest，meta 差异不影响测量。
6. #14 diff 复核：纯 `stable_lexsort` 搬家重构，无事件语义变化——**排除引擎回归**。HEAD 在 1h scenario 上的数亿事件才是设计负载（200NT×50Hz×3600s=3600 万提交，MM 每 500µs×20 档≈数十亿事件量级）。

**决定**：
- **D1**：EXAMPLE 采用 B2 惯例 5s 覆盖（S1 字节，工作区未提交改动，窗口结束后恢复 1h 版）。跨窗可比性保持；input 哈希为新值 `275c7573…`（= HEAD meta + S1，可解释），与 B2 的 `1af68ffd…` 语义等价而非字节等价。
- **D2**：plan disk-gib 64（对齐 B2 政策与 validate_caps 的 arm64 约定；caps 跨链比对不漂）。
- **D3**：scratch 维持 10 GiB（≤64 GiB cap 通过校验；B2 全盘峰值 0.05 GiB，余量 200×）。vdb1 幻影占用（df 74G vs du 可见 14.6G）记为已知运维问题，不阻塞。
- 旧证据归档：`baseline-run-failed-disk{,.log}`、`plan-baseline-disk10-SUPERSEDED.json`。

**v2 启动 01:30 前后**：新 plan `7936da96f51043bd…`（host native/Linux、caps 64GiB、roster 71/72 与 B2 全同），bench-lock + nohup，本地任务 bash-qnw0bld3。warmup 已过，进入 group-0。

### 2026-09-22 01:55–02:10 +0800 — 运行 v2 全绿；assess 卡 holdout 验证者身份（已修）
- 运行 v2：**432/432 passed**，stop=completed-once，被测墙钟合计 3.6 min，全程 ~22 min。
- 单臂噪声：median log sd 1.38% / p90 5.52%（n=72）；hetero-mix 4.81%（五组 120.8K–136.1K）——
  B2 窗 54.5% 的极端配对 sd 是该窗臂间交互产物，非单元固性；挂单问题降级关闭。详见
  `out/exp-0002/noise-single-arm.md`。
- **#14/#15 收益（跨窗参考，HEAD vs B2-B）**：72/72 全正；geomean 1.2488；聚合算术平均中位
  **244,673 vs 178,425 = +37.13%**。batch 单元 +71%~+110%（#15 主杠杆），全盘面 +5%~+10%
  （#14 冷启动修剪），mr-deep-book +52.2%。
- assess v1 失败：`independent-validation-failed` → 逐层定位为
  `validate_candidate_parameters.py:407` 的 `stale heldout validator evidence` →
  **pandas 3.0.6（新 venv）vs 3.0.5（holdout 运行环境）**。数值库版本属于验证者身份。
  修法：venv-t3 钉 `pandas==3.0.5`，DIFF 归零。已固化进 PROTOCOL §6.1 重建配方。

## 结论（2026-09-22 02:05 +0800，参谋复核后入账）

**结果：accepted（promote）——hash 链创世完成。**

- **四门**：G1 pass（`out/exp-0002/development.g1.json`）；G2 pass（assess 零 reasons）；
  G3 missing（baseline 单臂按设计豁免）；holdout 独立校验 pass（pandas 钉版后）。
- **decide 事件**：`out/arm64-history/000000.json`，action=promote，
  事件 sha256 `4fcd0923c1314fac721c03bcbaee7147d544893c2a892d13e124c4698fd60a92`，
  previous=None（创世），previous_stable=None。
- **stable 身份**：`ghcr.io/youxuanxue/track3-simulation-public@sha256:607bec51f29a941dd0ed701b3c154bd8b70715b962e02569e5be018f503f1f18`
  （HEAD `7d41ed5`，identity 仅哈希代码文件，与文档/ops 变更解耦）。
- **测量**：plan `7936da96f51043bd…`（native/Linux、arm64、caps 64GiB、72 单元 roster
  仅 EXAMPLE 与 B2 不同——5s 覆盖新哈希 `275c7573…`，语义等价）；432/432 passed；
  聚合算术平均中位 **244,672.8 eps**。
- **跨窗参考（非晋级依据）**：vs B2-B 72/72 全正、geomean 1.2488、算术 +37.13%
  （#15 batch 修复主杠杆 +71%~+110%；#14 冷启动全盘面 +5%~+10%）。
- **hetero-mix 读数**：单臂 sd 4.81%，B2"天生高噪"证伪，SCOREBOARD 挂单关闭。
- **方法论资产**（已条令化）：5s exemplar 覆盖惯例（PROTOCOL §6.1 + PRIORS D3）；
  venv 数值库版本=验证者身份（pandas==3.0.5 钉版配方）；vdb1 幻影占用已知问题；
  scratch 容量 ≤ disk-gib 且单 run 峰值为 sizing 依据。
- **窗口关闭**：EXAMPLE scenario 已 `git checkout` 恢复 1h 上游版（工作区干净）。

**遗留问题（移交后续实验）**：
1. EXAMPLE 是否计入官方名册未公布——1h 版攻关立项前必须先确认（SCOREBOARD 悬置）。
2. amd64（舰队真实架构）验证通路不存在（PRIORS §E）——终赛前必须建。
3. CodaBench 实际上传次数对账缺口（QUOTA.md）——上传冻结中，待统帅查后台回填。
4. #14/#15 收益为跨窗参考值；下次 paired 实验将以同窗口径重新锚定 stable=A。

**EXP-0003 候选方向**（供统帅决策）：
- a) paired 确认实验：stable(A) vs 下一候选(B)，同窗口径锚定新基线（例行节奏）；
- b) 杠杆攻关：SCOREBOARD 贡献列头部（gb 家族 / mr-deep-book / EXAMPLE 名册确认后）；
- c) amd64 通路建设（终赛硬需求，越早越好）；
- d) QUOTA 对账 + Development 上传策略（需统帅后台数据）。
