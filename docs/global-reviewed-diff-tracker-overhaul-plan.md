# Global reviewed-diff tracker overhaul plan

## 背景

当前 RVF 的 review scope 以单个 Codex chat session 为中心：`plugins/review-validate-fix/skills/review-validate-fix/scripts/session_manifest.py` 从 transcript 推导 `owned_paths` / `owned_dirty_paths` / `unattributed_dirty_paths`，Stop hook 只有看到当前 session-owned dirty paths 时才继续创建 GUI fork、Cline Kanban task 或 Kanban follow-up。

这解决了「同一 worktree 中其他 session 的 dirty WIP 被误纳入本轮 review」的第一层问题，但它不是 repo 级并发协调机制：

- 手动 fork 出来的多个 chat session 共享同一批 fork 前 dirty changes，每个 fork 都能从自己 transcript 把这些路径视作可 review scope，导致 reviewer 重复审查同一段 diff。
- 同一物理 clone 下的 `cline-kanban` worktree、validate/fix 子代理无法看见其他 session 已认领的 hunk，scope 在并发场景下会重叠。
- `session_manifest.py` 的 `unattributed_dirty_paths` 把所有不归属本 session 的 dirty 都归为「背景 WIP」，永远不会作为冲突暴露给 reviewer，也没有跨 session 的事实来源。

Phase 1 已经落地了 repo 级 shared registry（hunk-level claim、`state/tracker/<repo-key>/state.json` + `events.jsonl`、`build_review_packet.py` 的 `## Cross-Session Conflicts` section、heartbeat hook），让单文件 JSON 布局先跑起来。本文档把后续方向落成可执行的多阶段设计：把 diff ownership 从「per-chat session manifest」提升为「per-repo global reviewed-diff tracker」，引入 reviewer lease、activity probe、stale release、手动 fork takeover，并把状态层从单文件 JSON 迁到 SQLite。session manifest 仍可作为输入信号，但不再是最终 scope 决策源。

## 目标

- 每个 repo 拥有一个全局 reviewed-diff tracker；同一 repo 的多个 branch/worktree 在同一个 tracker 下分层记录。
- reviewer 领取 review 任务时，scope generator 必须给它分配未被其他 reviewer lease 的 diff units，并把这些 units 从其他 reviewer 后续 scope generation 中排除。
- 这个锁只影响 reviewer scope generation，不阻止主会话、validate/fix agent、Kanban task 或用户继续编辑文件；命令并发仍由 `command_lock.py` 处理。
- reviewer 有 activity probe / heartbeat；如果 reviewer stale，它持有的 diff units 自动回到 available pool。
- commit 不清除 tracker 中已 observed / assigned / reviewed 的 diff units。dirty diff 被提交后只改变 `observed_state`，不等同于「需要重新进入未审查池」。
- branch 或 worktree 删除时，对应 branch/worktree tracker state 应被 prune。
- 单个 chat session 的 assignment、manual RVF marker、Stop hook gating 都要改到 global tracker 内工作。
- 手动 fork 的 session 自动接管 parent session 的 unassigned diff scope；自动触发 review 时，candidate scope 必须严格来自该 takeover scope、child session assignment 和 child session-local hints 新归属的 units。

## 非目标

- 不把 tracker 变成文件写锁、merge 锁或 checkout 锁。
- 不要求 reviewer 拥有独占 repo 使用权；测试缓存、coverage、dev server 等仍由现有 command lock 处理。
- 不以 commit 作为 review 完成信号。review 完成必须由 reviewer result / run ledger / tracker completion event 表达。
- 不重新引入旧 runner、MCP 或 client 支线设计；涉及 Kanban backend 时仍以 `cline-kanban` / `kanban` CLI 契约为准。
- 不覆盖跨 host / 跨机器分布式 tracker（NFS、远端 sqlite 等）。本设计只覆盖单机多进程；远端协调走 git remote / Cline Kanban diff viewer。

## 阶段总览

| Phase | Scope | 状态 |
|---|---|---|
| 1 | repo-key 推导 + tracker state（单文件 JSON 布局）+ session_manifest 注册 claim + build_review_packet 暴露 cross-session conflict + heartbeat / lease / sweep_stale stub | **已落地**（commit `3f62fc1`） |
| 2 | 状态层迁移到 SQLite + JSONL；引入 units / sessions / leases / branches / worktrees 表；canonical_patch_hash unit identity；commit ≠ clear；branch/worktree prune | 设计落定，待实现 |
| 3 | reviewer scope allocator（8 步事务）；`scope.contract.json` 加 `primary_units` / `tracker_lease_id` / `tracker_scope_hash`；`prepare_review_run.py --tracker-scope` | 设计落定，待实现 |
| 4 | reviewer lease 的 acquire / refresh / release（含 TTL）；`run_alternative_reviewer.py`（claude + codex 两路）与 Codex-native reviewer 子代理在拿到 scope 时申请 lease；validate/fix 同理 | 设计落定，待实现 |
| 5 | activity probe / heartbeat 矩阵 + stale-release sweeper；dispatcher 在每个 Stop event 上 refresh；TTL 默认 6h | 设计落定，待实现 |
| 6 | Stop hook gate 重构（`resolve_stop_context` / `refresh_global_diff_tracker` / `evaluate_session_gate` / `allocate_auto_review_scope` 4 函数拆分）；reason code `no_session_owned_dirty` → `no_unassigned_review_scope`；手动 fork takeover；prompt / reference 更新 | 设计落定，待实现 |

