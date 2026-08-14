"""cline-kanban 终端投递契约的 RVF 侧判读层。

契约真相源 = ``~/.rvf/cross-repo-coordination-with-cline-kanban/terminal-delivery-interface-contract.md``
（单写者：cline-kanban task ``cdea3``；RVF 只读）。本包只做**判读**，不做子进程调用——
CLI 调用面在 ``plugins/.../scripts/cline_kanban_client.py``，两者刻意分开，使契约语义可以
在没有 kanban runtime 的环境里被完整测试。

⚠️ 命名边界（见仓库 ``AGENTS.md``）：本包只谈 **cline-kanban**，与 vibe-kanban 无关。
"""

from core.cline_kanban_terminal_delivery.cline_kanban_terminal_delivery_receipt import (
    ClineKanbanTerminalDeliveryReceipt,
    classify_cline_kanban_terminal_delivery_receipt,
    describe_terminal_delivery_receipt_for_operator,
    redispatch_disposition_for_terminal_delivery_failure_reason,
    resolve_terminal_delivery_status_convergence_timeout_seconds,
)

__all__ = [
    "ClineKanbanTerminalDeliveryReceipt",
    "classify_cline_kanban_terminal_delivery_receipt",
    "describe_terminal_delivery_receipt_for_operator",
    "redispatch_disposition_for_terminal_delivery_failure_reason",
    "resolve_terminal_delivery_status_convergence_timeout_seconds",
]
