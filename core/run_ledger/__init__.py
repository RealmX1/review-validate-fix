"""RVF run-ledger 基础设施（host-中性、stdlib-only）。

承载 ``RunLedger``、run/event id 生成、summary/events.jsonl 落盘、deploy 元数据
与 ``log_root`` 解析等「一次 RVF run 的可观测性账本」原语。纯 stdlib，无 host SDK、
无 ``subprocess``——满足 ``core/`` host-free 不变量。

消费方直接 ``from core.run_ledger.run_ledger import RunLedger, start_run, ...``，
本包不做 re-export（保持单一来源、避免符号双暴露）。
"""
