# PROTOCOL.md — 作战条令

> 摘要：本文件是军团唯一的行动准则。变动由统帅签发，全军团即刻生效。任何 agent
> 开工前必须已读：`ops/README.md`、本文件、`PRIORS.md`、`SCOREBOARD.md`、
> 仓库根 `AGENTS.md`。

## 1. 排名函数对齐（一切取舍的第一性原理）

- 排名 = 全场名册**算术平均** events/sec，降序；**挂任意一个单元 = 零分**。
- 推论 1：优化收益按**绝对增量**论功。把 500k eps 的单元提升 10%（+50k）大于把
  1.4k eps 的单元翻倍（+1.4k）。选杠杆看 SCOREBOARD 的贡献列，不看相对倍数。
- 推论 2：72/72 全绿是不可谈判底线。任何以牺牲边缘单元为代价换取主体提升的改动，
  一律 rejected，不论总收益多大。
- 推论 3：sealed 场景存在 ⇒ 公开集特化必须搭配"按参数分发、按最坏情况评估"的稳定
  备胎策略（调研 §6.3/§6.6）。禁止按 scenario ID 特判（PRIORS B7）。

## 2. 编制与分工

| 角色 | 担任者 | 职责 | 禁止事项 |
|---|---|---|---|
| 统帅 | 人 | §7 决策点独占：上传、路线转向、终赛选型、额度签发、本文件修订 | — |
| 参谋 | 主会话 agent | 读记分板立项、写简报、派工兵、**亲自复核验收**、维护台账/记分板/死路清单 | 不亲手做大规模实施（保持统筹上下文）；不签发上传 |
| 工兵 | coder subagent | 单 EXP 实施：改码、跑检查、产证据、填 EXP"原始记录"栏 | 不超白名单改文件；不自报晋级；不碰 stable 分支 |
| 侦察 | explore subagent | 只读：代码/文献/数据调查，回结构化结论 | 不改任何文件 |
| 门卫 | 验收流程（机器） | §4 四门，只认重跑结果 | 不接受任何自报 |

## 3. 简报模板（参谋派活时的唯一格式）

每个 subagent 任务必须包含六要素，缺一不派：

```
1. 目标：一句话 + 对应的 EXP 编号
2. 上下文：必读文件清单（具体路径，含本文件相关节与 PRIORS 相关条）
3. 白名单：允许新建/修改的路径；其余只读
4. 验收命令：逐条可执行 shell；工兵必须原样跑并留存输出
5. 证据输出：写入 out/exp-NNNN-*/ 的具体文件
6. 红线提醒：本任务相关的 PRIORS 条目编号
```

工兵回收的结论只回答：做了什么、验收命令的原始输出、证据路径、遇到的意外。
**不做**"建议晋级"判断——晋级是参谋复核后的决定。

## 4. 验收四门（机器判，参谋亲跑）

全部通过才允许 `decide` 晋级；任一不过即 rejected 或返工：

| 门 | 内容 | 工具 |
|---|---|---|
| 门 1 回归 | 65/65 公开场景通过 + stylized facts 四门不劣化 | `regression_suite/run_regression.py`（workers ≤4） |
| 门 2 提升 | paired A/B ≥5 组交替 A/B，**双层判定**（阈值经 EXP-0001 实测校准，见下）：聚合层算术平均配对中位提升 **≥2%**；单元层无确认回归 | `scripts/benchmark_candidates.py freeze/run/assess` + `ops/bin/noise-report`，bench-lock 独占 |
| 门 3 泛化 | holdouts 参数集（seed/horizon/protocol 合法变体 + native 异常探针）全过 | `scripts/validate_candidate_parameters.py generate/run` |
| 门 4 确定 | 同镜像同输入双跑，`trace.parquet` / `message_trace.parquet` sha256 一致 | runbook 的 repeats 校验 |

门 2 双层判定细则（EXP-0001 于 b2 证据实测校准，2026-09-21；测量噪声结构：
聚合比 95% 带 ±0.77%，单单元中位比 95% 带 median ±7.8% / p90 ±14.7%）：

- **聚合层（晋级判据）**：全场算术平均的配对中位提升 **≥2%**（≈2.6× 实测噪声带）。
  <2% 的聚合进步不可声称——它淹没在噪声里，攒到可分辨再说。
- **单元层（回归判据）**：逐单元看中位比，≤0.85 = **确认回归**（超出 p90 单元噪声带
  ±14.7%，取整 15%），实验 rejected 或返工；0.85–1.0 = 可疑区，**不直接拒，加测
  5 组合并 10 组再判**；≥1.0 = 通过。单元自带噪声带（`noise-report` 给出）宽于
  15% 的，以自带带为准。
