# agent-skills Review skill family 与 RVF 设计对比

本文记录对 `/Users/bominzhang/Documents/GitHub/agent-skills/` 中 Review 类 skills、review 相关 agents / commands 的阅读结论，并与本仓库 `review-validate-fix` 当前设计做对比。

## 阅读范围

主要阅读对象：

- `/Users/bominzhang/Documents/GitHub/agent-skills/skills/code-review-and-quality/SKILL.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/skills/code-simplification/SKILL.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/skills/security-and-hardening/SKILL.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/skills/performance-optimization/SKILL.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/agents/code-reviewer.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/agents/test-engineer.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/agents/security-auditor.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/agents/README.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/references/orchestration-patterns.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/.claude/commands/review.md`
- `/Users/bominzhang/Documents/GitHub/agent-skills/.claude/commands/ship.md`

对照的 RVF 设计对象：

- `plugins/review-validate-fix/skills/review-validate-fix/SKILL.md`
- `plugins/review-validate-fix/skills/review-validate-fix/references/review-prompt.md`
- `plugins/review-validate-fix/skills/review-validate-fix/references/review-merge-policy.md`
- `plugins/review-validate-fix/skills/review-validate-fix/references/validate-then-fix-prompt.md`
- `plugins/review-validate-fix/skills/review-validate-fix/agents/openai.yaml`

## 总体结论

`agent-skills` 的 Review 体系是通用工程质量门和专项审查方法库。它不只有 `code-review-and-quality`，还包括 complexity simplification、security hardening 和 performance optimization 这些可在 review 阶段被调用的专项 skill。它强调人或 slash command 主动触发 review，由 skill / persona 产出结构化报告或专项判断，再由主会话或用户做决策。

RVF 的设计是自动化 post-work review loop。它不仅要发现问题，还要把问题合并、验证、最小修复，并生成 handoff。RVF 同时要解决 Codex Stop hook、session-scoped scope、背景 WIP 隔离、reviewer 输出可解析性、外部 reviewer 配置、run ledger 排障等问题。

因此，两者在价值观上兼容，但不是同一层抽象。`agent-skills` 更像 review 方法论、专项审查 checklist 和 persona catalog；RVF 是一个可执行的审查闭环协议。

## agent-skills 的 Review skill family

### code-review-and-quality

`code-review-and-quality` 定义了五轴审查：

- Correctness：需求匹配、边界条件、错误路径、竞态、状态一致性。
- Readability & Simplicity：命名、控制流、组织方式、复杂度、死代码。
- Architecture：是否符合现有模式、模块边界、依赖方向、抽象层次。
- Security：输入校验、秘密泄漏、鉴权、注入、XSS、依赖风险。
- Performance：N+1、无界操作、同步阻塞、UI 重渲染、分页缺失。

它还要求先理解上下文，再看测试，然后看实现，最后检查 verification story。

这个 skill 还覆盖 change sizing、severity labels、review speed norms、splitting strategies、dependency discipline 和 dead-code hygiene。它更像默认 review gate：任何 change merge 前都应经过它的判断。

### code-simplification

`code-simplification` 是 review 阶段的 complexity / maintainability 专项 skill。它不主张为了变短而改代码，而是要求在精确保留行为的前提下降低理解和维护成本。

它的核心原则包括：

- Preserve behavior exactly：输入、输出、副作用、错误行为和边界条件必须保持一致。
- Follow project conventions：简化必须贴合项目已有模式，而不是引入外部偏好。
- Prefer clarity over cleverness：明确表达优先于压缩行数。
- Maintain balance：避免过度内联、合并无关逻辑、移除有意义抽象。
- Scope to what changed：默认只简化最近修改或被要求审查的代码，避免 drive-by refactor。

它的关键过程是 Chesterton's Fence：改动或删除代码前必须先理解其存在原因、调用关系、边界条件、测试覆盖和历史上下文。它还提出 Rule of 500：大规模重构超过约 500 行时应使用自动化方式，而不是手工编辑。

### security-and-hardening

