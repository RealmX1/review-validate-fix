"""Host 注入契约包：host-身份词汇 + transcript-format 探测组合根（host-中性）。

S9c 起持有 RVF 唯一允许同时知晓 Codex / Claude 两套 transcript 签名的地方
（``host_transcript_format_detection``）。S9 收尾起再持有 ``HostAdapter`` 注入
契约（``host_adapter_protocol``）——core 业务逻辑模块经注入拿 host 行为，而 host
身份词汇与注入面本身集中于此包（core host-free 退出门对本包 carve-out）。
具体 Codex / Claude bundle 与 resolver 在 S10 装配（见 ``host_adapter_protocol`` 文档）。
"""

from core.host_adapter.host_adapter_protocol import HostAdapter
from core.host_adapter.host_transcript_format_detection import (
    HOST_CLAUDE,
    HOST_CODEX,
    detect_transcript_format,
)

__all__ = ["HOST_CLAUDE", "HOST_CODEX", "HostAdapter", "detect_transcript_format"]
