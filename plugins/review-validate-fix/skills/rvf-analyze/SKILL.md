---
name: rvf-analyze
description: 用户要求复盘精确 RVF run 时使用，只读分析，不启动新评审或修复。
---

# RVF 按需复盘

使用用户指定的精确 run 目录；没有目录时先定位其任务中的运行材料，不选择 latest 代替身份核对。

运行 `rvf analyze --run-dir <目录>` 获取状态及真实尝试记录。按需读取需求、patch、角色 prompt、stdout/stderr、findings、fixer 补丁、最终验证和 Git 提交事实。

解释发现、验证和补丁之间的因果关系，区分未完成、未验证与已完成；给出实际成本、重复工作及可改进点。复盘发现的有价值问题交主 Agent 统一匹配 GitHub Issue 并追加 observation。

不自动启动复盘，不修改业务源码，不制造成功事实或正式归档资格，不阻断提交或 base 同步。
