"""scope marker 的文件名 key：仓身份（+ 可选 harness 身份）。

**背景**：``review_highwater_marker`` 与 ``round_baseline_marker`` 的 key 一直只有
``task-<task_id>`` / ``sess-<session_id>``。两处都撞得到：

1. **跨仓撞 task id。** cline-kanban 的 task id 是短哈希前缀，不同 project 会撞。
   实测反例：``round-baseline-pending/task-a877b.json`` 在一个状态根里指
   ``.cline/worktrees/a877b/cline-kanban``、在另一个根里指
   ``Documents/GitHub/my-ai-setup``——两个毫不相干的仓共用一个 baseline。
2. **跨 harness 撞同一 task。**（仅 round-baseline，见
   ``core.host_adapter.parent_harness_identity`` 模块头。）

**为什么两种 marker 的 key 不一样**——这条不对称是刻意的，别「顺手统一」：

- ``review-highwater`` 记的是「最近一次完成的 review 覆盖到哪个 HEAD」，那是**仓/工作树
  层面的事实**，与哪个 harness 发起无关。Claude 审过的 commit 对 Codex 同样是审过的，
  共用一份反而正确（且 ``write_review_highwater`` 自带「只前移不回退」守卫兜住次序）。
  把 harness 编进它的 key 只会把水位切碎、逼每个 harness 各自 bootstrap ⇒ 多派。
  更要命的是**迁移不可逆**：历史 marker 里没记过 harness，切了 key 就等于把 72 份
  真实水位全部作废。
- ``round-baseline`` 记的是「**这个会话**上一条 user prompt 提交时的 HEAD」，天生 per-会话。
  两个 harness 共用一份就是互相覆盖，且覆盖方向可能把窗口下界**抬高** ⇒ 漏审。

**失败方向一律选偏宽**：仓身份解析不出来时返回空候选列表（写者 no-op、读者读不到
⇒ 退回无 committed 观测），而不是编一个 ``repo-unspecified`` 桶去和别的仓混住。
harness 判不出时用 ``unspecified`` token，且读侧候选列表**显式包含**它作回退，
于是「写者判不出、读者判得出」只退化成窗口偏宽（多审），不会退化成 marker 消失。
"""

from __future__ import annotations

from pathlib import Path

from core.host_adapter.parent_harness_identity import (
    PARENT_HARNESS_IDENTITY_UNSPECIFIED,
    resolve_parent_harness_identity,
)

TASK_KEYED_MARKER_FILE_NAME_PREFIX = "task"
SESSION_KEYED_MARKER_FILE_NAME_PREFIX = "sess"
REPOSITORY_IDENTITY_FILE_NAME_SEGMENT_PREFIX = "repo"
PARENT_HARNESS_IDENTITY_FILE_NAME_SEGMENT_PREFIX = "harness"
MARKER_FILE_NAME_SEGMENT_SEPARATOR = "--"

_repository_identity_cache_by_resolved_repo_path: dict[str, str | None] = {}


def resolve_repository_identity_for_scope_marker_key(repo: str | Path | None) -> str | None:
    """仓身份 token；解析不出返回 None（调用方据此 fail-closed，别编一个占位桶）。

    真相源是 tracker 的 ``repo_key(git_common_dir(repo))``——**刻意复用而不是另写一份**：
    这个项目已经被「同一个逻辑标识两份实现、判据不同」坑过好几次（``stop_hook_router``
    与 ``session_hook_state_dir`` 的状态目录、``_ensure_schema`` 的迁移清单）。用
    ``git_common_dir`` 而不是 worktree 路径，是为了让同一个仓的所有 worktree / attempt
    共享一个身份——要区分的是**不同的仓**，不是不同的工作树。

    tracker 是个体量很大的模块，故延迟 import：本模块被 UserPromptSubmit 路径上的
    marker 模块顶层 import，不该把整个 tracker 拖进那条热路径的 import 成本。
    结果按 repo 路径缓存（``git_common_dir`` 要 spawn git）。
    """
    if repo is None:
        return None
    repo_text = str(repo).strip()
    if not repo_text:
        return None
    if repo_text in _repository_identity_cache_by_resolved_repo_path:
        return _repository_identity_cache_by_resolved_repo_path[repo_text]

    identity: str | None = None
    try:
        from core.session_scope_allocation.reviewable_unit_diff_tracker import (  # noqa: PLC0415
            git_common_dir,
            repo_key,
        )

        common_dir = git_common_dir(Path(repo_text))
        if common_dir is not None:
            identity = repo_key(common_dir)
    except Exception:  # noqa: BLE001 - 身份解析永不阻断 prompt / Stop
        identity = None

    _repository_identity_cache_by_resolved_repo_path[repo_text] = identity
    return identity


