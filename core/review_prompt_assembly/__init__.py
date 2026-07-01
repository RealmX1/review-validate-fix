"""RVF fork prompt 装配（review prompt assembly）——host-中性核（S10g）。

从引擎 PROMPT_ASSEM 簇迁入：把已冻结的父会话来源 dict 渲染成注入 fork prompt 的
``RVF_PARENT_CONVERSATION_*`` 元数据块。块标题里的 host 人类可读名（"Claude Code" /
"Codex"）由引擎 assembly wrapper 经注入的 ``HostAdapter.host_display_name()`` 预解析后
以 ``host_display_label`` 字符串传入，故本包不出现任何 host 身份常量或 host 归属键
token（守 R-1 core host-free 不变量）。
"""
