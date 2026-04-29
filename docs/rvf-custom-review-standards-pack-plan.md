# RVF 定制 Review Standards Pack 计划

本文记录一个后续可展开为详细实现计划的设计方向：在 Review-Validate-Fix plugin 内引入一套 RVF-customized Review Standards Pack，使主会话、reviewer 子代理、validate/fix 子代理分别获得适合自身职责的审查标准子集，并扩展主会话与子代理之间的协议，让子代理能请求受控的专项子任务、测量或更深一层的调查。

本文不是实现计划，不要求本轮修改运行期协议。它是给后续 agent 使用的设计 checkpoint。

## 背景

前一轮对比确认：`agent-skills` 的 Review 体系不只包含 `code-review-and-quality`，还包含 `code-simplification`、`security-and-hardening`、`performance-optimization` 这些 review 阶段可用的专项 skill。RVF 不应直接复用这些原版 skill 的输出格式，因为 RVF reviewer 必须遵守严格的可解析输出契约。但 RVF 可以吸收它们的判断标准，并按 RVF 的 scope、provenance、run ledger、handoff 和 validate/fix 协议重新打包。

核心设计判断：

- 不把 `agent-skills` 原版 skill 直接 vendoring 成 RVF reviewer。
- 在 RVF plugin 内维护一个定制子集，作为 RVF 内部 standards pack。
- 不让 reviewer 输出完整 report / checklist / launch decision。
- 允许 reviewer 和 validate/fix 子代理在受控协议下请求子任务、专项标准、测量或更深调查。
- 默认由主会话承担 spawn / routing / audit 责任，保证 run ledger、scope 和 provenance 不丢。

## 参考资料

agent-skills 原始参考：

- `/Users/bominzhang/Documents/GitHub/agent-skills/skills/code-review-and-quality/SKILL.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/skills/code-simplification/SKILL.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/skills/security-and-hardening/SKILL.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/skills/performance-optimization/SKILL.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/agents/code-reviewer.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/agents/security-auditor.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/agents/test-engineer.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/agents/README.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/references/orchestration-patterns.md`

RVF 当前设计参考：

- `plugins/review-validate-fix/skills/review-validate-fix/SKILL.md`
- `plugins/review-validate-fix/skills/review-validate-fix/references/review-prompt.md`
- `plugins/review-validate-fix/skills/review-validate-fix/references/review-merge-policy.md`
- `plugins/review-validate-fix/skills/review-validate-fix/references/validate-then-fix-prompt.md`
- `plugins/review-validate-fix/skills/review-validate-fix/references/handoff-template.md`
- `plugins/review-validate-fix/skills/review-validate-fix/scripts/prepare_review_run.py`
- `plugins/review-validate-fix/skills/review-validate-fix/scripts/build_review_packet.py`
- `plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py`
- `plugins/review-validate-fix/skills/review-validate-fix/scripts/command_lock.py`
- `plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_logging.py`

本仓库分析文档：

- `docs/agent-skills-review-comparison.md`

## 目标形态

新增一套 RVF 内部 review standards package。建议位置：

```text
plugins/review-validate-fix/skills/review-validate-fix/references/review-standards/
  index.md
  main-agent.md
  reviewer.md
  validate-fix.md
  simplification-subset.md
  security-subset.md
  performance-subset.md
  protocol-extensions.md
```

这套文档不应原样复制 `agent-skills`。它应是 RVF-compatible 版本：

- 保留 RVF 的 session-scoped review。
- 保留 scope-of-work / session manifest / review packet 作为审查入口。
- 保留 reviewer 完成态输出契约。
- 保留 validate/fix 的 `REAL` / `FALSE_POSITIVE` / `ELEVATE`。
- 保留 source-agnostic validate/fix prompt boundary。
- 保留 run ledger 和 command lock。
- 增加受控的 request protocol，而不是让子代理自由输出长报告。

## 角色可见范围

### 主会话

主会话可以访问完整 standards pack。它负责：

