# adapters/codex/

Codex adapter（native 实装）。

## 当前状态

- **hooks chain**：仍在 `plugins/review-validate-fix/skills/review-validate-fix/scripts/`：
  - `stop_hook_router.py`（609 行）
  - `stop_hook_dispatcher.py`（1470 行）
  - `stop_review_validate_fix.py`（6594 行 main dispatch）
  - 物理迁移到 `adapters/codex/hooks/` 需要 `~/.codex/hooks.json` 路径同步；S3 之后再考虑。
- **subagent**：S2 落点。`adapters/codex/subagent.py` 包装现有 6–8 处 `subprocess.Popen("codex exec", ...)`。
- **transcript**：S1 落点。`adapters/codex/transcript.py`（从 `trajectory_distill.py` 拆 Codex 栈部分）。

## 6 维契约现状

| 维度 | 现状 |
|---|---|
| hook entry | `~/.codex/hooks.json` 注册 Stop hook（绝对路径，由 `install_to_codex.py` 写入） |
| subagent | `codex exec` Popen，6–8 处调用点；S2 经 `invoke_subagent` 抽象 |
| transcript | `RVF_USER_LOCAL_DATA_ROOT/run-logs/runs/rvf-*/artifacts/trajectory.jsonl`（Codex JSONL 格式）→ `NormalizedTranscript` |
| permission | Codex CLI 标准（cwd/env merge，sandbox 由 codex 控制） |
| config | nested `plugins/review-validate-fix/.codex-plugin/plugin.json`；生成的 `.agents/plugins/marketplace.json` 由官方 CLI 注册 selector `review-validate-fix@review-validate-fix-local-main` |
| discovery | `rvf_local_deploy.py` 在 RVF 专用 deploy 源 worktree（detached，同步到 local `main` 的 HEAD）上生成 commit-addressed marketplace；local main 工作树只被读取 ref，其未提交改动不阻塞部署也不进 payload。Codex 官方 `plugin marketplace add` / `plugin add` 管理 cache 与 installed state。RVF wrapper 仅结构化维护 `~/.codex/hooks.json`。 |

## 等待 reviewer：单次长 exec 会话（RVF 降本 T1.2）

Codex 没有 Claude Code 那种「后台任务退出即唤醒 agent」的完成通知，所以 `rvf wait` 在 Codex 下的等法是：

1. 用 `exec_command` 跑**一次** `rvf wait`。它按 host 推导默认等待时长（Codex 与 Claude 相同：detached 派发总超时 2700s + 120s 余量），一直阻塞到 reviewer 终态，再打印合并摘要。
2. `exec_command` 按自身 `yield_time_ms` 先返回一个仍在运行的 session；之后用 `write_stdin`（`chars` 为空）**空轮询**，`yield_time_ms` 取 **300000**，直到进程退出。每次空轮询就是一个 agent 请求，所以单次 yield 越长，等待阶段的请求数越少。

上限依据（Codex 0.158.0，2026-09-29 实测）：

- 二进制内嵌的 `write_stdin` 工具说明：`Non-empty writes default to 250 ms and cap at 30000 ms; empty polls wait 5000-300000 ms by default.`——空轮询的等待区间是 5000–300000ms。
- 实测：`codex exec` 里跑 `sleep 420`，以 `yield_time_ms=600000` 空轮询。第一次空轮询不晚于第 181s 发出、到第 483s 进程退出时才返回，即单次空轮询至少阻塞了约 300s；全程只用了 2 次 `write_stdin`（第二次只为取到退出前最后一行输出）。
- 这次实测分不清「600000 被照单执行、进程退出时提前返回」和「被钳到 300000、恰好在进程退出前后到点」：第一次空轮询的精确发出时刻测不到（`--ephemeral` 不留 rollout）。所以文档与 `rvf` 的提示一律取 300000，不依赖可能被钳掉的更大值。

<!-- EXPERIMENT_NODE: Codex 空轮询 yield_time_ms 是否真能超过 300000（能的话等待阶段可再省一半请求）；复测需保留 rollout 以拿到 write_stdin 的发出时间戳。 -->

Kimi 等其它 host 既没有完成通知、也没有可长 yield 的会话，保持有界前台轮询：`rvf wait` 单次最多阻塞 480s（< 前台工具 600s 上限），打印 `RVF_DISPATCH_STATE=running` 就再调一次。
