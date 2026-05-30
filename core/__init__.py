"""RVF host-agnostic 业务核心（core）。

本包内代码必须保持 host 无关：**不得** import host SDK（如 ``claude_code_sdk``）
或 ``subprocess``，只消费由 ``adapters/<host>/`` 归一后的统一结构
(``core.transcript.models.NormalizedTranscript`` 等)。host 特定解析放 adapters。
"""