`security-and-hardening` 是 review 阶段的安全专项 skill。它适用于用户输入、认证授权、数据存储、外部集成、文件上传、webhook、支付和 PII 等场景。

它的核心是 three-tier boundary system：

- Always Do：边界输入校验、参数化查询、输出编码、HTTPS、强密码哈希、安全 cookie、安全 headers、依赖 audit。
- Ask First：新增或改变 auth flow、存储新敏感数据、引入外部服务、改 CORS、加文件上传、改限流、提升权限。
- Never Do：提交 secrets、记录敏感数据、信任客户端校验、关闭安全 headers、对用户输入使用 `eval()` / `innerHTML`、把 auth token 放进 client-accessible storage、向用户暴露 stack trace。

它还覆盖 OWASP Top 10 风险、schema validation、file upload safety、npm audit triage、rate limiting、secrets management 和安全 review checklist。

### performance-optimization

`performance-optimization` 是 review 阶段的性能专项 skill。它的第一原则是 measure before optimizing：没有测量就不优化。

它的流程是：

1. Measure：用 synthetic 和 RUM 建立 baseline。
2. Identify：定位真实 bottleneck，而不是猜。
3. Fix：只修具体瓶颈。
4. Verify：再次测量确认改善。
5. Guard：加入 monitoring 或测试防止回归。

它覆盖 Core Web Vitals、frontend / backend profiling、N+1、无界数据获取、图片优化、React 重渲染、bundle size、caching 和 performance budget。

在 review 语境下，它不应该鼓励 premature optimization。只有存在性能需求、用户/监控报告、疑似回归、大数据量或高流量场景时，它才应成为专项审查入口。

### Persona 分层

`agent-skills/agents` 中有三个 review 相关 persona，它们与上述 skills 互补：

- `code-reviewer`：Staff Engineer 视角，做五轴 code review。它会吸收 `code-review-and-quality` 的标准，并可在报告中指出 simplification、security、performance 相关问题。
- `security-auditor`：Security Engineer 视角，做漏洞、威胁模型和 OWASP 风格审查。它与 `security-and-hardening` 的关注面重合，但 persona 输出是 audit report。
- `test-engineer`：QA Engineer 视角，做测试策略、覆盖缺口和 Prove-It pattern。它补足 verification / regression coverage 维度。

这些 persona 都是单一角色。它们不会互相调用。组合由用户或 slash command 完成。

### Orchestration 规则

`agent-skills` 明确区分三层：

- Skill 是 workflow，即 how。
- Persona 是角色，即 who。
- Command 是入口和组合器，即 when。

它认可的主要多 agent 模式是 `/ship` 的并行 fan-out：

- `code-reviewer` 产出 code quality report。
- `security-auditor` 产出 security audit report。
- `test-engineer` 产出 coverage analysis。
- 主会话合并三个报告，给出 go / no-go 和 rollback plan。

这个模型适合 production-bound review 或 pre-launch gate。

### Review family 的内部关系

`code-review-and-quality` 是默认入口。它的五轴 review 已经包含 readability、security 和 performance 的粗粒度检查。

当发现复杂度问题时，应转向 `code-simplification` 的更严格原则：先理解，再最小化、行为不变地简化。

当 change 触及输入、auth、数据、外部服务、secrets、权限或依赖风险时，应转向 `security-and-hardening` 的边界和 OWASP 检查。

当 change 触及性能预算、用户感知速度、大数据、高流量、bundle 或疑似回归时，应转向 `performance-optimization` 的 measure-first workflow。

这些专项 skill 是 review 的深挖路径，不是所有 change 都必须完整展开的固定流水线。

## RVF 的当前模型

### 核心目标

RVF 默认处理当前对话的 session-scoped 未提交工作，流程是：

1. 生成 scope-of-work / session context。
2. 尽量从 transcript 生成 session ownership manifest。
3. 构建 self-contained review packet。
4. 并行执行 santa-method double review。
5. 合并 reviewer 输出。
6. 对每个 processed issue 执行 validate/fix。
7. 生成中文总结和 handoff context。