- **测量纪律**：`noise-report` 标 ⚠️ 的单元（配对 sd >50%，如 t3-gbatch-hetero-mix）
  任何单元级结论须 ≥10 组；汇总口径符号相反（中位比与配对 log 均值）时按"不可判"
  处理，禁止取对自己有利的口径入账。
- **重校准**：每次确认实验 assess 之后必须跑 `ops/bin/noise-report` 并存档
  `out/exp-NNNN-noise-report.md`；噪声结构漂移超一倍时回报统帅走 D5 修阈值。

## 5. 隔离与互斥（保护数据可信度的唯一手段）

1. **实施隔离**：每个 EXP 在独立 worktree 工作（`git worktree add
   ../.worktrees/EXP-NNNN-slug -b exp/EXP-NNNN-slug`）。主仓 checkout 永远停在
   stable。合并回主线必须走 PR + 本地四门。
2. **测量互斥**：一切产生 events/sec 数字的命令必须包在
   `ops/bin/bench-lock exec -- <cmd>` 里。锁是全队单例，跨 worktree 生效
   （锁目录默认 `<主仓>/out/bench.lock`，用 `BENCH_LOCK_DIR` 指到共享路径）。
3. **构建串行**：Docker build 不同时进行（本地 colima/docker 资源争用会拖垮
   构建与计时）。镜像 tag 规则：`track3-exp-NNNN:<arch>`；stable 只以 digest 引用，
   永不覆写。
4. **证据纪律**：`execution.json` / `measurement.json` 在保留策略清理前已落盘
   （runbook 既定行为）；EXP 结论引用证据必须带 sha256。
5. **代码冻结窗**（EXP-0001 血泪教训）：`freeze → run → assess → noise-report →
   decide` 必须在**同一个代码冻结窗**内完成。窗口内禁止提交任何触及
   `identity()` 覆盖面的文件（`baselines/`、`qfbench2_track_simulation/`、
   `throughput/`、`scripts/` 下的 .py/.pyx/.patch，两个 Dockerfile，`simulate`、
   `simulate-batch`，`templates/trace_column_registry.json`）——B2 证据在 freeze 后
   90 分钟因代码提交永久失去 decide 资格。文档与 `ops/` 不在覆盖面内，可正常提交。
   窗口内如需改代码：先 decide，再改。

## 6. 硬件与口径

- 本地计时主机：`colima-t3-arm64`（native aarch64 Linux，容器 4 CPU / 16 GiB，
  与单元卡 `cpus=4 / memory=16G` 一致）。**它是相对测量仪，不是官方计时器。**
- 口径红线：本地 paired A/B 结论 = 相对证据；官方 ranked 计时 = 组织方 runner 墙钟
  **含容器启动**，且要求 repeat 间字节一致（README §How throughput is measured）。
- arm64→amd64 外推风险：舰队是 x86-64 + B200。凡涉及 ABI/编译器/向量化的结论，
  终赛前必须在 amd64 通路复核（当前缺此通路，属已知缺口，见 PRIORS §E）。
- Development 提交反馈 `rankable=false`，是练习成绩，不是官方计时（AGENTS.md）。

### 6.1 测量主机运维（EXP-0002 实测固化，VM 重启后必读）

- **启动**：`colima start t3-arm64`；docker 访问用
  `DOCKER_HOST=unix:///Users/feng/.colima/t3-arm64/docker.sock`（勿改全局 context）。
- **控制面必须在 VM 内运行**（EXP-0002 教训）：资格测量（freeze/run/assess/decide）
  在 macOS 上执行会记录 `system=Darwin, native=False` 的宿主身份，直接毒化计划
  （`not-native-qualification`）。runbook 开篇即此意："Run the controller on the
  native Linux host"。VM 内已备工具链：`~/venv-t3`（Linux py3.13 + qfbench2-common
  v2.4.3 + scipy/pandas/pyarrow/numpy，uv 重建约 3 秒）。**数值库版本是 holdout
  验证身份的组成部分**（`validation_identity()` 含 pandas 等版本）：venv-t3 必须
  `pandas==3.0.5`（与既有 holdout 证据链一致；uv 默认装最新 3.0.6 会让 assess 以
  `stale heldout validator evidence` 拒收）。重建配方：
  `uv venv ~/venv-t3 && uv pip install --python ~/venv-t3/bin/python
  "qfbench2-common[data] @ git+https://github.com/Agenthon-2026/Agenthon2026-public.git@v2.4.3#subdirectory=common"
  "pandas==3.0.5" scipy pyarrow numpy`（出网需 `HTTPS_PROXY=http://host.lima.internal:7890`）。VM 内执行示例：
  `colima ssh -p t3-arm64 -- bash -lc 'cd /Users/feng/.../track3-simulation-public &&
  ~/venv-t3/bin/python scripts/benchmark_candidates.py freeze …'`。仓库经 /Users
  挂载同路径可见；docker socket 走 VM 默认 `/var/run/docker.sock`（勿设 DOCKER_HOST）。
  仅 macOS 侧步骤：`prepare_submission.py`（urlopen 需 `HTTPS_PROXY=127.0.0.1:7890`，
  匿名拉取需 `DOCKER_HOST` 指向 t3-arm64 VM）。
