# EXP-0003 — 建立 amd64 交付验证通路（multi-arch 构建 + QEMU 语义兜底 + 真实硬件选项簿记）

- **编号**：EXP-0003
- **立项日期**：2026-09-22
- **状态**：running
- **承办**：参谋（统帅 2026-09-22 批准方向）
- **关联**：PRIORS §E（amd64 通路缺失）；`docs/SUBMISSION-READINESS.md`（amd64 单独验收要求）；EXP-0002 结论遗留问题 2

## 假设（可证伪）

交付 Dockerfile（根 `Dockerfile`）无需任何代码改动即可产出 linux/amd64 镜像
（基镜像 digest `528257d4…` 已证实为 multi-arch OCI index，含 amd64 variant
`b1add8a6…`）；该镜像在 QEMU x86_64 下语义冒烟全绿。**反证条件**：构建暴露
amd64 专属 pin / 依赖缺口 / 编译失败 → 实验转为产出精确缺口清单并暂停升级。

## 依据

- PRIORS §E：amd64（舰队真实架构）与本地 arm64 相对性能差无任何证据，终赛前必须建通路。
- 2026-09-22 侦察：base digest 为 `oci.image.index`（multi-arch）；VM 已装
  qemu-user-static，`qemu-x86_64` handler 注册，amd64 容器试运行 `uname -m` → `x86_64`。
- 本队无自有 amd64 硬件（ssh 仅 github）；真实硬件属统帅决策项（成本/凭证），
  官方 Development 队列是 amd64 通路但消耗冻结中配额——QEMU 为零成本语义兜底。

## 改动范围（白名单）

- `ops/**`（实验档 / PROTOCOL / SCOREBOARD / PRIORS）
- `out/exp-0003-*/`（证据，gitignored）
- VM 环境配置（qemu-user-static，已落地）
- ghcr：只新增 tag，严禁覆盖既有引用（尤其 stable `607bec51…` 的既有 tag）
- **禁触**：identity() 哈希集内全部代码文件；`units/**`。若构建暴露缺口必须改
  Dockerfile → 立即暂停并升级统帅（改动 → 新 identity → 需重走 EXP-0002 链条）

## 验收线（先写死，后动手）

- [ ] 门 1（构建）：`buildx --platform linux/amd64` 构建成功，`track3-exp-0003:amd64`
  落地并通过 docker 匿名拉取验证（ghcr 推送后）
- [ ] 门 2（语义冒烟）：QEMU 下 ≥2 代表单元（单场景 `t3-s001-price-time-priority`
  + 1 个 batch 单元）rc=0 且产物三件套齐全（trace.parquet / events.json /
  message ledger 按卡要求）
- [ ] 门 3（流程固化）：PROTOCOL §6 增补 amd64 通路章节（构建 / 冒烟 / 推送 /
  配额含义，命令全文可复跑）
- [ ] 门 4（硬件通路）：向统帅提交真实 amd64 选项簿记（云规格+时价 vs Development
  配额消耗）→ 获批则执行校准测量落盘相对因子；未获批则本项 parked 并记录
- [ ] 附（禁止项）：QEMU 数字**不做任何计时结论**（仿真时钟失真），仅作语义证据

## 测量协议

- 本实验不产生排名数字；QEMU 冒烟仅记录 rc 与产物完整性
- 冒烟执行持 `ops/bin/bench-lock`（与测量实验互斥，防宿主状态互相污染）
- 镜像：`track3-exp-0003:amd64`（QEMU 构建）对照 stable arm64 `607bec51…`

## 原始记录（工兵自报，未复核）

### 2026-09-22 侦察（已复核入"依据"）

- `imagetools inspect python:3.11-slim-bookworm@sha256:528257d4…` → `oci.image.index`，
  amd64 manifest `b1add8a6f2aca6bc…` 在列
- `apt install qemu-user-static`（VM）→ binfmt 注册 `qemu-x86_64`；
  `docker run --platform=linux/amd64 python:3.11-slim-bookworm uname -m` → `x86_64`

## 结论（参谋复核后填写）

**结果：accepted（门 1–3 全过；门 4 parked 移交统帅）。**

- 门 1 ✓：amd64 交叉构建成功（`track3-exp-0003:amd64`，182MB，arch=amd64），根
  Dockerfile **零代码改动**（基镜像 digest 为 multi-arch index，反证条件未触发）；
  推送 ghcr `:exp-0003-amd64-7d41ed5`（image `sha256:96dcbeea4a5fe7cc…`），
  **匿名拉取验证 HTTP 200**（ghcr 匿名 token 流程，平台集合正确）。
