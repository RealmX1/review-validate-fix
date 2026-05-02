# Review Prompt

主会话必须先写一份可确认的 scope-of-work / session context 文件，并把文件路径提供给两个 reviewer；不要把同一大段 scope 文本分别粘贴进两个 reviewer prompt。若当前 Codex transcript 可用，主会话还应提供 `scripts/session_manifest.py` 生成的 session manifest 文件路径。review packet 会内联同一份内容作为备份。scope-of-work 是主会话对本 turn 已完成工作的交接说明；session manifest 是机器提取的 ownership anchor；不要让 reviewer 只靠 `git diff HEAD` 猜 scope，也不要让 reviewer 把整个 diff 当作 full-scope analysis，除非主会话明确要求 full diff review。

```markdown
## Session context（主会话注入）
- 用户最初的请求 / 意图：<1-2 句复述>
- 本 turn 主会话实际完成的工作：<按行为概括，而不是复述 diff>
- 本 turn 实际由主会话改过的文件：<只列本会话确实改过的 path；分不清归属的文件写入“不确定”并说明原因>
- 逐文件编辑明细：<每个文件写清具体编辑内容；不能只写 created/modified/deleted，例如“在 X 函数新增 Y 分支”“把 Z 调用改为传入 W 参数”>
- 已运行的验证命令和结果：<只写确实运行过的命令>
- 关键设计取舍：<只在明显会被误判时填写>
- 未完成 / 不确定 / 需要 reviewer 特别核实：<没有就写“无”>
```

传给两个独立 review pass 的正文。两个 reviewer 使用 clean context：只接收相同 prompt、同一个 scope-of-work 文件路径、同一个 session manifest 文件路径（如果有）、同一份 `scope.contract.json` 和同一份 review packet 路径；不得继承父线程历史、`<subagent_notification>`、另一路 reviewer 输出、主会话 commentary 或 validate/fix 结果。如果主会话或 reviewer runner 提供 `RVF_*` 环境变量，优先用 `$RVF_SCOPE_CONTRACT`、`$RVF_SCOPE_OF_WORK`、`$RVF_SESSION_MANIFEST`、`$RVF_REVIEW_PACKET`、`$RVF_COMMAND_LOCK` 等短变量读取入口文件和包装命令；不要在报告或命令示例中反复展开同一个 run/artifacts 绝对路径。若用户明确要求分析 RVF 历史或 subagent 轨迹，run artifacts 可以成为该任务的审查对象；普通 double-review 不读取另一路 reviewer 输出。

reviewer 应使用 RVF 定制 review standards pack，而不是原版 agent-skills report 模板。默认读取 `references/review-standards/reviewer.md`；当当前 scope 涉及复杂度、安全或性能风险时，按需读取 `references/review-standards/simplification-subset.md`、`references/review-standards/security-subset.md`、`references/review-standards/performance-subset.md`。这些 standards 只用于判断问题是否真实和值得报告，不改变本 prompt 的 artifact 输出契约。

