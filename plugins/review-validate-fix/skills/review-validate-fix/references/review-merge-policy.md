# Review Merge Policy

## Reviewer Roles

每轮 review 必须有两个独立来源：

- `codex-reviewer`：Codex-native review pass。
- `alternative-reviewer:<agent-name>`：用户配置的 santa-method alternative reviewer。它可以来自任意外部 coding agent、MCP server、CLI、IDE agent 或本地 wrapper；不要在 skill 中硬编码 vendor、模型名或命令名。
- `codex-mimic-reviewer-a` / `codex-mimic-reviewer-b`：alternative reviewer 未配置、配置未完成或无法启动时的默认 Codex-only fallback。

两个 reviewer 应并行运行，使用同一份主会话 session context、同一份 review packet 和 `references/review-prompt.md`，且不要互相读取对方输出。session context 必须说明主会话本 turn 实际完成的工作；review packet/diff/status 用来核实和补足证据，不应成为 reviewer 推断 scope 的唯一来源。

如果 `config/alternative-reviewer.json` 已配置且 `scripts/run_alternative_reviewer.py --check` 通过，使用 `codex-reviewer` + `alternative-reviewer:<agent-name>`。需要认证/健康状态确认时，可先运行 `scripts/run_alternative_reviewer.py --preflight`。运行 external reviewer 时调用 `scripts/run_alternative_reviewer.py --repo <repo> --review-packet <packet> --session-context <file>`，并按配置中的 `label` 记录来源；reviewer 可以读取仓库并运行测试/lint/build，但不能直接编辑、写入、stage、commit 或执行 validate/fix。external reviewer 应自行完成审查，不要默认等待开发者手动协助。`run_alternative_reviewer.py` 使用可观测活动空闲超时：默认每 300 秒检查一次 stdout/stderr 活动，连续 300 秒没有新活动时返回 `RVF_EXTERNAL_REVIEWER_TIMEOUT` 和 exit code `124`；对 Claude Code 等支持事件流的 CLI，默认配置应使用 stream-json 事件刷新活动时间，并让 runner 只提取最终 review 文本。这种 partial 输出不得并入 issue list。如果 alternative reviewer 未配置、配置未完成、命令不可用、本轮无法启动或空闲超时，默认使用 Codex-only fallback：并行启动两个独立 Codex-native 子代理，provenance 分别标为 `codex-mimic-reviewer-a` 和 `codex-mimic-reviewer-b`。这仍然是 double review；不要由主会话单独 review 后伪装成两路结果。

只有用户在本轮明确要求必须使用外部 alternative reviewer、且不接受 Codex-only fallback 时，才因 alternative reviewer 不可用而 fail-close。

## Contract Gate

合并前先校验每个 reviewer 的输出：

1. 用 `scripts/check_review_output.py` 或等价严格解析器确认输出是精确 `NO_ISSUES`、编号 `路径:行号` issue list，或纯 `RVF_LOCK_REQUEST ...`。
2. 输出中包含 validate/fix verdict、修复说明、handoff、纯 prose 或中文化“没有问题”时，该 reviewer 标记为 `CONTRACT_VIOLATION`。
3. 如果输出是 `RVF_LOCK_REQUEST ...`，这不是完成态 review；主会话应提供 `scripts/command_lock.py --repo <repo> --name <stable-lock-name> -- <command ...>` 包装命令，或明确驳回该命令并重试 reviewer。锁请求不得进入 merge table。
4. 如果 reviewer 运行期间 `scripts/workspace_snapshot.py compare` 报告状态变化，标记为 `WORKSPACE_CHANGED_DURING_REVIEW`。这只是状态污染信号，不推断 reviewer 的意图，也不要自动 revert；测试缓存、报告、coverage、临时构建产物等可解释副作用不构成 review 契约违规。
5. `CONTRACT_VIOLATION` 的输出不得进入合格 provenance。`WORKSPACE_CHANGED_DURING_REVIEW` 需要主会话先检查污染范围；如果只是可解释测试副作用，仍可保留 reviewer provenance；如果出现未授权源文件、lockfile、snapshot 或文档写入，应 fail-close 或询问用户。

## Merge Rules

合并两个 reviewer 的结果时，先保留一张主会话内部合并表：

| processed_id | canonical_issue | source_reviewers | source_items |
|-------------|-----------------|------------------|--------------|
| RVF-001 | <合并后的 issue 描述> | codex-reviewer, alternative-reviewer:<agent-name> | C1, A2 |

规则：

1. 两边都输出精确 `NO_ISSUES`：进入 clean path。
2. 一边 `NO_ISSUES`、另一边有 issue：保留有 issue 的一边，source 只记发现该 issue 的 reviewer。
3. 两边报同一根因或同一失败模式：合并为一个 processed issue，source 记两个 reviewer。
4. 同一文件相邻行、同一测试失败、同一 API contract 或同一状态机不变量导致的问题：优先分组为一个 processed issue，并在描述里列出涉及的 path/line。
5. 表面相似但修复方向不同的问题不要强行合并；保留多个 processed issue。
6. 只保留 bug、回归、遗漏边界、安全问题、错误假设、未完成实现和死代码；过滤风格偏好、泛泛重构建议和无法追溯到当前 diff 的问题。

## Source Provenance

主会话必须记住每个 processed issue 的来源：

- `source_reviewers`：`codex-reviewer`、`alternative-reviewer:<agent-name>`、Codex-only fallback 的两个 mimic reviewer，或其中多个来源。
- `source_items`：原 reviewer 输出中的编号，例如 `codex-reviewer#1`、`alternative-reviewer:<agent-name>#2`。

来源 provenance 可以出现在主会话合并摘要和 handoff 中，方便后续审计；但它不能传给 validate/fix 子代理。

## Validation Prompt Boundary

发给 validate/fix 子代理的 issue context 必须 source-agnostic：

- 不写 `codex-reviewer` / `alternative-reviewer:<agent-name>` / `codex-mimic-reviewer-*`。
- 不写“两个模型都认为”或“只有某模型发现”。
- 不写来源编号。
- 只给 canonical issue、相关 path/line、必要代码上下文、复现线索和 validate/fix 指令。

这样 validate/fix 子代理只验证问题本身，不受来源模型声望、重复发现或分歧影响。
