"""子代理的 host 无关模型与原语（观测侧 + 调用侧）。

``SpawnRecord``（**观测侧**）描述「一次子代理派生」的足够元数据，以便定位其
独立 transcript 并写进 manifest。它对 host 中性：Codex 的 ``spawn_agent`` 与
Claude 的 ``Task``/``Agent`` 都归一到同一结构，host 特定的「如何发现 / 如何定位
rollout」逻辑放 ``adapters/<host>/subagent.py``。

``InvokeCommand``（**调用侧**）描述「以何 argv 启一个 headless 子代理」的 host
无关结果。host 特定的「构造哪条 argv」逻辑同样放 ``adapters/<host>/subagent.py``
的 ``build_analyze_command``；按 host 选哪个 adapter 的分派则留在 skill facade
（``rvf_analyze_thread.build_analyze_command``），与观测侧 ``subagent_capture``
facade 的分派形态一致。

``iter_jsonl_dicts`` 是通用 JSONL 逐行读取原语（跳过空行 / 解析失败行），供
adapters 扫描 rollout / transcript 时复用。本模块保持 ``core`` 契约——不 import
host SDK 或 ``subprocess``。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Iterable


@dataclasses.dataclass(frozen=True)
class SpawnRecord:
    """一次子代理派生的 host 无关描述，足以定位其独立 transcript。

    各字段含义在不同 host 下的映射：

    - ``call_id``: Codex = ``collab_agent_spawn_end.call_id``；Claude = 父会话
      ``Task`` tool_use 的 id（无法对应时为 None）。
    - ``agent_id``: Codex = ``new_thread_id``；Claude = 子代理文件的 ``agentId``。
    - ``role``: Codex = ``new_agent_role``；Claude 暂无显式 agent-type 字段，为 None。
    - ``nickname``: Codex = ``new_agent_nickname``；Claude = 子代理 ``slug``（随机名）。
    - ``prompt``: 派生时给子代理的初始 prompt（best-effort）。
    - ``ts``: 派生时刻（Codex = spawn 事件 timestamp；Claude = 子代理首条 record ts）。
    - ``line_index``: 在被扫描源中的序号（Codex = 主 rollout 行号；Claude = 发现序号）。
    """

    call_id: str | None
    agent_id: str
    role: str | None
    nickname: str | None
    prompt: str | None
    ts: str | None
    line_index: int


@dataclasses.dataclass(frozen=True)
class InvokeCommand:
    """启一个 headless 子代理的 host 无关调用结果。

    - ``argv``: 完整命令向量（首元素为 host CLI 二进制，由调用方解析后传入）。
    - ``uses_stdin``: prompt 是否经 stdin 喂入（``cat <prompt> | <argv>``）。当前两
      个 host 的 analyze 调用都为 True；保留该字段以便未来出现 prompt-as-arg 的
      host 形态时无需改签名。

    注意此结构只描述「怎么调」，不负责「调起来并收结果」——reviewer 经外部 config
    驱动、validate/fix 经 Kanban CLI 启动、analyze 经 detached tmux 启动，三者均**不**
    走统一 in-process runner，故本切片不引入 ``invoke_subagent(role,prompt,ctx)
    -> SubagentResult`` 那类 runner 抽象（详见 plan S2-invoke 实施后修正）。
    """

    argv: list[str]
    uses_stdin: bool


def iter_jsonl_dicts(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    """逐行读 JSONL，yield ``(line_index, obj)``，仅对 dict 行产出。

    空行 / 解析失败行静默跳过；文件不存在则不产出任何项。host 无关。
    """
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_index, raw in enumerate(handle):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                yield line_index, obj
