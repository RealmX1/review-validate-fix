---
name: review-validate-fix
description: Use only when the user explicitly invokes $review-validate-fix, /review-validate-fix, or :review-validate-fix, or asks for the RVF post-work loop with review_only, validate_fix, skip-review, or no-handoff modes.
---

# Review Validate Fix

本 skill 只处理显式 `$review-validate-fix`、`/review-validate-fix` 或 `:review-validate-fix` 调用。`agents/openai.yaml` 的 `policy.allow_implicit_invocation` 必须保持 `false`。

不要因为上下文提到 review、fix、handoff 或 Stop hook 就自动启动 RVF。

## 正常入口

**Hook-prepared 路径（默认）**：进入会话第一条用户消息若含 `RVF_DISPATCH=token=...` 或显式触发字面量（`/review-validate-fix`、`$review-validate-fix`、`:review-validate-fix`），UserPromptSubmit hook 会自动调用 shared prepare 入口（`prepare_review_run.prepare_run_from_prep_file()`）。开始任何工作前先 `cat $RVF_PREP_FILE`（或在 hook 输出的 `prep_file_path` 上 `cat`），确认 `rvf_run.shared_workflow_state.status == "completed"` 且 `artifacts` 字段齐全；齐全则**跳过**下方第 1 / 5 / 6 步，直接 source `$RVF_REVIEW_ENV` 并按既有模式继续。Hook 自动写过的 `startup-scope-of-work.md` 是最小 stub，主会话仍必须用真实 reasoning 内容覆盖该文件或新写一份 scope-of-work（步骤 3）。

> Manual same-session 触发（`/review-validate-fix` 等字面量在已激活的对话里直接发出）走的也是这条路径，但因为 hook 不修改用户 prompt、也不向 agent 进程导出 `$RVF_PREP_FILE`，hook 会通过 `hookSpecificOutput.additionalContext` 把 `prep_file` 路径与 `shared_workflow_state.status` 注入到主会话上下文里。看到 `RVF dispatch prep (post-user-prompt manual auto-prep): - prep_file: …` 这段提示后再 `cat` 该 prep 文件、source 其中的 `review_env`，然后按既有 review 流程继续。

**Fallback / 无 hook 路径**（hook 失败、`shared_workflow_state.status` 是 `failed` / `timeout` / `pending`，或环境无 hook 注册）：

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
- `kind: issues` 进入 validate/fix；每条 issue 先验证，再通过 `rvf_fix_attempt.py stop --status fixed|false_positive|elevated|failed` 写入完成状态。
- 默认 `full` 流程中，只要有可解析 issue list，主会话必须至少启动一个 `validate_fix` 子代理，除非平台没有子代理接口、用户明确要求本地执行，或只剩机械收尾。
- 按根因、文件区域、测试路径或决策前提把 issue 分组；最终汇总要说明 validate/fix 分组和结果。
- `--status elevated` 必须通过 `--result-file <elevation-detail.json>` 写入升级详情；自然语言 final 只作日志。

## Handoff

- Handoff 默认写入当前 RVF run 的 `artifacts/handoff.md` 并持续更新。
- 最终回复第一行输出 `RVF_HANDOFF_FILE: <绝对路径>`，空一行后按 `references/handoff-template.md` 规定的固定分行标签结构追加极短中文摘要（不要挤成一段）；该模板是摘要结构的唯一详述处。Stop hook 会把该 marker 当作完成信号，run 结束时发送 OS 系统通知（不再自动用编辑器打开 handoff）；不要再手动调用任何「打开 handoff」脚本。
- Handoff 只写确认过的事实，不把背景 WIP 或其他 session 的改动混成本轮工作。

## 拿 handoff 回到实现起点后的两条再入分支

用户把 RVF handoff 拿回「早先实现刚完成那一刻」时，成功判据是「**实现本身是否达成原始目标**」（用户实测决定，**与 RVF 的 fix 是否达标无关**），据此分两条互斥分支：

- **实现达标** → `$rvf-land`：在同一 worktree sanity-check future-self 已应用的修复并提交；不启动新的 review。
- **实现未达标 / 有问题**（用户带失败信号回来，无论是否粘贴 handoff）→ `$rvf-reopen`：先按「最近一次刚经过 RVF 的那次实现 run」武装一次性 rescope state（`scripts/rvf_rescope.py arm`），再修用户暴露的问题；修复带来的新增改动会让下一次 Stop 全量重审「该实现 units ∪ 本次 fix delta」（run-scoped 重开，不波及无关已审工作）。详见 `skills/rvf-reopen/SKILL.md`。

## 文档分层

- `prompts/`：脚本直接传给 reviewer 和 validate/fix 子代理的 self-contained prompt。
- `protocols/`：协议事实源索引；优先指向脚本/checker/schema，避免自然语言重复定义。
- `references/`：主会话正常执行时可能按需读取的材料，例如 review merge policy、review standards 和 handoff template。
- `internals/`：Stop hook、backend、Kanban、env/config、run ledger 等内部 workflow 说明；正常 agent 不读。
- `debug/`：脚本失败、用户要求排障或维护 runtime 时才读的 runbook。
- `setup/`：只用于用户明确要求配置或重配 external reviewer / MCP / agent 集成。