Phase 1 的 stub（`lease_*` `NotImplementedError`、`heartbeat` no-op、`sweep_stale` 返回 `[]`）保留 API 形状，让 Phase 2–6 落地时不再破坏调用方。

## 分层模型

全局 tracker state 放在 plugin `state/` 下：

```text
<log_root>/diff-tracker/repos/<repo-key>/
  tracker.sqlite3                  # 主关系状态（units / sessions / leases / branches / worktrees / tombstones）
  tracker.sqlite3-wal              # SQLite WAL（运行时存在）
  tracker.sqlite3-shm              # SQLite shared memory（运行时存在）
  events.jsonl                     # append-only 审计日志（claim_added / lease_acquired / takeover / sweep / prune ...）
  meta.json                        # repo 绝对路径、repo-key 推导、schema_version、首次创建时间
```

`log_root` 沿用 `rvf_logging.log_root()`（`CODEX_RVF_LOG_ROOT` → `CODEX_RVF_STATE_DIR` → `default_log_root_for_skill_dir`）。`repo-key` = `<basename(parent of git_common_dir)>-<sha1(git_common_dir_abspath)[:12]>`（与 Phase 1 一致），由 `git rev-parse --path-format=absolute --git-common-dir` 派生，使同一 clone 的所有 worktree（包括 `cline-kanban` 创建的）落到同一个 tracker。不同 clone 即使 remote 相同，默认视为不同 repo。

repo 下两个语义层：

- **branch tracker**：记录某 branch 相对 base ref 的 committed / observed diff units，commit 后仍保留 review 状态。
- **worktree tracker**：记录某 worktree 当前 `HEAD` 上的 staged / unstaged / untracked overlay。

普通 branch worktree 的 tracker refresh 同时观察 branch unreviewed units 与 worktree overlay units，但自动 Stop hook allocation 不直接领整个 branch/worktree pool。scope generator 必须先经过当前 session assignment、manual fork takeover、session-local hints 过滤；detached HEAD 或无法稳定识别 branch 的情况以 worktree tracker 的 observation 为主，但仍保留同样的 session 过滤。

## 为什么用 SQLite + JSONL

Phase 1 用单文件 `state.json` + `flock` 已能跑，但 Phase 2 引入 units × sessions × leases × branches × worktrees 之间大量关系查询：

- 「找出某 branch 中未被 lease 的 unit」= JOIN `units` × `branches` LEFT JOIN `lease_units`。
- 「prune 一个 branch 同时 release 其 lease」= 单事务内 `UPDATE branches SET state='deleted'` + `UPDATE leases SET state='stale-released' WHERE lease_id IN (...)`。
- 「sweep 所有 expired lease」= `UPDATE leases SET state='stale-released' WHERE expires_at < ? AND state='active'`。
- 「session X 的 takeover 候选」= `units WHERE branch_key=? AND review_state='available' AND NOT EXISTS (SELECT 1 FROM session_units WHERE unit_id=units.unit_id)`。

这些操作在多文件 JSON 下是「load 所有相关 JSON → 在 Python 里过滤 → 多文件 temp+rename 写回」，原子性靠手写 flock 保证，且 prune / sweep 这类批量更新在 JSON 下要么写大量样板要么留一致性窟窿。SQLite 提供：

- 原生 ACID 事务（`BEGIN IMMEDIATE` 即可拿到 repo 内 cross-row 写锁）。
- `UNIQUE` / `FOREIGN KEY` / `CHECK` 约束替掉手写校验。
- 索引加 JOIN 让 8 步 allocator 走在毫秒级。
- WAL 模式让 reader 不阻塞 writer，多 reviewer 并发查询互不阻塞。
- Python stdlib 自带，零新依赖。

代价：失去对 state 文件 `cat`/`grep` 的直观排障，引入 `PRAGMA user_version` 的 schema migration 流程，测试要从「读 JSON」迁到「sqlite3 query」。`events.jsonl` 留作 append-only 审计 log 抵消「想 `tail -f` 看实时活动」的痛点 —— append-only log 本来也不需要关系查询。

## SQL schema

`tracker.sqlite3` 的 DDL 形如：

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;

