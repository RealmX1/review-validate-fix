"""RVF review dispatch backend 选择（review dispatch）——host-中性核（S10i-a-0）。

从 scripts-level ``rvf_dispatch_flow`` 迁入：纯 stdlib-only 决策逻辑，把 RVF 运行模式 /
fork 启动模式 / 是否在 Kanban task 中，解析成派发后端（kanban / kanban-followup / gui /
manual / …）与 launch mode，并判定 cline-kanban 失败是否允许回退 legacy GUI。零 host
身份常量或 host 归属键 token（守 R-1 core host-free 不变量）。
"""