- 门 2 ✓：QEMU 语义冒烟两单元 rc=0——s001（ledger 必须）三件套齐；
  gbatch-hetero-mix 5 子场景三件套齐、total_events=56,712。首轮 gbatch 故障定性为
  冒烟配置偏离资格配置（漏 `--run-as-host-user`），非镜像缺陷；已条令化（§6.2）。
- 门 3 ✓：PROTOCOL §6.2 固化构建/冒烟/推送/multi-arch/匿名验证全流程；
  §6.1 增补 ghcr 凭证位置（macOS 侧）教训。multi-arch tag
  `:exp-0003-multi-7d41ed5`（index `sha256:2f3de103…`，arm64+amd64）已推送。
- 门 4 parked：硬件选项簿记（云 Spot 单窗 <$1 推荐 / Development 队列终验 / QEMU
  仅语义）已交付统帅；凭证与预算获批前，相对性能校准因子挂 PRIORS §E。
- **决定**：accepted。amd64 交付通路自本实验起存在；终赛 amd64 引用已可用。
- 证据指针：`out/exp-0003/`（build-amd64.log、smoke-{s001,gbatch}-record.json、
  push-amd64.log、kept 产物）；ghcr 两 tag 及上述 digest。

## 死路登记（仅 rejected 必填）

<无>

### 2026-09-22 门 4 簿记：真实 amd64 硬件选项（待统帅决策，未获批前 parked）

- **Option A — 云 x86_64 按需/Spot VM（推荐）**：画像 = 4 vCPU x86-64 + 16 GiB（官方卡
  caps）+ docker + ghcr 匿名拉取，可选 runsc（gVisor）复核。候选规格：AWS c7i.2xlarge /
  c6i.2xlarge（8C/16G，cgroup 限 4C）、GCP e2-standard-4（4C/16G）、Azure D4as_v5。
  成本量级：按需 $0.15–0.35/h、Spot $0.05–0.12/h（下单前须复核时价）；单校准窗
  （环境 + 全 roster 或 24 单元子集）≈1–2h → **单窗 <$1**。fleet CPU 型号未公布，
  型号差本身就是校准因子的一部分。数据安全：镜像与公开单元本即 public，sealed 不上云。
  产出：`--execution-platform linux/amd64`（caps 走 10 GiB 档）同窗 freeze/run/assess，
  相对因子落盘 `out/exp-0003-amd64-calib/`。
- **Option B — 官方 Development 队列**：即真实 amd64+gVisor 计时（rankable=False），
  但消耗冻结中配额且历史次数未知（QUOTA 缺口）。定位：终赛前 1–2 次终验，不做例行。
- **Option C — QEMU 全 roster（本机零成本）**：本实验内建；仅语义证据，计时失真
  5–20× 且非线性，**禁止任何计时结论**。
- **推荐**：A 作例行校准（每候选一窗，<$1），B 仅终验，C 作日常语义回归网。
  决策点（云账号/凭证/预算归属）在统帅。
- **构建任务**：amd64 交叉构建进行中（本地任务 bash-o9k6m3ob，日志
  `out/exp-0003/build-amd64.log`）；早期检查：x86_64 manylinux 轮子下载正常。
- **gVisor 注记**：repo 实测 Python 事件循环在 gVisor 下开销为噪声级
  （baselines/README §3：allocation +0.3%、heap −0.9%）；但我方引擎是 parquet 流式
  I/O 密集，gVisor 的 I/O 拦截开销在该路径无公开数据——Option A 环境建议附带
  runsc 对照组（docker 加装 runsc runtime 即可，成本为零）。

### 2026-09-22 门 1/门 2 中间记录
- **门 1 构建**：`buildx --platform linux/amd64` 成功（任务 bash-o9k6m3ob，exit 0），
  镜像 `track3-exp-0003:amd64`（arch=amd64，182MB）。**Dockerfile 零改动，反证条件未触发。**
- **门 2 冒烟 a**：s001（matching-engine-semantics，ledger 必须）QEMU 下 rc=0，
  三件套齐（trace.parquet 6.4KB / message_trace.parquet 15KB / events.json）。
- **门 2 冒烟 b 首跑故障**：gbatch-hetero-mix harness rc=1——**非镜像缺陷**：容器成功
  （kept 输出含 batch_events.json + 5 子场景目录，total_events=56,712，子目录三件套齐），
  故障在 harness 清理 root 拥有的输出子目录（CPython 3.13 tempfile `_resetperms` chmod
  传播 PermissionError）。根因：我的冒烟漏了 `run_as_host_user=True`——资格路径
  （benchmark_candidates.py:436）是带此旗标的。教训：**冒烟/手工调用必须对齐资格运行时
  配置**。已用 `--run-as-host-user` 重跑（任务 bash-5dceele1），并 sudo 清理 /tmp 残留。
