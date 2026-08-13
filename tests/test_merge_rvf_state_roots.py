"""``scripts/merge_rvf_state_roots.py`` 的 merge / verify / switch 三个子命令。

这个工具做的是**不可逆**的事：把两棵状态树合成一棵、改写 64 个 review 高水位、
把一批 committed 单元推出观察窗口、再把 legacy 根封成只读归档。所以这里的测试
不追求覆盖率数字，只钉三类东西：

1. **静默丢数据**的路径——tombstone 主键撞号、WAL 未 checkpoint、身份冲突静默决胜、
   canonical 侧的 legacy 绝对路径漏改写。这类缺陷不报错，只是事后少了一截。
2. **fail-closed 闸真的会红**——未登记的顶层条目 / 未登记的表 / 源库在 merge 之后
   被改动 / 排练产物被拿去正式切换。闸写了却不生效，比没有闸更糟。
3. **marker 命名与运行时同源**——本工具写出的 marker 文件名必须与运行时读的那个
   一致；运行时哪天把仓身份 / harness 编进 key，这里要先红。

⚠️ 全部测试都在 ``tmp_path`` 里自建两棵根，**绝不碰真实的 ``~/.rvf`` 与
``~/.claude/rvf``**：本仓有过测试往真实根写出 462 个 ``repo-*`` 残留目录的前科。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import merge_rvf_state_roots as tool  # noqa: E402
from core.session_scope_allocation import review_highwater_marker  # noqa: E402
from core.session_scope_allocation import round_baseline_marker  # noqa: E402
from core.session_scope_allocation.reviewable_unit_diff_tracker import (  # noqa: E402
    DDL,
    SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def pin_every_rvf_state_root_environment_variable_into_the_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """兜底隔离：把所有状态根环境变量钉进 ``tmp_path``。

    本文件的每条用例都显式传 ``root=`` / 显式两根，理论上碰不到真实状态根；这个
    fixture 是第二道闸——万一某天有人加了一条走默认 ``log_root()`` 的用例，它会写进
    临时目录而不是真实的 ``~/.rvf``。本仓有过测试往真实根写出 462 个 ``repo-*`` 残留
    的前科，所以这道闸值得白拿。

    刻意**不**改 ``HOME``：隔离 HOME 会让用户 site-packages 里的 pytest 不可见，
    整个套件当场跑不起来（这条弯路已经走过一次）。
    """

    isolated = tmp_path / "isolated-rvf-state"
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(isolated / "run-logs"))
    monkeypatch.setenv("CODEX_RVF_STATE_DIR", str(isolated / "runtime-state"))
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(isolated))
    monkeypatch.setenv("RVF_HOME", str(isolated / "rvf-home"))


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def linear_git_repository(tmp_path: Path) -> tuple[Path, list[str]]:
    """三个提交的线性仓库，返回 ``(路径, [老→新 三个全 sha])``。"""

    repository = tmp_path / "sample-repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test")
    commits: list[str] = []
    for index in range(3):
        (repository / f"file-{index}.txt").write_text(f"content {index}\n", encoding="utf-8")
        _git(repository, "add", "-A")
        _git(repository, "commit", "-q", "-m", f"commit {index}")
        commits.append(_git(repository, "rev-parse", "HEAD"))
    return repository, commits


def _create_tracker_database(path: Path) -> sqlite3.Connection:
    """建一个**当前 schema 版本**的 tracker 库。

    ``PRAGMA user_version`` 必须显式钉到 ``SCHEMA_VERSION``：留在 0 会让
    ``_ensure_schema`` 走 bootstrap 分支（直接 executescript DDL），而真实的库走的是
    「已是当前版本 → 逐条 ``_ensure_*`` 补列」分支。两条路径读 ``PRAGMA table_info``
    的方式不同，夹具若停在 0，整条真实路径就一次都没被测到——实测正是这样漏掉了一个
    在真数据上百分之百会炸的缺陷。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    # 与 tracker 的 ``_open_conn`` 一致跑 WAL。夹具若停在默认 rollback journal，
    # 「merge 之后又写了一批事务」这类违规在测试里会表现为主文件字节变化，而真实的
    # WAL 库**主文件根本不动**——那条闸就等于没测到。
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(DDL)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()
    return connection


def _seed_minimal_repository_rows(
    connection: sqlite3.Connection,
    *,
    worktree_key: str,
    worktree_path: str,
    session_id: str = "sess-1",
) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO worktrees "
        "(worktree_key, worktree_path, branch_key, head_oid, state, first_seen_at, last_seen_at) "
        "VALUES (?,?,NULL,NULL,'active','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
        (worktree_key, worktree_path),
    )
    connection.execute(
        "INSERT OR REPLACE INTO sessions "
        "(session_id, current_worktree_key, parent_session_id, channel, disabled, created_at, last_seen_at) "
        "VALUES (?,?,NULL,NULL,0,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
        (session_id, worktree_key),
    )
    connection.commit()


def _insert_unit(
    connection: sqlite3.Connection,
    *,
    unit_id: str,
    worktree_key: str,
    path: str,
    observed_state: str = "committed",
    review_state: str = "available",
    last_observed_at: str = "2026-01-01T00:00:00Z",
    is_tombstoned: int = 0,
) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO units "
        "(unit_id, branch_key, worktree_key, path, old_path, kind, change_type, "
        " preimage_blob, postimage_hash, hunk_header, canonical_patch_hash, "
        " first_observed_at, last_observed_at, observed_state, review_state, is_tombstoned) "
        "VALUES (?,NULL,?,?,NULL,'tracked_hunk','modify',NULL,NULL,NULL,?, "
        " '2026-01-01T00:00:00Z',?,?,?,?)",
        (
            unit_id,
            worktree_key,
            path,
            f"hash-{unit_id}",
            last_observed_at,
            observed_state,
            review_state,
            is_tombstoned,
        ),
    )
    connection.commit()


def _insert_session(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    current_worktree_key: str,
    channel: str | None = None,
    last_seen_at: str = "2026-01-01T00:00:00Z",
) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO sessions "
        "(session_id, current_worktree_key, parent_session_id, channel, disabled, "
        " created_at, last_seen_at) "
        "VALUES (?,?,NULL,?,0,'2026-01-01T00:00:00Z',?)",
        (session_id, current_worktree_key, channel, last_seen_at),
    )
    connection.commit()


def _insert_session_unit(
    connection: sqlite3.Connection, *, session_id: str, unit_id: str
) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO session_units "
        "(session_id, unit_id, assignment_kind, assigned_at, run_id, branch, worktree, "
        " evidence, last_seen_at) "
        "VALUES (?,?,'owned','2026-01-01T00:00:00Z',NULL,NULL,NULL,NULL,"
        " '2026-01-01T00:00:00Z')",
        (session_id, unit_id),
    )
    connection.commit()


def _insert_tombstone(
    connection: sqlite3.Connection, *, ref_id: str, kind: str = "unit"
) -> None:
    connection.execute(
        "INSERT INTO tombstones (kind, ref_id, reason, payload, retired_at, expires_at) "
        "VALUES (?,?,'retired','{}','2026-01-01T00:00:00Z','2027-01-01T00:00:00Z')",
        (kind, ref_id),
    )
    connection.commit()


def _build_state_root_pair(
    tmp_path: Path,
) -> tuple[Path, Path]:
    legacy_root = tmp_path / "legacy-state-root"
    canonical_root = tmp_path / "canonical-state-root"
    for root in (legacy_root, canonical_root):
        (root / tool.RUNS_SUBDIRECTORY).mkdir(parents=True)
        (root / tool.REVIEW_HIGHWATER_SUBDIRECTORY).mkdir(parents=True)
        (root / tool.ROUND_BASELINE_PENDING_SUBDIRECTORY).mkdir(parents=True)
    return legacy_root, canonical_root


def _tracker_directory(root: Path, repository_key: str) -> Path:
    return root / tool.TRACKER_REPOSITORIES_SUBDIRECTORY / repository_key