RVF 明确禁止把 `git diff HEAD` 当作默认审查范围。diff 是证据，不是 scope 来源。

### 输出契约

RVF reviewer 不是报告型 persona。review pass 的完成态输出只能是：

- 精确 `NO_ISSUES`。
- 编号 issue list，每条必须包含 `路径:行号` 和简短中文说明。

以下输出都被视为 contract violation 或不可解析结果：

- “没有问题”。
- 空响应。
- 纯 prose。
- validate/fix verdict。
- 修复说明。
- handoff。
- 缺少 `路径:行号` 的问题列表。

这个约束是 RVF 能自动进入 merge 和 validate/fix 的基础。

### Reviewer 策略

RVF 默认执行两路独立 reviewer：

- 如果 external alternative reviewer 配置可用，则使用 Codex-native reviewer + external alternative reviewer。
- 如果 external reviewer 不可用，则使用两个 Codex-native mimic reviewer。

两个 reviewer 使用同一份 scope-of-work、manifest 和 review packet，但彼此不看对方输出。

### Validate / Fix 策略

RVF 不把 reviewer finding 直接当成真问题。每个 processed issue 必须进入验证：

- `REAL`：真问题，可独立最小修复。
- `FALSE_POSITIVE`：不成立，不改文件。
- `ELEVATE`：真问题但需要用户决策，不改文件。

默认 full 流程中，只要存在可解析 issue list，就必须启动至少一个 `pass_type: validate_fix` 子代理处理验证包。主会话不能因为问题看起来简单就直接跳过 validate/fix 子代理。

## 关键差异

### 触发边界不同

`agent-skills`：

- 通过 `/review`、`/ship` 或直接 persona 调用。
- 用户或 slash command 是 orchestrator。
- 适合明确要求“review this PR / current changes / before ship”的场景。
- 专项 skills 通常由风险面触发：复杂度、安全、性能分别进入对应 workflow。

RVF：

- 只应由 `$review-validate-fix` 显式调用。
- `agents/openai.yaml` 明确关闭隐式调用。
- Stop hook 只能在用户预配置后，通过 GUI fork 注入以 `$review-validate-fix` 开头的新用户 prompt。
- Stop hook 不应把 continuation 当作 fallback。

### Scope 模型不同

`agent-skills`：

- 默认围绕 current changes、staged changes 或 recent commits。
- 更接近普通 PR review。

RVF：

- 默认围绕当前 session 实际完成的工作。
- 必须写 scope-of-work。
- 有 transcript 时应生成 session manifest。
- 背景 WIP 和 unattributed dirty paths 不得主动纳入审查。

这是 RVF 和通用 review 最大的设计分界。

### 输出目标不同

`agent-skills`：

- 输出面向人读的 report。
- 会包含 Critical / Important / Suggestion、Overview、Verification Story、What's Done Well 等内容。
- 专项 skills 还可能输出 checklist、measurements、recommended refactor、security recommendations 或 performance budgets。

RVF：

- review pass 输出面向机器解析。
- 不允许概括代码做了什么。
- 不允许恭维。
- 不允许风格建议。
- 不允许输出 handoff。

RVF 的主会话才负责最终中文总结、分组、provenance 和 handoff。

### 多 agent 组合方式不同

`agent-skills`：

- `/ship` 的 fan-out 是不同角色并行：code quality、security、test coverage。
- 每个 persona 从不同专业角度产出不同类型报告。
- `code-simplification`、`security-and-hardening`、`performance-optimization` 更像按风险触发的专项 pass，而不是 `/ship` 固定 fan-out 中的 persona。

RVF：

- double review 是两个独立 reviewer 对同一审查范围找 bug。
- 两路 reviewer 的角色不一定不同，重点是独立性和盲点差异。
- merge 后进入 validate/fix，而不是只做 go / no-go。

### 写权限模型不同

`agent-skills`：

- review persona 通常只报告问题。
- 是否修复由后续用户或主 agent 决定。

RVF：

