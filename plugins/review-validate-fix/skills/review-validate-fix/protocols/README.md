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

- Writer/opener: `scripts/rvf_handoff.py`
- Template: `references/handoff-template.md`

## Run Ledger And Dispatch State

Normative source:

- Ledger helpers: `scripts/rvf_logging.py`
- Stop hook dispatcher/runtime: `scripts/codex_stop_hook_router.py`, `scripts/codex_stop_hook_dispatcher.py`, `scripts/codex_stop_review_validate_fix.py`

内部字段说明属于 `internals/`；失败排查入口属于 `debug/`。
