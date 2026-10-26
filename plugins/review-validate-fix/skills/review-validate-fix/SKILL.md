---
name: review-validate-fix
description: 用户明确调用 RVF，或主会话依据任务授权选择执行任务快照双审时使用。
---

# 任务快照评审

固定任务快照 → 两路独立 reviewer → 匿名合并 validate/fix → 主会话最终验证与提交 → 已授权的 base 同步。保留 SANTA-method 与角色独立上下文；默认一个 fixer 处理全部相关发现。

1. 核对任务需求、有效授权和实际 Git 工作树。选择固定基线及明确任务路径，背景 WIP 保持原样。将用户原话和必要实现说明保存为一份持久需求文件。
2. `rvf review-task-snapshot --repo <工作树> --base-ref <基线> --scope <相对路径> ... --requirements-file <文件>`。工具保存精确 run 目录、原始输入与输出，脚本等待两路独立进程；需要时用 `--reviewer-config <JSON>` 指定 CLI。两路都必须实际完成，缺腿和格式异常如实保留。不要另建 run 补一条腿。
3. 使用回执中的精确目录执行 `rvf resume-task-review --run-dir <目录> --fix`。先读结果，必要时主会话归并同根因发现；默认 fixer 只收到匿名 findings 和需求。在隔离工作树验证后生成补丁，主工作树有并发变化时保留补丁并接续合并。
4. 最终验证命令写成 JSON argv 数组列表，执行 `rvf resume-task-review --run-dir <目录> --validation-commands <文件>`。真实失败须解决。确认实际改动与结果后，主会话使用普通 Git 精确暂存并提交，再接续原 run 核对已发生的提交；不 rewind、不另起 land 会话。base 同步仅按既有授权执行，由同步工具自行保全 WIP 和核对 Git 状态。
5. 主 Agent 统一收集 reviewers、fixer 与自己发现的有价值问题。用 `rvf collect-issues --list-repository <owner/repo>` 查询开放和关闭问题，语义匹配后沿用稳定 `problem_key` 或指定已有 `issue_number`，新发现用稳定的 `observation_id`。准备报告后 `rvf collect-issues --report <JSON>`；格式按需读 `references/github-issue-observation-report.md`。写入失败保留持久材料，恢复后 `rvf collect-issues --flush`，不阻断提交或同步。

重启只接续原 run。每个 CLI 的同一故障最多原路重试一次，随后使用其他可用配置；结果未知先核对实际进程、Git 与远端写入事实。机器信息缺口自主补证，只有真实价值取舍、新权限或人独有信息才需用户决定。用户的 Stop、提交、同步、push 和部署限制继续有效。

常规回执只给状态、必要下一步与持久目录；详细日志按需读取。`rvf analyze --run-dir <目录>` 只在需要复盘时使用，不是交付门禁。报告分别说明评审、修复、验证、提交与同步的实际结果。
