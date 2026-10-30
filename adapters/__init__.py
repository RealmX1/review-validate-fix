"""宿主适配原生 hook 输入与线上格式；RVF 核心不依赖原生控制面。"""
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HostAdapter:
    host_id: str
    directive_channel: str

    def stop_hook_agent_directive_channel(self):
        return self.directive_channel

    def normalized_user_prompt_text(self, prompt):
        if self.host_id == 'kimi_code' and isinstance(prompt, list):
            return '\n'.join(part['text'] for part in prompt
                             if isinstance(part, dict) and part.get('type') == 'text'
                             and isinstance(part.get('text'), str))
        return prompt if isinstance(prompt, str) else ''

    def user_prompt_recovery_event(self, event, prompt):
        normalized = {**event, 'prompt': prompt}
        if (self.host_id != 'kimi_code' or event.get('transcript_path')
                or any(event.get(key) not in (None, '') for key in ('turn_id', 'prompt_id', 'event_id'))):
            return normalized
        # 无可核对的原生消息身份时只检查来源变化，不退回文本摘要伪造新回合。
        normalized.pop('prompt')
        session = event.get('session_id')
        if not isinstance(session, str) or not session or Path(session).name != session or session in ('.', '..'):
            return normalized
        home = Path(os.environ.get('KIMI_CODE_HOME') or Path.home() / '.kimi-code').expanduser()
        # Kimi 在 UserPromptSubmit 前保存 prompt.submitted；turn.prompt 尚未写入 wire。
        # 只用宿主已保存的消息 ID：assistant/Stop/重复 hook 不会产生新的补证机会。
        latest = None
        try:
            with (home / 'server/events' / f'{session}.jsonl').open() as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    envelope = record.get('envelope') if isinstance(record, dict) else None
                    if not isinstance(envelope, dict) or envelope.get('session_id') != session:
                        continue
                    payload = envelope.get('payload')
                    if (isinstance(payload, dict) and payload.get('type') == 'prompt.submitted'
                            and payload.get('sessionId') == session and payload.get('agentId') == 'main'):
                        latest = payload
        except (OSError, ValueError):
            # 缺失/不可读的宿主记录不制造随机身份，也不向模型注入诊断。
            return normalized
        if (latest and latest.get('status') == 'running'
                and self.normalized_user_prompt_text(latest.get('content')) == prompt
                and isinstance(latest.get('promptId'), str) and latest['promptId']):
            normalized.update(prompt=prompt, prompt_id=latest['promptId'])
        return normalized

    def user_prompt_context_payload(self, body):
        if self.host_id == 'kimi_code':
            return {'message':body}
        return {'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':body}}


def resolve_hook_host_adapter(host_id):
    channels = {'codex':'blocking_decision','claude_code':'non_error_additional_context',
                'kimi_code':'hook_specific_output_permission_decision_deny'}
    return HostAdapter(host_id, channels[host_id])
