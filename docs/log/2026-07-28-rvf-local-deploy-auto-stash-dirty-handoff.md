# 历史归档：local main dirty 自动 stash / pop 提案（已被替代）

> **状态：历史提案，已被专用部署源 worktree 方案替代。** 本文件用于追溯设计交接，不能作为当前部署或实施指令。
>
> **归档日期：2026-10-24。**

## 来源、受众与后续决策

- **原始来源与受众**：2026-07-28，开发者在 Cursor 会话 `0ba0e00f-aa76-4575-a7d7-0722589dcbfc` 中接受了 stash 后部署、结束后 pop 并汇报的方向，随后要求“生成一份 handoff，另外一个 agent 会对齐 review 并且完成改动”。下方原文是该 agent 写给下一位评估和实现 agent 的交接规格。
- **实际使用与方案变更**：Cline Kanban 任务 `052e2` 的初始请求直接引用本文件，要求分析提案。后续在 Claude Code 会话 `1f5acdcd-eedd-456a-9100-bda802a16646` 中，开发者提出 stash 会干扰同时在 local main 工作的其他 agent，并明确选择了“专用 deploy 源 worktree”方案。
- **替代实现**：2026-07-29，提交 `b214dcd7dda33572b17a76a5c73a7e5cbd0139ab`（`feat(deploy): rvf-local-deploy 改用专用 deploy 源 worktree 取代 dirty 硬拦`）提交了替代实现，并进入本地 `main`；归档时已核对该提交是当前本地 `main` 的祖先。
- **长期未提交的原因**：创建者当轮只交付了交接文件；任务 `052e2` 收尾时明确保留了 base worktree 原有的 dirty，包括本提案，没有将其纳入那次 8 文件提交，也没有更新提案状态。归档前的文件内容与原始写入记录完全一致。本次补充历史说明并纳入 Git 跟踪。

## 当前适用性

原文要求发布件绑定已提交 commit、避免未提交内容混入 commit-addressed payload 的目标仍然成立。当前流程从唯一 local main 启动 runner，将 RVF 自有的 detached 部署源 worktree 同步到冻结的部署 commit，再从该树执行部署；local main 的未提交内容保留在原处，并作为 `local_main_dirty_paths` 汇报。

原文的自动 stash/pop 方案及其恢复回执建议已经失效；文中“dirty 即硬失败”的现状描述，以及对应的旧测试和契约锚点也已被替换。当前行为以[当前 `rvf-local-deploy` skill](../../plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md)为准，其中明确禁止改回 stash local main。

下方“已拍板”“尚未改代码”“勿回退”以及待实现、验收和提交要求均保留为原始交接时的历史表述，不代表当前任务要求。原 stash 方案未按原文实施，因此不将原文状态改写为“已实现”。

## 历史原文（逐字保留）

原文 SHA-256：`0e3acedc5c389137130ea4e76ed1b654908e24533a78b145cd5418d0207431ee`（仅计算下方原文，不含归档说明）。

---

# Handoff — `rvf-local-deploy`：local main dirty 时自动 stash / pop

> - **日期**：2026-07-28
> - **受众**：下一位 agent — 对齐 review 本设计，并完成实现 + 契约/测试更新
> - **状态**：设计已由开发者拍板；**尚未改代码**。本 handoff 只交付规格，不做 git commit / deploy。
> - **工作区**：`/Users/bominzhang/Documents/GitHub/review-validate-fix`（当前对话仅讨论与写本文件；`AGENTS.md` / `Claude.md` 若有无关 dirty，保留、勿回滚）

---

## TL;DR

`scripts/rvf_local_deploy.py` 今日对 local main dirty **硬失败**。开发者接受的替代方案：

1. 仍默认且仅从**唯一** `refs/heads/main` local-main worktree 部署；
2. dirty 时**自动** `git stash push -u`（**无确认**），按干净 HEAD 打包/安装；
3. deploy **彻底结束**后（成功或失败）`stash pop` 恢复 dirty；
4. 最终 JSON / 人读输出**必须汇报** stash/pop 全过程；
5. **不**引入 `--allow-dirty`；**不**允许非 local-main source + 确认绕过。

打包语义不变目标：**进 marketplace bundle 的永远是 HEAD 树内容**，版本号仍为 commit-addressed；dirty 只是暂时挪开，不得进包。

---

## 背景：为何不能「放行 dirty」

Installer（`scripts/install_to_codex.py`）用 `shutil.copytree` 从 **worktree 文件系统** 拷插件/core/adapters，**不是** `git archive HEAD`。版本却标成：