- review pass 必须只读。
- validate/fix pass 才允许最小修复。
- Stop hook 必须跳过 subagent，避免子代理停止时递归触发新的 RVF fork。

### 审计和排障深度不同

`agent-skills`：

- 主要依赖报告文本和 command convention。
- 安全和性能专项 workflow 依赖外部证据，例如 audit 输出、profile、Core Web Vitals、bundle analysis 或 before/after measurements。

RVF：

- 维护 run ledger。
- 保存 review packet、workspace snapshot、stdout/stderr、summary、events。
- 支持 command lock。
- 支持 external reviewer idle timeout 和 contract validation。

这些能力是 Stop hook 自动化和外部 reviewer 可靠运行所需，不属于普通 review skill 的职责。

## 可借鉴内容

RVF 可以继续吸收 `agent-skills` 的审查标准，但不应直接继承它的报告格式。

适合吸收的内容：

- 五轴 review 标准，尤其 correctness、architecture、security、performance。
- 先看测试、再看实现的习惯。
- 对 dead code、backwards-compat shim、无用变量和遗留注释的敏感度。
- `code-simplification` 的 Chesterton's Fence、preserve behavior exactly、scope to what changed、Rule of 500。
- `security-and-hardening` 的 three-tier boundary system、OWASP baseline、secrets / auth / input validation / dependency audit 检查。
- `performance-optimization` 的 measure-first workflow、before/after verification、N+1 / unbounded fetch / bundle / Core Web Vitals 风险识别。
- dependency discipline：新增依赖前检查现有栈、维护状态、漏洞、license、体积。
- review honesty：不 rubber-stamp，不把真实 bug 软化成偏好建议。
- large change splitting 的判断原则。

不适合直接吸收的内容：

- `code-reviewer` 的完整 report template。
- Critical / Important / Suggestion 作为 RVF reviewer 输出格式。
- “What's Done Well” 正向观察。
- `/ship` 的三 persona go / no-go 输出。
- 让 reviewer 自行建议调用其他 persona。
- 在没有测量证据时输出性能优化建议。
- 在 RVF review pass 中输出完整安全 audit checklist 或泛化 hardening plan。
- 把 simplification 变成主动改代码的 review pass；RVF review pass 仍必须只读。

这些内容会破坏 RVF 的可解析 review contract，或把 review pass 从 bug-finding 阶段升级成报告、专项咨询、重构执行或发布决策阶段。

## RVF 如何映射 Review family

### code-review-and-quality 的映射

RVF reviewer prompt 应吸收它的 bug-finding 标准：

- correctness bug。
- boundary / error path omission。
- broken invariants。
- dead code and stale compatibility remnants。
- architecture mismatch that creates real behavioral or maintenance risk。

但 RVF 不应采用它的 severity label 或 report template。RVF 的 severity / grouping 应由主会话在 merge 和 validate/fix 阶段内部处理。

### code-simplification 的映射

RVF reviewer 可以报告 simplification 相关问题，但条件应更窄：

- 复杂度导致真实 bug 风险、错误分支、遗漏边界或维护不可判定性。
- 改动留下死代码、无用 shim、不可达分支、误导性命名。
- 新增抽象没有当前用途，并且已经干扰正确性或审查。

RVF reviewer 不应输出纯风格简化建议。若简化需要实际改代码，应作为 `REAL` issue 进入 validate/fix，由 validate/fix pass 在行为不变前提下做最小修复，或在需要设计判断时 `ELEVATE`。

### security-and-hardening 的映射

RVF reviewer 应把安全问题当作一等 bug，特别是：

- 未校验外部输入。
- 注入、XSS、命令执行或路径处理风险。
- auth / authorization 缺失。
- secrets 泄漏到代码、日志、配置或 handoff。
- CORS / cookie / session / security headers 误配置。
- 依赖 audit 中可达的 high / critical 风险。

但 RVF review pass 不应输出完整 security audit report。它只应输出当前 scope 内可定位到 `路径:行号` 的真实问题。需要广泛安全设计决策时进入 `ELEVATE`。

