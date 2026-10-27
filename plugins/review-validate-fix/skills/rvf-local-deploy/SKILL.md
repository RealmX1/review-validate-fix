---
name: rvf-local-deploy
description: 用户明确要求从 local main 安装或部署本机 RVF 插件时使用。
---

# 本机插件部署

只在已有本机部署授权时执行。用 `git worktree list --porcelain` 找到唯一 checkout `refs/heads/main` 的工作树，从那里运行：

```sh
python3 scripts/rvf_local_deploy.py --expected-local-main-commit HEAD --json
```

runner 核对本地 main 和运行来源，使用独立、带归属标记的部署源工作树构建 commit-addressed release，再通过 Codex/Claude Code 官方插件 CLI 安装。主工作树的 WIP 不参与打包或被 stash。插件安装不要求 RVF 内部 land、token、封窗或历史 run 资格。

工具自动选择已安装且包含 pytest/tomllib 的 Python >=3.11；可用 `RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE` 明确固定。显式环境不合格时保留原因，不自动安装依赖或替换用户配置。

预先读取安装回执中的旧 commit，完成后根据实际结果说明新旧源 commit、已安装版本、验证状态及持久回执路径。只有退出 0 且 `status: ok` 才报告安装成功；实际 source delta、重复注册、未知状态和失败不可隐藏。详细诊断按需读取回执及日志，不把全部机器字段复制到常规回复。

runner 失败时核对阶段和已发生的安装结果，再恢复；不要手写官方 cache 或伪造回执。部署源目录归属不明时保留原内容，先查其来源及是否可安全恢复；真实新增删除权限才需用户决定。

本 skill 不自动提交、同步 base、fetch、push、重启 Cline Kanban 或启动 Analyze。源代码整合与本机安装分别报告。
