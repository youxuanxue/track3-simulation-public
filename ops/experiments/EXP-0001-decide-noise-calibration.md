# EXP-0001 — 补 B2 确认实验 decide 落账，并用既有 paired 证据实测校准门2噪声带阈值

- **编号**：EXP-0001
- **立项日期**：2026-09-21
- **状态**：accepted（流程实验，无代码改动；产出 = 阈值修订 + 流程修正 + 风险登记）
- **承办**：参谋（主会话）
- **关联**：SCOREBOARD 态势区"decide 落账记录待补"；PROTOCOL §4 门2 暂定阈值（D5）；QUOTA 对账缺口

## 假设（可证伪）

1. B2 确认证据（`out/b2-qualify/b2-run-confirm/`）可以事后补跑 `decide` 完成 stable 落账。
2. PROTOCOL §4 门2 暂定阈值（提升 ≥3%、回归 <2%）高于测量噪声带，即该门能分辨
   它声称要分辨的效应。

两条均被实测**证伪**。

## 依据

- 调研 §6.7（失败结论资产化）与 §6.5（聚合指标下不许单点回归）要求验收阈值有实测
  噪声带背书，而非拍脑袋。
- 未命中 PRIORS 任何条目；本实验不改模拟器代码。

## 改动范围（白名单）

- `ops/`（本档、PROTOCOL、SCOREBOARD、`bin/noise-report`）
- `out/exp-0001-*`（证据文件）
- 只读使用：`out/b2-qualify/`、`scripts/benchmark_candidates.py`

## 原始记录（命令与原始输出指针）

1. `python scripts/benchmark_candidates.py assess --evidence out/b2-qualify/b2-run-confirm/evidence.json`
   → **拒绝**：`stale code/toolkit/scorer evidence: code_sha256`
   （`out/exp-0001-assess-confirm.err`；stdout 空文件 `out/exp-0001-assess-confirm.json`）。
2. `ops/bin/noise-report --run-dir out/b2-qualify/b2-run-confirm`
   → `out/exp-0001-noise-report.md`（72 单元 × 2 臂 × 5 组，720 条 measurement）。
3. 时间线复核（git log）：确认证据冻结于 2026-09-19T00:05:10Z；代码提交 `97821c3`
   落于同日 01:34Z——**冻结后 90 分钟内证据即过期**。

## 结论（参谋复核）

### 发现 1：decide 不可补登，根因是流程而非工具

`assess` 前置校验 `validate_plan(plan, current=True)` 要求证据身份与当前代码一致
（`scripts/benchmark_candidates.py:287-297`）。B2 证据冻结后，代码在 90 分钟内
（`97821c3`）及之后（`87ce16f` #14、`7bc5111` #15）三次变更，旧证据**永久失去**
decide 资格。`identity()` 只哈希代码文件（baselines/qfbench2_track_simulation/
throughput/scripts 的 .py/.pyx/.patch + 两个 Dockerfile + simulate  shim + trace
registry），文档与 `ops/` 不影响证据时效（`benchmark_candidates.py:99-114`，
设计注释明确："Documentation-only changes need not invalidate evidence"）。

**含义**：当前 hash 链历史为空（`out/**/[0-9]*.json` 不存在），stable 身份
（B=`track3-b2@sha256:ac2b7848…`）是"测量最优、未经正式链条"。不可补登、不可伪造
（runbook 明令禁止手写 pass）。重建链条的唯一正路：**在 HEAD 冻结一次新鲜
baseline/confirmation，并在同一窗口内完成 decide**（→ EXP-0002 建议）。

### 发现 2：门2 暂定阈值低于噪声地板，已按实测重写

配对 log(B/A) 组间噪声（5 组交替，colima-t3-arm64）：

| 层 | 实测 | 旧阈值 | 新阈值（本实验校准） |
|---|---|---|---|
| 聚合（算术平均比） | 组间 sd 0.687%，中位比 95% 带 **±0.77%** | 提升 ≥3% | 提升 **≥2%**（≈2.6× 噪声带，保留且可分辨） |
| 单元（中位比） | sd median 6.9% / p90 13.1%；95% 带 **±7.8% / ±14.7%** | 回归 <2% | ≤0.85 确认回归；0.85–1.0 可疑区**加测 5 组**合并再判；≥1.0 通过 |

旧"回归 <2%"在 ±8~15% 的单元噪声带下必然假警报连发；旧"提升 ≥3%"相对聚合噪声
过保守。新阈值已写入 PROTOCOL §4（D5 决策点，统帅可否决）。

### 发现 3（计划外，重大）：`t3-gbatch-hetero-mix` 的 B 胜结论不可信

该单元配对 sd **54.5%**（95% 带 ±61%），且两个汇总口径**符号相反**：中位比
+21.2% vs 配对 log 均值 **−5.9%**。"72/72 B 全胜"在此单元不成立也不可否——
测量本身不足以判决。任何 batch 结构优化必须对该单元加测（≥10 组）方可下结论。
已登记 SCOREBOARD 态势区与 PROTOCOL §4 测量纪律。

### 发现 4：噪声结构确认配对设计有效

单臂独立噪声（median σ 8.0%、p90 22.6%）远大于配对后比值的聚合噪声（±0.77%），
证明交替 A/B 配对消除了主机漂移——**paired 协议必须原样保留**，聚合级 1–2% 的
效应在 5 组下可分辨。

- 四门：本实验为流程/测量实验，门 1/3/4 不适用；门 2（阈值自身）即本实验校准对象
- **决定**：accepted
- 证据指针：`out/exp-0001-assess-confirm.err`、`out/exp-0001-noise-report.md`、
  本档引用的 git 时间线（`97821c3`/`87ce16f`/`7bc5111`）

## 后续（转入 EXP-0002 建议）

1. HEAD 冻结新鲜 baseline（单镜像）→ run → assess → decide **同窗完成**，建立
   hash 链与 stable 身份，同时测出 #14/#15 的未计量收益。
2. 每次确认实验 assess 后强制跑 `ops/bin/noise-report` 重校准（已写入 PROTOCOL §4）。
3. `t3-gbatch-hetero-mix` 加测专项（≥10 组）判明符号。

## 死路登记

无代码死路。流程教训已固化进 PROTOCOL §5.5（代码冻结窗），不重复登记 PRIORS。
