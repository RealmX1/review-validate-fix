目前该项目尚未被分发；一切对于Review-Validate-Fix本身的backward compatibility work都应该在commit前被清理；
- 如果该工作是通过直接改动主程序达成，那么需要明确注明该backward compatiblity work的改动，并在验证了已完成任务后，在commit前清理并log入已被gitignore的`dev_backward_compatibility`folder。

commit风格应遵循conventional commit

Kanban 项目语境：
- `cline-kanban` 与 `vibe-kanban` 是两个不同项目，后续分析、实现、文档和总结中不得混用名称；用户提到 cline-kanban 时不要自动切到 `/Users/bominzhang/Documents/GitHub/vibe-kanban`。
- 本项目历史上曾经从 Vibe-Kanban 管理路径迁移到 Cline Kanban 路径。当前已知原因是：Vibe-Kanban 方案主要作为可视化管理平面，实际 RVF review 仍通过后台 `codex exec` 在父 worktree 中执行，不能提供真正由 Kanban 管理的独立 task/worktree/checkpoint；同时当时 Vibe-Kanban 0.1.44 的 project/kanban UI 已变为 export-only，remote project 路径主要只剩兼容用途。Cline Kanban 路径改为通过 `kanban` CLI 创建/启动真实 task，在独立 worktree 中重放 RVF bootstrap，并可利用 Kanban diff viewer、checkpoints、inline comments、Commit/Open PR 和可选 auto-review。
- 因此，涉及当前 RVF 自动化的 Kanban backend 时，默认以 `cline-kanban` / `kanban` CLI 契约为准；除非用户明确要求考古或维护旧 Vibe 路径，不要重新引入 `vibe-kanban` runner/MCP/client 设计。

当某当前session先前已阅读文件出现超出预期的更改，可能是由其他agent进行的。对此情形默认行为是保留其变动。
- 如果该变动与你已经进行或计划进行的变动完全或部分重合，分析并自行决定是否进行进一步修改。
- 如果存在冲突部分，
  - 若你的计划是由开发者明确声明的任务，分析影响并将冲突部分覆盖；
  - 若非如此，将你的计划以及依赖与其的计划搁置并在未来回复中告知开发者。

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **review-validate-fix** (8371 symbols, 15685 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/review-validate-fix/context` | Codebase overview, check index freshness |
| `gitnexus://repo/review-validate-fix/clusters` | All functional areas |
| `gitnexus://repo/review-validate-fix/processes` | All execution flows |
| `gitnexus://repo/review-validate-fix/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