- 读取 `index.md` 和 `main-agent.md`。
- 根据 scope-of-work、manifest、review packet 和 reviewer request 决定是否提供专项标准。
- 决定是否 spawn additional subtask。
- 维护 processed issue merge table。
- 维护 validate/fix grouping audit table。
- 记录每个 request 的来源、处理结果和 run ledger artifact。
- 保证最终 handoff 不混入背景 WIP。

主会话可以知道 provenance，但传给 validate/fix 子代理的 issue context 仍必须 source-agnostic。

### Reviewer 子代理

reviewer 子代理默认只访问 reviewer subset：

- `reviewer.md`
- 必要时的 `simplification-subset.md`
- 必要时的 `security-subset.md`
- 必要时的 `performance-subset.md`
- scope-of-work / session manifest / review packet
- command lock 入口

reviewer 的默认职责仍是找出当前 scope 内真实 bug、回归、未完成实现、错误假设、遗漏边界、安全问题、性能回归和死代码。

reviewer 完成态仍只能输出：

- `NO_ISSUES`
- 编号 issue list，每条含 `路径:行号` 和 1-2 句中文说明

reviewer 非完成态可以新增 request 输出，见“协议扩展”。

### Validate/Fix 子代理

validate/fix 子代理默认只访问 validate/fix subset：

- `validate-fix.md`
- 与 assigned issue 相关的专项 subset
- canonical issue package
- 相关文件上下文和复现线索
- command lock 入口

validate/fix 子代理不应读取 reviewer provenance，不应重新执行 double review，不应扩大 scope，不应生成 handoff。

validate/fix 可以使用 standards pack 进行验证和最小修复：

- simplification issue：必须行为保持，必要时先补验证。
- security issue：必须确认当前代码存在可触达风险，不能只给泛化 hardening。
- performance issue：必须先确认 anti-pattern 或测量需求，不能凭感觉改。

## Standards 子集边界

### code-review-and-quality 子集

吸收内容：

- correctness / edge case / error path / race / invariant 检查。
- architecture consistency and boundary 检查。
- dead code hygiene。
- dependency discipline。
- change sizing 和 split 判断。

排除内容：

- Critical / Important / Suggestion 作为 reviewer 输出格式。
- full review report template。
- positive observation。
- generic suggestions。

### code-simplification 子集

吸收内容：

- Chesterton's Fence。
- Preserve behavior exactly。
- Follow project conventions。
- Prefer clarity over cleverness。
- Scope to what changed。
- Rule of 500。

RVF 内的报告门槛：

- 只报告导致真实 bug 风险、维护不可判定性、死代码、误导性命名、错误抽象或遗漏边界的问题。
- 不报告纯风格建议。
- 不在 review pass 里直接重构。

### security-and-hardening 子集

吸收内容：

- Three-tier boundary system。
- OWASP baseline。
- 输入校验、参数化查询、输出编码。
- auth / authorization。
- secrets management。
- dependency audit triage。
- rate limiting / CORS / cookie / security headers。

RVF 内的报告门槛：

- 只报告当前 scope 内可定位到文件和行号的真实安全问题。
- 跨系统 hardening plan 不进入 reviewer issue list。
- 需要用户安全决策时进入 `ELEVATE`。

### performance-optimization 子集

吸收内容：

- Measure before optimizing。
- Before / after verification。
- N+1。
- unbounded data fetching。
- blocking main thread / expensive recomputation。
- bundle / asset / layout shift / excessive re-render。
- performance budget 和 Core Web Vitals 风险。

RVF 内的报告门槛：

- 没有明确需求、测量、anti-pattern 或可定位 regression 时，不输出性能 issue。
- 需要测量才能判断时，用 request protocol 请求测量，或在 validate/fix 中 `ELEVATE`。
- 不做 premature optimization。

## 协议扩展

当前 reviewer 完成态输出契约应保留。新增内容应作为非完成态 request contract，而不是混入完成态 issue list。

已有非完成态：

```text
RVF_LOCK_REQUEST name=<stable-lock-name> command=<command> reason=<why>
```

建议新增：

