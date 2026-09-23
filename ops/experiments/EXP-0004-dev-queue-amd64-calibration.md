# EXP-0004 — amd64 校准终验：Development 队列取得真实 amd64+gVisor 反馈，产出校准因子

- **编号**：EXP-0004
- **立项日期**：2026-09-22
- **状态**：accepted（2026-09-23 归档；门 4 口径警示入档，EXAMPLE crash 拆为 EXP-0005 候选）
- **承办**：参谋（统帅 2026-09-22 签发：门 4 走官方 Development 队列终验，消耗冻结中配额一次）
- **关联**：EXP-0003 门 4 簿记（Option B）；PRIORS §E；QUOTA.md 上传触发规则 §4（目的=验证口径）

## 假设（可证伪）

stable（`607bec51…`，HEAD 7d41ed5）的 amd64 变体在官方 Development 队列
（真实 amd64 + gVisor，developer profile，rankable=False）跑通并返回每单元
events/sec 反馈；将该反馈与本地 arm64 配对中位（EXP-0002，`244,672.8` 聚合）
逐单元对比，可得 **arm64→amd64 校准因子分布**（geomean + 离散度）与 gVisor
开销估计。**反证条件**：队列运行失败 / 反馈缺单元 / 反馈与本地零相关
（|Kendall-τ| < 0.5）→ 校准不可用，转产出失败诊断。

## 依据

- EXP-0003：amd64 镜像已构建并 QEMU 语义验证；ghcr 匿名可拉。
- SUBMISSION_CLI：Development 反馈在组织方 amd64+gVisor worker 上执行——这正是
  终赛计时环境的最近似物（rankable=False，但架构/沙箱真实）。
- 配额：统帅签发一次；规则簿记见 QUOTA.md（对账缺口仍在，本表同步登记授权）。

## 改动范围（白名单）

- `ops/**`、`out/exp-0004-*/`
- ghcr：仅引用既有 tag，不新增不覆盖
- CodaBench web 上传：**由统帅本人执行**（指定账号凭证不在参谋侧）
- **禁触**：identity() 集内代码；`submission/candidate.json`（现钉 `468ec20b…`
  历史候选，是否换钉属统帅决策，不在本实验自动改）

## 验收线（先写死，后动手）

- [x] 门 1：amd64 候选 `verify-delivery` passed（匿名拉取 + 双动词离线执行，amd64 scope）
- [x] 门 2：`package` 产出 `development.zip` + G1 报告 passed（team key 只传路径）
- [x] 门 3：zip 内容复核（submission.json 摘要=digest 一致、team-claim 在场、无 Team Key 泄漏）
- [x] 门 4：统帅完成 web 上传后，反馈到手 → 逐单元校准因子落盘
  `out/exp-0004/calibration.md`（geomean / p10 / p90 + 每家族读数 + gVisor 注记）
- [x] 附：QUOTA.md 台账登记（授权行 + 实际上传时刻回填位）

## 测量协议

- 本地步骤零配额（verify-delivery / package 均为本地动作）
- 上传 = 配额消耗点：统帅在 CodaBench 页面提交 `out/exp-0004/development.zip`
- 反馈字段以组织方返回为准（per-unit events/sec，developer profile 自查口径）

## 候选镜像决议（2026-09-22，参谋建议，统帅可改）

- **引用**：`ghcr.io/youxuanxue/track3-simulation-public@sha256:ac14f59c6b7fb86bb4c05f34935d174d9982ab761e10bfcec0eb1533e426367d`
  （amd64-only tag index，amd64 内容 = stable `607bec51` 的同代码 amd64 构建）
- **为何不是 multi-arch `2f3de103…`**：`prepare_submission.py:181` 的
  `docker image inspect` 无 `--platform`，按宿主架构解析——arm64 主机 + 本地已存
  arm64 变体时 multi index 解析为 arm64，触发 `candidate must resolve to linux/amd64`。
  amd64-only 引用消除一切解析歧义（本机验证与官方 amd64 队列通吃）。

## 原始记录（工兵自报，未复核）

- 2026-09-22 verify-delivery 首跑：multi-arch digest 被 inspect 解析为 arm64 → 拒收
  （机制见上"候选镜像决议"）。改 amd64-only digest 重跑（任务 bash-pr30u7tc）。

## 结论（参谋复核后填写）

**accepted（2026-09-23）**。官方 Development 反馈到手：primary **1,447,360.0444**
（71/72 scored，EXAMPLE container_crashed 按 0 计），逐单元分数存
`out/exp-0004/dev-feedback-2026-09-22.json`。

