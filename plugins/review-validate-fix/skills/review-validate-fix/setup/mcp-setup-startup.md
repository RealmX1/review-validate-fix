# Review Validate Fix MCP Setup Startup

把本 markdown 作为一个新的 coding agent session 的启动提示，用来协助设置 `$review-validate-fix` 的 santa-method external reviewer harness 注册表（`config/reviewer-registry.json`）。目标只限于让 `scripts/dispatch_reviewers.py` 能 probe 到至少两路 external reviewer harness；不要顺手重写 review loop、Stop hook 或 legacy 逻辑。setup 未完成不能阻塞 `$review-validate-fix` 正常运行；运行期当 `dispatch_reviewers.py` 报告 0 个 external 可用时会按 `references/zero-external-reviewer-last-resort-in-harness-fallback.md` 走 in-harness 最后兜底。

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
2. 记录一个稳定 label，例如 `alternative-reviewer:<agent-name>`。label 用于主会话来源记录；不要传给 validate/fix 子代理。
3. 确认它能接收 `prompts/reviewer.md` 的完整 prompt，并能在目标 repo 中运行 `git status --short`、`git diff HEAD` 和必要文件读取。
4. 如果需要新脚本或配置，保持通用：参数化 agent 命令、工作目录、prompt 文件、输出文件，不要把实现绑定到某个单一 agent。
5. 更新 `SKILL.md`、`prompts/reviewer.md`、`references/review-merge-policy.md`、`references/handoff-template.md` 或相关脚本时，保持 “Codex reviewer + arbitrary alternative reviewer” 的抽象。

## 仓库已自带的 alternative reviewer 模板

仓库已带三份 per-harness 模板，并由 `config/reviewer-registry.json` 统一注册（`harness_id → config_path / enabled / priority_default`）。`dispatch_reviewers.py` 据 registry 对每个 enabled harness 跑 `run_alternative_reviewer.py --config <path> --preflight` probe；也可单独把某个模板传给 `run_alternative_reviewer.py --config <path>`：

- `config/alternative-reviewer.cursor.json` — Cursor CLI（`cursor-agent`）模板，默认路由的首选一腿；`cursor-agent` 已在 `scripts/discover_santa_alternative_agents.sh` 的候选列表中。`run_alternative_reviewer.py` 无 `--config` 时的默认即指向它。
- `config/alternative-reviewer.claude.json` — Claude Code CLI 模板。
- `config/alternative-reviewer.codex.json` — Codex CLI 模板。
- `config/reviewer-registry.json` — 上述三者的注册表，是「本机启用哪些 external reviewer」的唯一事实源；`install_to_codex.py` 重装时保留本机这份 registry，不被仓库版本覆盖。

Cursor 模板的命令为 `cursor-agent -p --output-format stream-json --force --trust --sandbox disabled`，几个 flag 的理由：

- `-p --output-format stream-json`：headless 打印模式 + 流式 JSON，终止事件 `{"type":"result","result":"…"}` 与 Claude 形状一致，调用器以 `cursor_stream_json` 复用同一套 result 提取。
- `--force --trust --sandbox disabled`：reviewer 需能自主运行只读命令并把结论写到 **repo 外的 `run_dir`**；Cursor 没有 Codex `--add-dir` 那样的细粒度可写目录授权、沙箱又是粗粒度开关，故 disabled。该自治姿态与现有 codex（`--ask-for-approval never`）/claude（bypass）等价，并非新增风险。`--mode plan/ask` 是只读模式，不可用于需要写回的 reviewer。
- 不 pin `--model`（用账户默认）；如需固定模型可在 command 里加 `--model <name>`。

## 用户没有已知 agent 时

1. 运行 `scripts/discover_santa_alternative_agents.sh` 或手动做等价检查，列出当前环境中看起来像 coding agent 的候选。
2. 把候选用中文展示给用户，请用户选择一个，或补充未被发现的 agent。
3. 如果发现多个候选，不要自行替用户决定；说明每个候选的可验证入口，例如命令路径或配置文件。
4. 如果没有发现候选，问用户是否还有未被发现的 coding agent 可以提供。

## 没有可用 external reviewer 时的最后兜底

如果 registry 中没有任何 enabled 且 probe 通过的 external reviewer harness，或用户没有完成这一部分 setup，运行期由 `dispatch_reviewers.py` 自动判定（plan `routing_rule: R3`、`needs_last_resort_fallback: true`），退到 in-harness mimic 最后兜底。无需用户额外声明没有其他 coding agent。

最后兜底的完整 setup（两路 `codex-mimic-reviewer-a` / `codex-mimic-reviewer-b`、clean context、同 packet、同 `prompts/reviewer.md`、写 `review-result.json`、互不可见、source-agnostic 传递）见 `references/zero-external-reviewer-last-resort-in-harness-fallback.md`，仅当所有 external 路径都失败时才读。setup 阶段无需为该兜底做额外配置——它是零 external 时的运行期自动行为。用户之后让任一 harness 在 registry 中 enabled 且 probe 通过后，下一轮自动回到两路 external，不改动 review/validate/fix 的问题契约。

## 交付要求

完成 setup 后，用中文总结：

- registry 中启用并 probe 通过了哪些 external reviewer harness，或是否将退到 in-harness 最后兜底。
- 具体改了哪些文件。
- 如何手动验证该 setup。
- 尚未解决的风险或需要用户补充的凭据 / 命令。

在源仓库中运行 `scripts/check_plugin_contracts.py`。如果无法运行，说明原因。
