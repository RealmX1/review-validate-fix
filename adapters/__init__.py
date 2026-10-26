"""宿主只负责 hook 线上格式；RVF 核心不依赖原生回退或阶段控制面。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class HostAdapter:
    host_id: str
    directive_channel: str

    def stop_hook_agent_directive_channel(self):
        return self.directive_channel

    def user_prompt_context_payload(self, body):
        if self.host_id == 'kimi_code':
            return {'message':body}
        return {'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':body}}


def resolve_hook_host_adapter(host_id):
    channels = {'codex':'blocking_decision','claude_code':'non_error_additional_context',
                'kimi_code':'hook_specific_output_permission_decision_deny'}
    return HostAdapter(host_id, channels[host_id])