```text
RVF_STANDARD_REQUEST domain=<simplification|security|performance> reason=<why> scope=<paths-or-issue>
```

用途：reviewer 或 validate/fix 子代理认为当前任务需要某个专项 standards subset，但 prompt 未提供。

```text
RVF_MEASUREMENT_REQUEST metric=<metric-or-signal> command=<command> reason=<why>
```

用途：性能问题需要测量、audit 需要命令输出、或复现需要受控命令。主会话决定是否运行、加锁、替换命令或驳回。

```text
RVF_SUBTASK_REQUEST type=<read_only_investigation|security_check|performance_measurement|simplification_probe> scope=<paths-or-issue> reason=<why>
```

用途：子代理认为当前 issue 需要更独立的专项调查。默认由主会话 spawn，避免 provenance 和 run ledger 丢失。

```text
RVF_CONTEXT_REQUEST need=<file|manifest|packet|prior-output|test-result> reason=<why>
```

用途：子代理缺少必要上下文，但不应自行扩大 scope 或猜测。

所有 request contract 都必须满足：

- 不能和 `NO_ISSUES` 混写。
- 不能和正常 issue list 混写。
- 必须可由 parser 区分。
- 必须记录到 run ledger。
- 主会话必须显式处理：满足、驳回、重试、或升级给用户。

后续实现需要更新：

- `references/review-prompt.md`
- `references/validate-then-fix-prompt.md`
- `references/review-merge-policy.md`
- `scripts/check_review_output.py`
- 相关 tests

## 子任务与嵌套子代理策略

### 默认策略：由主会话 spawn

推荐默认策略是：subagent 可以请求一个子任务，但默认由主会话 spawn。

原因：

- 主会话能把 subtask 写入 run ledger。
- 主会话能保留原始 scope 和 manifest。
- 主会话能记录 provenance。
- 主会话能把结果合并进 audit table。
- 主会话能避免子代理绕过 source-agnostic boundary。

这适用于：

- reviewer 请求专项 security check。
- reviewer 请求 performance measurement。
- validate/fix 请求局部 read-only investigation。
- validate/fix 需要另一个 agent 验证修复是否行为保持。

### 可选策略：允许 max_depth=1 的受控嵌套

如果未来 Codex agent API 支持安全的 nested subagent，并能保留 run id / scope / ledger context，可以考虑允许一层嵌套。

建议限制：

- `max_depth=1`。
- child subagent 必须继承 parent 的 `pass_type`。
- child subagent 必须继承 target repo、scope-of-work、manifest、review packet 和 exclusions。
- child subagent 不得生成 handoff。
- reviewer child 只能 read-only。
- validate/fix child 只能处理 parent 分配的 issue 子集。
- child output 必须返回 parent，再由 parent 返回主会话。
- 主会话最终仍要记录 child provenance。

如果平台无法保证这些条件，就不要开放 nested spawn。使用 request-to-main 模式。

## Main-agent 处理 request 的建议流程

主会话收到 request 后应：

1. 校验 request 格式。
2. 确认 request 没有混入完成态输出。
3. 判断 request 是否在当前 scope 内。
4. 判断是否需要 command lock。
5. 决定满足、驳回、spawn subtask 或 ELEVATE。
6. 记录 request、决策、命令、subtask id 和结果到 run ledger。
7. 将结果以最小必要上下文传回原 requester。
8. 要求 requester 重试并输出完成态结果。

主会话不应把 request 本身合并为 bug finding。只有 requester 重试后输出的 issue list 才进入 review merge。

## Validate/Fix 处理专项 issue 的建议流程

validate/fix 子代理拿到 canonical issue 后：

1. 读取 assigned issue 和相关标准 subset。
2. 只读验证 issue 是否真实。
3. 如果不真实，返回 `FALSE_POSITIVE`。
4. 如果真实且可最小修复，实施最小修复并返回 `REAL`。
5. 如果需要用户决策、跨 scope 改动、缺少测量或存在多种同等方案，返回 `ELEVATE`。
6. 如果只缺少可由主会话提供的上下文、命令锁或测量，先输出 request contract。