- **scratch 卷（不持久，VM 重启必挂）**：
  `colima ssh -p t3-arm64 -- sudo sh -c 'fallocate -l 10G /var/lib/docker/t3-arm64-scratch.img
  && mkfs.ext4 -m 0 -F -q /var/lib/docker/t3-arm64-scratch.img
  && mount -o loop /var/lib/docker/t3-arm64-scratch.img /mnt/t3-scratch
  && chmod 1777 /mnt/t3-scratch'`
  （若 img 已存在先 `umount`+`losetup -d`+`rm`；**chmod 1777 不可省**——ext4 默认
  root 755，控制面以普通用户建 `t3_in_*` 临时目录会 PermissionError。容量必须 ≤
  freeze 的 `--disk-gib` 预算，否则 run 拒跑）。
- **disk 预算约定（EXP-0002 实测）**：arm64 实验一律 `freeze --disk-gib 64`——这是
  B2 政策，也是 `validate_caps` 给 arm64 的唯一放宽档；scratch 10 GiB 对当前
  roster 余量 200×（B2 全部 864 条记录单 run 峰值仅 0.05 GiB）。**例外**：若
  EXAMPLE 恢复 1h 版，单 run trace ≥10 GiB（实测 24.5 min 写 9.73 GB 仍未完），
  scratch 需 ≥32 GiB 且窗口约 +66 min×6——本地 roster 禁止，见 PRIORS §E。
- **本地 roster 约定（B2 部落知识正式化）**：资格测量前必须确认
  `units/t3-EXAMPLE-vectorized-matching/scenario.json` 处于 **5s 覆盖态**
  （md5 `9c77997023ed522536d9f7a71eaad8d3`，真迹存于
  `out/b2-qualify/smoke-io/bench2/t3-EXAMPLE-vectorized-matching/track3-b2_opt-coldstart/0/in/scenario.json`；
  上游 1h 版存于 `out/b2-qualify/exemplar-scenario-upstream-1h.json`）。覆盖是
  未提交工作区改动，**窗口结束立即 `git checkout` 恢复**；manifest 校验和随覆盖
  失效属预期（harness 运行时不验 manifest）。EXAMPLE 是否计入官方名册悬置中，
  任何 1h 本地证据只作专项参考，不进晋级链条。
- **出网代理**：docker.io 在该网络被 DNS 污染；dockerd 代理已固化在
  `/etc/systemd/system/docker.service.d/proxy.conf`（指向宿主机
  `host.lima.internal:7890`）。docker build 的 RUN 步骤需
  `--build-arg HTTP(S)_PROXY=http://host.lima.internal:7890`。代理不可用时先查宿主机
  7890 端口服务。
- **ghcr 凭证在 macOS 侧**（EXP-0003 教训）：VM 内 `~/.docker/config.json` 不存在，
  VM 内 `docker push` 一律 `unauthenticated`。推送必须从 macOS CLI 执行：
  `DOCKER_HOST=unix:///Users/feng/.colima/t3-arm64/docker.sock docker push …`
  （凭证走 macOS `~/.docker/config.json`，daemon 经 VM 代理出网）。
  `buildx imagetools` 等 CLI 直连 registry 的操作在 macOS 侧需
  `HTTPS_PROXY=http://127.0.0.1:7890`。
- **磁盘纪律**：vdb1 126G 是全部预算（镜像 + scratch + 构建缓存）。b2 时代曾因
  挂载卷泄漏（17.96GB）+ 废弃 scratch.img（33G）写爆；每轮实验后
  `docker volume prune -f`，旧实验镜像只留 ghcr 可回拉的。