def _write_repository_metadata(directory: Path, git_common_dir: str, repo: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / tool.TRACKER_REPOSITORY_METADATA_FILE_NAME).write_text(
        json.dumps({"git_common_dir": git_common_dir, "repo": repo}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 顶层条目登记表（fail-closed）
# ---------------------------------------------------------------------------


def test_every_directory_observed_on_the_real_state_roots_has_a_registered_strategy() -> None:
    """登记表必须覆盖实测在真实两根上出现过的每一个顶层条目。

    这份清单是**实测抄下来的**，不是照着计划抄的：原计划只列了四处分裂面，磁盘上
    实际有八处活的（多出 handoff-notified / kanban-task-suppressions /
    dispatch-confirmations / latest.json）。把实测清单钉进测试，是为了让「计划漏
    列」这件事只会发生一次。
    """

    observed_on_real_roots = {
        "diff-tracker",
        "runs",
        "review-highwater",
        "round-baseline-pending",
        "handoff-notified",
        "handoff-advised",
        "handoff-opened",
        "dispatch-confirmations",
        "followups",
        "kanban-task-suppressions",
        "post-analyze-quiet",
        "latest.json",
        "cross-run-analysis-2026-05-28.md",
    }
    unclassified = sorted(
        name for name in observed_on_real_roots if tool.classify_state_root_entry(name) is None
    )
    assert unclassified == []


def test_an_unregistered_state_root_entry_is_refused_rather_than_silently_skipped(
    tmp_path: Path,
) -> None:
    legacy_root, canonical_root = _build_state_root_pair(tmp_path)
    (legacy_root / "a-brand-new-state-subsystem").mkdir()

    assert tool.unregistered_state_root_entry_names(legacy_root, canonical_root) == [
        "a-brand-new-state-subsystem"
    ]

    exit_code, payload = tool.run_merge_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=tmp_path / "staging",
        conflict_policy=tool.DEFAULT_HIGHWATER_CONFLICT_POLICY,
        canonical_root_provenance_description="test",
        rehearse_without_quiescence_gates=True,
    )
    assert exit_code == 2
    assert payload["status"] == "refused"
    assert any(
        "a-brand-new-state-subsystem" in reason for reason in payload["blocking_reasons"]
    )


def test_atomic_write_temporary_files_do_not_count_as_unregistered_entries(
    tmp_path: Path,
) -> None:
    """marker 的原子写会留下 ``.name.<hex>.tmp``；闸不能因此在正常情况下也红。"""

    legacy_root, canonical_root = _build_state_root_pair(tmp_path)
    (legacy_root / ".task-abc.json.deadbeef.tmp").write_text("{}", encoding="utf-8")
    assert tool.unregistered_state_root_entry_names(legacy_root, canonical_root) == []


# ---------------------------------------------------------------------------
# 一致性快照 / 路径重写
# ---------------------------------------------------------------------------


def test_coherent_backup_captures_wal_content_that_a_plain_file_copy_would_lose(
    tmp_path: Path,
) -> None:
    """tracker 跑 WAL；只拷主文件会丢掉尚未 checkpoint 的事务且**不报错**。"""

    source = tmp_path / "source.sqlite3"
    connection = sqlite3.connect(str(source))
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE sample (value TEXT)")
    connection.execute("INSERT INTO sample VALUES ('committed-into-wal')")
    connection.commit()

    naive_copy = tmp_path / "naive-copy.sqlite3"
    naive_copy.write_bytes(source.read_bytes())
    coherent = tmp_path / "coherent.sqlite3"
    tool.create_sqlite_coherent_backup(source, coherent)
    connection.close()

    def visible_row_count(path: Path) -> int:
        """看得见几行。表本身还在 WAL 里时 sqlite 直接报 no such table ⇒ 记 0 行。"""

        probe = sqlite3.connect(str(path))
        try:
            return probe.execute("SELECT COUNT(*) FROM sample").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
        finally:
            probe.close()

    assert visible_row_count(coherent) == 1
    assert visible_row_count(naive_copy) == 0, "夹具没构造出 WAL 落差，这条测试就没在测东西"
    assert not coherent.with_name(coherent.name + "-wal").exists()


@pytest.mark.parametrize(
    ("value", "expected", "changed"),
    [
        ("/legacy/root/runs/a/b.json", "/canonical/root/runs/a/b.json", True),
        ("/legacy/root", "/canonical/root", True),
        # 兄弟目录只是**前缀相同**，不是根内部；改写它会把无关路径搬走。
        ("/legacy/root-other/runs/a", "/legacy/root-other/runs/a", False),
        ("/somewhere/else", "/somewhere/else", False),
        (None, None, False),
    ],
)
def test_legacy_absolute_path_rewrite_only_matches_true_path_prefixes(
    value: str | None, expected: str | None, changed: bool
) -> None:
    result, was_changed = tool.rewrite_legacy_root_absolute_path(
        value, Path("/legacy/root"), Path("/canonical/root")
    )
    assert (result, was_changed) == (expected, changed)


# ---------------------------------------------------------------------------
# tracker 合并
# ---------------------------------------------------------------------------


def _merge_one_repository(
    tmp_path: Path,
    *,
    seed_legacy,
    seed_canonical,
    repository_key: str = "sample-repo-000000000000",
    git_common_dir: str = "/tmp/sample/.git",
) -> tuple[tool.TrackerRepositoryMergeOutcome, Path]:
    legacy_root, canonical_root = _build_state_root_pair(tmp_path)
    for root, seed in ((legacy_root, seed_legacy), (canonical_root, seed_canonical)):
        if seed is None:
            continue
        directory = _tracker_directory(root, repository_key)
        _write_repository_metadata(directory, git_common_dir, "/tmp/sample")
        connection = _create_tracker_database(directory / tool.TRACKER_DATABASE_FILE_NAME)
        try:
            seed(connection)
        finally:
            connection.close()

    pairs = tool.discover_tracker_repository_pairs(legacy_root, canonical_root)
    assert len(pairs) == 1
    staging_root = tmp_path / "staging"
    outcome = tool.merge_tracker_repository_into_staging(
        pairs[0],
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
    )
    return outcome, Path(outcome.merged_database_path)


def test_tombstones_are_rekeyed_by_content_because_both_roots_start_numbering_at_one(
    tmp_path: Path,
) -> None:
    """``tombstone_id`` 是 AUTOINCREMENT，两根**必然**从 1 起算撞号。

    实测 legacy 侧 12 个仓的 ``max(tombstone_id)`` 全部等于行数。按主键并集会让一边
    的 1..N 覆盖另一边的 1..M，静默丢掉一批退休记录——而 tombstone 丢失会让**已退休
    的 legacy claim 复活**。
    """

    def seed_legacy(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(connection, worktree_key="wt", worktree_path="/tmp/sample")
        _insert_tombstone(connection, ref_id="legacy-only-a")
        _insert_tombstone(connection, ref_id="present-on-both")

    def seed_canonical(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(connection, worktree_key="wt", worktree_path="/tmp/sample")
        _insert_tombstone(connection, ref_id="canonical-only-a")
        _insert_tombstone(connection, ref_id="canonical-only-b")
        _insert_tombstone(connection, ref_id="present-on-both")

    outcome, merged_database = _merge_one_repository(
        tmp_path, seed_legacy=seed_legacy, seed_canonical=seed_canonical
    )
    assert outcome.blocking_reasons == []

    connection = sqlite3.connect(str(merged_database))
    try:
        reference_identifiers = {
            row[0] for row in connection.execute("SELECT ref_id FROM tombstones").fetchall()
        }
        total = connection.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0]
    finally:
        connection.close()

    assert reference_identifiers == {
        "legacy-only-a",
        "canonical-only-a",
        "canonical-only-b",
        "present-on-both",
    }
    assert total == 4, "内容相同的那条不该重复导入"


def test_assigned_units_are_demoted_because_active_leases_are_never_migrated(
    tmp_path: Path,
) -> None:
    def seed_legacy(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(connection, worktree_key="wt", worktree_path="/tmp/sample")
        _insert_unit(connection, unit_id="u-legacy", worktree_key="wt", path="a.py",
                     review_state="assigned")

    def seed_canonical(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(connection, worktree_key="wt", worktree_path="/tmp/sample")
        _insert_unit(connection, unit_id="u-canonical", worktree_key="wt", path="b.py",
                     review_state="assigned")

    outcome, merged_database = _merge_one_repository(
        tmp_path, seed_legacy=seed_legacy, seed_canonical=seed_canonical
    )
    connection = sqlite3.connect(str(merged_database))
    try:
        states = dict(
            connection.execute("SELECT unit_id, review_state FROM units").fetchall()
        )
    finally:
        connection.close()

    # 两侧都要降级：canonical 独有的那条也没有 lease_units 行可反查复位。
    assert states == {"u-legacy": "available", "u-canonical": "available"}
    assert outcome.unit_decision_class_counts.get(
        "demoted_assigned_to_available_because_lease_not_migrated"
    ) == 2


def test_undrained_legacy_lease_and_its_unit_links_are_not_migrated(tmp_path: Path) -> None:
    def seed_legacy(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(connection, worktree_key="wt", worktree_path="/tmp/sample")
        _insert_unit(connection, unit_id="u-1", worktree_key="wt", path="a.py")
        for lease_id, state in (("lease-active", "active"), ("lease-done", "completed")):
            connection.execute(
                "INSERT INTO leases (lease_id, session_id, run_id, reviewer_id, holder_kind, "
                " scope_hash, state, ttl_seconds, created_at, last_activity_at, expires_at) "
                "VALUES (?, 'sess-1', 'run-1', 'r1', 'reviewer', 'h', ?, 600, "
                " '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')",
                (lease_id, state),
            )
            connection.execute(
                "INSERT INTO lease_units (lease_id, unit_id) VALUES (?, 'u-1')", (lease_id,)
            )
        connection.commit()

    def seed_canonical(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(connection, worktree_key="wt", worktree_path="/tmp/sample")

    outcome, merged_database = _merge_one_repository(
        tmp_path, seed_legacy=seed_legacy, seed_canonical=seed_canonical
    )
    connection = sqlite3.connect(str(merged_database))
    try:
        leases = {row[0] for row in connection.execute("SELECT lease_id FROM leases").fetchall()}
        lease_units = {
            row[0] for row in connection.execute("SELECT lease_id FROM lease_units").fetchall()
        }
        dangling = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    assert leases == {"lease-done"}
    # 父行没迁，子行必须跟着不迁——否则留下悬挂引用。
    assert lease_units == {"lease-done"}
    assert dangling == []
    assert outcome.foreign_key_violations == []


def test_updating_a_unit_row_does_not_cascade_away_its_session_and_lease_links(
    tmp_path: Path,
) -> None:
    """``INSERT OR REPLACE`` 是先 DELETE 再 INSERT，而 units 被三张表 CASCADE 引用。

    ``session_units`` / ``lease_units`` / ``edit_claim_units`` 全部是
    ``ON DELETE CASCADE``。只要合并时外键是开的，每更新一条 unit 就会连带删掉它的归属
    行——静默、不报错，事后只表现为「归属证据凭空少了一批」，而归属证据正是 RVF 判定
    「这批改动属于本会话」的依据。这条用例钉住的是：**合并必须在外键关闭下进行**。
    """

    def seed_legacy(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(connection, worktree_key="wt", worktree_path="/tmp/sample")
        # legacy 侧的观察更新 ⇒ 合并时这条 unit 一定会被重写。
        _insert_unit(
            connection, unit_id="u-1", worktree_key="wt", path="a.py",
            last_observed_at="2026-02-01T00:00:00Z",
        )

    def seed_canonical(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(connection, worktree_key="wt", worktree_path="/tmp/sample")
        _insert_unit(
            connection, unit_id="u-1", worktree_key="wt", path="a.py",
            last_observed_at="2026-01-01T00:00:00Z",
        )
        connection.execute(
            "INSERT INTO session_units (session_id, unit_id, assignment_kind, assigned_at) "
            "VALUES ('sess-1','u-1','owned','2026-01-01T00:00:00Z')"
        )
        connection.commit()

    outcome, merged_database = _merge_one_repository(
        tmp_path, seed_legacy=seed_legacy, seed_canonical=seed_canonical
    )
    assert outcome.rows_updated_by_table.get("units") == 1, "夹具没触发 unit 重写，这条没在测东西"

    connection = sqlite3.connect(str(merged_database))
    try:
        surviving = connection.execute(
            "SELECT COUNT(*) FROM session_units WHERE unit_id='u-1'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert surviving == 1, "unit 被重写时把它的会话归属行一起 CASCADE 掉了"


def test_legacy_absolute_paths_are_rewritten_on_canonical_only_rows_too(
    tmp_path: Path,
) -> None:
    """同一个 run 的 tracker 行会跨两根写，所以 canonical 库里同样存着 legacy 路径。

    只改写「从 legacy 导入的行」会漏掉这一批，切根后它们全部变成死路。
    """

    legacy_root = tmp_path / "legacy-state-root"

    def issue_row(connection: sqlite3.Connection, issue_key: str, artifact_path: str) -> None:
        connection.execute(
            "INSERT INTO rvf_issues (issue_key, repo_key, run_id, issue_id, payload, "
            " source_refs, artifact_path, state, created_at, updated_at) "
            "VALUES (?, 'rk', ?, 'RVF-001', '{}', '[]', ?, 'open', "
            " '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
            (issue_key, issue_key, artifact_path),
        )
        connection.commit()

    def seed_legacy(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(connection, worktree_key="wt", worktree_path="/tmp/sample")
        issue_row(connection, "issue-from-legacy", f"{legacy_root}/runs/r1/artifacts/x.json")

    def seed_canonical(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(connection, worktree_key="wt", worktree_path="/tmp/sample")
        issue_row(connection, "issue-from-canonical", f"{legacy_root}/runs/r2/artifacts/y.json")

    outcome, merged_database = _merge_one_repository(
        tmp_path, seed_legacy=seed_legacy, seed_canonical=seed_canonical
    )
    connection = sqlite3.connect(str(merged_database))
    try:
        paths = dict(
            connection.execute("SELECT issue_key, artifact_path FROM rvf_issues").fetchall()
        )
    finally:
        connection.close()

    assert not any(str(value).startswith(str(legacy_root)) for value in paths.values())
    assert paths["issue-from-canonical"].endswith("/runs/r2/artifacts/y.json")
    assert outcome.absolute_paths_rewritten == 2


def test_an_unregistered_tracker_table_blocks_the_repository_merge(tmp_path: Path) -> None:
    def seed_legacy(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(connection, worktree_key="wt", worktree_path="/tmp/sample")
        connection.execute("CREATE TABLE brand_new_feature_state (key TEXT PRIMARY KEY)")
        connection.commit()

    def seed_canonical(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(connection, worktree_key="wt", worktree_path="/tmp/sample")

    outcome, _ = _merge_one_repository(
        tmp_path, seed_legacy=seed_legacy, seed_canonical=seed_canonical
    )
    assert any(
        "brand_new_feature_state" in reason for reason in outcome.blocking_reasons
    )


def test_identity_column_conflicts_are_isolated_rather_than_silently_decided(
    tmp_path: Path,
) -> None:
    """两根对同一个 ``worktree_key`` 说出不同的 ``worktree_path`` = 说的不是同一个东西。"""

    def seed_legacy(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(
            connection, worktree_key="wt", worktree_path="/tmp/one"
        )

    def seed_canonical(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(
            connection, worktree_key="wt", worktree_path="/tmp/two"
        )

    outcome, merged_database = _merge_one_repository(
        tmp_path, seed_legacy=seed_legacy, seed_canonical=seed_canonical
    )
    assert any(row.startswith("worktrees:wt:") for row in outcome.identity_mismatch_rows)
    connection = sqlite3.connect(str(merged_database))
    try:
        path = connection.execute(
            "SELECT worktree_path FROM worktrees WHERE worktree_key='wt'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert path == "/tmp/two", "冲突时保留 canonical，不静默决胜"


def test_rows_depending_on_an_identity_conflicted_key_are_excluded_not_transplanted(
    tmp_path: Path,
) -> None:
    """只隔离父行是**假隔离**：引用同一个 key 的 legacy 子行会被改嫁到 canonical 的实体上。

    ``worktree_key`` / ``session_id`` 在 RVF 里决定「哪些改动属于哪个会话」。两根对
    ``wt`` 说出不同的 ``worktree_path`` 之后，legacy 侧那些 ``current_worktree_key='wt'``
    的 session、``worktree_key='wt'`` 的 unit、以及挂在它们下面的 ``session_units``，
    说的都是**legacy 那个 worktree**里的事。把它们照常导入，就等于对外报「已隔离」、
    实际做「父行保留、子行改嫁」，评审范围归属会直接算错。
    """

    def seed_legacy(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(
            connection,
            worktree_key="wt",
            worktree_path="/tmp/one",
            session_id="sess-legacy-only",
        )
        _insert_unit(connection, unit_id="u-legacy-only", worktree_key="wt", path="legacy.py")
        _insert_session_unit(
            connection, session_id="sess-legacy-only", unit_id="u-legacy-only"
        )
        # 同一个 session_id 两根都有：legacy 侧更新，通用策略会让它整行胜出。
        _insert_session(
            connection,
            session_id="sess-present-on-both-roots",
            current_worktree_key="wt",
            channel="legacy-channel",
            last_seen_at="2026-06-01T00:00:00Z",
        )

    def seed_canonical(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(
            connection,
            worktree_key="wt",
            worktree_path="/tmp/two",
            session_id="sess-canonical-only",
        )
        _insert_unit(
            connection, unit_id="u-canonical-only", worktree_key="wt", path="canonical.py"
        )
        _insert_session(
            connection,
            session_id="sess-present-on-both-roots",
            current_worktree_key="wt",
            channel="canonical-channel",
            last_seen_at="2026-01-01T00:00:00Z",
        )

    outcome, merged_database = _merge_one_repository(
        tmp_path, seed_legacy=seed_legacy, seed_canonical=seed_canonical
    )
    assert outcome.blocking_reasons == []
    assert any(row.startswith("worktrees:wt:") for row in outcome.identity_mismatch_rows)

    connection = sqlite3.connect(str(merged_database))
    try:
        session_identifiers = {
            row[0] for row in connection.execute("SELECT session_id FROM sessions").fetchall()
        }
        unit_identifiers = {
            row[0] for row in connection.execute("SELECT unit_id FROM units").fetchall()
        }
        session_unit_pairs = {
            tuple(row)
            for row in connection.execute(
                "SELECT session_id, unit_id FROM session_units"
            ).fetchall()
        }
        channel_of_shared_session = connection.execute(
            "SELECT channel FROM sessions WHERE session_id='sess-present-on-both-roots'"
        ).fetchone()[0]
        dangling = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    # canonical 侧的一切原样保留：隔离的代价只落在 legacy 侧那批有歧义的行上。
    assert session_identifiers == {"sess-canonical-only", "sess-present-on-both-roots"}
    assert unit_identifiers == {"u-canonical-only"}
    assert session_unit_pairs == set(), "legacy 的归属行必须整条闭包一起排除"
    assert channel_of_shared_session == "canonical-channel", (
        "两根都有的那条 session，legacy 侧同样引用着有歧义的 worktree，不能让它整行胜出"
    )
    assert dangling == []
    # 排除的每一行都要能被读出来，否则「已隔离」仍旧只是嘴上说说。
    assert {row.split(":", 2)[0] for row in outcome.identity_quarantined_dependent_rows} == {
        "units",
        "sessions",
        "session_units",
    }


def test_identity_quarantine_reaches_terminal_leases_and_tombstones_too(
    tmp_path: Path,
) -> None:
    """隔离闭包必须是**传递**的，且要盖住没有外键的 ``tombstones``。

    ``worktrees.wt`` 冲突 ⇒ legacy 的 session 被排除 ⇒ 挂在它下面那把**已终结**的
    lease（终结 lease 本来是会迁移的）以及 lease 的 unit 链接也要跟着排除。
    ``tombstones`` 靠 ``ref_id`` 指向实体却**没有外键**，通用检查看不见它——一条针对
    被隔离 ``wt`` 的 legacy 退休记录导入进来，读起来就是「canonical 的 wt 已退休」。
    """

    def seed_legacy(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(
            connection,
            worktree_key="wt",
            worktree_path="/tmp/one",
            session_id="sess-legacy-only",
        )
        _insert_unit(connection, unit_id="u-legacy-only", worktree_key="wt", path="legacy.py")
        connection.execute(
            "INSERT INTO leases (lease_id, session_id, run_id, reviewer_id, holder_kind, "
            " scope_hash, state, ttl_seconds, created_at, last_activity_at, expires_at) "
            "VALUES ('lease-legacy-done','sess-legacy-only','run-1','r1','reviewer','h',"
            " 'completed',600,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',"
            " '2026-01-01T01:00:00Z')"
        )
        connection.execute(
            "INSERT INTO lease_units (lease_id, unit_id) "
            "VALUES ('lease-legacy-done','u-legacy-only')"
        )
        connection.commit()
        _insert_tombstone(connection, ref_id="wt", kind="worktree")
        _insert_tombstone(connection, ref_id="unrelated-entity", kind="unit")

    def seed_canonical(connection: sqlite3.Connection) -> None:
        _seed_minimal_repository_rows(
            connection,
            worktree_key="wt",
            worktree_path="/tmp/two",
            session_id="sess-canonical-only",
        )

    outcome, merged_database = _merge_one_repository(
        tmp_path, seed_legacy=seed_legacy, seed_canonical=seed_canonical
    )
    assert outcome.blocking_reasons == []

    connection = sqlite3.connect(str(merged_database))
    try:
        lease_identifiers = {
            row[0] for row in connection.execute("SELECT lease_id FROM leases").fetchall()
        }
        lease_unit_pairs = {
            tuple(row)
            for row in connection.execute("SELECT lease_id, unit_id FROM lease_units").fetchall()
        }
        tombstone_reference_identifiers = {
            row[0] for row in connection.execute("SELECT ref_id FROM tombstones").fetchall()
        }
        dangling = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    assert lease_identifiers == set(), "已终结的 lease 也要跟着被排除的 session 一起排除"
    assert lease_unit_pairs == set()
    assert tombstone_reference_identifiers == {"unrelated-entity"}, (
        "针对被隔离 key 的 legacy 退休记录会被读成「canonical 的那个实体已退休」"
    )
    assert dangling == []
    assert {row.split(":", 2)[0] for row in outcome.identity_quarantined_dependent_rows} >= {
        "leases",
        "tombstones",
    }


# ---------------------------------------------------------------------------
# round-baseline 裁决（方向与 highwater 相反）
# ---------------------------------------------------------------------------


def test_round_baseline_conflict_takes_the_older_ancestor(
    linear_git_repository: tuple[Path, list[str]],
) -> None:
    repository, commits = linear_git_repository
    reconciliation = tool.choose_round_baseline(
        task_marker_name="task-abc",
        legacy_marker={"baseline_head": commits[0], "repo": str(repository)},
        canonical_marker={"baseline_head": commits[2], "repo": str(repository)},
    )
    # round-baseline 只是 bootstrap 下界：取更旧 = 窗口更宽 = 宁可多看几个 commit。
    assert reconciliation.chosen_baseline_head == commits[0]
    assert reconciliation.chosen_side == "legacy_is_older_ancestor"


def test_round_baseline_refuses_when_a_marker_is_present_but_unreadable() -> None:
    reconciliation = tool.choose_round_baseline(
        task_marker_name="task-abc",
        legacy_marker=None,
        canonical_marker={"baseline_head": "a" * 40, "repo": "/tmp/x"},
        legacy_marker_file_present_but_unreadable=True,
    )
    assert reconciliation.chosen_side == "refused"
    assert any("unreadable" in item or "not_parseable" in item
               for item in reconciliation.conflict_classes)


# ---------------------------------------------------------------------------
# 静默期闸
# ---------------------------------------------------------------------------


def test_quiescence_gate_blocks_while_either_root_is_still_being_written(
    tmp_path: Path,
) -> None:
    legacy_root, canonical_root = _build_state_root_pair(tmp_path)
    verdict = tool.evaluate_migration_quiescence_gates(legacy_root, canonical_root, [])
    assert not verdict.quiesced
    # canonical 也要闸：merge 与 switch 之间对 canonical 的写会被整体换入静默丢掉。
    assert any(reason.startswith("canonical_root_was_written") for reason in verdict.blocking_reasons)
    assert any(reason.startswith("legacy_root_was_written") for reason in verdict.blocking_reasons)


def test_quiescence_gate_blocks_on_undrained_leases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool, "LEGACY_ROOT_QUIESCENCE_MINIMUM_IDLE_SECONDS", 0)
    legacy_root, canonical_root = _build_state_root_pair(tmp_path)
    directory = _tracker_directory(legacy_root, "sample-repo-000000000000")
    _write_repository_metadata(directory, "/tmp/sample/.git", "/tmp/sample")
    connection = _create_tracker_database(directory / tool.TRACKER_DATABASE_FILE_NAME)
    try:
        _seed_minimal_repository_rows(connection, worktree_key="wt", worktree_path="/tmp/sample")
        connection.execute(
            "INSERT INTO leases (lease_id, session_id, run_id, reviewer_id, holder_kind, "
            " scope_hash, state, ttl_seconds, created_at, last_activity_at, expires_at) "
            "VALUES ('l1','sess-1','run-1','r1','reviewer','h','active',600,"
            " '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')"
        )
        connection.commit()
    finally:
        connection.close()

    pairs = tool.discover_tracker_repository_pairs(legacy_root, canonical_root)
    verdict = tool.evaluate_migration_quiescence_gates(legacy_root, canonical_root, pairs)
    assert verdict.undrained_lease_count == 1
    assert any("undrained_leases" in reason for reason in verdict.blocking_reasons)


# ---------------------------------------------------------------------------
# marker 命名与运行时同源
# ---------------------------------------------------------------------------


def test_migration_writes_markers_under_exactly_the_file_names_the_runtime_reads(
    tmp_path: Path, linear_git_repository: tuple[Path, list[str]]
) -> None:
    """本工具**绝不另立一套 marker 命名**；运行时的 ``marker_paths`` 是唯一真相源。

    这条守卫存在的理由：迁移若按自己那套名字写，产出的就是**运行时读不到的文件**——每个
    task 静默丢掉自己的水位，而对账表上一切正常。「仓身份 + harness 编进 key」那一刀落下
    时它确实先红了（迁移那侧随之改成从 ``marker_paths`` 现场派生），这条守卫按设计生效过
    一次；再改 key 仍会先红。
    """

    repository, commits = linear_git_repository
    legacy_root, canonical_root = _build_state_root_pair(tmp_path)
    task_identifier = "abc12"
    for root, head in ((legacy_root, commits[0]), (canonical_root, commits[2])):
        review_highwater_marker.write_review_highwater(
            task_id=task_identifier,
            session_id=None,
            reviewed_head=head,
            repo=str(repository),
            root=root,
        )
        round_baseline_marker.write_round_baseline_marker(
            task_id=task_identifier,
            session_id=None,
            baseline_head=head,
            repo=str(repository),
            root=root,
        )

    # 对账表的 ``task_marker`` 是**来源侧磁盘上的**文件名，而不是「task-<id>」这种手搓的
    # 逻辑名——运行时改过 key 之后两者不再相等，写死逻辑名会让本测试自己造出一条查无此
    # marker 的报告行、然后把「迁移拒绝了它」误读成「命名不一致」。
    source_marker_name = review_highwater_marker.marker_paths(
        task_id=task_identifier, session_id=None, repo=str(repository), root=canonical_root
    )[0].stem

    staging_root = tmp_path / "staging"
    tool.stage_review_highwater_markers(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        highwater_report_rows=[
            {
                "task_marker": source_marker_name,
                "chosen_reviewed_head": commits[2],
                "chosen_side": "canonical",
                "legacy_reviewed_head": commits[0],
                "canonical_reviewed_head": commits[2],
                "ancestry_relation": "left_is_ancestor_of_right",
                "conflict_classes": [],
                "written_off_unit_ids": [],
            }
        ],
    )
    tool.stage_round_baseline_markers(
        legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
    )

    merged = staging_root / tool.STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME
    for module, subdirectory in (
        (review_highwater_marker, tool.REVIEW_HIGHWATER_SUBDIRECTORY),
        (round_baseline_marker, tool.ROUND_BASELINE_PENDING_SUBDIRECTORY),
    ):
        runtime_candidates = [
            candidate.relative_to(canonical_root)
            for candidate in module.marker_paths(
                task_id=task_identifier,
                session_id=None,
                repo=str(repository),
                root=canonical_root,
            )
        ]
        assert runtime_candidates, "运行时连一条候选路径都给不出，key 派生已经坏了"
        assert all(
            candidate.parent.name == subdirectory for candidate in runtime_candidates
        )
        # 判据是「运行时的候选列表里**有一条**能读到」，不是「恰好等于第一候选」：
        # round-baseline 的 key 含 harness，而迁移过来的历史 marker 里没有 harness 可恢复，
        # 一律落在 ``--harness-unspecified`` 那条回退候选上。本测试进程自己会解析出一个真实
        # harness（``CLAUDECODE`` 之类），所以这条断言顺带证明了那条回退确实接得住迁移产物。
        assert any((merged / candidate).is_file() for candidate in runtime_candidates), (
            f"迁移写出的 {subdirectory} marker 文件名运行时一条都读不到："
            f"运行时候选 {[str(candidate) for candidate in runtime_candidates]}，"
            f"staging 实有 {sorted(path.name for path in (merged / subdirectory).glob('*.json'))}"
        )


def test_marker_rekeying_is_idempotent_for_names_already_in_the_new_format(
    linear_git_repository: tuple[Path, list[str]]
) -> None:
    """已经是新格式的文件名再过一遍重键，必须原样不变。

    这不是洁癖：F1.2 的硬顺序是「先 land + 部署新代码，再迁移数据」，所以真正跑迁移那一刻
    两个根里**必然新旧两种名字并存**——部署之后写出的是新名。若重键不幂等，新名会被叠成
    ``task-x--repo-A--repo-A``，而那个名字运行时一条候选都对不上 ⇒ 该 task 的水位静默消失。
    """

    repository, _commits = linear_git_repository
    for module, subdirectory in (
        (review_highwater_marker, tool.REVIEW_HIGHWATER_SUBDIRECTORY),
        (round_baseline_marker, tool.ROUND_BASELINE_PENDING_SUBDIRECTORY),
    ):
        document = {"repo": str(repository)}
        from_legacy_name = tool.runtime_marker_file_name_for_migrated_marker(
            legacy_marker_name="task-abc12",
            marker_document=document,
            marker_subdirectory=subdirectory,
        )
        assert from_legacy_name is not None
        from_new_name = tool.runtime_marker_file_name_for_migrated_marker(
            legacy_marker_name=Path(from_legacy_name).stem,
            marker_document=document,
            marker_subdirectory=subdirectory,
        )
        assert from_new_name == from_legacy_name, (
            f"{subdirectory} 的重键不幂等：{from_legacy_name} → {from_new_name}"
        )
        assert from_legacy_name.startswith("task-abc12--repo-"), from_legacy_name

    # session-keyed 那半边走同一条路径。
    session_keyed = tool.runtime_marker_file_name_for_migrated_marker(
        legacy_marker_name="sess-0191b2c3-dead-beef",
        marker_document={"repo": str(repository)},
        marker_subdirectory=tool.REVIEW_HIGHWATER_SUBDIRECTORY,
    )
    assert session_keyed is not None and session_keyed.startswith("sess-0191b2c3-dead-beef--repo-")


def test_round_baseline_rekeying_keeps_the_harness_identity_already_in_the_file_name(
    tmp_path: Path, linear_git_repository: tuple[Path, list[str]]
) -> None:
    """同一 task 下两个 harness 的**新格式** round-baseline marker，迁移后仍是两个文件。

    这条守卫钉死的正是「重键把 harness 段吃掉」那个缺陷：剥壳若按 ``--repo-`` 一刀切到底，
    ``--harness-claude-code`` 与 ``--harness-codex-cli`` 会被一起丢掉、再统统重派成
    ``--harness-unspecified``，于是两份 marker 写进**同一个**目标文件、后写的覆盖先写的，
    而对账表两条都记成 written（静默丢一份）。后果恰好是把「harness 身份编进 round-baseline
    key」这一刀要消除的跨 harness 互相覆盖原样恢复回来——一个 harness 的 Stop 会读到另一个
    harness 上一条 prompt 的 HEAD，窗口下界被抬高 ⇒ 漏审。

    这个组合不是假想输入：F1.2 的硬顺序是「先部署新代码、再迁移数据」，所以迁移执行的那一刻
    两根里**必然**同时躺着旧格式（无 harness 段）与新格式（含 harness 段）两批 marker。

    两个 marker 刻意分居两根，因为这正是现实形状：Claude 的 hook 落 ``~/.claude/rvf``、
    Codex/shell 落 ``~/.rvf/user-local-data/run-logs``——双根分裂本身就是按 harness 分的。
    """

    repository, commits = linear_git_repository
    legacy_root, canonical_root = _build_state_root_pair(tmp_path)
    task_identifier = "abc12"
    baseline_head_by_harness_identity = {"claude-code": commits[0], "codex-cli": commits[1]}
    root_by_harness_identity = {"claude-code": legacy_root, "codex-cli": canonical_root}

    source_marker_name_by_harness_identity: dict[str, str] = {}
    for harness_identity, baseline_head in baseline_head_by_harness_identity.items():
        written_path = round_baseline_marker.write_round_baseline_marker(
            task_id=task_identifier,
            session_id=None,
            baseline_head=baseline_head,
            repo=str(repository),
            parent_harness_identity=harness_identity,
            root=root_by_harness_identity[harness_identity],
        )
        assert written_path is not None
        assert f"--harness-{harness_identity}" in written_path.name, (
            f"运行时自己就没按 harness 分键，本测试的前提已经不成立：{written_path.name}"
        )
        source_marker_name_by_harness_identity[harness_identity] = written_path.stem
    assert len(set(source_marker_name_by_harness_identity.values())) == 2

    # ① 命名层：正文**刻意只给 repo、不给 parent_harness_identity**，以证明「文件名里已有的
    #    harness 段」这一条来源自己就够用，而不是靠正文兜底。
    migrated_name_by_harness_identity = {
        harness_identity: tool.runtime_marker_file_name_for_migrated_marker(
            legacy_marker_name=source_marker_name,
            marker_document={"repo": str(repository)},
            marker_subdirectory=tool.ROUND_BASELINE_PENDING_SUBDIRECTORY,
        )
        for harness_identity, source_marker_name in (
            source_marker_name_by_harness_identity.items()
        )
    }
    for harness_identity, migrated_name in migrated_name_by_harness_identity.items():
        assert migrated_name == (
            source_marker_name_by_harness_identity[harness_identity] + ".json"
        ), f"{harness_identity} 的新格式名重键后变了：{migrated_name}"
    assert len(set(migrated_name_by_harness_identity.values())) == 2, (
        f"两个 harness 的 marker 被重键到了同一个目标名：{migrated_name_by_harness_identity}"
    )

    # ② 落盘层：staging 里必须真的落出两个文件，且各自带着自己那份 baseline_head。
    #    只断言 ① 是不够的——真正的损失发生在写盘那一步（同名互相覆盖）。
    staging_root = tmp_path / "staging"
    staging = tool.stage_round_baseline_markers(
        legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
    )
    assert staging["refused_marker_names"] == [], staging["refused_marker_names"]
    assert staging["skipped_marker_names_whose_repository_is_gone"] == []
    assert staging["written_marker_names"] == sorted(
        source_marker_name_by_harness_identity.values()
    )

    merged_round_baseline_directory = (
        staging_root
        / tool.STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME
        / tool.ROUND_BASELINE_PENDING_SUBDIRECTORY
    )
    staged_file_names = sorted(path.name for path in merged_round_baseline_directory.glob("*.json"))
    assert staged_file_names == sorted(
        name + ".json" for name in source_marker_name_by_harness_identity.values()
    ), f"迁移没有落出两个独立文件（互相覆盖了）：{staged_file_names}"
    for harness_identity, baseline_head in baseline_head_by_harness_identity.items():
        staged_document = json.loads(
            (
                merged_round_baseline_directory
                / (source_marker_name_by_harness_identity[harness_identity] + ".json")
            ).read_text(encoding="utf-8")
        )
        assert staged_document["baseline_head"] == baseline_head, (
            f"{harness_identity} 的 baseline_head 被另一个 harness 的值顶掉了"
        )

    # ③ 运行时读得回来：各自 harness 的第一候选就命中自己那份，不必落到 unspecified 回退。
    for harness_identity in baseline_head_by_harness_identity:
        first_runtime_candidate = round_baseline_marker.marker_paths(
            task_id=task_identifier,
            session_id=None,
            repo=str(repository),
            parent_harness_identity=harness_identity,
            root=canonical_root,
        )[0].name
        assert (merged_round_baseline_directory / first_runtime_candidate).is_file()


def test_round_baseline_rekeying_falls_back_to_the_harness_identity_recorded_in_the_document(
    linear_git_repository: tuple[Path, list[str]]
) -> None:
    """文件名没有 harness 段、但正文记了 ⇒ 用正文的，仍然不许一律塌成 unspecified。

    正文的 ``parent_harness_identity`` 是次要来源：正常写入里它与文件名共用同一个取值
    （``write_round_baseline_marker`` 只判定一次），所以它接的是「正文健在、文件名残缺」
    这类输入。真正的旧格式两处都没有，才落到 ``unspecified``——那一条由
    ``test_marker_rekeying_is_idempotent_for_names_already_in_the_new_format`` 与下面
    最后一段一并钉住。
    """

    repository, _commits = linear_git_repository

    from_document = tool.runtime_marker_file_name_for_migrated_marker(
        legacy_marker_name="task-abc12",
        marker_document={"repo": str(repository), "parent_harness_identity": "codex-cli"},
        marker_subdirectory=tool.ROUND_BASELINE_PENDING_SUBDIRECTORY,
    )
    assert from_document is not None and from_document.endswith("--harness-codex-cli.json")

    # 旧格式（文件名与正文都没有 harness）仍旧回退 unspecified——读侧候选列表显式包含
    # 这条回退，所以历史 marker 迁过来照样读得到。
    from_legacy_format = tool.runtime_marker_file_name_for_migrated_marker(
        legacy_marker_name="task-abc12",
        marker_document={"repo": str(repository)},
        marker_subdirectory=tool.ROUND_BASELINE_PENDING_SUBDIRECTORY,
    )
    assert from_legacy_format is not None
    assert from_legacy_format.endswith("--harness-unspecified.json"), from_legacy_format

    # review-highwater 不按 harness 分键，任何 harness 信号都不许渗进它的文件名。
    highwater_name = tool.runtime_marker_file_name_for_migrated_marker(
        legacy_marker_name="task-abc12",
        marker_document={"repo": str(repository), "parent_harness_identity": "codex-cli"},
        marker_subdirectory=tool.REVIEW_HIGHWATER_SUBDIRECTORY,
    )
    assert highwater_name is not None and "--harness-" not in highwater_name, highwater_name


def test_markers_whose_repository_is_gone_are_skipped_rather_than_blocking_the_migration(
    tmp_path: Path, linear_git_repository: tuple[Path, list[str]]
) -> None:
    """仓已被删的 marker 走「跳过并报数」，不走「拒绝并阻断」。

    两者都不写出文件，但后果天差地别：``refused`` 是 ``verify`` 的 blocking，意思是「先
    手工裁决再重跑」；而实测真实两根里 169 个无法重键的 marker **全部**是「cline-kanban
    task worktree 已被删」。那份 marker 无论迁不迁都不会再被命中（运行时同样算不出它的仓
    身份），把它算成阻断只会让静默期被一批早已消失的 worktree 永久卡住。

    但**必须报数**——静默丢掉几百个 marker 正是这类工具最典型的假绿。
    """

    repository, commits = linear_git_repository
    legacy_root, canonical_root = _build_state_root_pair(tmp_path)
    vanished_repository = tmp_path / "worktree-that-was-deleted"

    for root in (legacy_root, canonical_root):
        directory = root / tool.ROUND_BASELINE_PENDING_SUBDIRECTORY
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "task-deleted.json").write_text(
            json.dumps(
                {
                    "marker_version": 1,
                    "state": "round_baseline",
                    "baseline_head": commits[0],
                    "repo": str(vanished_repository),
                    "captured_at": "2026-07-01T00:00:00Z",
                    "expires_at": "2030-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )

    staging = tool.stage_round_baseline_markers(
        legacy_root=legacy_root, canonical_root=canonical_root, staging_root=tmp_path / "staging"
    )
    assert staging["skipped_marker_names_whose_repository_is_gone"] == ["task-deleted"]
    assert staging["refused_marker_names"] == [], staging["refused_marker_names"]
    assert staging["written_marker_names"] == []


def test_marker_rekeying_refuses_when_the_repository_identity_cannot_be_resolved(
    tmp_path: Path,
) -> None:
    """仓身份解析不出来 ⇒ 拒绝迁移该 marker，绝不退到共享占位桶。

    退到占位桶等于把「不同仓撞同一个 task id」原样搬个地方——而这正是本次改 key 要消除的
    东西。拒绝会走进 ``refused_marker_names``，``verify`` 对它是 blocking：宁可当场停下来
    手工裁决，也不要让一份认不出归属的水位混进别人的桶里。
    """

    for subdirectory in (
        tool.REVIEW_HIGHWATER_SUBDIRECTORY,
        tool.ROUND_BASELINE_PENDING_SUBDIRECTORY,
    ):
        # 正文里没有 repo
        assert (
            tool.runtime_marker_file_name_for_migrated_marker(
                legacy_marker_name="task-abc12",
                marker_document={},
                marker_subdirectory=subdirectory,
            )
            is None
        )
        # repo 指向一个已经不在磁盘上的路径（仓被删 / 被搬走）
        assert (
            tool.runtime_marker_file_name_for_migrated_marker(
                legacy_marker_name="task-abc12",
                marker_document={"repo": str(tmp_path / "vanished-repository")},
                marker_subdirectory=subdirectory,
            )
            is None
        )
        # 认不出的 key 前缀
        assert (
            tool.runtime_marker_file_name_for_migrated_marker(
                legacy_marker_name="mystery-abc12",
                marker_document={"repo": str(tmp_path)},
                marker_subdirectory=subdirectory,
            )
            is None
        )


# ---------------------------------------------------------------------------
# 端到端：merge → verify → switch
# ---------------------------------------------------------------------------


@pytest.fixture()
def quiesced_state_root_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linear_git_repository
) -> tuple[Path, Path, Path, str]:
    monkeypatch.setattr(tool, "LEGACY_ROOT_QUIESCENCE_MINIMUM_IDLE_SECONDS", 0)
    repository, commits = linear_git_repository
    legacy_root, canonical_root = _build_state_root_pair(tmp_path)
    repository_key = "sample-repo-000000000000"
    git_common_dir = str(repository / ".git")

    for root, unit_identifier in ((legacy_root, "u-legacy"), (canonical_root, "u-canonical")):
        directory = _tracker_directory(root, repository_key)
        _write_repository_metadata(directory, git_common_dir, str(repository))
        connection = _create_tracker_database(directory / tool.TRACKER_DATABASE_FILE_NAME)
        try:
            _seed_minimal_repository_rows(
                connection, worktree_key="wt", worktree_path=str(repository)
            )
            _insert_unit(
                connection, unit_id=unit_identifier, worktree_key="wt", path="file-0.txt"
            )
        finally:
            connection.close()
        (root / tool.RUNS_SUBDIRECTORY / f"run-from-{root.name}").mkdir(parents=True)
        (root / "handoff-notified").mkdir(exist_ok=True)
        (root / "handoff-notified" / f"{root.name}.json").write_text("{}", encoding="utf-8")

    review_highwater_marker.write_review_highwater(
        task_id="abc12", session_id=None, reviewed_head=commits[0],
        repo=str(repository), root=legacy_root,
    )
    review_highwater_marker.write_review_highwater(
        task_id="abc12", session_id=None, reviewed_head=commits[2],
        repo=str(repository), root=canonical_root,
    )
    (canonical_root / "latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-legacy",
                "summary_path": f"{legacy_root}/runs/run-from-legacy-state-root/summary.json",
                "events_path": f"{legacy_root}/runs/run-from-legacy-state-root/events.jsonl",
                "updated_at": "2026-08-13T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return legacy_root, canonical_root, tmp_path / "staging", repository_key


def test_merge_then_verify_then_switch_installs_state_and_seals_the_legacy_root(
    quiesced_state_root_pair, tmp_path: Path
) -> None:
    legacy_root, canonical_root, staging_root, repository_key = quiesced_state_root_pair

    merge_exit_code, ledger = tool.run_merge_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        conflict_policy=tool.DEFAULT_HIGHWATER_CONFLICT_POLICY,
        canonical_root_provenance_description="test",
        rehearse_without_quiescence_gates=False,
    )
    assert merge_exit_code == 0, ledger
    assert ledger["status"] == "staged"
    assert ledger["run_directory_move_plan"]["run_identifiers_to_move"] == [
        "run-from-legacy-state-root"
    ]

    verify_exit_code, receipt = tool.run_verify_command(
        legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
    )
    assert verify_exit_code == 0, receipt["findings"]

    archive_root = tmp_path / "legacy-state-root.migrated"
    switch_exit_code, switch_receipt = tool.run_switch_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        archive_root=archive_root,
    )
    assert switch_exit_code == 0, switch_receipt
    assert switch_receipt["status"] == "switched"

    # tracker：两侧的 unit 都在合并后的库里。
    merged_database = (
        canonical_root / tool.TRACKER_REPOSITORIES_SUBDIRECTORY / repository_key
        / tool.TRACKER_DATABASE_FILE_NAME
    )
    connection = sqlite3.connect(str(merged_database))
    try:
        unit_identifiers = {
            row[0] for row in connection.execute("SELECT unit_id FROM units").fetchall()
        }
    finally:
        connection.close()
    assert unit_identifiers == {"u-legacy", "u-canonical"}

    # runs：legacy 的 run 目录被 rename 过来，canonical 原有的还在。
    moved = canonical_root / tool.RUNS_SUBDIRECTORY
    assert {child.name for child in moved.iterdir()} == {
        "run-from-legacy-state-root",
        "run-from-canonical-state-root",
    }

    # 并集目录与 latest 指针。
    assert {child.name for child in (canonical_root / "handoff-notified").iterdir()} == {
        "legacy-state-root.json",
        "canonical-state-root.json",
    }
    latest = json.loads((canonical_root / "latest.json").read_text(encoding="utf-8"))
    assert latest["summary_path"].startswith(str(canonical_root))

    # legacy 根：改名归档 + 原位留不可写哨兵。
    try:
        assert archive_root.is_dir()
        assert (archive_root / tool.LEGACY_ROOT_ORIGIN_STAMP_FILE_NAME).is_file()
        assert legacy_root.is_dir()
        assert not any(legacy_root.iterdir())
        assert (os.stat(legacy_root).st_mode & 0o777) == tool.LEGACY_ROOT_SENTINEL_DIRECTORY_MODE
        with pytest.raises(PermissionError):
            (legacy_root / "should-not-be-writable").mkdir()
    finally:
        # 归档树是只读的，放开权限，别把不可删的残留留给 tmp_path 的保留期清理。
        _restore_write_permissions_so_pytest_can_clean_up(archive_root)


def test_switch_refuses_a_staging_produced_without_the_quiescence_gates(
    tmp_path: Path,
) -> None:
    """排练产物只能用来读对账结果；拿去正式切换必须被拒。"""

    legacy_root, canonical_root = _build_state_root_pair(tmp_path)
    staging_root = tmp_path / "staging"
    exit_code, ledger = tool.run_merge_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        conflict_policy=tool.DEFAULT_HIGHWATER_CONFLICT_POLICY,
        canonical_root_provenance_description="test",
        rehearse_without_quiescence_gates=True,
    )
    assert exit_code == 0
    assert ledger["staging_was_produced_without_quiescence_gates"] is True

    switch_exit_code, payload = tool.run_switch_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        archive_root=tmp_path / "archive",
    )
    assert switch_exit_code == 2
    assert payload["reason"] == "staging_was_produced_without_quiescence_gates"
    assert not (tmp_path / "archive").exists()


def test_verify_refuses_when_a_source_database_changed_after_merge(
    quiesced_state_root_pair,
) -> None:
    """merge 与 switch 之间对源库的写，会被整体换入静默丢掉——必须挡住。"""

    legacy_root, canonical_root, staging_root, repository_key = quiesced_state_root_pair
    exit_code, _ = tool.run_merge_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        conflict_policy=tool.DEFAULT_HIGHWATER_CONFLICT_POLICY,
        canonical_root_provenance_description="test",
        rehearse_without_quiescence_gates=False,
    )
    assert exit_code == 0

    database_path = (
        canonical_root / tool.TRACKER_REPOSITORIES_SUBDIRECTORY / repository_key
        / tool.TRACKER_DATABASE_FILE_NAME
    )
    main_file_digest_before = tool.sha256_of_file(database_path)

    # 关键：这批写入必须**留在 WAL 里、不 checkpoint**，才是真实 tracker 被并发写时
    # 的样子。随手 connect/close 会在最后一个连接关闭时自动 checkpoint、把数据刷回主
    # 文件，于是连只看主文件的指纹都能发现变化——那样这条用例就退化成测了个寂寞。
    # 故：关掉 autocheckpoint，并在跑 verify 期间**保持连接打开**。
    writer = sqlite3.connect(str(database_path))
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        _insert_unit(
            writer, unit_id="u-written-after-merge", worktree_key="wt", path="late.py"
        )
        assert tool.sha256_of_file(database_path) == main_file_digest_before, (
            "夹具没构造出「写入只在 WAL 里」的状态，这条用例就测不到它要测的东西"
        )

        verify_exit_code, receipt = tool.run_verify_command(
            legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
        )
    finally:
        writer.close()

    assert verify_exit_code == 1
    assert any(
        finding["check"] == "source_database_unchanged_since_merge"
        for finding in receipt["findings"]
    )


def test_merge_refuses_when_a_source_database_is_written_while_it_is_being_snapshotted(
    quiesced_state_root_pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """coherent 快照与源库指纹必须是**同一份字节**的两个说法。

    两步分开取，中间落地的那笔事务就会让账本记下**新**指纹、staging 里躺着**旧**快照。
    于是 verify 的 ``source_database_unchanged_since_merge`` 闸会认为「源库自 merge 以来
    没变过」而放行，``switch`` 把那笔事务整体丢掉，且全程没有任何 finding——这正是那道
    闸本来要挡的事情本身。所以指纹要在快照前后各取一次并要求相等，不等就拒绝这个仓。
    """

    legacy_root, canonical_root, staging_root, repository_key = quiesced_state_root_pair
    legacy_database = (
        legacy_root / tool.TRACKER_REPOSITORIES_SUBDIRECTORY / repository_key
        / tool.TRACKER_DATABASE_FILE_NAME
    )
    main_file_digest_before = tool.sha256_of_file(legacy_database)

    take_coherent_backup = tool.create_sqlite_coherent_backup
    writers: list[sqlite3.Connection] = []

    def backup_then_let_exactly_one_transaction_land(source: Path, destination: Path) -> None:
        """在「快照已落盘、指纹还没取」这道缝里塞一笔事务，且**留在 WAL 不 checkpoint**。

        随手 connect/close 会在最后一个连接关闭时自动 checkpoint、把数据刷回主文件，
        于是连只看主文件的指纹都能发现变化——那样这条用例就退化成测了个寂寞。
        """

        take_coherent_backup(source, destination)
        if Path(source) != legacy_database or writers:
            return
        writer = sqlite3.connect(str(legacy_database))
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        _insert_unit(
            writer, unit_id="u-landed-inside-the-window", worktree_key="wt", path="raced.py"
        )
        writers.append(writer)

    monkeypatch.setattr(
        tool, "create_sqlite_coherent_backup", backup_then_let_exactly_one_transaction_land
    )
    try:
        merge_exit_code, ledger = tool.run_merge_command(
            legacy_root=legacy_root,
            canonical_root=canonical_root,
            staging_root=staging_root,
            conflict_policy=tool.DEFAULT_HIGHWATER_CONFLICT_POLICY,
            canonical_root_provenance_description="test",
            rehearse_without_quiescence_gates=False,
        )
        assert writers, "夹具没在那道缝里塞进事务，这条用例就没在测东西"
        assert tool.sha256_of_file(legacy_database) == main_file_digest_before, (
            "夹具没构造出「写入只留在 WAL 里」的状态，这条用例就测不到它要测的东西"
        )
        verify_exit_code, receipt = tool.run_verify_command(
            legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
        )
    finally:
        for writer in writers:
            writer.close()

    entry = next(
        item
        for item in ledger["tracker_repositories"]
        if item["repository_key"] == repository_key
    )
    assert any(
        "changed_while_being_snapshotted" in reason for reason in entry["blocking_reasons"]
    ), entry
    assert (merge_exit_code, ledger["status"]) == (1, "staged_with_refusals")
    assert verify_exit_code == 1, receipt["findings"]


def _merge_quiesced_pair(
    legacy_root: Path, canonical_root: Path, staging_root: Path
) -> tuple[int, dict]:
    return tool.run_merge_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        conflict_policy=tool.DEFAULT_HIGHWATER_CONFLICT_POLICY,
        canonical_root_provenance_description="test",
        rehearse_without_quiescence_gates=False,
    )


def test_verify_refuses_when_the_ledger_recorded_a_marker_it_could_not_reconcile(
    quiesced_state_root_pair, tmp_path: Path
) -> None:
    """裁决被拒的 marker **不写出文件**，于是它在 staging 里与「两根本来就没有这个
    marker」完全不可区分。只遍历 staging 现存文件的检查因此永远看不见它，而 switch
    随后把 legacy 根整棵改名归档 ⇒ 那份**本来可读**的水位永久取不回来。

    这里构造的正是真实排练里出现过的形状：一侧可读、另一侧损坏 ⇒ 整体拒绝。
    """

    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    (canonical_root / tool.ROUND_BASELINE_PENDING_SUBDIRECTORY).mkdir(
        parents=True, exist_ok=True
    )
    (legacy_root / tool.ROUND_BASELINE_PENDING_SUBDIRECTORY).mkdir(
        parents=True, exist_ok=True
    )
    (canonical_root / tool.ROUND_BASELINE_PENDING_SUBDIRECTORY / "task-a877b.json").write_text(
        json.dumps({"baseline_head": "a" * 40, "repo": "/tmp/does-not-matter"}),
        encoding="utf-8",
    )
    (legacy_root / tool.ROUND_BASELINE_PENDING_SUBDIRECTORY / "task-a877b.json").write_text(
        "{ this is not json", encoding="utf-8"
    )

    merge_exit_code, ledger = _merge_quiesced_pair(legacy_root, canonical_root, staging_root)
    assert merge_exit_code == 0, "前提：tracker 侧没有拒绝，所以 merge 自己是绿的"
    assert ledger["round_baseline_staging"]["refused_marker_names"] == ["task-a877b"], (
        "夹具没造出「账本记了 refusal」的状态，这条用例就测不到它要测的东西"
    )

    verify_exit_code, receipt = tool.run_verify_command(
        legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
    )
    assert verify_exit_code == 1, receipt["findings"]
    assert any(
        finding["check"] == "no_marker_left_unreconciled"
        and finding["severity"] == "blocking"
        and "task-a877b" in finding["detail"]
        for finding in receipt["findings"]
    ), receipt["findings"]

    switch_exit_code, payload = tool.run_switch_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        archive_root=tmp_path / "archive",
    )
    assert switch_exit_code == 2
    assert payload["reason"] == "verification_failed"
    assert not (tmp_path / "archive").exists(), "拒绝时绝不能已经归档 legacy 根"
    # RVF-002 之后 switch 是分阶段可续跑的，全量校验**只在第一次**跑；这条断言钉死
    # 新闸确实落在那道一次性前置校验里：阶段账本压根没被写出 ⇒ 破坏性阶段从未进入，
    # 于是也不存在「续跑绕过这道闸」的路径。
    assert not tool.switch_stage_journal_path(staging_root).exists(), (
        "被拒的 switch 不该留下阶段账本，否则下一次 switch 会当成续跑而跳过校验"
    )


def test_verify_refuses_when_a_marker_the_ledger_recorded_as_written_is_gone_from_staging(
    quiesced_state_root_pair,
) -> None:
    """账本说写出去了、staging 里却没有 ⇒ 那条水位会随 legacy 根一起被封进归档。"""

    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    merge_exit_code, ledger = _merge_quiesced_pair(legacy_root, canonical_root, staging_root)
    assert merge_exit_code == 0
    written_marker_names = ledger["review_highwater_staging"]["written_marker_names"]
    assert written_marker_names, "夹具本该写出至少一个 highwater marker"

    (
        staging_root
        / tool.STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME
        / tool.REVIEW_HIGHWATER_SUBDIRECTORY
        / f"{written_marker_names[0]}.json"
    ).unlink()

    verify_exit_code, receipt = tool.run_verify_command(
        legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
    )
    assert verify_exit_code == 1
    assert any(
        finding["check"] == "ledger_written_markers_all_present_in_staging"
        and finding["severity"] == "blocking"
        for finding in receipt["findings"]
    ), receipt["findings"]


def test_verify_refuses_when_staging_grew_a_marker_the_ledger_never_recorded(
    quiesced_state_root_pair,
) -> None:
    """反方向同样要挡：staging 在 merge 之后被人塞进过东西，账本就不再是它的凭据。"""

    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    merge_exit_code, _ = _merge_quiesced_pair(legacy_root, canonical_root, staging_root)
    assert merge_exit_code == 0

    (
        staging_root
        / tool.STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME
        / tool.REVIEW_HIGHWATER_SUBDIRECTORY
        / "task-never-merged.json"
    ).write_text(json.dumps({"reviewed_head": "c" * 40}), encoding="utf-8")

    verify_exit_code, receipt = tool.run_verify_command(
        legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
    )
    assert verify_exit_code == 1
    assert any(
        finding["check"] == "staged_markers_all_recorded_in_ledger"
        and finding["severity"] == "blocking"
        and "task-never-merged" in finding["detail"]
        for finding in receipt["findings"]
    ), receipt["findings"]


@pytest.mark.parametrize("staging_ledger_key", ["review_highwater_staging", "round_baseline_staging"])
def test_verify_refuses_a_ledger_that_never_recorded_the_marker_staging_summary(
    quiesced_state_root_pair, staging_ledger_key: str
) -> None:
    """账本没这一段就无法证明「每个 marker 都有归宿」——旧版 merge 的产物必须 fail-closed。"""

    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    merge_exit_code, ledger = _merge_quiesced_pair(legacy_root, canonical_root, staging_root)
    assert merge_exit_code == 0
    ledger.pop(staging_ledger_key)
    (staging_root / tool.MERGE_LEDGER_FILE_NAME).write_text(
        json.dumps(ledger), encoding="utf-8"
    )

    verify_exit_code, receipt = tool.run_verify_command(
        legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
    )
    assert verify_exit_code == 1
    assert any(
        finding["check"] == "marker_staging_summary_present_in_ledger"
        and finding["severity"] == "blocking"
        for finding in receipt["findings"]
    ), receipt["findings"]


@pytest.mark.parametrize(
    ("side", "relative_path", "content"),
    [
        # 并集目录：issue 给的复现步骤——merge 之后往 legacy 根新增一个 suppression 戳。
        # 目录在 merge **之前**就已存在且已有一个同内容的戳（见下面的预置），所以这一
        # 例是一次**纯粹的文件名清单变化**：内容多重集都没变。
        ("legacy", "kanban-task-suppressions/task-written-after-merge.json", "{}"),
        # marker 目录：新增一个 task 的水位。
        (
            "legacy",
            f"{tool.REVIEW_HIGHWATER_SUBDIRECTORY}/task-late.json",
            '{"reviewed_head": "%s"}' % ("b" * 40),
        ),
        # 根下单文件：latest.json 被改写。
        (
            "canonical",
            "latest.json",
            '{"run_id": "run-written-after-merge", "updated_at": "2099-01-01T00:00:00Z"}',
        ),
    ],
)
def test_verify_refuses_when_a_non_tracker_source_changed_after_merge(
    quiesced_state_root_pair, tmp_path: Path, side: str, relative_path: str, content: str
) -> None:
    """merge 与 switch 之间的写会被整体换入静默丢掉——这道闸不能只覆盖 tracker 库。

    marker / ``latest.json`` / 三个并集目录同样是 merge 读、switch 换入、legacy 侧随后
    整棵归档的来源；它们在 merge 之后被改动而闸不红，就是「文件只留在 archive 里、
    canonical 侧不可见」。
    """

    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    root = legacy_root if side == "legacy" else canonical_root
    target = root / relative_path
    # 预置一个**同内容、不同名**的邻居：这样「merge 之后新增一个文件」不会顺带改变
    # 「条目从缺席变成目录」或内容多重集，闸只有真的把文件名清单编进指纹才会红。
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent != root:
        (target.parent / "already-there-before-merge.json").write_text(content, encoding="utf-8")

    merge_exit_code, _ = _merge_quiesced_pair(legacy_root, canonical_root, staging_root)
    assert merge_exit_code == 0

    target.write_text(content, encoding="utf-8")

    verify_exit_code, receipt = tool.run_verify_command(
        legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
    )
    assert verify_exit_code == 1, receipt["findings"]
    assert any(
        finding["check"] == "non_tracker_source_unchanged_since_merge"
        and finding["severity"] == "blocking"
        for finding in receipt["findings"]
    ), receipt["findings"]

    switch_exit_code, payload = tool.run_switch_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        archive_root=tmp_path / "archive",
    )
    assert switch_exit_code == 2
    assert payload["reason"] == "verification_failed"
    assert not (tmp_path / "archive").exists()
    # 同 RVF-001 那条：钉死新闸落在 switch 的一次性前置校验里，而不是某条续跑会跳过的路径。
    assert not tool.switch_stage_journal_path(staging_root).exists()


def test_verify_refuses_a_ledger_that_never_fingerprinted_the_non_tracker_sources(
    quiesced_state_root_pair,
) -> None:
    """账本无该字段时 fail-closed：旧版 merge 的 staging 不能靠「没记就当没变」放行。"""

    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    merge_exit_code, ledger = _merge_quiesced_pair(legacy_root, canonical_root, staging_root)
    assert merge_exit_code == 0
    ledger.pop("non_tracker_source_fingerprints_by_side")
    (staging_root / tool.MERGE_LEDGER_FILE_NAME).write_text(
        json.dumps(ledger), encoding="utf-8"
    )

    verify_exit_code, receipt = tool.run_verify_command(
        legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
    )
    assert verify_exit_code == 1
    assert any(
        finding["check"] == "non_tracker_sources_fingerprinted_in_ledger"
        and finding["severity"] == "blocking"
        for finding in receipt["findings"]
    ), receipt["findings"]


def test_every_non_tracker_merge_source_in_the_registry_is_fingerprinted(tmp_path: Path) -> None:
    """指纹的条目清单必须**从登记表派生**，不另抄一份。

    下一个人往登记表里加一个并集目录时，这道闸要自动跟着覆盖它，而不是悄悄漏掉。
    ``runs`` / ``diff-tracker`` 是刻意的例外（前者 switch 遍历活目录且 legacy 侧
    18 GB，后者已由 per-repo sha256 覆盖），归档专用条目没有活读者、不参与合并。
    """

    root = tmp_path / "state-root"
    root.mkdir()
    fingerprinted = set(tool.fingerprint_non_tracker_merge_sources(root))
    expected = {
        strategy.entry_name_pattern
        for strategy in tool.STATE_ROOT_ENTRY_MIGRATION_STRATEGIES
        if strategy.kind
        in {
            tool.STATE_ROOT_ENTRY_KIND_REVIEW_HIGHWATER_MARKERS,
            tool.STATE_ROOT_ENTRY_KIND_ROUND_BASELINE_MARKERS,
            tool.STATE_ROOT_ENTRY_KIND_UNION_BY_FILE_NAME,
            tool.STATE_ROOT_ENTRY_KIND_LATEST_RUN_POINTER,
        }
    }
    assert fingerprinted == expected
    assert tool.RUNS_SUBDIRECTORY not in fingerprinted
    assert "diff-tracker" not in fingerprinted


def test_state_root_entry_fingerprint_sees_a_pure_addition_and_a_pure_deletion(
    tmp_path: Path,
) -> None:
    """指纹必须把**文件名清单**编进摘要。

    只对文件内容取哈希、不管清单，「新增一个 marker」与「删掉一个 marker」这两种最
    典型的丢数据形态都不会改变摘要——这道闸要挡的恰恰就是它们。同理，「条目不存在」
    与「条目存在但是空目录」必须可区分。
    """

    directory = tmp_path / "kanban-task-suppressions"
    absent_fingerprint = tool.sha256_of_state_root_entry(directory)
    directory.mkdir()
    empty_fingerprint = tool.sha256_of_state_root_entry(directory)
    (directory / "a.json").write_text("{}", encoding="utf-8")
    with_one_fingerprint = tool.sha256_of_state_root_entry(directory)
    # 内容与 a.json 完全一致的第二个文件：只哈希内容集合的实现在这里会看不见变化。
    (directory / "b.json").write_text("{}", encoding="utf-8")
    with_two_fingerprint = tool.sha256_of_state_root_entry(directory)
    (directory / "a.json").unlink()
    only_b_fingerprint = tool.sha256_of_state_root_entry(directory)

    assert (
        len(
            {
                absent_fingerprint,
                empty_fingerprint,
                with_one_fingerprint,
                with_two_fingerprint,
                only_b_fingerprint,
            }
        )
        == 5
    )


def test_state_root_entry_fingerprint_ignores_atomic_write_temporary_files(
    tmp_path: Path,
) -> None:
    """marker 的原子写会留下 ``.name.<hex>.tmp`` 中间文件；把它们算进指纹只会让闸在
    正常情况下也红。口径与 ``unregistered_state_root_entry_names`` /
    ``stage_union_by_file_name_directory`` 一致。"""

    directory = tmp_path / tool.REVIEW_HIGHWATER_SUBDIRECTORY
    directory.mkdir()
    (directory / "task.json").write_text("{}", encoding="utf-8")
    before = tool.sha256_of_state_root_entry(directory)
    (directory / ".task.json.deadbeef.tmp").write_text("{}", encoding="utf-8")
    assert tool.sha256_of_state_root_entry(directory) == before


def test_switch_refuses_when_verification_fails(quiesced_state_root_pair, tmp_path: Path) -> None:
    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    tool.run_merge_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        conflict_policy=tool.DEFAULT_HIGHWATER_CONFLICT_POLICY,
        canonical_root_provenance_description="test",
        rehearse_without_quiescence_gates=False,
    )
    # 把一个合并产物弄坏：staging 里塞一个 reviewed_head 不是全 sha 的 marker。
    (
        staging_root
        / tool.STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME
        / tool.REVIEW_HIGHWATER_SUBDIRECTORY
        / "task-broken.json"
    ).write_text(json.dumps({"reviewed_head": "not-a-sha"}), encoding="utf-8")

    exit_code, payload = tool.run_switch_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        archive_root=tmp_path / "archive",
    )
    assert exit_code == 2
    assert payload["reason"] == "verification_failed"
    assert legacy_root.is_dir() and any(legacy_root.iterdir()), "拒绝时绝不能已经动过 legacy 根"


def test_merge_is_rerunnable_and_rebuilds_staging_from_scratch(
    quiesced_state_root_pair,
) -> None:
    """「目标已存在就跳过」不是合法的幂等判据——合并是有状态的（水位只裁决一次）。"""

    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    first_exit_code, first_ledger = tool.run_merge_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        conflict_policy=tool.DEFAULT_HIGHWATER_CONFLICT_POLICY,
        canonical_root_provenance_description="test",
        rehearse_without_quiescence_gates=False,
    )
    stray = (
        staging_root / tool.STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME / "review-highwater"
        / "task-stale-from-a-previous-run.json"
    )
    stray.write_text(json.dumps({"reviewed_head": "c" * 40}), encoding="utf-8")

    second_exit_code, second_ledger = tool.run_merge_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        conflict_policy=tool.DEFAULT_HIGHWATER_CONFLICT_POLICY,
        canonical_root_provenance_description="test",
        rehearse_without_quiescence_gates=False,
    )
    assert (first_exit_code, second_exit_code) == (0, 0)
    assert not stray.exists(), "重跑必须从干净 staging 重建，不能留下上一轮的残留"
    assert (
        first_ledger["review_highwater_staging"]["written_marker_names"]
        == second_ledger["review_highwater_staging"]["written_marker_names"]
    )


# ---------------------------------------------------------------------------
# switch 破坏性阶段的生命周期契约：阶段账本 / 归档目标闸 / 只读归档
# ---------------------------------------------------------------------------


def _restore_write_permissions_so_pytest_can_clean_up(root: Path) -> None:
    """把只读归档的权限放开。

    ``switch`` 会把归档树整棵封成 ``0o500`` / ``0o400``，用例断言完必须放开，
    否则 ``tmp_path`` 的保留期清理会在别的 session 里撞 ``PermissionError``。
    """

    if not root.exists():
        return
    for current_directory, _directory_names, file_names in os.walk(root):
        os.chmod(current_directory, 0o700)
        for name in file_names:
            target = Path(current_directory) / name
            if not target.is_symlink():
                os.chmod(target, 0o600)


def _merge_and_verify(legacy_root: Path, canonical_root: Path, staging_root: Path) -> None:
    merge_exit_code, ledger = tool.run_merge_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        conflict_policy=tool.DEFAULT_HIGHWATER_CONFLICT_POLICY,
        canonical_root_provenance_description="test",
        rehearse_without_quiescence_gates=False,
    )
    assert merge_exit_code == 0, ledger
    verify_exit_code, receipt = tool.run_verify_command(
        legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
    )
    assert verify_exit_code == 0, receipt["findings"]


def _unit_identifiers_in(database_path: Path) -> set[str]:
    connection = sqlite3.connect(str(database_path))
    try:
        return {row[0] for row in connection.execute("SELECT unit_id FROM units").fetchall()}
    finally:
        connection.close()


def test_switch_resumes_after_a_crash_that_already_swapped_in_a_tracker_repository(
    quiesced_state_root_pair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """换入第一个 tracker 仓之后崩溃，必须还能续跑——不能两个命令都拒绝接手。

    崩溃点之后 canonical 侧的源库**已经是合并结果**，``source_database_unchanged_since_merge``
    这道闸必然红；而 staging 的 merged 目录已被 rename 消费掉，重跑 merge 会把合并库
    当成新基库再并一遍。所以「续跑」必须由阶段账本承担，而不是靠重跑前置校验。
    """

    legacy_root, canonical_root, staging_root, repository_key = quiesced_state_root_pair
    _merge_and_verify(legacy_root, canonical_root, staging_root)
    archive_root = tmp_path / "legacy-state-root.migrated"

    canonical_database = (
        canonical_root / tool.TRACKER_REPOSITORIES_SUBDIRECTORY / repository_key
        / tool.TRACKER_DATABASE_FILE_NAME
    )
    assert _unit_identifiers_in(canonical_database) == {"u-canonical"}

    # tracker 换入用 os.rename、紧随其后的顶层条目换入用 os.replace：让后者当场爆炸，
    # 就精确停在「tracker 已换入、其余全没做」这个最难受的中间态。
    def exploding_replace(*_arguments: object, **_keywords: object) -> None:
        raise RuntimeError("模拟 switch 在换入 tracker 之后崩溃")

    monkeypatch.setattr(tool.os, "replace", exploding_replace)
    with pytest.raises(RuntimeError):
        tool.run_switch_command(
            legacy_root=legacy_root,
            canonical_root=canonical_root,
            staging_root=staging_root,
            archive_root=archive_root,
        )
    monkeypatch.undo()

    assert _unit_identifiers_in(canonical_database) == {"u-legacy", "u-canonical"}, (
        "夹具没构造出「tracker 已经换入」的中间态，这条用例就测不到它要测的东西"
    )
    assert legacy_root.is_dir() and any(legacy_root.iterdir())

    exit_code, receipt = tool.run_switch_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        archive_root=archive_root,
    )
    try:
        assert exit_code == 0, receipt
        assert receipt["status"] == "switched"
        assert receipt["resumed_from_switch_stage_journal"] is True
        assert repository_key in receipt["installed_tracker_repositories"]
        assert _unit_identifiers_in(canonical_database) == {"u-legacy", "u-canonical"}
        assert {child.name for child in (canonical_root / tool.RUNS_SUBDIRECTORY).iterdir()} == {
            "run-from-legacy-state-root",
            "run-from-canonical-state-root",
        }
        assert archive_root.is_dir()
        assert legacy_root.is_dir() and not any(legacy_root.iterdir())
    finally:
        _restore_write_permissions_so_pytest_can_clean_up(archive_root)


def test_switch_resumes_after_a_crash_that_had_already_archived_the_legacy_root(
    quiesced_state_root_pair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """崩在归档之后：legacy 侧的源库已经整棵搬走，续跑更加不能依赖前置校验。

    这时 ``source_database_still_present`` 会对**每一个**仓报红——换句话说，越晚崩，
    重跑校验越不可能通过。续跑必须完全由阶段账本裁决。
    """

    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    _merge_and_verify(legacy_root, canonical_root, staging_root)
    archive_root = tmp_path / "legacy-state-root.migrated"

    def exploding_seal(*_arguments: object, **_keywords: object) -> None:
        raise RuntimeError("模拟 switch 在归档之后、封只读之前崩溃")

    monkeypatch.setattr(tool, "seal_directory_tree_read_only", exploding_seal)
    with pytest.raises(RuntimeError):
        tool.run_switch_command(
            legacy_root=legacy_root,
            canonical_root=canonical_root,
            staging_root=staging_root,
            archive_root=archive_root,
        )
    monkeypatch.undo()

    assert archive_root.is_dir(), "夹具没停在「已归档」这个中间态"
    assert not legacy_root.exists()

    exit_code, receipt = tool.run_switch_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        archive_root=archive_root,
    )
    try:
        assert exit_code == 0, receipt
        assert receipt["resumed_from_switch_stage_journal"] is True
        assert receipt["legacy_root_archive_read_only_sealing"]["failure_count"] == 0
        assert (os.stat(archive_root).st_mode & 0o777) == (
            tool.LEGACY_ROOT_ARCHIVE_DIRECTORY_MODE
        )
        assert legacy_root.is_dir() and not any(legacy_root.iterdir())
        assert (
            os.stat(legacy_root).st_mode & 0o777
        ) == tool.LEGACY_ROOT_SENTINEL_DIRECTORY_MODE
        assert (canonical_root / tool.SWITCH_RECEIPT_FILE_NAME).is_file()
    finally:
        _restore_write_permissions_so_pytest_can_clean_up(archive_root)


def test_command_line_switch_resumes_even_though_the_legacy_root_is_already_gone(
    quiesced_state_root_pair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """走 CLI 的续跑：legacy 根已被改名，命令行入口不能在「legacy 根不存在」处静默 return 0。

    顺带钉住归档目标的连续性：默认归档名带 UTC 日期，隔天续跑会算出另一个路径；
    没显式传 ``--archive-root`` 时必须沿用阶段账本记的那个，而不是撞身份闸被拒。
    """

    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    _merge_and_verify(legacy_root, canonical_root, staging_root)
    archive_root = tmp_path / "archive-chosen-on-the-first-attempt"

    def exploding_seal(*_arguments: object, **_keywords: object) -> None:
        raise RuntimeError("模拟 switch 在归档之后崩溃")

    monkeypatch.setattr(tool, "seal_directory_tree_read_only", exploding_seal)
    with pytest.raises(RuntimeError):
        tool.run_switch_command(
            legacy_root=legacy_root,
            canonical_root=canonical_root,
            staging_root=staging_root,
            archive_root=archive_root,
        )
    monkeypatch.undo()
    assert not legacy_root.exists()

    exit_code = tool.main(
        [
            "--legacy-root",
            str(legacy_root),
            "--canonical-root",
            str(canonical_root),
            "switch",
            "--staging-root",
            str(staging_root),
        ]
    )
    try:
        assert exit_code == 0
        assert archive_root.is_dir(), "续跑不该另起一个带今天日期的归档目标"
        assert (os.stat(archive_root).st_mode & 0o777) == (
            tool.LEGACY_ROOT_ARCHIVE_DIRECTORY_MODE
        )
        assert legacy_root.is_dir() and not any(legacy_root.iterdir())
        assert (canonical_root / tool.SWITCH_RECEIPT_FILE_NAME).is_file()
    finally:
        _restore_write_permissions_so_pytest_can_clean_up(archive_root)


def test_merge_refuses_to_rebuild_a_staging_that_switch_has_already_started_consuming(
    quiesced_state_root_pair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """半途而废的 switch 之后重跑 merge = 把已换入的合并库当新基库再并一遍。"""

    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    _merge_and_verify(legacy_root, canonical_root, staging_root)

    def exploding_replace(*_arguments: object, **_keywords: object) -> None:
        raise RuntimeError("模拟 switch 在换入 tracker 之后崩溃")

    monkeypatch.setattr(tool.os, "replace", exploding_replace)
    with pytest.raises(RuntimeError):
        tool.run_switch_command(
            legacy_root=legacy_root,
            canonical_root=canonical_root,
            staging_root=staging_root,
            archive_root=tmp_path / "archive",
        )
    monkeypatch.undo()

    exit_code, payload = tool.run_merge_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        conflict_policy=tool.DEFAULT_HIGHWATER_CONFLICT_POLICY,
        canonical_root_provenance_description="test",
        rehearse_without_quiescence_gates=False,
    )
    assert exit_code == 2
    assert payload["status"] == "refused"
    assert "switch_already_started_consuming_this_staging" in payload["blocking_reasons"]


def test_switch_refuses_a_stage_journal_left_by_a_different_merge(
    quiesced_state_root_pair, tmp_path: Path
) -> None:
    """账本是「校验已通过」的唯一凭据，凭据对不上号就不能跳过校验往下走。"""

    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    _merge_and_verify(legacy_root, canonical_root, staging_root)

    journal_path = tool.switch_stage_journal_path(staging_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        json.dumps(
            {
                "migration_tool_contract_version": tool.MIGRATION_TOOL_CONTRACT_VERSION,
                "destructive_phase_entered": True,
                "legacy_root": str(legacy_root),
                "canonical_root": str(canonical_root),
                "staging_root": str(staging_root),
                "archive_root": str(tmp_path / "archive"),
                "merge_ledger_generated_at": "1999-01-01T00:00:00Z",
                "completed_stages": [],
            }
        ),
        encoding="utf-8",
    )

    exit_code, payload = tool.run_switch_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        archive_root=tmp_path / "archive",
    )
    assert exit_code == 2
    assert payload["reason"] == "switch_stage_journal_belongs_to_a_different_merge"
    assert legacy_root.is_dir() and any(legacy_root.iterdir()), "拒绝时绝不能已经动过 legacy 根"


@pytest.mark.parametrize(
    "overlapping_root_name",
    ["legacy", "canonical", "staging"],
)
def test_switch_refuses_an_archive_root_that_overlaps_any_state_root(
    quiesced_state_root_pair, overlapping_root_name: str
) -> None:
    """归档目标重叠必须在**动手之前**被拒。

    ``os.rename(legacy_root, archive_root)`` 排在整条破坏性序列的最末端：tracker 已
    换入、runs 已搬完、origin stamp 已写。这时候才 EINVAL，是最不可恢复的失败位置。
    """

    legacy_root, canonical_root, staging_root, repository_key = quiesced_state_root_pair
    _merge_and_verify(legacy_root, canonical_root, staging_root)
    container = {
        "legacy": legacy_root,
        "canonical": canonical_root,
        "staging": staging_root,
    }[overlapping_root_name]

    exit_code, payload = tool.run_switch_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        archive_root=container / "nested-archive-target",
    )
    assert exit_code == 2
    assert payload["reason"] == "archive_root_overlaps_state_roots"
    assert legacy_root.is_dir() and any(legacy_root.iterdir()), "拒绝时绝不能已经动过 legacy 根"
    canonical_database = (
        canonical_root / tool.TRACKER_REPOSITORIES_SUBDIRECTORY / repository_key
        / tool.TRACKER_DATABASE_FILE_NAME
    )
    assert _unit_identifiers_in(canonical_database) == {"u-canonical"}, (
        "闸排在破坏性阶段之前，canonical 侧的库不该已经被换掉"
    )
    assert not tool.switch_stage_journal_path(staging_root).exists()


def test_switch_refuses_an_archive_root_on_a_different_volume(
    quiesced_state_root_pair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跨卷 ``os.rename`` 会 EXDEV，同样发生在最末端——必须提前挡。"""

    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    _merge_and_verify(legacy_root, canonical_root, staging_root)
    archive_root = tmp_path / "archive-on-another-volume"
    real_device_identifier = tool.device_identifier_of_nearest_existing_ancestor

    def device_identifier_pretending_the_archive_is_on_another_volume(path: Path) -> int | None:
        if path == archive_root:
            return -1
        return real_device_identifier(path)

    monkeypatch.setattr(
        tool,
        "device_identifier_of_nearest_existing_ancestor",
        device_identifier_pretending_the_archive_is_on_another_volume,
    )
    exit_code, payload = tool.run_switch_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        archive_root=archive_root,
    )
    assert exit_code == 2
    assert payload["reason"] == "archive_root_on_a_different_volume"
    assert legacy_root.is_dir() and any(legacy_root.iterdir()), "拒绝时绝不能已经动过 legacy 根"


def test_switch_seals_the_archived_tree_read_only_not_just_the_new_sentinel(
    quiesced_state_root_pair, tmp_path: Path
) -> None:
    """「只读归档」不能只是个说法：被 rename 走的旧状态树本身必须不可写。

    归档的存在理由就是「保留回滚证据」。只把新建的空哨兵目录 chmod 成 0o500、
    旧树原样保留 0755，等于契约是空的。
    """

    legacy_root, canonical_root, staging_root, _ = quiesced_state_root_pair
    _merge_and_verify(legacy_root, canonical_root, staging_root)
    archive_root = tmp_path / "legacy-state-root.migrated"

    exit_code, receipt = tool.run_switch_command(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        archive_root=archive_root,
    )
    try:
        assert exit_code == 0, receipt
        sealing = receipt["legacy_root_archive_read_only_sealing"]
        assert sealing["failure_count"] == 0, sealing
        assert sealing["directory_count"] >= 1 and sealing["file_count"] >= 1

        assert (os.stat(archive_root).st_mode & 0o777) == (
            tool.LEGACY_ROOT_ARCHIVE_DIRECTORY_MODE
        )
        for descendant in archive_root.rglob("*"):
            if descendant.is_symlink():
                continue
            expected = (
                tool.LEGACY_ROOT_ARCHIVE_DIRECTORY_MODE
                if descendant.is_dir()
                else tool.LEGACY_ROOT_ARCHIVE_FILE_MODE
            )
            assert (os.stat(descendant).st_mode & 0o777) == expected, descendant
        with pytest.raises(PermissionError):
            (archive_root / "should-not-be-writable").mkdir()
    finally:
        _restore_write_permissions_so_pytest_can_clean_up(archive_root)