def scope_marker_file_name(
    *,
    key_prefix: str,
    key_token: str,
    repository_identity: str,
    parent_harness_identity: str | None,
) -> str:
    """组装一个 marker 文件名。

    形如 ``task-7ec31--repo-review-validate-fix-8f3a2b1c4d5e--harness-claude-code.json``；
    ``parent_harness_identity=None`` 表示这种 marker 不按 harness 分键（highwater）。
    名字刻意冗长自解释：这些文件会被人直接 ``ls`` 出来做取证，段前缀省掉之后没人分得清
    第二段是仓还是 harness。
    """
    segments = [
        f"{key_prefix}-{key_token}",
        f"{REPOSITORY_IDENTITY_FILE_NAME_SEGMENT_PREFIX}-{repository_identity}",
    ]
    if parent_harness_identity is not None:
        segments.append(
            f"{PARENT_HARNESS_IDENTITY_FILE_NAME_SEGMENT_PREFIX}-{parent_harness_identity}"
        )
    return MARKER_FILE_NAME_SEGMENT_SEPARATOR.join(segments) + ".json"


def scope_marker_candidate_paths(
    *,
    marker_directory: Path,
    task_id_token: str | None,
    session_id_token: str | None,
    repository_identity: str | None,
    parent_harness_identity: str | None,
) -> list[Path]:
    """候选路径列表：写者用 ``[0]``，读者取第一个存在的，清理者遍历全部。

    ``repository_identity is None`` ⇒ 返回空列表（fail-closed，见模块头）。

    ``parent_harness_identity`` 非 None 且非 ``unspecified`` 时，每个 key 后面**追加**
    一条 ``--harness-unspecified`` 回退候选。它接住两类 marker：迁移工具写出的历史
    marker（旧格式里根本没有 harness 可恢复），以及某个写入点比读取点少一层可用信号时
    留下的那份。命中回退只会让窗口偏宽——这是安全方向。
    """
    if repository_identity is None:
        return []

    harness_variants: list[str | None]
    if parent_harness_identity is None:
        harness_variants = [None]
    elif parent_harness_identity == PARENT_HARNESS_IDENTITY_UNSPECIFIED:
        harness_variants = [PARENT_HARNESS_IDENTITY_UNSPECIFIED]
    else:
        harness_variants = [parent_harness_identity, PARENT_HARNESS_IDENTITY_UNSPECIFIED]

    paths: list[Path] = []
    for key_prefix, key_token in (
        (TASK_KEYED_MARKER_FILE_NAME_PREFIX, task_id_token),
        (SESSION_KEYED_MARKER_FILE_NAME_PREFIX, session_id_token),
    ):
        if not key_token:
            continue
        for harness_variant in harness_variants:
            paths.append(
                marker_directory
                / scope_marker_file_name(
                    key_prefix=key_prefix,
                    key_token=key_token,
                    repository_identity=repository_identity,
                    parent_harness_identity=harness_variant,
                )
            )
    return paths


def resolve_parent_harness_identity_for_scope_marker_key(
    parent_harness_identity: str | None,
) -> str:
    """显式传入优先；``None`` 表示「就地判定」（见 ``resolve_parent_harness_identity``）。"""
    if isinstance(parent_harness_identity, str) and parent_harness_identity.strip():
        return parent_harness_identity.strip()
    return resolve_parent_harness_identity()
