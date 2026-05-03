# Global reviewed-diff tracker overhaul plan

## 背景

当前 RVF 的 change tracking 仍是 per-chat-session、transcript-derived：

- `plugins/review-validate-fix/skills/review-validate-fix/scripts/session_manifest.py` 解析 Codex JSONL 中的 `apply_patch` / `exec_command` 事件，得到 `owned_paths` / `owned_dirty_paths` / `unattributed_dirty_paths`。
- `plugins/review-validate-fix/skills/review-validate-fix/scripts/build_review_packet.py` 据此构造 review packet 的 scope。

问题在于没有跨 session 的 shared registry。当两个 Codex session、validate/fix 子代理或同一 clone 下的 Cline Kanban worktree 并发修改同一仓库时，每个 manifest 只能看到自己 transcript 内的归属；其他 session 拥有的路径在当前 session 看来只是 `unattributed_dirty_paths`，被静默归为背景 WIP。

`plugins/review-validate-fix/skills/review-validate-fix/references/session-scoped-change-tracking-plan.md` 第 5 行向前引用了本文档，作为长期方向指引。本文档把方向落成可执行的多阶段设计。

## 目标

- repo 级共享状态：跨 session、跨 worktree、跨 branch 维护 diff unit ownership。
- hunk-level granularity：精确到 git diff hunk，避免相邻编辑无谓互踢。
- 与现有 manifest / packet 流程兼容：不破坏既有 `--session-manifest` 调用方与旧 manifest schema。
- 渐进落地：四阶段，下一阶段不依赖前一阶段未发布的临时 hack。

## 阶段总览

| Phase | 范围 | 主要新增/改动 |
|---|---|---|
| 1 | tracker state schema + repo-key 推导 + atomic 写入；session_manifest 注册 claim；build_review_packet 读 tracker、暴露 cross-session conflict | `scripts/diff_tracker.py`、`scripts/session_manifest.py`、`scripts/build_review_packet.py`、`scripts/prepare_review_run.py` |
| 2 | reviewer lease 的 acquire / refresh / release（含 TTL）；run_alternative_reviewer 与 Codex-native reviewer 子代理在拿到 scope 时申请 lease | `scripts/diff_tracker.py` lease API；`scripts/run_alternative_reviewer.py`；reviewer prompt 协议 |
| 3 | activity probe / heartbeat：长跑 RVF session 周期性 tick `last_seen_at`；dispatcher 在每个 Stop event 上 refresh | `scripts/diff_tracker.py` heartbeat loop；dispatcher 集成 |
| 4 | stale-release sweeper：基于 TTL 回收被遗弃的 claim；在 prepare_review_run 启动期触发 | `scripts/diff_tracker.py` sweep_stale；`scripts/prepare_review_run.py` 启动钩子 |

Phase 1 已落地：`heartbeat` 是真实实现（在 prepare_review_run 复制 manifest 后调用一次以 tick `last_seen_at`）；`lease_*` 仍是 `NotImplementedError` stub，留给 Phase 2；`sweep_stale` 是返回空列表的 no-op，留给 Phase 4。Phase 2–4 落地时只需替换对应 stub，无需修改调用方。

## Phase 1 设计

### 状态目录

Tracker state 位于 `<log_root>/tracker/<repo-key>/`。`log_root` 沿用 `rvf_logging.log_root()`（依次解析 `CODEX_RVF_LOG_ROOT` → `CODEX_RVF_STATE_DIR` → `default_log_root_for_skill_dir`）。每个物理 clone 一个 tracker：`repo-key` 由 `git rev-parse --git-common-dir` 的绝对路径推导，因此同一 clone 的所有 worktree 共享同一个 tracker。

```
<log_root>/tracker/<repo-key>/
  state.json          # 当前 claims（temp+rename 原子写）
  events.jsonl        # append-only 审计日志（claim_added / claim_refreshed / claim_dropped / conflict_detected）
  meta.json           # repo 绝对路径、repo-key 推导、schema_version
  state.lock          # advisory file lock（flock），保护并发写
```

`repo-key` = `<basename(repo_root)>-<sha1(git_common_dir_abspath)[:12]>`。basename 用于人读，sha1 用于抗碰撞。

### state.json schema

```json
{
  "schema_version": 1,
  "repo": "/abs/path/to/repo",
  "git_common_dir": "/abs/path/to/.git",
  "updated_at": "2026-05-02T...",
  "claims": [
    {
      "claim_id": "clm-<hex>",
      "session_id": "<codex session_id 或 fallback run_id>",
      "run_id": "rvf-...",
      "worktree": "/abs/path/to/worktree",
      "branch": "main",
      "path": "src/foo.py",
      "unit": "hunk",
      "hunk_anchor": {
        "header": "@@ -10,4 +10,6 @@ def bar():",
        "context_hash": "<sha1(prefix lines)[:16]>",
        "old_range": [10, 4],
        "new_range": [10, 6]
      },
      "evidence": "apply_patch" | "exec_command" | "git_diff",
      "claimed_at": "...",
      "last_seen_at": "...",
      "lease": null
    }
  ],
  "tombstones": []
}
```

