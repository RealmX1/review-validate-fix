# RVF Protocol Sources

本目录只索引机器协议的事实源，避免在 `SKILL.md` 中用自然语言复制 schema。

## Review Result

Normative source:

- Writer: `scripts/write_review_result.py`
- Checker: `scripts/check_review_result.py`

Reviewer 或 external reviewer 必须通过 writer 写 canonical artifact，并通过 checker 自检。主会话只消费 checker 认可的 artifact。

## Scope Contract

Normative source:

- Producer: `scripts/prepare_review_run.py`
- Packet builder: `scripts/build_review_packet.py`
- Consumers: review / validate-fix prompt artifacts generated for each run

`scope.contract.json` 是 review scope 的最终机器合同；session manifest 和 git diff 只作证据。

## Handoff

Normative source:

- CLI / 完成信号 / OS 通知: `scripts/rvf_handoff.py`
- 脚本拥有的确定性表头（`rvf:generated-header` marker 区块）: `scripts/handoff_generated_header.py`
- agent 拥有的正文契约（状态行、canonical 计数句、intake hints、验证段）: `scripts/handoff_document_contract.py`
- Template: `references/handoff-template.md`

表头由 `prepare_review_run.py` 在 prepare 阶段写入、由 `rvf_run_finalize.py` 在 run 终态幂等重渲；
正文契约由 `write_reviewed_task_candidate_evidence.py`（landing candidate 硬门控）与 finalize 的
`handoff_contract` 体检（只报不拦）共用同一组正则。

## Run Ledger And Dispatch State

Normative source:

- Ledger helpers: `scripts/rvf_logging.py`
- Stop hook dispatcher/runtime: `scripts/stop_hook_router.py`, `scripts/stop_hook_dispatcher.py`, `scripts/stop_review_validate_fix.py`

内部字段说明属于 `internals/`；失败排查入口属于 `debug/`。
