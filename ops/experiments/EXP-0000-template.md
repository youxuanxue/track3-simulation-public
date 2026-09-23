# EXP-NNNN — <一句话假设>

- **编号**：EXP-NNNN
- **立项日期**：<YYYY-MM-DD>
- **状态**：proposed
- **承办**：<工兵标识 / 会话>
- **关联**：<上游 EXP / SCOREBOARD 杠杆行 / PRIORS 条目>

## 假设（可证伪）

改 <X> 后，单元集合 <Y> 的 paired 中位 events/sec 提升 ≥<Z%>，且其余单元回归
<<R%>（超出即 rejected）。

## 依据

<为什么认为成立：profiler 数据 / 文献 / SCOREBOARD 杠杆分析。引用 PRIORS.md 确认
本方向未命中死路；若命中，说明推翻旧证据的新证据。>

## 改动范围（白名单）

- `path/to/allowed/files...`

## 验收线（先写死，后动手）

- [ ] 门 1：65/65 公开回归全绿（`regression_suite/run_regression.py`）
- [ ] 门 2：paired A/B ≥5 组交替，目标单元集合中位提升 ≥<Z%>，其余单元回归 <<R%>
- [ ] 门 3：holdouts 参数集通过（`scripts/validate_candidate_parameters.py run`）
- [ ] 门 4：同镜像双跑输出字节一致（确定性复核）
- [ ] 附：stylized facts 四门不劣化（KS≤0.08 / ACF≤0.12 / Hill≤1.5 / depth JS≤0.10）

## 测量协议

- 持锁：`ops/bin/bench-lock exec ...`（未持锁的数字一律作废）
- 基线镜像：<stable digest>；挑战镜像：`track3-exp-NNNN:<arch>`
- 命令：<freeze/run/assess 具体命令，照 `docs/CANDIDATE-RUNBOOK.md`>

## 原始记录（工兵自报，未复核）

<日期 / 命令 / 原始输出指针>

## 结论（参谋复核后填写）

- paired 中位：A=<...> B=<...> B/A=<...>（算术平均口径 A=<...> B=<...>）
- 四门：1✓/✗ 2✓/✗ 3✓/✗ 4✓/✗
- **决定**：accepted / rejected / parked
- 证据指针：<out/ 路径 + 关键文件 sha256>

## 死路登记（仅 rejected 必填）

<一句话，已同步 PRIORS.md §D 第 __ 条>
