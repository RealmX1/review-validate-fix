"""Stop hook 出口受众分级契约包（host-中性）。

core 只声明「这条出口的受众是谁」，具体线上形状（``decision:block`` 还是
``hookSpecificOutput.additionalContext``）由注入的 ``HostAdapter`` 决定。详见
``stop_hook_output_audience_contract`` 模块文档与
``docs/architecture/stop-hook-output-audience-contract.md``。
"""

from core.stop_hook_output_audience.stop_hook_output_audience_contract import (
    STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION,
    STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY,
    STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT,
    STOP_HOOK_AGENT_DIRECTIVE_CHANNELS,
    STOP_HOOK_EVENT_NAME,
    STOP_HOOK_PERMISSION_DECISION_DENY,
    STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
    STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY,
    STOP_HOOK_OUTPUT_AUDIENCES,
    AgentDirectiveStopHookOutput,
    StopHookOutputAudienceContractViolation,
    render_stop_hook_payload,
)

__all__ = [
    "STOP_HOOK_AGENT_DIRECTIVE_CHANNELS",
    "STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_BLOCKING_DECISION",
    "STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_HOOK_SPECIFIC_OUTPUT_PERMISSION_DECISION_DENY",
    "STOP_HOOK_AGENT_DIRECTIVE_CHANNEL_NON_ERROR_ADDITIONAL_CONTEXT",
    "STOP_HOOK_EVENT_NAME",
    "STOP_HOOK_PERMISSION_DECISION_DENY",
    "STOP_HOOK_OUTPUT_AUDIENCES",
    "STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW",
    "STOP_HOOK_OUTPUT_AUDIENCE_DEVELOPER_ONLY",
    "AgentDirectiveStopHookOutput",
    "StopHookOutputAudienceContractViolation",
    "render_stop_hook_payload",
]