### performance-optimization 的映射

RVF reviewer 应报告明确性能回归或高风险 anti-pattern：

- N+1 query。
- 无界数据获取。
- 明显阻塞主线程或同步重计算。
- bundle / large asset 明显膨胀且属于本次改动。
- list endpoint 缺分页。
- UI 变更导致可预期 layout shift 或 excessive re-render。

但 RVF reviewer 不应猜测优化。没有测量、没有明确性能需求、没有可定位 regression 时，performance concern 应避免进入 issue list。若确实需要 profiling 才能判断，应该 `ELEVATE` 或在 validate/fix 阶段要求先测量。

## 集成建议

### 不要把 agent-skills persona 或专项 skill 直接设为 RVF 内置 reviewer

`code-reviewer`、`security-auditor`、`test-engineer` 以及专项 skills 的默认输出都不是 RVF 可解析格式。直接使用会导致以下问题：

- 输出不是 `NO_ISSUES` 或编号 issue list。
- 容易包含 summary、suggestion、positive observation。
- 可能主动扩大到 full diff 或 launch readiness。
- 不保证 source-agnostic validate/fix handoff。
- security / performance / simplification 专项输出可能包含 checklist、plan、measurements 或 refactor advice，而不是单条 bug finding。

### 可以作为 external reviewer adapter 的输入参考

如果未来要让某个外部 reviewer 使用 `agent-skills` 体系，建议做 adapter：

1. 外部 agent 可以在内部参考五轴标准和 Review family 的专项标准。
2. adapter prompt 必须覆盖其最终输出格式。
3. 最终输出仍必须经过 `check_review_output.py`。
4. contract violation 时按 RVF 现有 retry / fallback / fail-close 规则处理。

也就是说，`agent-skills` 可以影响 reviewer 的思考标准，不能替代 RVF 的输出协议。

### 可以在 RVF 文档中补充审查标准说明

RVF 的 `references/review-prompt.md` 已经列出 bug、回归、未完成实现、错误假设、遗漏边界、安全问题、死代码等目标。若需要更明确对齐 `agent-skills`，可以补一小段：

- RVF reviewer 的 bug-finding 标准覆盖 correctness、architecture、security、performance、dead code 和 complexity-induced correctness risk。
- 但 reviewer 输出仍只允许 `NO_ISSUES` 或编号 issue list。
- readability / style 只有在导致真实 bug、维护风险或死代码时才应报告。
- performance issue 需要可定位 regression、明确 anti-pattern 或测量需求，不能凭感觉优化。
- security issue 只报告当前 scope 内可定位问题；跨系统 hardening plan 应升级或另开专项审查。

这样能吸收五轴思维，但不会把 RVF 变成通用 report review。

## 设计判断

RVF 当前设计不应退化为 `/review` 或 `/ship` 的替代实现。

更准确的定位是：

- `agent-skills /review`：一次人工可读的 code review，可按风险调用 simplification / security / performance 专项标准。
- `agent-skills /ship`：生产发布前多角色 go / no-go gate。
- `review-validate-fix`：一次 session-scoped post-work 自动审查、验证、修复和 handoff checkpoint。

三者可以共存。RVF 负责把“AI 完成了一段工作之后，怎样可靠地发现并处理自己可能引入的问题”自动化；`agent-skills` 负责提供通用工程实践和可复用 persona。

## 后续可选动作

如果要把这次对比转成实际改动，优先级建议如下：

1. 在 `references/review-prompt.md` 中补充 Review family 标准的一小段，但保留严格输出契约。
2. 明确 RVF reviewer 对 simplification / security / performance 的报告门槛：只报 scope 内、可定位、会影响行为/安全/性能预算的问题。
3. 在 setup 或 adapter 文档里说明 external reviewer 可以参考 `agent-skills` persona 和专项 skill，但最终必须遵守 RVF contract。
4. 不新增 `code-reviewer` / `security-auditor` / `test-engineer` 作为 RVF plugin agents，除非它们被明确包成只读且 contract-compliant 的 adapter。