`{base}-dev.{commit_count}+g{commit12}`

若允许 dirty 直接跑：payload 含未提交改动，receipt/version 仍声称干净 HEAD → **身份撒谎**。因此「放宽干净检查」不可接受；「stash 清场再部署」可接受。

当前硬拦点：

```165:170:scripts/rvf_local_deploy.py
    dirty_paths = _status_paths(local_main_worktree)
    if dirty_paths:
        raise DeployError(
            f"local main worktree 含未提交改动: {sorted(dirty_paths)}",
            exit_code=2,
        )
```

`_status_paths` 使用 `git status --porcelain=v1 -z --untracked-files=all`，**含 untracked**。

契约现状（必须同步改）：

- `scripts/check_skill_contracts.sh`：`require_repo_literal ... 'local main worktree 含未提交改动'`；`forbid_repo_literal ... '--allow-dirty'`
- `tests/test_developer_local_main_installation.py`：`test_validate_source_rejects_dirty_local_main`；`test_public_runner_cli_has_no_repo_or_dirty_bypass`
- skill：`plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md` 写「整个 worktree 干净」「禁止 dirty bypass」

---

## 已拍板语义（实现必须对齐）

### In scope

| 项 | 规格 |
|----|------|
| 触发 | local main 经 `_status_paths` 非空 |
| 确认 | **不需要**用户/agent 额外确认 |
| stash 命令 | `git stash push -u`（含 untracked；**不含** ignored） |
| stash 标识 | 命名 message，例如 `rvf-local-deploy:<pid>:<commit12>`，便于精确 pop / 失败后人工找回 |
| 清场后 | worktree 必须再次通过 `_status_paths == []`，否则 fail-closed |
| 打包/安装 | 仍只读 local main worktree；此时应等价于 HEAD |
| 恢复时机 | deploy **整段**结束后（含 contract / installer / verify / 任意早期失败），用 `try/finally` 保证尝试 pop |
| 进行中 Git 操作 | merge/rebase/cherry-pick 等 marker **仍硬拦**，不 stash |
| CLI | **禁止**新增 `--allow-dirty` / `--repo` |
| 非 local-main | **仍硬拦**（runner 必须来自唯一 main worktree）；不因「确认」放开 |

### Out of scope（明确不做）

- 非 local-main / task worktree 作为 source（即便确认）
- dirty 内容进 bundle
- dirty 版本后缀 / 内容 digest 实验通道（若以后要，另开 handoff）
- 改 `install_to_codex.py` 为 `git archive`（本任务用 stash 即可；不必顺手改打包）
- `rvf-land` / `base-branch-sync` / fetch / push / Kanban listener

### 失败与退出语义

1. **stash 创建失败** → 不进入 install；`status` 失败；无 pop。
2. **deploy 成功 + pop 成功** → `status=ok`；汇报 stash 曾发生并已恢复。
3. **deploy 失败 + pop 成功** → 保持原 deploy 失败 `status`/`phase`/`error`；额外汇报 dirty 已恢复。
4. **deploy 任意结果 + pop 冲突/失败** → **不得静默**；即使 install 已 ok，整体也不得报单纯成功：
   - 建议：`status` 用明确值（例如 `stash_restore_failed`）或 `ok` 时另设 `stash_restore_ok=false` 且 **exit code ≠ 0**；
   - receipt 必须留下 `stash_ref` / message，便于 `git stash list` 找回；
   - remediation 写明：安装可能已完成，但 local main dirty **未**自动恢复，需人工处理该 stash。
5. 并发：已有 `_deployment_lock`；stash 应在持锁之后、对 local main 动刀，避免与另一 deploy 交错（实现时把 stash/pop 放进现有 lock 临界区）。

### 最终汇报字段（JSON + 人读 stdout 均需可见）

至少包含：

- `dirty_paths_before`: string[]
- `stash_created`: bool
- `stash_message` / `stash_ref`（创建时）
- `stash_restored`: bool
- `stash_restore_error`: string | null

Completion 证据列表（skill）应追加：若发生过 stash，必须向用户汇报上述字段摘要。

---

## 建议实现落点

主文件：`scripts/rvf_local_deploy.py`

建议结构（实现者可微调，但行为必须等价）：