- **已知问题（2026-09-22，不阻塞）**：vdb1 有幻影占用——df 报 74G used，du 全树
  可见仅 ~15G，无 deleted-open 句柄（lsof +L1 空）、无多余 loop。疑似多次硬关机
  留下的 ext4 孤儿块；下次维护窗可 `umount && fsck` 回收。另：现行 scratch.img
  在挂载状态下被删过文件名（loop 持有，功能正常），VM 重启后按上条配方重建即可。


### 6.2 amd64 交付验证通路（EXP-0003 建成，2026-09-22）

- **定位**：arm64 = 测量主机；amd64 = 交付目标架构（官方 Runner + gVisor）。本通路覆盖
  构建 / 语义 / 分发；**QEMU 数字一律禁止作计时证据**，计时结论只在真实 amd64 硬件
  产出（门 4 选项簿记见 `ops/experiments/EXP-0003-amd64-pathway.md`）。
- **交叉构建**（VM 内，QEMU，数十分钟；前提是 binfmt 有 `qemu-x86_64`，缺失则
  `sudo apt-get install -y qemu-user-static`，VM 重启后复查）：
  `docker buildx build --platform linux/amd64 --build-arg HTTP_PROXY=http://192.168.5.2:7890
  --build-arg HTTPS_PROXY=http://192.168.5.2:7890 -t <tag>:amd64 --load .`
  根 Dockerfile 基镜像 digest（`528257d4…`）为 multi-arch OCI index，双架构零代码改动。
- **语义冒烟**（VM 内，QEMU，仅语义；持 bench-lock）：
  `~/venv-t3/bin/python -m throughput.run_unit --image <tag>:amd64 --unit
  units/t3-s001-price-time-priority --runs 1 --no-discard-warmup --timeout-sec 900
  --run-as-host-user --keep-output <dir>/kept --out <dir>/record.json`
  （batch 单元换 `units/t3-gbatch-hetero-mix` + `--timeout-sec 1800`）。
  **`--run-as-host-user` 不可省**——对齐 `benchmark_candidates.py:436` 的资格配置；
  省略时容器以 root 写输出子目录，harness 清理触发 CPython 3.13 tempfile
  `_resetperms` 的 PermissionError（是 harness 故障，不是镜像缺陷）。验收：rc=0 +
  三件套（trace.parquet / events.json / message_trace.parquet 按卡要求）。
- **multi-arch 合并**（macOS 侧，`HTTPS_PROXY=127.0.0.1:7890`）：
  `docker buildx imagetools create -t <ghcr>:<multi-tag> <ghcr>:<arm64-tag> <ghcr>:<amd64-tag>`
- **匿名拉取验证**：ghcr 匿名 token + GET manifest（完整命令见 EXP-0003 档），
  要求 HTTP 200 且平台集合正确。
- **现行产物**（HEAD `7d41ed5`）：amd64 tag `:exp-0003-amd64-7d41ed5`
  （image `sha256:96dcbeea…`）；multi tag `:exp-0003-multi-7d41ed5`
  （index `sha256:2f3de103…`，arm64 + amd64）。终赛 amd64 交付直接引用 multi 或
  amd64 tag 即可；新候选镜像按本节流程同法产出。
- **真实 amd64 校准**：推荐云 Spot 单窗 <$1（规格/报价/ runsc 对照见 EXP-0003 门 4
  簿记），凭证与预算待统帅批准；未批前此项 parked（PRIORS §E）。


## 7. 统帅决策点（参谋只有建议权）

| # | 决策 | 触发依据 |
|---|---|---|
| D1 | 消耗一次 Development 上传 | 见 QUOTA.md 触发规则 |
| D2 | 路线级转向（如纯 CPU 重写 vs 混合扩展的取舍） | 参谋提交 roofline 对照簿记（调研 §6.2 方法） |
| D3 | 终赛唯一提交的镜像选型 | sealed 最坏情况证据 + 稳定备胎对照 |
| D4 | GPU 专项奖是否投入 | 主排名不受影响的证明 + 额度余量；当前默认：**不投入**（PRIORS A4） |
| D5 | 本文件与四门阈值的修订 | 实验噪声带实测回报后 |

## 8. 红线速查（派活前逐条过）

- 不碰 `AGENTS.md` "Never touch" 清单；不引入 sealed 信息入仓。
- 命中 PRIORS §A/§B/§C 任一条 → 直接驳回。
- 不在无 EXP 编号下改代码；不在无 bench-lock 下计时；不以自报代替复核。
- 不修改参考 trace 让候选通过（runbook 明令）；不微调容差常数过关。
- 提交动词只用 `simulate` / `simulate-batch`；事件类型字符串以
  `templates/trace_column_registry.json` 为准。
