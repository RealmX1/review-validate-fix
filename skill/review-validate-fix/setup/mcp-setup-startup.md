# Review Validate Fix MCP Setup Startup

把本 markdown 作为一个新的 coding agent session 的启动提示，用来协助设置 `$review-validate-fix` 的 MCP / agent 集成。目标只限于配置 santa-method 的 alternative reviewer；不要顺手重写 review loop、Stop hook 或 legacy Claude 逻辑。setup 未完成不能阻塞 `$review-validate-fix` 正常运行；运行期会默认使用 Codex-only fallback。

## 启动时先问用户

请先用中文问用户一个问题，避免无谓搜索：

```markdown
你是否已经知道要把哪个替代 coding agent 作为 `$review-validate-fix` 的 santa-method alternative reviewer？

如果有，请告诉我：
- agent 名称
- 启动方式或命令
- 它是否通过 MCP、CLI、IDE 插件或本地 wrapper 暴露
- 任何必要的配置路径或使用约束

如果没有，请直接说“没有已知替代 agent”。我会再搜索当前机器上可能可用的候选。
```

用户可以提供你没有搜索到的 agent。只要它能独立读取仓库、执行 review prompt、返回文本结果，就可以作为 alternative reviewer 的候选。

## 用户已提供 agent 时

1. 先确认该 agent 的调用方式是否真实可用：检查命令、MCP server、配置文件或 wrapper，但不要写死 vendor 名称。
2. 记录一个稳定 label，例如 `alternative-reviewer:<agent-name>`。label 用于主会话 provenance；不要传给 validate/fix 子代理。
3. 确认它能接收 `references/review-prompt.md` 的完整 prompt，并能在目标 repo 中运行 `git status --short`、`git diff HEAD` 和必要文件读取。
4. 如果需要新脚本或配置，保持通用：参数化 agent 命令、工作目录、prompt 文件、输出文件，不要把实现绑定到某个单一 agent。
5. 更新 `SKILL.md`、`references/review-merge-policy.md`、`references/handoff-template.md` 或相关脚本时，保持 “Codex reviewer + arbitrary alternative reviewer” 的抽象。

## 用户没有已知 agent 时

1. 运行 `scripts/discover_santa_alternative_agents.sh` 或手动做等价检查，列出当前环境中看起来像 coding agent 的候选。
2. 把候选用中文展示给用户，请用户选择一个，或补充未被发现的 agent。
3. 如果发现多个候选，不要自行替用户决定；说明每个候选的可验证入口，例如命令路径或配置文件。
4. 如果没有发现候选，问用户是否还有未被发现的 coding agent 可以提供。

## 没有 alternative agent 时的 fallback

如果没有可用 alternative reviewer，或用户没有完成这一部分 setup，默认使用 Codex-only fallback。无需用户额外声明没有其他 coding agent。

fallback 行为：

- 不降级为单 reviewer。
- 并行启动两个 Codex-native 子代理来模拟 santa-method double review。
- 两个子代理使用同一份 `references/review-prompt.md` 和同一份 session context，彼此不看对方输出。
- provenance 使用两个独立来源，例如 `codex-mimic-reviewer-a` 和 `codex-mimic-reviewer-b`。
- validate/fix 子代理仍然只接收 source-agnostic issue context。
- 如果用户之后配置了真实 alternative reviewer，运行期可切回 `codex-reviewer` + `alternative-reviewer:<agent-name>`；不需要改动 review/validate/fix 的问题契约。

## 交付要求

完成 setup 后，用中文总结：

- 选择或配置了哪个 alternative reviewer，或是否启用了 Codex-only fallback。
- 具体改了哪些文件。
- 如何手动验证该 setup。
- 尚未解决的风险或需要用户补充的凭据 / 命令。

运行 `scripts/check_contracts.sh`。如果无法运行，说明原因。
