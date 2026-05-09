---
name: review-validate-fix
description: Use only when the user explicitly invokes $review-validate-fix or asks for the RVF post-work loop with review_only, validate_fix, skip-review, or no-handoff modes.
---

# Review Validate Fix

本 skill 只处理显式 `$review-validate-fix` 调用。`agents/openai.yaml` 的 `policy.allow_implicit_invocation` 必须保持 `false`。

不要因为上下文提到 review、fix、handoff 或 Stop hook 就自动启动 RVF。

## 正常入口

1. 在目标仓库运行 `git status --porcelain`，或使用 `scripts/review_validate_fix_gate.sh <repo>`。
2. 如果仓库 clean，只有用户本轮明确给出 manual review scope 时才继续；否则中文说明没有可审查改动并结束。
3. 写一份 scope-of-work / session context 文件，说明用户意图、本轮实际完成的工作或 manual scope、需要审查的文件、逐文件编辑明细、已跑验证、关键取舍和不确定点。
4. Scope-of-work 不要只列 created/modified/deleted 文件。
5. 若当前 prompt 或环境已提供冻结的 RVF run artifacts，复用既有 `review-env.sh`、scope contract 和 review packet；不要新建 run。
6. 否则运行 `scripts/prepare_review_run.py --repo <repo> --session-context <file>`；在 transcript 或 manifest 可用时传入对应参数；在已知主修改文件、manual scope 文件或背景 WIP 时，分别传入 `--primary-file` / `--background-file`，避免无 manifest 时降级到 `manual-all-uncommitted` 把背景 WIP 纳入 `primary_files` / `fix_allowlist`。
7. 后续使用脚本生成的 `review-agent-context.md` / `review-env.sh` / prompt artifacts；不要手写 `RVF_*` export block，也不要把脚本内部 env/config 当成 agent 需要记忆的状态机。

## 模式

- `full`：默认完整流程，执行 prepare -> double review -> merge -> validate/fix -> handoff。
- `review_only`：只读 review pass，只写 canonical review result artifact，不修复、不 handoff。
- `validate_fix`：只处理主会话分配的 canonical issue 包，不重新 review、不扩大范围、不 handoff。
- `research_checkpoint_no_handoff`：不是 review loop，只输出用户要求的研究 checkpoint。
- 用户显式 `skip review` 时不启动 reviewer；若同时没有 issue list，则不进入 validate/fix。
- Handoff 默认开启；用户显式 `no handoff`、子 pass 或 research checkpoint 时关闭。

## Agent 边界

- 主会话负责 scope-of-work、模式选择、启动脚本、合并 reviewer artifact、分配 validate/fix 包和最终中文汇总。
- Reviewer 子代理使用 `prompts/reviewer.md` 的 self-contained prompt。它们不读取本 `SKILL.md`，不继承父线程历史，不读取另一路 reviewer 输出。
- Validate/fix 子代理使用 `prompts/validate-fix.md` 的 self-contained prompt。它们只处理分配给自己的 canonical issue 包。
- Review result protocol 的事实源是 `scripts/write_review_result.py` 和 `scripts/check_review_result.py`；自然语言 final prose 只作日志。
- Stop hook、backend selection、Kanban dispatch、GUI fallback、env/config 注入和 run ledger 字段由脚本拥有。正常 agent 不需要读取这些内部状态机，也不应把 env/config 手工传给脚本。

## Review

- 默认执行两个独立 review pass，除非用户显式跳过 review。
- Reviewer 必须以 `scope.contract.json` 为最终范围合同；session manifest 只是 ownership evidence / tracker audit context。
- `git diff HEAD` 是证据来源，不是默认 review scope。除非用户要求 full diff review，否则只审查合同范围及其直接连带影响。
- 每个 reviewer artifact 必须通过 `scripts/check_review_result.py` 校验；artifact 缺失、schema invalid、excluded path、clean/issues/request 混写都不能当作合格结果。
- 合并 reviewer artifact 时使用 `references/review-merge-policy.md`。

## Validate / Fix

- `kind: no_issues` 进入 clean path。
- `kind: issues` 进入 validate/fix；每条 issue 先验证，再判定 `REAL`、`FALSE_POSITIVE` 或 `ELEVATE`。
- 默认 `full` 流程中，只要有可解析 issue list，主会话必须至少启动一个 `validate_fix` 子代理，除非平台没有子代理接口、用户明确要求本地执行，或只剩机械收尾。
- 按根因、文件区域、测试路径或决策前提把 issue 分组；最终汇总要说明 validate/fix 分组和结果。
- `ELEVATE` verdict 必须包含 `elevation-detail` fenced block；可用 `scripts/parse_elevation_detail.py` 解析。

## Handoff

- Handoff 默认写入当前 RVF run 的 `artifacts/handoff.md` 并持续更新。
- 最终回复前调用 `scripts/rvf_handoff.py open <handoff.md>`；最终回复第一行输出 `RVF_HANDOFF_FILE: <绝对路径>`，后面只用 1-3 句中文概括 reviewers 和 validate/fixers 的结果。
- Handoff 只写确认过的事实，不把背景 WIP 或其他 session 的改动混成本轮工作。

## 文档分层

- `prompts/`：脚本直接传给 reviewer 和 validate/fix 子代理的 self-contained prompt。
- `protocols/`：协议事实源索引；优先指向脚本/checker/schema，避免自然语言重复定义。
- `references/`：主会话正常执行时可能按需读取的材料，例如 review merge policy、review standards 和 handoff template。
- `internals/`：Stop hook、backend、Kanban、env/config、run ledger 等内部 workflow 说明；正常 agent 不读。
- `debug/`：脚本失败、用户要求排障或维护 runtime 时才读的 runbook。
- `setup/`：只用于用户明确要求配置或重配 external reviewer / MCP / agent 集成。
