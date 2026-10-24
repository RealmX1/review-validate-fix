---
name: rvf-local-deploy
description: 当在 review-validate-fix 仓库中，用户要求 deploy、local install、sync 或从 local main 安装本机 Codex/Claude Code RVF plugin 时使用。
---

# RVF Local-Main Official CLI Deploy

本 skill 是稳定 trampoline。它只委托唯一 checkout `refs/heads/main` 的 local-main
worktree 中的 runner；installed plugin、官方 cache、task worktree 都不是 source checkout。

## Phase Zero

仅在用户明确要求本机部署，且工作已经完成 `rvf-land` 与 `base-branch-sync` handback 后：

0. **只读记录 OLD**：读 official installation receipt
   `~/.rvf/user-local-data/deployment-receipts/latest-official-cli-installation.json`
   的 `local_main_commit`，记为 OLD —— 它是「上次装进去的是哪个源 commit」的唯一事实源
   （设了 `RVF_USER_LOCAL_DATA_ROOT` 就读该根下的同一相对路径）。
   文件不存在或该字段缺失就如实记「无基线」，不要猜。**禁止用当前 HEAD 冒充 OLD**：HEAD 是本次
   要装的 NEW，拿它当 OLD 会让每次部署都报成「没有变化」；`generated_plugin_version` 只是版本串，
   同样反推不出 commit。这一步只读，**不得新增第二套部署标记，不得手写或改写任何 receipt**
   —— receipt 只由官方 CLI 安装路径写。
1. 用 `git worktree list --porcelain` 找到唯一 `branch refs/heads/main` 的 worktree。
2. 从该 worktree 运行：

```bash
python3 scripts/rvf_local_deploy.py --expected-local-main-commit HEAD --json
```

runner 会先选择并预检已有 Python 环境，再获取部署锁。部署及完整契约检查需要
Python >=3.11、`tomllib` 和同环境 pytest；自动选择优先 RVF 自有 `.venv`，其次为已有
3.12，再检查当前解释器和其他已安装 Python。调用仓库的 venv 不自动成为工具环境。
系统 3.9 只可用于选择器自举，不执行部署与契约业务。

若需显式固定，设置 `RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE=/绝对路径/到/python3.12`；
runner 和整个 Python/shell 子进程链都会使用该环境。显式环境不合格时直接报告原因，
不静默换环境。预检不下载解释器、不安装依赖，不要因旧环境恰好有 pytest 就退回旧版本。
单独测试使用 `"$RVF_TOOL_RUNTIME_PYTHON_EXECUTABLE" -m pytest`。

runner 会自行验证：脚本来自 local main、HEAD 等于本地 `refs/heads/main`、无进行中的 Git
操作，然后把 RVF 专用的 deploy 源 worktree（detached，`~/.rvf/deployment-source-worktree-at-local-main-head`）
同步到该 commit，并在**那棵树里**串行执行 contract check、生成 commit-addressed marketplace
bundle、Codex/Claude Code 官方 CLI 安装、双 cache/hook/user-local state verifier。

local main 的工作树全程只被读取 ref，从不被写入：它带着未提交改动也照常部署，不阻塞、不
参与 payload，也不会被 stash 挪走。payload 恒等于 HEAD 是结构性保证 —— deploy 源 worktree
是纯 tracked checkout，未提交内容不在对象库里，没有渗入路径。

deploy 源 worktree 的落点是 fail-closed 的：runner 只在该树带着自己建树时写下的归属证据
（存放在这棵 worktree 私有的 git admin dir 里，`reset --hard` / `clean -xdff` 抹不掉）时才
就地清空复用。只要该路径被 RVF 无法证明归属的内容占用（非空普通目录、别的仓库的 worktree、
挂着分支或没有归属证据的本仓 worktree、符号链接、文件），runner 会带占用原因报错
退出而**不做任何删除**。这是设计行为，不是 bug：请把处置权交回用户，报告占用原因并请其自行确认
后移走，禁止代为 `rm -rf`、`git worktree remove --force` 或换一个落点绕开。

禁止从 task worktree 执行旧 runner；禁止使用 dirty bypass；禁止改回 stash local main 的做法；
禁止在 local-main runner 失败后降级到手写 `~/.codex/plugins/cache`、`~/.claude/plugins/cache`
或 installed payload。此流程不 fetch、不 push，也不要求 commit 存在于 remote。

## Completion

仅当 runner 退出 0 且 JSON `status` 为 `ok` 才报告成功。最终证据必须包含：