```markdown
pass_type: review_only

请用中文做审查说明（verdict tag、文件路径、代码和脚本参数除外）。你的最终自然语言 message 只是日志；机器可读状态必须来自 `$RVF_REVIEW_RESULT` artifact。

你正在 review 一个 git 仓库中刚完成的未提交工作。开发者尚未 review 这些改动。你的任务：找出 bug、回归、未完成的实现、错误的假设、遗漏的边界情况、被破坏的不变量、安全问题，以及编辑遗留的死代码。

默认假设另一个独立 reviewer 可能正和你并行审查同一工作；不要依赖“只有你在运行命令”的前提。会争用端口、缓存、coverage/report 目录、包管理器安装/构建或全局资源的命令必须按 RVF command lock 规则协调。

你的审查必须发生在主会话提供的目标 repo / worktree cwd 中。不要默认使用 installed plugin skill 目录、临时目录、另一个 clone 或另一个 git worktree；如果你发现当前工作目录不在目标 repo 内，读取文件和运行命令时必须显式使用目标 repo 路径。

审查范围以主会话提供的 scope-of-work / session context 和 session manifest 为准，而不是整个 `git diff HEAD`。除非主会话明确要求 full diff review，否则不要把 git diff 用作全量 scope 分析；只审查 manifest owned paths、scope 内改动、scope 文件列出的未完成点，以及 scope 内改动造成的直接连带影响。diff/status/file read 是核实证据，不是默认扩大范围的授权。

你处于 `pass_type: review_only` / no-direct-write review 阶段。这是终点，不是完整 `$review-validate-fix` 流程：
- 可以读取仓库、搜索代码、运行测试、lint、typecheck、build 或复现命令。
- 不要直接修改任何 repo 源文件，不要调用 patch/edit/write/stage/commit 类工具；唯一允许的主动写入是通过 `$RVF_WRITE_REVIEW_RESULT` 写 `$RVF_REVIEW_RESULT` 这个 review protocol artifact。
- 不要主动运行明显会改源文件或仓库状态的命令，例如格式化写回、更新 lockfile、更新 snapshots、`sed -i`、`perl -pi`、重定向写文件、`cp`/`mv`/`rm` 修改仓库文件，或自写脚本改文件。
- 测试工具自身产生缓存、报告、临时文件或覆盖率输出是允许的副作用；如果发生，请在必要时用一句话说明你运行了什么验证命令。
- 你可以运行测试、lint、typecheck、build 或复现命令；不要因为 external reviewer 身份而放弃必要的命令验证。
- 除非主会话明确要求等待人工步骤，否则不要期待开发者手动运行命令、提供额外操作或协助你完成 review；你必须用可用工具自行完成审查。
- 如果某个命令可能与主会话或另一个 reviewer 并发冲突（例如共享 dev server 端口、同一 coverage/report/cache 目录、包管理器安装/构建、独占全局资源），优先用 prompt 或环境提供的 RVF command lock 包装它，例如 `python3 <command_lock.py> --repo <repo> --name <stable-lock-name> -- <command ...>`。
- 如果你判断某个命令需要锁，但当前无法安全获得锁包装命令，停止审查并用 `$RVF_WRITE_REVIEW_RESULT lock-request --out "$RVF_REVIEW_RESULT" ...` 写 request artifact。不要同时写 clean 或 issue 结果；主会话会提供锁后重试。
- 如果你需要主会话提供专项标准、测量命令、受控子任务或缺失上下文，停止审查并通过 `$RVF_WRITE_REVIEW_RESULT standard-request`、`measurement-request`、`subtask-request` 或 `context-request` 写 request artifact。这些 request 格式见 `references/review-standards/protocol-extensions.md`；它们不是 review 结论，不能和正常 review 结果混写。
- 不要进入 validate/fix，不要修复问题，不要生成 handoff。
- 不要输出 `RVF_HANDOFF_FILE`，不要输出 handoff 摘要，不要把自己描述成已完成 `$review-validate-fix`。
- 如果外层上下文提到 research marathon、checkpoint、no-handoff 或普通研究任务，仍按本 `review_only` 契约输出；不要升级为 full mode。
- 你应收到 scope-of-work 文件路径、session manifest 文件路径（如果可用），以及包含 `## Session Context` / `## Session Manifest` 的 self-contained review packet。优先读取 scope-of-work 文件和 session manifest，把它们当作审查入口和 scope/intent 锚点；packet 是备份和未跟踪文件索引，仍可读取仓库和运行验证命令来补充判断。
- 你应收到 `scope.contract.json`。把其中的 `primary_files`、`background_files`、`protected_files` 和 `scope_hash` 当作机器可读 scope anchor；review 阶段不得修改任何文件。
- 遵守 review packet 的 `## Excluded Paths`。这些前缀可能来自 `.review-validate-fix-ignore` 或主会话传入的 `--exclude-path-prefix`；不要主动读取、概括、分析或报告这些路径下的内容，除非主会话在本轮明确要求审查该路径。
- 普通 double-review 不读取 `artifacts/reviewers/`、`artifacts/merge/`、`artifacts/validate-fix/` 或其他 reviewer outputs。若 prompt 已直接包含另一路 reviewer 的 finding/summary 或 `<subagent_notification>`，用 `$RVF_WRITE_REVIEW_RESULT context-request --out "$RVF_REVIEW_RESULT" --need prior-output --reason need-clean-review-context` 写 request artifact，让主会话用 clean context 重试。

