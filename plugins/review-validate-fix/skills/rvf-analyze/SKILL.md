---
name: rvf-analyze
description: Use when the user asks for a post-mortem of a finalized RVF run — narrative summary plus issue ↔ patch causality scaffolding. Read-only complement to $review-validate-fix; never starts a new review, edits source, or generates handoff.md.
---

# RVF Analyze

本 skill 仅用于对一次**已 finalize 的 RVF run** 做事后复盘。它读
`<run_dir>/artifacts/` 下由 `rvf_run_finalize` 已经落盘的 trajectory + workspace
diff + reviewer 产物，写出叙事化的 `analysis/summary.md` 与
`analysis/causality.json`。

> ⚠️ 本 skill 与 `review-validate-fix` 严格分离。即便上下文里同时出现
> review/validate/fix 的叙事，本 skill 的唯一职责是**复盘已 finalize 的 run**：
> 不得启动新 review、不得改源码、不得跑 validate/fix、不得生成 handoff.md、
> 不得自动开 PR 或派生新 task。

本 skill 只应由用户显式调用：`$rvf-analyze`、`$rvf-analyze latest`、
`$rvf-analyze <run_id>` 或 `$rvf-analyze /abs/path/to/run_dir`。
不要因为上下文出现 `$review-validate-fix` 叙事自动转到本 skill，反之亦然。

## 入口与脚本

唯一入口脚本（确定性后端）：

```
plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_analyze.py
```

脚本住在 `review-validate-fix` 的 `scripts/` 目录里（与 `rvf_run_finalize`、
`trajectory_capture` 等在同一 import 域），不要复制到本 sister skill 下。

调用示例：

```bash
python3 plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_analyze.py \
  [--run-id <id> | --run-dir <path> | --latest | <positional target>]
```

如果当前 cwd 不在仓库根，使用脚本的绝对路径。

## 退出码语义（必须按此分支）

脚本 stdout 总是结构化 JSON。退出码语义：

| 退出码 | 含义 | agent 处理 |
|---|---|---|
| `0` | 已成功 scaffold `analysis/summary.md` 与 `analysis/causality.json` | 进入"叙事补全"流程 |
| `2` | classification == `running`（run 看起来还在跑） | **询问用户**：是否仍要复盘？得到肯定后追加 `--force` 重入 |
| `3` | classification 是 `orphan_candidate` 或 `cancel_without_lock`，需要决策 | **询问用户**："这个 run 看起来未完成，要现在 lazy finalize 一次再分析吗？"；yes → 追加 `--auto-finalize-orphan` 重入；no → 追加 `--decline-finalize` 重入 |
| `5` | 上轮已传 `--auto-finalize-orphan` 但 finalize 自身抛异常 | **不要再发起同一询问**；读 stdout 的 `error` 字段把失败原因转给用户，建议 `--decline-finalize` 降级或排查 finalize_run |
| `4` | 解析 run_dir 失败 | 反馈错误信息给用户，让其确认 `--run-id` / `--run-dir` |

退出码 2/3 上**不得**自作主张选择，必须用中文向用户提问。

## 复盘任务

成功 scaffold 后（退出码 0），agent 的本职是**叙事补全 + 因果归属**：
- 在 `analysis/summary.md` 各节填 `<!-- TODO(rvf-analyze): ... -->` 占位
- 在 `analysis/causality.json::issues[].candidate_patch_call_ids` 里填上每条
  issue 对应的 patch call_id 列表

详细操作流程、节标题、启发式判断规则、边界情况清单，全部在
`references/rvf-analyze.md`。**先读 reference 再操作**。

## 不做的事

- 不修改 `<run_dir>/` 下除 `analysis/` 之外的任何文件
- 不调用 `finalize_run`（脚本会在 `--auto-finalize-orphan` 时自己调）
- 不基于复盘结论改源码、开 PR 或派生新 task
- 不读 `~/.codex/sessions/`；所有需要的 trajectory 已在 `run_dir/artifacts/trajectory/` 下