- `local_main_worktree`、`local_main_commit` 与生成的 plugin version；
- `tool_runtime_python`：实际解释器绝对路径、版本、选择来源及模块预检结果；
- `deployment_source_worktree` 与 `deployment_source_worktree_synced_to_commit`（payload 的真实来源）；
- `local_main_dirty_paths`（本次部署期间 local main 上未参与打包的未提交改动）；
- `runner_matches_local_main_head`；为 `false` 时必须显式说明「本次执行的 runner 是未提交
  版本，被部署的是 HEAD 版本」，不得把它当成部署件里已含该改动；
- Codex/Claude marketplace 和 plugin selector；
- `removed_noncanonical_plugin_selectors`：本次自动清掉的非 canonical RVF 注册（正常应为空；
  非空说明机器上本来存在第二份 RVF，必须在报告里点名，不能当噪声略过）；
- `codex_registration_uniqueness.status`：**只有 `sole_registration` 可读成正常**，其余三态
  一律让 runner 非零退出——`multiple_registrations`（存在多份）、
  `canonical_selector_not_registered`（config 可读但 canonical 根本不在册，「没有第二份」
  不等于「canonical 在册」）、`unavailable`（根本没核到，不是「核过了、没问题」）；
- 双官方 cache digest 与 active release 一致；
- Stop/UserPromptSubmit hook 与 `RVF_USER_LOCAL_DATA_ROOT` 验证；
- 真实 reviewer routing smoke 及 fail-closed 结果（任务要求时）。
- `source_commit_delta`：**逐条转述 commit 级 delta**。只报 version 字符串、不给 commit 列表
  视为未完成。措辞直接引用 runner 写好的 `boundary_explanation`，锚在「已安装 plugin 现在反映
  这些源 commit」，不得写成「源码集成了这些 commit」。五个 `kind` 各自的汇报形态：
  - `forward`：逐条列出 `forward_commits` 的 `sha` 与 `subject`，一条不省。
  - `unchanged`：明说本次没有让已安装 plugin 反映任何新的源 commit。
  - `no_baseline`：明说没有上一次安装基线、本次不做区间比较，并给出 `new_commit`。
  - `rollback_or_diverged`：**必须同时给两个方向**（`reverse_commits` = 不再被反映、
    `forward_commits` = 新被反映），并显式标注这是回退或分叉；只报单向视为错报。
  - `unknown_revision`：只给 `old_commit` / `new_commit` 两端 SHA，明说不做区间，不要编区间。

  `subject` 里出现替换字符（`�`）说明该条提交信息不是 UTF-8，runner 已容错解码。原样转述并
  说明这是解码降级，**不要**把它猜回或「修」成通顺的文字——猜出来的 subject 看着可信却是伪造的。

  跑完 runner 后，若 `source_commit_delta.old_commit` 与 Phase Zero 第 0 步记下的 OLD 不一致，
  说明部署窗口内发生了并发部署：如实报告两个值，不要挑一个顺眼的讲。

注册唯一性这一类失败（`phase` 为 `official_cli_plugin_registration_uniqueness`——verifier 在
宿主插件清单或 `~/.codex/plugins/cache` 里看见第二份；或 `codex_registration_uniqueness`——
runner 收尾核对 `~/.codex/config.toml`）**刻意不回滚**：canonical 已经装好，回滚只会把它也移除、
留下残留那份，更差。这类失败的正确读法是「部署做完但拒绝认证」——按 `remediation` 清理残留后
重跑 runner 重新认证，`rollback_status` 会是 `not_required`。

失败时报告 `phase`、`installed_before/after`、`error`、receipt path，以及归档的上一份安装回执
`~/.rvf/user-local-data/deployment-receipts/previous-installation-before-latest-attempt.json`
（runner 在每次尝试前写它）；失败结果**不含** `source_commit_delta`，不要凭 `installed_before`
自行编一份「已投入使用」的 commit 列表。修复后重新从 local main 运行。本 skill 不自动执行
`rvf-land`、`base-branch-sync`、fetch、push 或 Cline Kanban listener restart。

若 `phase=python_runtime_preflight`，报告 `error_code` 和候选环境的预检结果；此时尚未获取
部署锁、未安装，也没有新的持久部署回执，不把上一份回执当作本次结果。版本不足
`python_version_too_old`、依赖缺失 `python_dependency_missing`、配置缺失
`configuration_missing`、读取失败 `configuration_read_failed` 和解析失败
`configuration_parse_failed` 必须分别报告。
