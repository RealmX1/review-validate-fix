# Plan/Doc Review Routing Scaffold

## 背景

RVF 的完整 review-validate-fix lane 面向实现改动：reviewer 找 bug，validate/fix
验证并修复，再由 handoff 总结。纯 planning / doc maintainer 工作不应默认进入完整
RVF，否则会为一份计划文档支付实现审查的 token、artifact 和 tracker lease 成本。

## 当前最小实现

Stop hook 在 dirty gate 后、session scope allocation 前做一个保守分类：

- dirty path 必须全部在 `docs/`、`doc/` 或 `.claude/plans/` 下；
- 文件后缀必须是 `.md`、`.mdx`、`.rst` 或 `.txt`；
- 至少一个文件名含 `plan`、`blueprint`、`prd`、`proposal`、`decision`、
  `scaffold`、`handoff`、`roadmap` 或 `rfc`。

命中后，Stop hook 写入 `reason_code=plan_document_only`，记录
`route=plan-doc-maintainer-review`，并跳过完整 RVF。当前只做 routing scaffold；
还没有启动独立 reviewer。

## 后续 Plan/Doc Maintainer Review Lane

独立 lane 的审查重点不是实现 bug，而是计划质量：

- 是否遵守 `AGENTS.md` 和 project-doc 约束；
- 是否混用 `cline-kanban` / `vibe-kanban` 等项目语境；
- 是否把 future work 写成已落地事实；
- 是否有明确 non-goals、acceptance criteria、handoff；
- 是否能进入 `planned-capability-implementation`；
- 是否存在不可验证、过大或无 owner 的 scope。

推荐形态是 sibling workflow，而不是 RVF mode：

- `$plan-review`
- `$doc-maintainer-review`
- 或由 `idea-development-planning` 产物触发的 review gate

## 非目标

- 不对普通 README / reference 文档自动跳过完整 RVF，除非文件名带 plan-like marker。
- 不引入 token budget guard。
- 不在本 scaffold 中实现独立 reviewer 或修复循环。