### Diff unit 身份（hunk-level）

权威 hunk 来源是 `git diff -U0 --no-color HEAD -- <path>`（编辑后的 working tree 状态），不解析 apply_patch 文本。这样能同时覆盖 apply_patch 与 exec_command 两种 ownership 来源，并且在 manifest 重跑时即便丢失原始 transcript 事件也能稳定推导。

hunk anchor：
- `header`：完整的 `@@ -A,B +C,D @@ <function context>` 行，去除尾部空白。
- `context_hash`：紧随其后的最多 3 行 unchanged context 的 sha1（小写 hex 前 16 位）。
- `old_range` / `new_range`：从 header 解析的整数对。

匹配规则：两个 anchor 视为相同当 `header` 完全一致 **或**（`context_hash` 一致 **且** `old_range[0]` 在 ±5 行内）。

对 exec_command 派生的 ownership（如新建文件、shell redirect、untracked 文件），无法可靠观测到 hunk，则 claim 落在 `unit: "path"`。**path-level claim 与同一路径上的任意 hunk-level claim 互斥冲突。**

### 并发

原子写：`state.json` 用 temp 文件 + `os.replace()`。所有读写者一律用 `state.lock` 上的 `fcntl.flock(LOCK_EX | LOCK_NB)`（带轮询的非阻塞 exclusive lock）。粒度更粗但实现简单且与 `heartbeat` / `list_conflicts` 的写入路径统一；后续 Phase 如有读写吞吐问题可再细化为 `LOCK_SH` 读锁。锁等待 5s 超时；超时降级为 read-only 并写 `tracker_status: "lock_timeout"` 而不是抛错中断 review loop。

### 边界情况

- **bare repo / submodule**：跳过 tracker，输出 `tracker_status: "unsupported_repo"`。
- **非 git 目录**：当前 manifest writer 已在 `git_root()` 处抛错。
- **repo 在磁盘上被移动**：`meta.json` 记录首次创建 tracker 的路径；后续写入更新 `repo` 字段；key 因为绑定到 git_common_dir 保持稳定。
- **`CODEX_RVF_TRACKER_DISABLE=1`**：完全短路 tracker 读写，状态 `"disabled"`。

### 公共 API

`scripts/diff_tracker.py`：

```python
def repo_key(git_common_dir: Path) -> str: ...
def tracker_dir(log_root: Path, repo_key: str) -> Path: ...
def derive_hunk_anchors(repo: Path, path: str) -> list[HunkAnchor]: ...
def register_claims(*, repo, session_id, run_id, worktree, branch,
                    owned_paths, apply_patch_paths, exec_only_paths) -> RegisterResult: ...
def list_conflicts(repo, *, current_session_id, owned_units) -> list[Conflict]: ...

# Phase 2-4 stub：
def lease_acquire(...): raise NotImplementedError
def lease_refresh(...): raise NotImplementedError
def lease_release(...): raise NotImplementedError
def heartbeat(...): pass    # Phase 1 仅更新 last_seen_at
def sweep_stale(...): return []
```

### 集成点

- `session_manifest.py` 在写出 manifest 时调用 `register_claims`，并在 manifest 输出加 `tracker` 字段（包含 `repo_key`、`claim_ids`、`status`、`tracker_dir`）。
- `build_review_packet.py` 在加载 manifest 后调用 `list_conflicts`，新增 `## Cross-Session Conflicts` section（仅在有冲突时出现），同步写 `metadata["cross_session_conflicts"]`。
- `prepare_review_run.py` 在复制 manifest 到 `artifacts/inputs/` 之后调用 `heartbeat`；通过 `review-env.sh` 注入 `RVF_TRACKER_DIR` 与 `RVF_TRACKER_REPO_KEY`，便于 reviewer 子代理在 Phase 2 接入。

### 失败语义

tracker 写入失败一律 non-fatal：尽力写 events.jsonl、把 `tracker_status` 写入 manifest，绝不抛错中断 review loop。这与 `rvf_logging` 的 `log_unavailable` 哲学一致。

## Phase 2：reviewer lease

### 动机

Phase 1 只暴露冲突，不阻止两个 reviewer 并发审同一段 hunk。Phase 2 引入 lease：reviewer 拿到 scope 时申请 lease，活跃期间持有，结束或异常时释放。冲突由 lease 状态决定。

### lease schema 扩展

`claim.lease`：

```json
{
  "lease_id": "lse-<hex>",
  "holder_kind": "reviewer" | "validate-fix" | "manual",
  "holder_id": "rvf-...:reviewer-id",
  "acquired_at": "...",
  "expires_at": "...",
  "ttl_seconds": 600
}
```

