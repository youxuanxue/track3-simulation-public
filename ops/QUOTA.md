# QUOTA.md — 提交配额账本

> 摘要：上传额度是全战役最稀缺资源，比 GPU 时间贵。每次消耗必须在此记账并由统帅签发。
> 本地验证与打包不占额度；**held / cancelled 上传也占额度**（SUBMISSION_CLI.md 明规则）。

## 额度（规则来源：README §Competition schedule and submission limits）

| 阶段 | 额度 | 窗口 |
|---|---|---|
| Development | **每天 5 次 + 全程共 20 次**（本 track，全队共用指定 CodaBench 账号） | 至 2026-10-12 23:59 AoE |
| Final + Verification | **每 track 仅 1 次提交** | 2026-10-13 ~ 10-25 23:59 AoE |

## 消耗台账（按上传时刻升序）

| # | 日期 | 镜像 digest | 本地依据（g1/evidence 指针） | 练习反馈 | 签发人 |
|---|---|---|---|---|---|
| A1（**已上传，反馈已回填**） | 2026-09-22 18:05 +0800 | `sha256:ac14f59c6b7fb86bb4c05f34935d174d9982ab761e10bfcec0eb1533e426367d`（amd64-only，HEAD 7d41ed5） | `out/exp-0004/development.g1.json`（G1 passed）；zip sha256 `13dadd3162cd…`；上传确认截图 `out/exp-0004/codabench-upload-confirmation.png` | **primary 1,447,360.0444（71/72，EXAMPLE container_crashed 计 0）**；逐单元分数 `out/exp-0004/dev-feedback-2026-09-22.json`；校准分析 `out/exp-0004/calibration.md` | 统帅（amd64 校准终验，EXP-0003 门 4 Option B） |

> ✅ **对账缺口已关闭（2026-09-23）**：A1 官方反馈页面显示"今日 1/5、累计 4/20"——
> 仓内仅 A1 留痕，**历史 3 次为仓外消耗**，无矛盾。**剩余额度 16 次**，上传冻结解除，
> 恢复按下文"上传触发规则"逐次签发。

### 本地证据（打包 ≠ 上传，仅备查）

- 2026-09-08 `submission/validation.json`：65/65 回归通过（developer profile，toolkit v2.3.1，注意：现 CI 已钉 v2.4.3）
- 2026-09-16 `out/development-arm64-1789536190.zip` + `.g1.json`：G1 passed，simulate 91,099 eps / batch 19,667 eps（arm64 local，rankable=false）
- `out/b2-qualify/development.zip`、`development-opt-io-fix.zip`：B2 轮打包件
- 2026-09-19 `out/b2-qualify/b2-run-confirm/`：opt-batch-fork(B) vs opt-io-fix(A) paired，72 单元 B 全胜，geomean +16.4%

## 上传触发规则（Development 阶段）

同时满足才向统帅提请签发一次上传：

1. 新 stable 相对**上一次已上传镜像**，本地 paired 算术平均提升 **≥10%**（暂定阈值，
   统帅可调）——小于此幅度的进步攒着，不浪费额度；
2. 验收四门全过（PROTOCOL §4），且 holdouts 通过时间距上传 <72h；
3. 距本次 Development 窗口关闭的剩余额度 ≥ 剩余计划里程碑数 + 2（保底冗余）；
4. 当日额度未达 5 次上限，且本次上传目的明确（验证口径 / 取得练习排名反馈 /
   注册候选镜像），不是"看看运气"。

## 终赛选型规则（Final 唯一一发）

- 候选集 = 所有通过四门的 stable 镜像；按 **sealed 最坏情况代理**（holdouts + 参数变体
  的最差家族表现）排序，不按公开集最好成绩排序（GPU MODE 公开/隐藏分背离教训，
  调研 §6.3）。
- 若最佳激进镜像的 holdout 最差家族表现相对其公开表现衰减 >15%，改用稳定备胎。
- 选型决议连同对照证据写入 `experiments/` 专项档，统帅签字。

## 计时点提醒

- 2026-10-12 23:59 AoE：注册与 Development 同时关闭——**最后一周的上传要预留
  排队与失败重试时间**，不要把第 20 次留到最后一天。
- 2026-10-13 ~ 10-25：Final 唯一提交窗口；组织方在同一阶段内完成验证，无单独
  参赛者验证提交。
