"""RVF 会话改动作用域分配（host-中性）。

承载「一次 RVF run 要审什么」的事实层：reviewable-unit / hunk 身份、review
lease、scope allocation、tracker 事件账本与 committed-round 观测。子进程仅 spawn
``git``（host-中性版本控制原语，非 host agent 二进制），满足 ``core/`` host-free
不变量（R-1 精炼守卫：core 永不 spawn host agent binary）。

消费方直接 ``from core.session_scope_allocation.reviewable_unit_diff_tracker
import ...``，本包不做 re-export（保持单一来源、避免符号双暴露）。
"""