### API

```python
def lease_acquire(repo, *, claim_ids, holder_kind, holder_id, ttl_seconds=600) -> LeaseAcquireResult
def lease_refresh(repo, *, lease_id, ttl_seconds=600) -> bool
def lease_release(repo, *, lease_id) -> bool
```

`lease_acquire` 返回 `acquired=False` + `conflicts=[...]` 时，调用方应：
- reviewer：写 `kind: request` 类型的 review-result artifact（lock-request 或 context-request），主会话决定如何处理。
- validate/fix：fail-close，主会话重新分配。

### 集成点

- `run_alternative_reviewer.py` 在启动外部 reviewer 前为 manifest 中的 `tracker.claim_ids` 申请 lease；reviewer 进程的活动 tick 同步 refresh lease；进程结束在 finally 中 release。
- Codex-native reviewer prompt 通过 `review-env.sh` 暴露 `RVF_TRACKER_LEASE_*` 入口；reviewer 启动 / 退出包装好 acquire/release。
- validate/fix 子代理同理。

### 死锁与饥饿

- TTL：默认 600s，reviewer 每 60s refresh。
- expired lease 视为已释放，由下一个申请方接管，并写 `events.jsonl: lease_expired`。
- 永不阻塞：lease_acquire 不等待，直接返回结果。

## Phase 3：activity probe / heartbeat

### 动机

Phase 2 的 lease TTL 解决进程崩溃；但长跑 manual `$review-validate-fix` 或 Cline Kanban task 在等待用户操作期间需要保持活跃。引入 heartbeat 让活跃但空闲的 session 主动续命。

### API

```python
def heartbeat(repo, *, session_id, run_id) -> HeartbeatResult
```

记录 session 级 `last_seen_at`，用于：
- Phase 2：所有该 session 持有的 lease 自动续期（最多续到 TTL 上限）。
- Phase 4：sweeper 判断 session 是否仍活跃。

### 集成点

- `codex_stop_hook_dispatcher.py` 在每个 Stop event 接收时调用 heartbeat。
- `run_alternative_reviewer.py` 在事件流中每 N 秒 heartbeat。
- `prepare_review_run.py` 启动期调用一次（Phase 1 已落地为 no-op stub）。

### 配置

- `CODEX_RVF_TRACKER_HEARTBEAT_INTERVAL_SECONDS`：默认 60s。
- `CODEX_RVF_TRACKER_SESSION_TTL_SECONDS`：默认 6h。

## Phase 4：stale-release sweeper

### 动机

session 异常退出（kill -9、机器重启、Codex desktop 崩溃）会留下 dangling claim/lease。Phase 4 通过 sweep 清理。

### API

```python
def sweep_stale(repo, *, max_age_seconds=21600) -> SweepResult
```

- 把 `last_seen_at` 早于阈值的 claim 移入 `tombstones`（保留 7 天用于回溯）。
- 释放对应 lease。
- 写 `events.jsonl: claim_dropped` / `lease_expired`。

### 集成点

- `prepare_review_run.py` 启动期调用一次；
- 长跑 reviewer 在拿到 lease 失败时主动调用一次再重试一次。

### 配置

- `CODEX_RVF_TRACKER_STALE_MAX_AGE_SECONDS`：默认 21600（6h）。
- `CODEX_RVF_TRACKER_TOMBSTONE_RETENTION_SECONDS`：默认 604800（7d）。

## 向后兼容 & rollout

- `--session-manifest` 调用方在 tracker 干净时 packet 字节级保持不变（新 section 仅在有冲突时出现）。
- Manifest schema 仅新增可选 `tracker` 对象；不读取该字段的旧 consumer 不受影响。
- 项目尚未分发，所有 backward compatibility shim 必须在 commit 前清理（见 `AGENTS.md`）。
- 灰度开关：`CODEX_RVF_TRACKER_DISABLE=1` 全程短路。

## 验证

- 单测：`tests/test_review_support_scripts.py` 增 10 条覆盖 Phase 1（写入、并发、idempotency、close-hunk 不折叠、冲突报告、path/hunk 互斥、disable、lock_timeout 降级、manifest tracker payload、cross-session conflict packet section 出现/缺席）。Phase 2–4 落地时各加对应测试。
- 集成手测：两个合成 transcript 跑 session_manifest + build_review_packet，预期看到 `## Cross-Session Conflicts`。
- 回归：原有 `test_build_packet_uses_session_manifest_as_scope_anchor` 等保持绿色。

## 不在本设计 scope

- 跨 host / 跨机器分布式 tracker（NFS、远端 sqlite 等）。本设计只覆盖单机多进程；远端协调走 git remote / Cline Kanban diff viewer。
- 把 `unattributed_dirty_paths` 强制清零。背景 WIP 仍是合法状态，tracker 只让“归属其他 session”从 unattributed 中分离出来。