-- 元信息（repo 路径、git_common_dir、schema 版本等）
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- diff observation
CREATE TABLE units (
  unit_id              TEXT PRIMARY KEY,         -- sha256(canonical_patch_hash)
  branch_key           TEXT,                     -- nullable for worktree-overlay-only
  worktree_key         TEXT NOT NULL,
  path                 TEXT NOT NULL,
  old_path             TEXT,                     -- rename source
  kind                 TEXT NOT NULL CHECK (kind IN
                         ('tracked_hunk','untracked_file','deleted_file','renamed_file','binary_file','path_only')),
  -- Slice 2-A annotation: `path_only` is the 6th legal kind, used as fallback
  -- for ownership the worktree can't observe (e.g. exec-only chmod). It is
  -- mutually-exclusive with any other kind on the same path.
  -- Slice 2-A annotation (rename): `renamed_file` 与 change_type='rename' 在
  -- Slice 2-A 的 DDL 中已收紧出 CHECK；现行 register/list 路径走单 path 的
  -- `git status -z -- <path>`，不会产出 `R` 行，因此 rename 在两种状态下分别落成：
  --   * worktree-only rename（`mv`）：新 path 是 untracked_file（`??` 行），
  --     旧 path 是 deleted_file（` D` 行）；
  --   * staged rename（`git mv`）：新 path 是 `A ` 行，落到 _classify_path 的
  --     tracked_hunk 分支（HEAD 无该路径 → preimage_blob=None → change_type='add'），
  --     旧 path 是 `D ` 行 → deleted_file。
  -- `old_path` 列为预留。后续切片若要恢复 first-class rename 单元，需 schema
  -- bump（重新加入两个枚举值）+ 改用 `git diff --name-status -M HEAD` 的观察
  -- pipeline，并补完整 staged/worktree rename 测试矩阵。
  change_type          TEXT NOT NULL CHECK (change_type IN ('add','modify','delete','rename')),
  preimage_blob        TEXT,                     -- git blob sha for tracked_hunk
  postimage_hash       TEXT,                     -- sha256 of post-image content
  hunk_header          TEXT,                     -- debug-only metadata; 不参与 unit_id
  canonical_patch_hash TEXT NOT NULL,
  first_observed_at    TEXT NOT NULL,
  last_observed_at     TEXT NOT NULL,
  observed_state       TEXT NOT NULL CHECK (observed_state IN ('dirty','committed','superseded')),
  review_state         TEXT NOT NULL CHECK (review_state IN ('available','assigned','reviewed','tombstoned')),
  FOREIGN KEY (branch_key)   REFERENCES branches(branch_key)   ON DELETE SET NULL,
  FOREIGN KEY (worktree_key) REFERENCES worktrees(worktree_key) ON DELETE CASCADE
);
CREATE INDEX idx_units_branch        ON units(branch_key, observed_state);
CREATE INDEX idx_units_worktree      ON units(worktree_key, observed_state);
CREATE INDEX idx_units_path          ON units(path);
CREATE INDEX idx_units_review_state  ON units(review_state);
CREATE INDEX idx_units_canonical     ON units(canonical_patch_hash);

-- chat session assignment
CREATE TABLE sessions (
  session_id            TEXT PRIMARY KEY,
  current_worktree_key  TEXT NOT NULL,
  parent_session_id     TEXT,
  channel               TEXT,                    -- 'stable' | 'dev'（来自 router）
  disabled              INTEGER NOT NULL DEFAULT 0,
  created_at            TEXT NOT NULL,
  last_seen_at          TEXT NOT NULL,
  FOREIGN KEY (current_worktree_key) REFERENCES worktrees(worktree_key)
);
CREATE INDEX idx_sessions_parent     ON sessions(parent_session_id);
CREATE INDEX idx_sessions_last_seen  ON sessions(last_seen_at);

-- 多对多：session ↔ unit
CREATE TABLE session_units (
  session_id      TEXT NOT NULL,
  unit_id         TEXT NOT NULL,
  assignment_kind TEXT NOT NULL CHECK (assignment_kind IN ('owned','takeover','transferred')),
  assigned_at     TEXT NOT NULL,
  PRIMARY KEY (session_id, unit_id),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
  FOREIGN KEY (unit_id)    REFERENCES units(unit_id)        ON DELETE CASCADE
);
CREATE INDEX idx_session_units_unit  ON session_units(unit_id);
CREATE INDEX idx_session_units_kind  ON session_units(assignment_kind);

