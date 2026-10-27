# RVF 核心

`task_snapshot_review/` 负责固定输入、独立角色执行、可恢复进度、安全应用合并补丁及最终验证。`github_issue_observations.py` 负责独立的 GitHub 问题收集。

部署、用户本机目录与 hook stdout 受众翻译是独立叶子模块。核心不依赖 Kanban 原生阶段控制面、session/hunk 归属、review tracker、rewind 或 land。
