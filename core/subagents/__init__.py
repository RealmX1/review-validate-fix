"""子代理的 host 无关模型（``SpawnRecord`` 观测侧 / ``InvokeCommand`` 调用侧）与原语。"""

from core.subagents.models import InvokeCommand, SpawnRecord, iter_jsonl_dicts

__all__ = ["InvokeCommand", "SpawnRecord", "iter_jsonl_dicts"]