-- per-session 已完成的 manual RVF marker（仅 suppress 已完成 scope_hash）
CREATE TABLE manual_rvf_runs (
  session_id   TEXT NOT NULL,
  run_id       TEXT NOT NULL,
  scope_hash   TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  PRIMARY KEY (session_id, run_id),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

-- reviewer lease
CREATE TABLE leases (
  lease_id          TEXT PRIMARY KEY,
  session_id        TEXT NOT NULL,
  run_id            TEXT NOT NULL,
  reviewer_id       TEXT NOT NULL,
  holder_kind       TEXT NOT NULL CHECK (holder_kind IN ('reviewer','validate-fix','manual')),
  scope_hash        TEXT NOT NULL,
  state             TEXT NOT NULL CHECK (state IN
                       ('active','paused','completed','stale-released','failed-released')),
  ttl_seconds       INTEGER NOT NULL,
  created_at        TEXT NOT NULL,
  last_activity_at  TEXT NOT NULL,
  expires_at        TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX idx_leases_state     ON leases(state, expires_at);
CREATE INDEX idx_leases_reviewer  ON leases(reviewer_id, state);

-- 多对多：lease ↔ unit
CREATE TABLE lease_units (
  lease_id TEXT NOT NULL,
  unit_id  TEXT NOT NULL,
  PRIMARY KEY (lease_id, unit_id),
  FOREIGN KEY (lease_id) REFERENCES leases(lease_id) ON DELETE CASCADE,
  FOREIGN KEY (unit_id)  REFERENCES units(unit_id)   ON DELETE CASCADE
);
CREATE INDEX idx_lease_units_unit ON lease_units(unit_id);

-- branch tracker
CREATE TABLE branches (
  branch_key     TEXT PRIMARY KEY,         -- sha1(refname)[:12]
  refname        TEXT NOT NULL,
  base_ref       TEXT,
  state          TEXT NOT NULL CHECK (state IN ('active','deleted')),
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL
);

-- worktree tracker
CREATE TABLE worktrees (
  worktree_key   TEXT PRIMARY KEY,         -- sha1(worktree_path_abspath)[:12]
  worktree_path  TEXT NOT NULL,
  branch_key     TEXT,
  head_oid       TEXT,
  state          TEXT NOT NULL CHECK (state IN ('active','deleted')),
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  FOREIGN KEY (branch_key) REFERENCES branches(branch_key)
);

-- 退役行（unit / lease / session / branch / worktree drop 后的 7 天回溯）
CREATE TABLE tombstones (
  tombstone_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL CHECK (kind IN ('unit','lease','session','branch','worktree')),
  ref_id        TEXT NOT NULL,
  reason        TEXT NOT NULL,
  payload       TEXT NOT NULL,             -- JSON snapshot
  retired_at    TEXT NOT NULL,
  expires_at    TEXT NOT NULL              -- 默认 retired_at + 7d
);
CREATE INDEX idx_tombstones_expires ON tombstones(expires_at);
```

每张表都用 ISO8601 字符串保存时间，便于直接 `sqlite3 tracker.sqlite3 'SELECT ...'` 排障。所有跨行写入都包在 `BEGIN IMMEDIATE; ... COMMIT;` 内。

## events.jsonl

append-only 审计 log，每行一个 JSON object：

```json
{"ts":"2026-05-03T...","kind":"claim_added","session_id":"...","unit_id":"...","run_id":"..."}
{"ts":"...","kind":"lease_acquired","lease_id":"lse-...","reviewer_id":"reviewer-a","unit_ids":["..."]}
{"ts":"...","kind":"takeover","parent_session_id":"...","child_session_id":"...","unit_ids":["..."]}
{"ts":"...","kind":"branch_pruned","branch_key":"...","released_lease_ids":["..."]}
{"ts":"...","kind":"sweep_stale","released":3,"checked":42}
```

事件不参与状态计算，只用于排障和 replay。复用 `rvf_logging._append_jsonl`，避免重复实现 io 原语。

## 并发模型

- SQLite WAL 模式（`PRAGMA journal_mode = WAL`）：reader 不阻塞 writer；多 reviewer 并发查询时不互相等。
- 写入（register、allocate、heartbeat、lease 操作、prune）一律包 `BEGIN IMMEDIATE; ... COMMIT;`，确保 cross-row 一致性。`BEGIN IMMEDIATE` 立刻拿到写锁，避免 `BEGIN DEFERRED` 升级时的 `SQLITE_BUSY` race。
- `busy_timeout = 5000`：阻塞最多 5s，超时降级为 `tracker_status: "lock_timeout"`，仍把事件追加到 `events.jsonl` 以便后续 replay；不抛错中断 review loop。
- 不再需要单独的 `state.lock` flock 文件 —— SQLite 自身的 fcntl 锁覆盖了所有 mutation。

## Diff unit 身份

unit identity 来源于 `canonical_patch_hash`，对纯行号漂移免疫但对实质内容变更敏感。

### tracked_hunk

权威观察源是 `git diff --no-color HEAD -- <path>`（worktree 当前状态），不解析 apply_patch 文本。这样 apply_patch 与 exec_command 两种 ownership 来源都能稳定推导。

`canonical_patch_hash = sha256(canonical_payload)`，`canonical_payload` 拼接：

1. `path` 与 `change_type`（`modify` / `add` / `delete` / `rename`，`rename` 还包含 `old_path`）。
2. 每条 hunk 的 unchanged context lines（**保留行内容，剥离行号 metadata**）。
3. 每条 hunk 的 `+` / `-` 行（保留前缀与内容）。
4. 不包含 `@@ -A,B +C,D @@` 这类行号 header；保存的 `hunk_header` 仅作 debug metadata。

匹配规则只比较 `unit_id`：内容一致即同 unit。如果上方无关编辑导致 `@@` 行号变了但 hunk 内容未变，hash 不变，不会产生新 unit；hunk 内容真改了又能正确失效（产生新 unit_id，旧 unit 标 `superseded`）。

### untracked_file

`canonical_patch_hash = sha256("untracked\0" + path + "\0" + sha256(file_content))`。`postimage_hash` 写入文件全文 sha256。

### deleted_file / renamed_file

- `deleted_file`：`canonical_patch_hash = sha256("delete\0" + path + "\0" + preimage_blob)`，`preimage_blob` 是 `HEAD:<path>` 的 git blob sha。
- `renamed_file`：`canonical_patch_hash = sha256("rename\0" + old_path + "\0" + new_path + "\0" + preimage_blob)`；如果 rename 同时改了内容，再追加内容 hunk 的同算法。

### binary_file

`canonical_patch_hash = sha256("binary\0" + path + "\0" + preimage_blob + "\0" + postimage_sha)`。binary diff 不再尝试 hunk 拆分。

### path-level fallback

对无法可靠观测到 hunk 的 ownership（exec_command 创建未跟踪文件、shell redirect、symlink 操作），发 `kind: untracked_file` 或 `kind: tracked_hunk + change_type: add`；只有当 worktree 状态完全无证据（如纯 `chmod`）时才退到 path-level claim。**path-level claim 与同一路径上任意 hunk-level claim 互斥冲突。**

## Session assignment 与手动 fork takeover

每个 chat session 对应 `sessions` 表一行 + 多条 `session_units`。session manifest 仍负责从 transcript 提供「本 session touched 哪些 paths / hunks」的证据，但 Stop hook 不再直接拿 `owned_dirty_paths` 判断是否 review。

新流程：

1. Stop hook 解析 session id、parent session id/path、repo/worktree/branch/channel 信息（`resolve_stop_context`）。
2. `session_manifest.py` 或 hook ledger 产出 session-local ownership hints。
3. tracker refresh 把当前 repo diff 切成 diff units，写 `units` / `branches` / `worktrees` 表（`refresh_global_diff_tracker`）。
4. session-local hints 映射到具体 unit_id，作为 `owned` 写进 `session_units`。
5. scope generator 从当前 session 的 `owned` + `takeover` 单元 + session-local hints 映射出的可用 units 中计算本次 review scope；默认不领其他 session 的 global available units。

`RVF_STOP_HOOK: off/on/status`、`RVF_STOP_HOOK_CHANNEL` 与 manual RVF marker 都写入 `sessions.disabled` / `sessions.channel` 与 `manual_rvf_runs`，subsume 当前 `state/session-hook/<session>.json`。manual marker 不按整个 session suppress 后续自动 review；它只记录已完成 run 的 `scope_hash`。同 session 后续新增了未 review units，Stop hook 仍可触发。

### 手动 fork takeover

关键语义是「接管 parent 未分配 scope」，不是「复制 parent 所有 dirty scope」。

检测到当前 session 是另一 session 的 manual fork 时（来源依次：Codex transcript 的 `parent_session_id` → `state/session-hook` 中匹配 transcript path → `RVF_PARENT_SESSION_ID` env override）：

1. 找到 parent session row。
2. 计算 parent 的 unassigned diff scope：
   ```sql
   SELECT u.unit_id
   FROM session_units su JOIN units u ON u.unit_id = su.unit_id
   LEFT JOIN lease_units lu ON lu.unit_id = u.unit_id
   LEFT JOIN leases     l  ON l.lease_id = lu.lease_id AND l.state = 'active'
   WHERE su.session_id = :parent
     AND su.assignment_kind = 'owned'
     AND u.review_state = 'available'
     AND lu.unit_id IS NULL
   ```
3. 在单事务中：把这些 unit 在 `session_units` 中的 `assignment_kind` 从 `owned` 改成 `transferred`，并为 child 插入 `assignment_kind = 'takeover'`。
4. child 的 Stop hook candidate scope = `takeover` + child 自己的 `owned` + child session-local hints 新归属的 units。
5. parent 后续 Stop hook 不再为已 transferred units 自动派 reviewer，除非 child session 被删除、显式 release、或 takeover stale。

无法可靠识别 parent 时 fail-soft：只用当前 session-local hints 可归属到当前 session 的 units，并在 RunLedger 写 `fork_parent_unresolved`，不伪造 takeover、不默认领取 repo/global available pool。

## Reviewer scope allocator

reviewer scope allocation 是 tracker 的原子操作。新增脚本入口 `scripts/diff_tracker.py allocate-review-scope`：

```bash
python3 scripts/diff_tracker.py allocate-review-scope \
  --repo "$RVF_REPO" \
  --session-id "$SESSION_ID" \
  --run-id "$RVF_RUN_ID" \
  --reviewer-id "reviewer-a" \
  --output-scope "$RVF_ARTIFACTS_DIR/reviewers/reviewer-a/scope.json"
```

8 步流程（全部在一个 `BEGIN IMMEDIATE` 事务内）：

1. 打开 SQLite 连接，`BEGIN IMMEDIATE`。
2. prune stale leases：`UPDATE leases SET state='stale-released' WHERE state='active' AND expires_at < :now`。
3. refresh observation：跑 `git diff --no-color HEAD`、`git status --porcelain` → upsert `units` 行（`observed_state` 转 `dirty`/`committed`，旧 `unit_id` 没出现的标 `superseded`）。
4. resolve session assignment：写 `sessions.last_seen_at`；若是 fork 第一次 stop，做一次 takeover transfer。
5. candidate = `session_units WHERE session_id=:current AND assignment_kind IN ('owned','takeover')` ∪ 新触碰的 hints；过滤 `units.review_state='available'`。
6. exclude leased：剔除已存在 active `lease_units` 中的 unit_id。
7. 创建 lease：`INSERT INTO leases (...)` + `INSERT INTO lease_units (...)`；`UPDATE units SET review_state='assigned' WHERE unit_id IN (...)`。
8. 写 scope manifest（含 `unit_ids`、`lease_id`、`scope_hash = sha256(sorted(unit_ids))`、`paths`、`hunks`、`source_session_id`、`takeover_from_session_id`），`COMMIT`，事件追加 `events.jsonl`。

lease 字段已在 schema 中定义。`lease_acquire` 不阻塞、不等待，candidate 为空时直接返回 `acquired=False, reason="no_unassigned_review_scope"`。

> Slice 3 实现注脚：`lease_acquire` / `lease_refresh` / `lease_release` 公共 API 仍保持 `NotImplementedError`；Slice 3 allocator 的 8 步流程直接在自己的 `BEGIN IMMEDIATE` 事务里写 `leases` / `lease_units` / `units.review_state='assigned'`。完整公共 API + reviewer-side heartbeat 落到 Slice 4。

lease 只影响 reviewer scope generation。validate/fix 仍受 `scope.contract.json` 的 `fix_allowlist` / `protected_files` 管理；命令并发仍用 `command_lock.py`。

## Stop hook gate 重构

把当前 `session_scope_gate_payload()`（`codex_stop_review_validate_fix.py`）拆成 4 个职责清晰的函数：

1. `resolve_stop_context()`：解析 repo、worktree、branch、session、parent session、latest user message、session hook control（`RVF_STOP_HOOK` / `RVF_STOP_HOOK_CHANNEL`）。
2. `refresh_global_diff_tracker()`：用 git diff/status、session manifest/hook ledger 和 parent fork info 更新 tracker（步骤 3–4 of allocator 的子集）。
3. `evaluate_session_gate()`：处理 `RVF_STOP_HOOK` 控制、`manual_rvf_runs` 中匹配 `scope_hash` 的 suppression。
4. `allocate_auto_review_scope()`：调上面的 allocator；scope 为空返回 `no_unassigned_review_scope`，scope 非空把 scope manifest 传给 `prepare_review_run.py`。

reason code 改名：`no_session_owned_dirty` → `no_unassigned_review_scope`。保留一个 release 周期的 alias，让 dispatcher 既有测试稳定。

dispatcher dev-sync gate（`codex_stop_hook_dispatcher.py`）也走 tracker：只有当前 Stop event 能 allocate 到非空 session/global scope 时才执行 dev sync 和 installed hook 转交。这避免 forked sessions 共享 dirty 时各自 transcript 都能看到 parent dirty 而重复同步。集成点：channel router（`codex_stop_hook_router.py`）选定 stable/dev 后，dispatcher 调 `allocate_auto_review_scope(dry_run=True)`。

## Activity probe 与 stale detector

每个 reviewer runner 必须能更新 lease heartbeat：

- **Codex-native reviewer**（主会话直接 spawn 的 codex 子进程）：主会话在 spawn、收到输出、wait 完成、超时或关闭时通过 `lease_refresh()` / `lease_release()` 写 events。reviewer 自身因为 `CODEX_RVF_SUPPRESS_STOP_HOOK=1`（commit `a7f9e67`）不会触发自己的 Stop hook，所以不会自 heartbeat —— heartbeat 必须由主会话承担。
- **External reviewer**（`run_alternative_reviewer.py`，包括 claude `claude_stream_json` 与 codex `codex_json` 两种 output_format）：在 stdout/stderr event 边界 tick；可选 `activity_probe_command` 做更深探测；超过 `idle_timeout_seconds` 没事件视为 idle。
- **Kanban reviewer/task**：通过 `cline_kanban_client.py` 的 `kanban task status` poll 写 heartbeat；task activity stream 与 run ledger checkpoint 同步进 events。

stale detector 不需要独立 daemon；首版在每次 allocation、heartbeat、Stop hook gate、prepare-run 前 lazy run（`sweep_stale`）：

- `last_activity_at` 超过 idle TTL 且 probe inactive：lease 标记 `stale-released`，对应 `lease_units` 释放，`units.review_state` 回到 `available`。
- process 已退出但没产生合格 review output：lease 标 `failed-released`。
- reviewer 完成并产出合格 `NO_ISSUES` 或 issue list：`lease.state='completed'`、对应 unit `review_state='reviewed'`。
- reviewer 输出 `RVF_*_REQUEST`：lease 保持 `active` 或转 `paused`，不释放 scope，直到主会话满足 request 后重试或显式取消。

probe 结果只写 tracker metadata 和 RunLedger，不进入 review packet、scope-of-work 或 reviewer issue merge。

## Branch / worktree prune

每次 tracker refresh 跑：

1. `git worktree list --porcelain`：不在列表里的 `worktrees.worktree_path` 标 `state='deleted'`，`UPDATE leases SET state='stale-released' WHERE session_id IN (SELECT session_id FROM sessions WHERE current_worktree_key=:wk)`，对应 `units` 因 `ON DELETE CASCADE` 跟随。
2. `git for-each-ref refs/heads`：不在列表里的 `branches.refname` 标 `state='deleted'`；`session_units.assignment_kind='takeover'` 中引用其 unit 的项 invalidate（标 `transferred` 让 parent 重新看到）。
3. tombstone：被 prune 的 unit / lease / session / branch / worktree row JSON snapshot 写 `tombstones`，默认保留 7 天供回溯。

prune 不会因 `git status --porcelain` clean 删除 unit。clean 只表示当前 worktree overlay 没有 dirty diff —— commit 后 unit 仍在 `units` 表中以 `observed_state='committed'` + 不变的 `review_state` 存在。这是「commit ≠ clear」契约的实现。

## Review packet 与 scope contract

`session-manifest.json` 演进为兼容的 `scope-manifest.json`：

- 保留 `owned_paths` / `owned_dirty_paths` / `unattributed_dirty_paths` 兼容字段，供旧 prompt 和测试迁移。
- 新增 `tracker_scope`：`unit_ids`、`lease_id`、`scope_hash`、`paths`、`hunks`、`source_session_id`、`takeover_from_session_id`。（Slice 2-B 已实现 consumer 侧：`prepare_review_run.py --tracker-scope <PATH>` splice 进 `manifest.tracker.tracker_scope`；producer 由 Slice 3 allocator 写入。）
- `build_review_packet.py` 优先用 tracker scope 生成 `## Tracker Scope` + path/hunk-limited `## Allocated Git Diff`。（Slice 2-B 实现 path-limited；hunk-limited 推迟，见下行。）
- `Full Git Diff HEAD (Evidence Only)` 保留但继续强调不是默认 review scope。
- Phase 1 的 `## Cross-Session Conflicts` section 退化为 debug aid：只在 allocator 完全 fail（candidate 为空或 lease 全冲突）时出现，列出阻塞的他 session lease。

`scope.contract.json` 扩展三个字段（Slice 2-B 已落 `SCOPE_CONTRACT_VERSION=2`，三字段 nullable 持久化于 top-level；`canonical_scope` 不含此三键以保持 `scope_hash` 派生稳定。validate-fix 在 unit 粒度 gate / lease release / manual-marker 抑制由 Slice 4–6 接管）：

- `primary_units: ["<unit_id>", ...]` —— validate-fix 在 unit 粒度而非 path 粒度上 gate fix scope。
- `tracker_lease_id: "lse-..."` —— validate-fix / 用户结束 review 时 release lease。
- `tracker_scope_hash: "sha256:..."` —— manual marker 据此 suppress 已完成 run。

`primary_files` 继续是 path 级 allowlist 兼容字段。首版可先 path-limited packet + hunk metadata 审计；真正 hunk-limited packet 留给后续切片，避免一次性重写 diff parser 与 packet builder。

## 与 RVF state phases / RVF_BACKEND 集成

main 在 commit `12c0f6b` 引入 `rvf_logging.RVF_STATE_PHASES` 与 `rvf_state_fields()`，run ledger 与 summary.json 携带 `rvf_state_phase` / `rvf_backend`。tracker events 与 lease records 同步采用：

- `events.jsonl` 每条事件加 `rvf_state_phase`（如 `prepare`、`review`、`merge`、`validate_fix`、`verify`、`handoff`、`complete`）与 `rvf_backend`（`manual` / `kanban-followup` / `kanban-task`）。
- `leases.holder_kind` 与 `rvf_state_phase` 对齐：`reviewer` 在 `review` phase，`validate-fix` 在 `validate_fix` phase，`manual` 跨 phase。
- channel router 的 `CODEX_RVF_SELECTED_CHANNEL`（commit `f03faa7`）作为 `sessions.channel` 持久化，让 dev/stable channel 的 Stop hook 都能落到同一 tracker，但 events 可分流分析。

## 配置 knobs

| 环境变量 | 用途 | 默认 |
|---|---|---|
| `CODEX_RVF_TRACKER_DISABLE` | 全程短路 tracker | unset |
| `CODEX_RVF_TRACKER_DB_PATH` | 覆盖 sqlite 路径（仅测试） | `<log_root>/diff-tracker/repos/<repo-key>/tracker.sqlite3` |
| `CODEX_RVF_TRACKER_LEASE_TTL_SECONDS` | reviewer lease TTL | 600 |
| `CODEX_RVF_TRACKER_HEARTBEAT_INTERVAL_SECONDS` | reviewer heartbeat 间隔 | 60 |
| `CODEX_RVF_TRACKER_SESSION_TTL_SECONDS` | session 视为 stale 的阈值 | 21600 (6h) |
| `CODEX_RVF_TRACKER_TOMBSTONE_RETENTION_SECONDS` | tombstone 保留期 | 604800 (7d) |
| `CODEX_RVF_TRACKER_BUSY_TIMEOUT_MS` | SQLite `busy_timeout` | 5000 |

## 兼容性与迁移

### 从 Phase 1 JSON 迁到 SQLite

首次以 Phase 2 代码访问已存在的 `state/tracker/<repo-key>/state.json`：

1. 读 `state.json` + `events.jsonl` + `meta.json`。
2. 创建 `state/diff-tracker/repos/<repo-key>/tracker.sqlite3`，跑全部 DDL。
3. 逐 claim 转换：每条 hunk-level claim 映射到 `units` 行（`canonical_patch_hash` 重新计算 —— Phase 1 的 anchor 与 Phase 2 不一致，重计算才能保证后续匹配稳定）；session 行从 claim 的 `session_id` 派生；当时没有 lease，跳过。
4. 旧的 `state.json` 移到 `state/tracker/<repo-key>/_legacy/state.json`，保留排障；`events.jsonl` 复制为 `state/diff-tracker/repos/<repo-key>/events.jsonl` 起头。
5. `meta.json` 写 `migrated_from: "json-v1"`、`migrated_at`、`schema_version`。

迁移幂等：再跑一次只刷新 `meta.json` 与 `last_seen_at`，不重复插入。

### 兼容字段

- manifest `tracker` 字段保持兼容；新增 `tracker_scope` 子对象，旧 consumer 不读。
- packet `## Cross-Session Conflicts` section 只在 allocator 失败时出现，干净路径下保持字节级兼容。
- `scope.contract.json` 的 `primary_units` / `tracker_lease_id` / `tracker_scope_hash` 字段加入 `SCOPE_CONTRACT_VERSION = 2`，旧 `version=1` 仍由 reviewer prompt 接受。
- 项目尚未分发，`AGENTS.md` 第 1–2 行要求 commit 前清理临时 BC；本设计仅添加永久字段或一次性迁移代码，无 `dev_backward_compatibility/` 入库。

### 灰度

- `CODEX_RVF_TRACKER_DISABLE=1`：完全短路 tracker 读写；manifest / packet 退到 Phase 0 行为。
- 有 `tracker.sqlite3` 但环境变量打开 disable：仍读 `meta.json` 报告 `tracker_status: "disabled"`，不写 sqlite。

## 测试矩阵

新增 / 替换以下用例（沿用 `tests/test_review_support_scripts.py` 的 shard-friendly 入口）：

- `test_canonical_patch_hash_stable_under_line_shift` —— 上方插入空行不改 hunk 内容，`unit_id` 不变。
- `test_canonical_patch_hash_changes_on_content_edit` —— hunk 内容真改了，旧 unit 标 `superseded` 并产生新 `unit_id`。
- `test_two_forked_sessions_only_one_allocates_shared_dirty` —— parent + child 同时 Stop event；第一个 allocator 拿到 lease，第二个返回 `no_unassigned_review_scope`。
- `test_takeover_stops_parent_auto_dispatch` —— child 第一次 stop 后，parent 后续 stop 不再为 transferred unit 派 reviewer。
- `test_stale_lease_release_allows_realloc` —— 模拟 lease expired，`sweep_stale` 后下一次 allocate 拿到同 unit。
- `test_completed_lease_marks_units_reviewed` —— reviewer 完成后 `lease.state='completed'` + `unit.review_state='reviewed'`，再次 allocate 不返回该 unit。
- `test_commit_preserves_review_state` —— dirty unit 被 commit；`observed_state='committed'`，`review_state` 不变。
- `test_branch_delete_prunes_units_and_releases_leases` —— 删 branch 后对应 row `state='deleted'`，相关 lease `stale-released`。
- `test_worktree_delete_prunes_only_worktree_overlay` —— 删 worktree 后只其 overlay unit 被 prune，branch 级 unit 不动。
- `test_manual_marker_only_suppresses_completed_scope_hash` —— 同 session 新增 hunk 后 Stop hook 仍触发。
- `test_kanban_worktree_shares_repo_key_with_parent_clone` —— `cline-kanban` 创建的 worktree 与父 clone 落到同一 sqlite。
- `test_allocator_concurrent_writers` —— 两个子进程同时 `allocate-review-scope`，`BEGIN IMMEDIATE` 串行化，不出现 unit 双分配。
- `test_migration_phase1_json_to_sqlite_idempotent` —— 跑两次迁移结果一致；`_legacy/state.json` 保留。
- `test_disable_env_short_circuits` —— `CODEX_RVF_TRACKER_DISABLE=1` 路径不创建 sqlite，manifest 报 `disabled`。
- `test_busy_timeout_degrades_gracefully` —— 外部进程持有写锁；超时降级为 `tracker_status: "lock_timeout"`，packet 仍能生成。

集成手测：两份合成 transcript 跑同一 repo + 一次 `allocate-review-scope` + 一次 reviewer release，预期 events.jsonl 有完整轨迹，sqlite 中 `units.review_state` 流转正确。

## 实施切片（与上节阶段总览对齐）

1. **Slice 2-A**：写 SQL DDL + migration runner（Phase 1 JSON → SQLite）；`diff_tracker.py` 内换实现，`register_claims` / `list_conflicts` API 表面不变。配套测试：`test_migration_phase1_json_to_sqlite_idempotent`、`test_canonical_patch_hash_*`。
2. **Slice 2-B**：`prepare_review_run.py --tracker-scope <json>`；`build_review_packet.py` 优先消费 tracker scope；`scope.contract.json` v2 字段。（已实现：`--tracker-scope <PATH>` splice 进 `manifest.tracker.tracker_scope`；strict-required-tolerant-extras 校验；`SCOPE_CONTRACT_VERSION=2` 顶层加 `primary_units` / `tracker_lease_id` / `tracker_scope_hash` 三字段（不进 `canonical_scope`）；packet 在 `## Session Manifest` 后插 `## Tracker Scope`，`## Allocated Git Diff` 替代 `## Session-Owned Git Diff`，path-limited（hunk-limited 推迟到 Slice 3+）；`review-env.sh` 导出 `RVF_TRACKER_SCOPE`。）
3. **Slice 3**：`scripts/diff_tracker.py allocate-review-scope` CLI + 8 步流程；Stop hook gate 4 函数拆分；reason code 改名 + alias。（已实现：CLI + 8 步原子流程在单 `BEGIN IMMEDIATE` 中跑完，`tracker-scope.json` / `events.jsonl` 落 post-COMMIT；`session_scope_gate_payload` 拆为 `resolve_stop_context` / `refresh_global_diff_tracker` / `evaluate_session_gate` / `allocate_auto_review_scope`；reason code 改名为 `no_unassigned_review_scope` / `unassigned_review_scope_available`，旧 `no_session_owned_dirty` / `session_owned_dirty` 作为 `reason_code_legacy_alias` 字段并保留 systemMessage 子串一个 release；`CODEX_RVF_TRACKER_DISABLE=1` 走 `legacy_session_scope_gate_payload`，不动旧 reason code；fork 第一次 stop 经 `_takeover_transfer_in_txn` 把 parent owned-and-unleased units 转给 child。dispatcher `should_sync_session_scope` 重写为 allocator dry-run + legacy 回退。lease 公共 API stub（`lease_acquire` / `lease_refresh` / `lease_release`）维持 `NotImplementedError`，留给 Slice 4。auto-flow 把 `tracker_scope_path` / `tracker_lease_id` / `tracker_scope_hash` stash 在 ledger 上（`tracker_scope_meta` 属性约定）；fork 提示词的 `--tracker-scope` 拼接交给 Slice 6。）
4. **Slice 4**：reviewer lease 集成（`run_alternative_reviewer.py` claude_json + codex_json 两路；Codex-native reviewer 主会话端 heartbeat）。
5. **Slice 5**：手动 fork takeover 算法 + 双 fork 共享 dirty 测试。
6. **Slice 6**：reference / prompt 更新；reviewer 从 `scope.contract.json.primary_units` 取 scope；session manifest 退为 evidence-only。

每个 slice 末尾跑：`python3 tests/test_review_support_scripts.py`、`python3 tests/test_codex_stop_hook_dispatcher.py`、`python3 tests/test_codex_stop_review_validate_fix.py`、`python3 tests/test_install_to_codex.py`、`bash scripts/check_skill_contracts.sh`、`python3 scripts/check_plugin_contracts.py`。

## 不在本设计 scope

- 跨 host / 跨机器分布式 tracker（NFS、远端 sqlite 等）。
- 把 `unattributed_dirty_paths` 强制清零；背景 WIP 仍是合法状态，tracker 只让「归属其他 session」从 unattributed 中分离出来。
- 用 tracker 做 commit / merge / push 锁。
- ORM。直接用 `sqlite3` stdlib + 手写 SQL（预计几百行）即可，避免引入额外依赖与抽象层。