对专项 issue 的额外要求：

- simplification：必须保留行为，不能为了简化修改测试期望。
- security：不能通过关闭安全控制解决问题。
- performance：必须能说明具体 bottleneck、anti-pattern 或测量需求。

## 与现有 RVF 契约的关系

必须保持不变：

- `$review-validate-fix` 仍只显式调用。
- Stop hook 仍通过 fork prompt 进入 RVF，不走 continuation fallback。
- review scope 仍来自 scope-of-work / manifest，而不是 whole diff。
- reviewer 完成态仍是 `NO_ISSUES` 或编号 issue list。
- validate/fix context 仍 source-agnostic。
- handoff 仍只由主会话生成。
- subagent Stop 不触发新的 RVF fork。

可以扩展：

- standards pack 文件结构。
- reviewer / validate-fix prompt 对专项 standards 的引用。
- request contract。
- parser 对非完成态 request 的识别。
- run ledger 对 request / subtask / measurement 的事件记录。
- 主会话处理 request 的 audit table。

## 未来详细更新计划的建议拆分

后续 agent 可以把实现拆成以下阶段。

### 阶段 1：文档和 standards pack

- 新增 `references/review-standards/`。
- 从 `agent-skills` 提炼 RVF-compatible 子集。
- 更新 `SKILL.md` 说明 standards pack 的角色边界。
- 更新 `review-prompt.md` 和 `validate-then-fix-prompt.md` 引用 standards subset。

### 阶段 2：request contract parser

- 扩展 `check_review_output.py`。
- 增加 `RVF_STANDARD_REQUEST`、`RVF_MEASUREMENT_REQUEST`、`RVF_SUBTASK_REQUEST`、`RVF_CONTEXT_REQUEST` 解析。
- 保持完成态输出契约不变。
- 增加 contract violation tests。

### 阶段 3：主会话协议和 merge policy

- 更新 `review-merge-policy.md`。
- 定义 request 不进入 merge table。
- 定义主会话 request audit table。
- 定义 retry 规则。

### 阶段 4：run ledger 和 command lock 集成

- 为 request / subtask / measurement 增加事件类型。
- 确保 artifact 保存 request 原文、主会话决策、命令输出和子任务结果。
- 保持 hook stdout 只输出 hook payload。

### 阶段 5：受控 subtask / nested strategy

- 先实现 request-to-main spawn 模式。
- 暂不默认开放 nested subagent。
- 如果平台能力允许，再增加 `max_depth=1` 的受控模式。
- 增加递归保护和测试。

### 阶段 6：端到端验证

- 添加 reviewer request fixture。
- 添加 performance measurement request fixture。
- 添加 security standard request fixture。
- 添加 validate/fix request fixture。
- 验证 request 重试后能回到正常 `NO_ISSUES` / issue list / verdict 流程。

## 风险和开放问题

开放问题：

- standards pack 是否应该进入 review packet，还是只作为 prompt reference path 提供。
- external alternative reviewer 如何获得 standards subset：内联、文件路径、还是 adapter 注入。
- request contract 是否需要 JSONL 形式，还是继续使用单行 sentinel。
- nested subagent 是否由当前 Codex API 支持并能继承 run ledger env。
- measurement request 执行结果是否应回传给原 reviewer，还是由主会话直接创建 processed issue / ELEVATE。

主要风险：

- standards pack 太大，导致 reviewer prompt 过载。
- request contract 太多，导致 parser 和 retry 逻辑复杂化。
- nested subagent 打破 provenance 或 scope isolation。
- performance standards 被误用成 premature optimization。
- security standards 被误用成泛化 hardening plan，而不是当前 scope 内 bug finding。

建议优先采用最小可行版本：

1. 先做 standards pack。
2. 只新增 `RVF_STANDARD_REQUEST` 和 `RVF_MEASUREMENT_REQUEST`。
3. 子任务先全部由主会话 spawn。
4. nested subagent 留作后续能力，不进入第一版实现。