1. 将「发现 dirty → 硬失败」从 `validate_local_main_deployment_source` 拆出，或改为返回 `dirty_paths` 而不直接 raise（仍校验：唯一 main、runner 路径、HEAD==main、无 operation markers、expected commit）。
2. 在 `run_local_deploy` 持 `_deployment_lock` 后：
   - 若 dirty → named `stash push -u` → 再确认 clean；
   - `try: ... 现有 contract/install/verify ...`
   - `finally: 若 stash_created → pop 并写回 result 字段`。
3. `result_for(...)` / receipt 写入 stash 字段；失败路径也要带上。
4. 模块 docstring / `PARTIAL_REMEDIATION`：失败后仍优先从 local main runner 重跑；若 stash 未恢复，先处理 stash 再重跑。

文档/skill：

- 更新 `plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md`：
  - 删除「必须事先干净」的绝对表述；
  - 改为：runner 可自动 stash（含 untracked）→ 按 HEAD 部署 → 结束后 pop → **完成时必须汇报 stash/pop**；
  - 保留：禁止 `--allow-dirty`、禁止 task worktree runner、禁止手写 cache 降级。

契约 `scripts/check_skill_contracts.sh`：

- 去掉或改写对字面量 `local main worktree 含未提交改动` 的 `require_repo_literal`（该错误字符串可能不再作为主路径）；
- 改为 require 新行为相关字面量（例如 stash message 前缀、`stash_restored`、skill 中的汇报要求）；
- **继续** `forbid_repo_literal ... '--allow-dirty'`。

测试 `tests/test_developer_local_main_installation.py`：

- **替换** `test_validate_source_rejects_dirty_local_main`：改为「dirty 时 run/helper 会 stash、部署路径看到 clean、结束后 dirty 恢复」的单元/集成测（可用 monkeypatch 短路真实 install，但 **真跑 git stash/pop**）。
- 覆盖 untracked 文件也被 stash。
- 覆盖 pop 失败时非静默成功（可用 stub pop 失败）。
- **保留** `test_public_runner_cli_has_no_repo_or_dirty_bypass`（仍无 `--allow-dirty` / `--repo`）。
- 保留 reject runner-from-task-worktree / expected-commit mismatch。

---

## Review 清单（实现前先对齐）

实现 agent 在动手前请确认：

1. [ ] 理解：目标是 **HEAD-only payload**，不是 dirty-in-bundle。
2. [ ] stash 用 `-u`，不含 ignored。
3. [ ] 无确认；有命名 stash；`try/finally` pop。
4. [ ] pop 失败 ≠ 静默 ok；receipt 可找回 stash。
5. [ ] 不引入 `--allow-dirty`；不放开非 local-main。
6. [ ] skill + contract + tests 同步；GitNexus：改符号前 `impact`，commit 前 `detect_changes`（若用户要求 commit）。

---

## 验收标准

- local main 仅有 tracked 修改或 untracked 文件时，`python3 scripts/rvf_local_deploy.py --expected-local-main-commit HEAD --json` 能跑通安装路径（测试可用 stub），结束后工作区 dirty **恢复**。
- 生成的 release version / `source_commit` 仍对应该 HEAD；payload 不含 stash 前 dirty 内容（可用「dirty 文件内容标记」断言：staging/release 中不出现该标记）。
- JSON `status=ok` 时含 stash 汇报字段；人读输出也提及。
- pop 失败时 exit ≠ 0 或等价 fail-closed，且 stash 仍可定位。
- `forbid --allow-dirty` 仍成立；从 task worktree 加载 runner 仍拒绝。
- 相关单测与 `check_skill_contracts` 中本能力相关断言通过。

---

## 明确拒绝的替代方案（勿回退）

- dirty bypass / `--allow-dirty`
- 非 local-main + 人工确认即可 deploy
- 只 stash tracked、留下 untracked（清不干净，会再次撞 status 门）
- deploy 成功后不汇报 stash（开发者要求：**彻底结束后必须汇报**）

---

## 给实现 agent 的建议起步命令

```bash
# 读现状
sed -n '125,195p;380,480p' scripts/rvf_local_deploy.py
rg -n 'dirty|allow-dirty|stash|validate_local_main' scripts/rvf_local_deploy.py tests/test_developer_local_main_installation.py scripts/check_skill_contracts.sh plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md

# 改完后至少
pytest tests/test_developer_local_main_installation.py -q
# 若触及 contract 字面量
bash scripts/check_skill_contracts.sh   # 或仓库惯用的等价入口
```

实现完成后：把本 handoff 顶部状态改为「已实现」并注明 commit（若用户要求提交）；未要求则不要自行 commit。
