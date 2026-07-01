"""父会话来源冻结（parent conversation origin freeze）——host-中性核（S10e）。

从引擎 PARENT_CONTEXT 簇迁入：把「父会话 transcript + 预解析的 host 元数据」冻结成
一份可读来源标签 + 元数据 dict（供 handoff/task-title/prompt-block 消费）。host 相关的
读取（首条 user message 摘录、会话深链）经注入的 ``HostAdapter`` 取得；host 身份（前缀
Codex/Claude、host 归属键）由引擎 assembly wrapper 预解析/注入，故本包不出现任何 host
身份常量或 host 归属键 token（守 R-1 core host-free 不变量）。
"""
