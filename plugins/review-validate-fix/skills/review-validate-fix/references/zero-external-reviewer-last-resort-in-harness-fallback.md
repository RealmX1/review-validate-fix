# Zero-External-Reviewer 最后兜底：in-harness mimic double review

**仅当 `scripts/dispatch_reviewers.py` 报告 0 个 external reviewer harness 可用
（plan `routing_rule: R3`、`needs_last_resort_fallback: true`），即所有其他路径都失败时，
才读本文件。** 正常 / 降级路径（R0/R1/R2）一律由 `dispatch_reviewers.py` 派发两路
`alternative-reviewer:<harness-id>` external CLI reviewer，不走本兜底。

本兜底不是「单 reviewer」也不是「主会话自己 review 后伪装两路」。它是在本机一个
external reviewer CLI 都不可用时，用主 harness 的 in-harness subagent **模拟**
santa-method double review，让 review loop 不被 setup 缺失阻塞。

## 触发条件（必须同时满足）

1. `dispatch_reviewers.py` 的 `reviewer-plan.json` 为 `routing_rule: R3` 且
   `needs_last_resort_fallback: true`（本机无任何 enabled+probe 通过的 external harness）。
2. 用户**没有**在本轮显式要求「必须使用 external reviewer、不接受兜底」。
   若用户显式要求 external，则用 `dispatch_reviewers.py --require-external`，
   plan 会是 `status: failed, reason: no_reviewer_harness_available`，此时 fail-close，不读本文件。

## 兜底 setup（与 external reviewer 同契约）

两个 in-harness subagent reviewer，与 external reviewer 走**完全相同**的 review 契约：

- **clean context**：各自独立 subagent，不继承父线程历史、不读 `SKILL.md`、不读对方输出。
- **同一份输入**：同一个 `artifacts/inputs/` 下的 scope-of-work / session context 文件路径、
  同一份 `scope.contract.json`、同一份 review packet、`prompts/reviewer.md` 生成的 self-contained prompt。
  把文件**路径**交给它们读取，不要把大段 scope 文本分别粘贴。
- **写 result artifact**：各自写 canonical `review-result.json` 到
  `artifacts/reviewers/<reviewer-id>/`（与 external reviewer 同一目录布局），
  通过 `scripts/write_review_result.py` 生成、`scripts/check_review_result.py` 校验。
- **互不可见**：合并前一路输出不得进入另一路的 prompt 或上下文。
- **command lock**：可能冲突的命令按锁规则处理（默认假设另一路 reviewer 可能并行）。

## 来源标签

两路使用两个独立来源标签：`codex-mimic-reviewer-a` 和 `codex-mimic-reviewer-b`。
它们只出现在主会话合并表和 handoff 审计里，**不得**传给 validate/fix 子代理
（validate/fix 的 issue context 必须 source-agnostic，见 `review-merge-policy.md`）。

## 合并

兜底产出的两份 `review-result.json` 与 external reviewer 产出走**同一套**合并规则
（见 `review-merge-policy.md` 的 Contract Gate / Merge Rules）。merge/analyze 下游按
`artifacts/reviewers/*` 目录遍历消费，对来源是 external 还是 mimic 不敏感。

## 恢复到 external

用户之后配置了真实 external reviewer（任一 harness 在 `reviewer-registry.json` 中
enabled 且 `run_alternative_reviewer.py --preflight` 通过）后，下一轮
`dispatch_reviewers.py` 会自动回到两路 external，无需改动 review/validate/fix 的问题契约。