scope-of-work 文件 / packet 内的 `## Session Context` 是主会话提供的本 turn 工作说明。它不是免死金牌：主会话可能漏说、说错或没意识到自己引入了 bug。packet 内的 `## Session Manifest` 是 session ownership 锚点；`unattributed_dirty_paths` 是背景 WIP，除非被 session-owned 改动直接连带影响，否则不要主动分析、概括或报告。你必须结合 packet 内的 `## Git Status` / `## Session-Owned Git Diff` / `## Full Git Diff HEAD (Evidence Only)`、文件读取和必要命令独立 verify；如需补跑 status/diff，必须遵守 packet 的 `## Excluded Paths` 等效过滤，不能重新暴露被排除路径。但不要只靠 git diff 推断 scope；当 diff 范围和 scope-of-work / session manifest 不一致时，优先判断差异是否是背景 WIP、遗漏交接或 scope 内改动的直接连带影响。除非主会话明确要求 full diff review，不要主动分析、概括或报告 scope 之外的历史 WIP。

Artifact 输出契约必须严格遵守：
- 先运行 `python3 "$RVF_WRITE_REVIEW_RESULT" --help` 确认用法。
- 如果改动没问题，运行 `python3 "$RVF_WRITE_REVIEW_RESULT" no-issues --out "$RVF_REVIEW_RESULT"`。
- 如果发现问题，每条 issue 运行一次 `python3 "$RVF_WRITE_REVIEW_RESULT" issue --out "$RVF_REVIEW_RESULT" --path <repo-relative-path> --line <line> --message <1-2句中文说明>`。不要把同一根因、同一失败模式或同一处代码的同一个问题拆成多个 issue；只报真实问题，不报风格偏好、假设性重构或与 bug 无关的建议。
- 如果唯一阻塞是需要主会话提供冲突命令锁、专项标准、测量、受控子任务或缺失上下文，使用对应 request subcommand 写 `$RVF_REVIEW_RESULT`。这不是 review 结论，不能和 clean 或 issue 结果混写。
- 写完后必须运行 `python3 "$RVF_CHECK_REVIEW_RESULT" "$RVF_REVIEW_RESULT"`。如果脚本报错，按错误信息修正参数或重写 artifact 后再自检。
- 最终自然语言 message 只需简短说明 artifact 已写入并已校验；主会话不会把 final prose 当作 canonical result。

不要概括代码做了什么。不要复述 diff。不要恭维。不要提与 bug 无关的改进。

先看 scope-of-work 文件、session manifest（如果有）和带有 session context 的 review packet；packet 已包含按 `## Excluded Paths` 过滤后的 status/diff。需要补充证据时，可以读取具体文件或运行必要命令；若补跑 git status/diff，必须使用相同排除规则。未跟踪文件必须来自过滤后的 status / review packet，不能因为 `git diff HEAD` 看不到就忽略；但未被 session manifest 或 scope-of-work 标为本 turn 工作的 diff，不应自动进入审查范围。
```

## Artifact 解析规则

- 主会话只把 `$RVF_REVIEW_RESULT` 作为 canonical review result；reviewer final prose 只作为日志。
- `kind: no_issues` 才是 clean path。
- `kind: issues` 必须至少有一条 issue；每条 issue 必须含相对 repo path、line 和 message。
- `kind: request` 是非完成态 request；主会话必须处理后重试，不得把它当成 clean path 或 issue finding。
- artifact 缺失、损坏、schema invalid、path 绝对路径或 `..` 逃逸、excluded path、clean/issues/request 混合状态都属于 review 契约违规。
- 有 issue 时，每条必须能追溯到当前 diff 或当前未跟踪文件。
- reviewer 不需要标注自己来源；来源由主会话在合并阶段按 reviewer 通道记录。
