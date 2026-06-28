"""Host 注入契约包：host-身份词汇 + transcript-format 探测组合根（host-中性）。

S9c 起持有 RVF 唯一允许同时知晓 Codex / Claude 两套 transcript 签名的地方
（``host_transcript_format_detection``）。后续 HostAdapter Protocol + resolver
也住本包——core 业务逻辑模块经注入拿 host 行为，而 host 身份词汇本身集中于此。
"""

from core.host_adapter.host_transcript_format_detection import (
    HOST_CLAUDE,
    HOST_CODEX,
    detect_transcript_format,
)

__all__ = ["HOST_CLAUDE", "HOST_CODEX", "detect_transcript_format"]