1. **语义链路全过**：71 个 scored 单元零组织者侧失败——amd64 镜像在官方真实
   amd64+gVisor 链路上语义、格式、双动词全部合格。
2. **校准因子取得，但性质是"开销口径因子"**：官方 Dev（checked 自报吞吐，引擎内计时）
   ÷ 本地 host-wall 的比值 geomean **10.28**（p10 4.44 / p50 10.54 / p90 23.82，
   min 1.55=gb-mega / max 45.15=s001），与负载大小强负相关（固定开销摊薄效应）。
   **不能**用于把本地 eps 换算成 Final eps。
3. **排序一致性过验收线**：官方 vs 本地 Kendall-τ = **0.7360**（≥0.5），
   Spearman-ρ = 0.9095（n=71）→ 本地 host-wall 的相对优化方向可信，其绝对口径
   仍是 Final（Runner 实测）的最近代理，定位不变。
4. **EXAMPLE crash 分诊**（推断排序，待 EXP-0005 本地复现确认）：①磁盘 >10G
   （本地 1h 版 24.5min 已写 9.73GB 未完）②1800s launcher 超时（card 无 timeout
   字段）③内存（峰值 12.56GB，可能性最低）。crash 按 0 计 → primary 损失
   ≈ +20,385 eps（+1.41%）。
5. **配额对账关闭**：官方页面"累计 4/20"——历史 3 次为仓外消耗，**余 16 次**，
   上传冻结解除（QUOTA.md 已回填）。
6. **名册悬置终结**：EXAMPLE 确在官方名册且计分（PRIORS D1/D3 已回填）。

全文：`out/exp-0004/calibration.md`。后续：EXAMPLE 可接纳性攻关拆分为
EXP-0005 候选（先本地复现绑定约束，再定向修复）。

## 死路登记（仅 rejected 必填）

<无>

### 2026-09-22 门 1–3 通过（本地零配额）
- 门 1 ✓：verify-delivery passed（amd64-only digest；匿名 `--platform=linux/amd64` 拉取
  + 双动词离线执行 rc=0，n_events=24,751；QEMU 下运行）。证据 `out/exp-0004/verification/`。
- 门 2 ✓：package 产出 `out/exp-0004/development.zip`（1,027B）+ `development.g1.json`
  （scope G1 / passed / rankable=false，archive sha256 `13dadd3162cd…`）。
  注意：`--verification` 收 delivery.json 文件路径，不是目录。
- 门 3 ✓：zip = submission.json + team-claim.json；submission 的 image digest 与候选一致
  （`ac14f59c…`）、image_access=public、team_id=team-6bcef4bc…、phase=dev、无 models；
  team-claim 仅四键（descriptor_sha256/proof/schema_version/site_team_id），无 Team Key 泄漏。
- QUOTA.md 台账已登记授权行 A1（签发人=统帅，目的=amd64 校准终验），上传时刻与反馈待回填。
- **待统帅动作**：CodaBench web 上传 `out/exp-0004/development.zip`（指定账号）；
  反馈到手后参谋执行门 4 校准分析。

### 2026-09-22 18:05 +0800 — 统帅已完成 web 上传（A1）
- 上传确认：CodaBench "Agenthon 2026 – Track 3: Market Simulation Speed (Development)"
  页面（截图存 `out/exp-0004/codabench-upload-confirmation.png`）。
- 页面观察：Development Phase 进行中（倒计时 ~21 天）；leaderboard 显示
  "no participants" + "results will be published once the phase is over"——
  **公开排行榜按阶段结束才发布**；练习反馈预期在 "My Submissions" 详情页出。
- 分数未出：worker 队列执行中（ingestion 阶段上限 12h，预期远短）。
- **仍缺**：CodaBench 后台累计上传次数（对账缺口未补）；每单元反馈。
- 已设提醒：今晚 22:37 查运行状态/反馈/次数。

### 2026-09-23 — 官方反馈到手，门 4 通过，归档 accepted
- 反馈全文落盘 `out/exp-0004/dev-feedback-2026-09-22.json`（落盘校验：sum71 与
  官方 primary×72 逐位对账一致，crash 单元按 0 计）。
- 校准分析 `out/exp-0004/calibration.md`：比值 geomean 10.28（开销口径因子，
  非硬件因子）；Kendall-τ=0.7360 ≥ 0.5 过线；EXAMPLE crash 分诊三段论。
- 配额对账关闭（累计 4/20，余 16）；EXAMPLE 在官方名册坐实。
- EXAMPLE 可接纳性攻关拆分：EXP-0005 候选（待统帅签发）。
