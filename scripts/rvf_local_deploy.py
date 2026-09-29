#!/usr/bin/env python3
"""从唯一 local main 启动 runner，在 RVF 专用 deploy 源 worktree 的 HEAD checkout 上部署。

local main 的工作树全程只被读取 ref（HEAD / refs/heads/main），从不被写入，因此开发者或
其它 agent 手上的未提交改动既不阻塞部署，也不会被挪走。payload 一律来自 RVF 自己拥有的
detached worktree，它是纯 tracked checkout —— 「发布件身份 == commit」因此是结构性保证，
而不是靠「先挪走再放回」的时序约定。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import string
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
RUNNER_REPOSITORY_RELATIVE_PATH = "scripts/rvf_local_deploy.py"
DEPLOYMENT_SOURCE_WORKTREE_DIRECTORY_NAME = (
    "deployment-source-worktree-at-local-main-head"
)
DEPLOYMENT_SOURCE_WORKTREE_OWNERSHIP_MARKER_FILENAME = (
    "rvf-deployment-source-worktree-ownership-marker.json"
)
PARTIAL_REMEDIATION = (
    "修复失败原因后，从 local main 重新运行 runner；payload 只能来自 RVF 专用 deploy 源 "
    "worktree 的 HEAD checkout，不得降级到手写 plugin cache 或 installed payload。"
)
OFFICIAL_CLI_VERIFIER_REGISTRATION_UNIQUENESS_EXIT_CODE = 3
"""verifier 判定「宿主里存在第二份 RVF 注册 / payload 树」时的专属退出码。

必须与 `plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py`
的 ``REGISTRATION_UNIQUENESS_EXIT_CODE`` 逐字一致（两边同源于一个 commit，
`tests/test_official_cli_local_deploy_verification.py` 里有等值断言守着漂移）。

这个退出码是「不回滚」的唯一判据：注册唯一性失败必须**保留刚装好的 canonical**、只拒绝认证；
把它并进通用失败分支就会回滚掉 canonical，把机器留在「只剩残留那份」的更差状态。

注意它**只**是 verifier 子进程的退出码，不透传成 runner 自己的退出码——runner 的 3 早已是
「已有 RVF local deploy 正在运行」（见 `_deployment_lock`）。
"""
CODEX_SOLE_REGISTRATION_STATUS = "sole_registration"
"""`_sole_review_validate_fix_registration_in_codex_config` 唯一够格认证部署的状态。

门禁按「是否等于这个状态」判定，而不是「是否等于某个已识别的坏状态」：后者会把
`unavailable`（根本没核到）与 `canonical_selector_not_registered`（canonical 根本没注册）
当成好状态放行。
"""
CODEX_CANONICAL_SELECTOR_NOT_REGISTERED_STATUS = "canonical_selector_not_registered"
"""config.toml 可读、但 canonical selector 根本不在 `[plugins.*]` 注册表里。

不能读成「唯一」：没有第二份，不等于 canonical 那一份在。
"""
REGISTRATION_UNIQUENESS_REMEDIATION = (
    "先按上面的清理命令清掉非 canonical 的 RVF 注册/残留，再重新运行 runner 重新认证；"
    "canonical 安装本身已就位，**不要**用回滚去「修」这个失败——回滚只会移除 canonical、"
    "把机器留在只剩残留那份的更差状态。"
)


class DeployError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        exit_code: int = 1,
        result: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.result = result


@dataclass(frozen=True)
class LocalMainDeploymentSource:
    worktree_path: Path
    commit: str
    dirty_paths: tuple[str, ...]
    runner_matches_local_main_head: bool


@dataclass(frozen=True)
class StructuredHookConfigurationSnapshot:
    path: Path
    existed: bool
    content: bytes | None


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DeployError(f"git {' '.join(args)} 失败: {detail}", exit_code=2)
    return completed.stdout.strip()


def _git_optional(repo: Path, *args: str) -> str | None:
    """跑 git 但把非零退出当成「答案是没有」而不是错误。

    用于那些「失败本身就是有效信号」的查询，例如 detached HEAD 下的 symbolic-ref。
    """
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _git_optional_narration_text(repo: Path, *args: str) -> str | None:
    """`_git_optional` 的容错解码版：专供「输出只拿去讲给人看」的 git 查询。

    与 `_git_optional` 的差别只有一处：不做严格解码。git 输出的字节序列并不保证是 UTF-8
    —— 仓库可以设 `i18n.logOutputEncoding=ISO-8859-1`，历史里也可能本就存在非 UTF-8 的提交
    信息 —— 此时 `text=True` 的严格解码会抛 `UnicodeDecodeError`。而 commit subject 这类
    查询是**事后统计**：一次已经安装并验证通过、receipt 也已收敛的部署，绝不能因为讲述用的
    字节不好解码而以异常收场。所以这里退化标准与非零退出对齐（返回 `None`，交调用方降级），
    能拿到字节就用 `errors="replace"` 容错解码。

    刻意**不**把这个宽容度并进 `_git_optional`：那个 helper 的其它调用点读的是路径、ref 名、
    `rev-parse --verify` 解析结果这类**结构性**值，用 U+FFFD 顶替坏字节只会把一次硬失败换成
    一个看着正常、实则错误的路径/ref 继续往下走。容错解码只在「输出纯属人读叙述」时才是对的。

    错误处理器必须是 `replace` 而不是 `surrogateescape`：这段文本最终会进 result receipt 的
    JSON，`surrogateescape` 留下的孤立代理字符会让 `json.dumps` 当场抛错——那等于把解码期的
    异常挪到序列化期，一次已收敛的部署照样以异常收场。
    """
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _git_common_directory(repo: Path) -> Path | None:
    raw_common_directory = _git_optional(repo, "rev-parse", "--git-common-dir")
    if raw_common_directory is None:
        return None
    common_directory = Path(raw_common_directory)
    if not common_directory.is_absolute():
        # main worktree 里 git 返回相对路径（`.git`），需按查询所在目录还原。
        common_directory = repo / common_directory
    return common_directory.resolve()


def _git_worktree_records(repository_hint: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current_record: dict[str, str] = {}
    for line in _git(repository_hint, "worktree", "list", "--porcelain").splitlines():
        if not line:
            if current_record:
                records.append(current_record)
                current_record = {}
            continue
        key, _, value = line.partition(" ")
        current_record[key] = value
    if current_record:
        records.append(current_record)
    return records


def _status_paths(repo: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise DeployError(f"无法读取 git status: {repo}", exit_code=2)
    tokens = completed.stdout.decode("utf-8", errors="replace").split("\0")
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        status = token[:2]
        paths.append(token[3:])
        if "R" in status or "C" in status:
            if index < len(tokens) and tokens[index]:
                paths.append(tokens[index])
                index += 1
    return paths


def _git_operation_markers(local_main_worktree: Path) -> list[Path]:
    marker_names = (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "BISECT_LOG",
        "rebase-apply",
        "rebase-merge",
    )
    markers: list[Path] = []
    for marker_name in marker_names:
        marker_path = Path(
            _git(local_main_worktree, "rev-parse", "--git-path", marker_name)
        )
        if not marker_path.is_absolute():
            marker_path = local_main_worktree / marker_path
        if marker_path.exists():
            markers.append(marker_path.resolve())
    return markers


def _runner_matches_local_main_head(
    *,
    local_main_worktree: Path,
    runner_path: Path,
    frozen_deployment_commit: str,
) -> bool:
    """本次执行的 runner 自身是否就是「本次被部署的那个已冻结 commit」里的版本。

    允许 local main 带 dirty 之后出现了一个新的语义缺口：runner 可能是未提交版本，却在
    部署已冻结 commit 的 payload。这不构成失败（发布件身份依然诚实），但必须暴露 ——
    否则「我改了编排逻辑并部署了」会被误读成「部署件里已经有这段编排逻辑」。

    基线刻意绑定调用方已冻结的 `frozen_deployment_commit`（也正是 payload 的来源），
    而不是现场再解析一次 `HEAD`：并行 agent 可能在冻结之后向 local main 提交，`HEAD`
    会漂到一个根本没被部署的 commit，比较结果就回答不了本字段唯一要回答的问题。
    """
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(local_main_worktree),
            "show",
            f"{frozen_deployment_commit}:{RUNNER_REPOSITORY_RELATIVE_PATH}",
        ],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return False
    try:
        return runner_path.read_bytes() == completed.stdout
    except OSError:
        return False


def validate_local_main_deployment_source(
    *,
    repository_hint: Path,
    runner_path: Path,
    expected_local_main_commit: str | None,
) -> LocalMainDeploymentSource:
    repository_hint = repository_hint.expanduser().resolve()
    local_main_records = [
        record
        for record in _git_worktree_records(repository_hint)
        if record.get("branch") == "refs/heads/main"
    ]
    if len(local_main_records) != 1:
        raise DeployError(
            "必须存在且只能存在一个 checkout refs/heads/main 的 local main worktree; "
            f"实际数量={len(local_main_records)}",
            exit_code=2,
        )
    local_main_worktree = Path(local_main_records[0]["worktree"]).expanduser().resolve()
    expected_runner_path = local_main_worktree / RUNNER_REPOSITORY_RELATIVE_PATH
    if runner_path.expanduser().resolve() != expected_runner_path.resolve():
        raise DeployError(
            "deployment runner 必须来自 local main worktree: "
            f"expected={expected_runner_path}, actual={runner_path.expanduser().resolve()}",
            exit_code=2,
        )
    branch_ref = _git(local_main_worktree, "symbolic-ref", "--quiet", "HEAD")
    if branch_ref != "refs/heads/main":
        raise DeployError(
            f"local main worktree 未 checkout refs/heads/main: {branch_ref}",
            exit_code=2,
        )
    local_main_commit = _git(local_main_worktree, "rev-parse", "HEAD")
    local_main_ref_commit = _git(local_main_worktree, "rev-parse", "refs/heads/main")
    if local_main_commit != local_main_ref_commit:
        raise DeployError(
            "local main worktree HEAD 与 refs/heads/main 不一致",
            exit_code=2,
        )
    # local main 的未提交改动只作为信息上报：payload 来自 deploy 源 worktree 的 HEAD
    # checkout，dirty 既进不了发布件，也不需要被挪走。
    local_main_dirty_paths = _status_paths(local_main_worktree)
    operation_markers = _git_operation_markers(local_main_worktree)
    if operation_markers:
        raise DeployError(
            f"local main worktree 存在未完成 Git 操作: {operation_markers}",
            exit_code=2,
        )
    if expected_local_main_commit:
        expected_commit = _git(
            local_main_worktree,
            "rev-parse",
            "--verify",
            f"{expected_local_main_commit}^{{commit}}",
        )
        if expected_commit != local_main_commit:
            raise DeployError(
                "expected local main commit 与实际 HEAD 不一致: "
                f"expected={expected_commit}, actual={local_main_commit}",
                exit_code=2,
            )
    return LocalMainDeploymentSource(
        worktree_path=local_main_worktree,
        commit=local_main_commit,
        dirty_paths=tuple(sorted(local_main_dirty_paths)),
        runner_matches_local_main_head=_runner_matches_local_main_head(
            local_main_worktree=local_main_worktree,
            runner_path=runner_path.expanduser().resolve(),
            frozen_deployment_commit=local_main_commit,
        ),
    )


def deployment_source_worktree_path(home: Path) -> Path:
    """RVF 自有 deploy 源 worktree 的落点。

    与 `local-deploy.lock` 同级、由 `home` 驱动：测试传一个临时 home 即天然隔离。
    刻意不放在 `RVF_USER_LOCAL_DATA_ROOT` 下 —— 那里被 verifier 的目录校验与契约的
    嵌套合成部署 fixture 共用，多一棵 checkout 只会把两者耦合起来。
    """
    return home / ".rvf" / DEPLOYMENT_SOURCE_WORKTREE_DIRECTORY_NAME


def _deployment_source_worktree_ownership_marker_path(
    deployment_source_worktree: Path,
) -> Path | None:
    """RVF 归属证据的落盘位置：该 worktree 私有的 git admin dir 内。

    刻意不写进工作树 —— 那里每轮都被 `reset --hard` + `clean -xdff` 抹平，证据活不过一次
    部署。admin dir 由 `git worktree add` 单独为这棵树创建、随 `git worktree prune` 一起
    消失，因此「证据在」与「这棵树是 RVF 建的、且仍然是那一棵」是同一件事。
    """
    absolute_git_directory = _git_optional(
        deployment_source_worktree, "rev-parse", "--absolute-git-dir"
    )
    if not absolute_git_directory:
        return None
    return (
        Path(absolute_git_directory)
        / DEPLOYMENT_SOURCE_WORKTREE_OWNERSHIP_MARKER_FILENAME
    )


def _record_deployment_source_worktree_ownership(
    deployment_source_worktree: Path,
) -> None:
    """在 RVF 亲手建出这棵树的那一刻写下归属证据；写不下就不许进入销毁回路。"""
    marker_path = _deployment_source_worktree_ownership_marker_path(
        deployment_source_worktree
    )
    if marker_path is None:
        raise DeployError(
            "无法为新建的 deploy 源 worktree 写入 RVF 归属证据: "
            f"{deployment_source_worktree}",
            exit_code=2,
        )
    marker_path.write_text(
        json.dumps(
            {
                "owner": "review-validate-fix",
                "purpose": DEPLOYMENT_SOURCE_WORKTREE_DIRECTORY_NAME,
                "worktree_path": str(deployment_source_worktree.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _deployment_source_worktree_has_rvf_ownership_evidence(
    deployment_source_worktree: Path,
) -> bool:
    marker_path = _deployment_source_worktree_ownership_marker_path(
        deployment_source_worktree
    )
    if marker_path is None or not marker_path.is_file():
        return False
    marker = _read_json_object(marker_path)
    if marker is None:
        return False
    # 证据与落点绑定：admin dir 若被指到别的树上，旧证据不得为新落点背书。
    return marker.get("worktree_path") == str(deployment_source_worktree.resolve())


def _deployment_source_worktree_is_reusable(
    deployment_source_worktree: Path,
    *,
    expected_git_common_directory: Path,
) -> bool:
    """判断现有目录是否确实是本仓库的、RVF 自有的 deploy 源 worktree（fail-closed）。

    复用分支的代价是紧接着的 `reset --hard` + `clean -xdff`，所以这里要的是「RVF 建过这棵
    树」的**可验证证据**，而不是「形状像 RVF 的树」的推定：同仓 + detached 这组形状任何人
    都手工造得出来，一旦据此放行，别人未提交的 tracked 改动与 untracked 文件就被不可逆抹掉。
    """
    # 符号链接一律不进销毁路径：`is_dir()` / `exists()` 都跟随链接，真实内容在别处，
    # 交由 `_describe_...` 报「该路径是一个符号链接」而不是就地 reset 别人的树。
    if deployment_source_worktree.is_symlink():
        return False
    if not deployment_source_worktree.is_dir():
        return False
    if not (deployment_source_worktree / ".git").exists():
        return False
    if _git_common_directory(deployment_source_worktree) != expected_git_common_directory:
        return False
    # detached 是 RVF 自有树的必要条件：一旦挂上分支，说明它已被当作普通开发树使用，
    # 此时 reset --hard + clean -xdff 会毁掉别人的工作。判不可复用后由
    # `_require_vacant_deployment_source_worktree_landing_site` 拒绝退出（不是删除重建）。
    if (
        _git_optional(deployment_source_worktree, "symbolic-ref", "--quiet", "HEAD")
        is not None
    ):
        return False
    return _deployment_source_worktree_has_rvf_ownership_evidence(
        deployment_source_worktree
    )


def _describe_deployment_source_worktree_landing_site_occupant(
    *,
    local_main_worktree: Path,
    deployment_source_worktree: Path,
) -> str | None:
    """描述落点上「一旦删掉就再也拿不回来」的东西；没有可丢失内容时返回 None。

    返回 None 只有两种情形：路径根本不存在，或它是一个空目录。其余一切都算「有人占着」——
    basename 等于 `DEPLOYMENT_SOURCE_WORKTREE_DIRECTORY_NAME` 只证明名字对得上，不证明
    这棵树是 RVF 建的：那个名字任何人都写得出来，而删除是不可逆的。
    """
    if deployment_source_worktree.is_symlink():
        return "该路径是一个符号链接，真实内容在别处"
    if not deployment_source_worktree.exists():
        return None
    if not deployment_source_worktree.is_dir():
        return "该路径是一个文件，不是 worktree"
    if not any(deployment_source_worktree.iterdir()):
        return None
    if not (deployment_source_worktree / ".git").exists():
        return "该路径是一个非空的普通目录，不受任何 git 仓库管理"
    git_common_directory = _git_common_directory(deployment_source_worktree)
    if git_common_directory != _git_common_directory(local_main_worktree):
        return (
            "该路径是另一个 git 仓库的 worktree"
            f"（git common dir={git_common_directory}）"
        )
    attached_branch = _git_optional(
        deployment_source_worktree, "symbolic-ref", "--quiet", "HEAD"
    )
    if attached_branch:
        return (
            f"该路径是本仓库的 worktree，但挂在分支 {attached_branch} 上，"
            "里面可能是别人未提交的工作"
        )
    return (
        "该路径是本仓库的 detached worktree，但没有 RVF 归属证据，"
        "无法证明它是 RVF 建的（里面可能是别人未提交的工作）"
    )


def _require_vacant_deployment_source_worktree_landing_site(
    *,
    local_main_worktree: Path,
    deployment_source_worktree: Path,
) -> None:
    """新建 deploy 源 worktree 之前，确认落点上没有任何会被销毁的东西（fail-closed）。

    只有「路径不存在」和「空目录」两种落点能证明删除不丢东西。非空普通目录、符号链接、
    别的仓库的 worktree、本仓但挂着分支或没有 RVF 归属证据的 worktree 一律拒绝：无法证明
    归 RVF 所有就不动它，把处置权交回给人，而不是替他决定那些未提交改动可以丢。

    稳态下根本走不到这里 —— RVF 自有的树带着 `_record_deployment_source_worktree_ownership`
    写下的归属证据，会在 `_deployment_source_worktree_is_reusable` 处走复用分支就地 reset。
    """
    if deployment_source_worktree.name != DEPLOYMENT_SOURCE_WORKTREE_DIRECTORY_NAME:
        raise DeployError(
            "拒绝在非 RVF 命名的路径上创建 deploy 源 worktree: "
            f"{deployment_source_worktree}",
            exit_code=2,
        )
    occupant = _describe_deployment_source_worktree_landing_site_occupant(
        local_main_worktree=local_main_worktree,
        deployment_source_worktree=deployment_source_worktree,
    )
    if occupant is not None:
        raise DeployError(
            "deploy 源 worktree 落点已被占用，且无法证明它归 RVF 所有: "
            f"{deployment_source_worktree}（{occupant}）。"
            "RVF 不会删除无法证明所有权的路径。请人工确认其中没有需要保留的内容后，"
            "自行移走或删除该路径，再重新运行部署。",
            exit_code=2,
        )
    # 落点已确认为空（不存在或空目录），此时只可能残留「注册还在、树已不可用」的僵尸条目，
    # 不清掉它 `worktree add` 会被拒。prune 只摘除失效注册，不碰任何仍然有效的 worktree。
    _git(local_main_worktree, "worktree", "prune")


def _ensure_deployment_source_worktree(
    *,
    local_main_worktree: Path,
    deployment_source_worktree: Path,
    commit: str,
) -> dict[str, Any]:
    """把 RVF 自有的 deploy 源 worktree 收敛到 local main 的 HEAD commit。"""
    expected_git_common_directory = _git_common_directory(local_main_worktree)
    if expected_git_common_directory is None:
        raise DeployError(
            f"无法解析 local main 的 git common dir: {local_main_worktree}",
            exit_code=2,
        )
    created = False
    if not _deployment_source_worktree_is_reusable(
        deployment_source_worktree,
        expected_git_common_directory=expected_git_common_directory,
    ):
        _require_vacant_deployment_source_worktree_landing_site(
            local_main_worktree=local_main_worktree,
            deployment_source_worktree=deployment_source_worktree,
        )
        deployment_source_worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(
            local_main_worktree,
            "worktree",
            "add",
            "--detach",
            str(deployment_source_worktree),
            commit,
        )
        # 归属证据必须在第一次 reset/clean 之前落盘：它是下一轮判「这棵树可以被清空」的
        # 唯一依据，缺了它下一轮会 fail-closed 报占用，而不是误把别人的树当自己的清掉。
        _record_deployment_source_worktree_ownership(deployment_source_worktree)
        created = True
    _git(deployment_source_worktree, "reset", "--hard", commit)
    # 这棵树是 RVF 独占的，清到底是安全的；顺带清掉上一轮契约留下的
    # `__pycache__` / `.pytest_cache` 之类 gitignored 产物。
    _git(deployment_source_worktree, "clean", "-xdff")
    synced_commit = _git(deployment_source_worktree, "rev-parse", "HEAD")
    if synced_commit != commit:
        raise DeployError(
            "deploy 源 worktree 未收敛到 local main commit: "
            f"expected={commit}, actual={synced_commit}",
            exit_code=2,
        )
    residual_paths = _status_paths(deployment_source_worktree)
    if residual_paths:
        raise DeployError(
            f"deploy 源 worktree 同步后仍不干净: {sorted(residual_paths)}",
            exit_code=2,
        )
    return {
        "deployment_source_worktree": str(deployment_source_worktree),
        "deployment_source_worktree_created": created,
        "deployment_source_worktree_synced_to_commit": synced_commit,
    }


def _required_deployment_entrypoint_paths(
    deployment_source_worktree: Path,
) -> dict[str, Path]:
    paths = {
        "contract": deployment_source_worktree / "scripts/check_plugin_contracts.py",
        "installer": deployment_source_worktree / "scripts/install_to_codex.py",
        "verifier": (
            deployment_source_worktree
            / "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py"
        ),
    }
    missing_paths = [str(path) for path in paths.values() if not path.is_file()]
    if missing_paths:
        raise DeployError(
            f"deploy 源 worktree 入口缺失: {missing_paths}",
            exit_code=2,
        )
    return paths


def _user_local_data_root(home: Path, environment: dict[str, str]) -> Path:
    configured = environment.get("RVF_USER_LOCAL_DATA_ROOT", "").strip()
    return Path(configured).expanduser() if configured else home / ".rvf/user-local-data"


def _write_result_receipt(path: Path, result: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)
    return path


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _installed_release_summary(installation_receipt_path: Path) -> dict[str, Any] | None:
    receipt = _read_json_object(installation_receipt_path)
    if receipt is None:
        return None
    return {
        "local_main_commit": receipt.get("local_main_commit"),
        "generated_plugin_version": receipt.get("generated_plugin_version"),
        "status": receipt.get("status"),
    }


_SOURCE_COMMIT_LOG_FIELD_SEPARATOR = "\x1f"


def _looks_like_commit_sha(revision: str) -> bool:
    """只放行纯十六进制的 commit 名。

    双重作用：既是「这串东西还像不像一个 commit」的廉价证伪，也堵死把 receipt 里的任意
    字符串直接当 git revision 参数递进去（`-` 开头会被当 flag 解析）的路径。
    """
    return len(revision) >= 7 and all(character in string.hexdigits for character in revision)


def _read_source_commit_log(
    repository: Path, *revision_arguments: str
) -> list[dict[str, str]] | None:
    """读一段 commit 列表；git 查询失败返回 None（交给调用方降级，不抛错）。

    走 `_git_optional_narration_text` 而不是 `_git_optional`：subject 是纯人读叙述，且不保证
    是 UTF-8，严格解码抛出的 `UnicodeDecodeError` 会绕过调用方的降级分支直接逃逸。
    """
    output = _git_optional_narration_text(
        repository,
        "log",
        "--no-decorate",
        f"--format=%H{_SOURCE_COMMIT_LOG_FIELD_SEPARATOR}%s",
        *revision_arguments,
    )
    if output is None:
        return None
    commits: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition(_SOURCE_COMMIT_LOG_FIELD_SEPARATOR)
        commits.append({"sha": sha, "subject": subject})
    return commits


def describe_source_commit_delta_since_last_installation(
    *,
    repository: Path,
    previously_installed_commit: str | None,
    newly_installed_commit: str,
) -> dict[str, Any]:
    """回答「已安装 plugin 现在反映了哪些源 commit」。

    OLD 的唯一事实源是**部署开始时读到的 official installation receipt**
    （`latest-official-cli-installation.json` 的 `local_main_commit`）。
    **禁止用当前 HEAD 冒充 OLD**：HEAD 是本次要装的 NEW，拿它当 OLD 会让每一次部署都塌成
    `unchanged`；`generated_plugin_version` 只是版本串，也反推不出 commit。

    分类先证伪再归类，顺序固定：无基线 → 基线不可解析（`unknown_revision`）→ 未变 →
    前进（`forward`）→ 回退或分叉（`rollback_or_diverged`）。函数内一律走 `_git_optional`，
    任何 git 查询失败都降级成 `unknown_revision` 而不是抛 `DeployError` —— 一次已经成功并
    收敛的部署，不该因为事后统计失败被判成失败。

    `boundary_explanation` 是措辞的唯一事实源：人读输出与 skill 汇报都直接引用它，措辞一律
    锚在 runtime 语义（「已安装 plugin 现在反映…」），不写成「源码集成了这些 commit」。
    """
    # OLD 直接来自磁盘上的 receipt JSON，字段类型不受本进程控制：非字符串一律按「没有基线」
    # 处理，绝不让一次已经收敛的部署栽在类型上。
    old_commit = (
        previously_installed_commit.strip() or None
        if isinstance(previously_installed_commit, str)
        else None
    )
    new_commit = (
        newly_installed_commit.strip() if isinstance(newly_installed_commit, str) else ""
    )
    new_commit_is_addressable = _looks_like_commit_sha(new_commit)

    def delta(
        kind: str,
        *,
        forward_commits: list[dict[str, str]],
        reverse_commits: list[dict[str, str]],
        boundary_explanation: str,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "old_commit": old_commit,
            "new_commit": new_commit or None,
            "forward_commits": forward_commits,
            "reverse_commits": reverse_commits,
            "boundary_explanation": boundary_explanation,
        }

    def unknown_revision(reason: str) -> dict[str, Any]:
        return delta(
            "unknown_revision",
            forward_commits=[],
            reverse_commits=[],
            boundary_explanation=(
                f"{reason}，无法计算区间；只能给出两端：上次安装反映 "
                f"{old_commit or '（无记录）'}，现在反映 {new_commit or '（无记录）'}。"
            ),
        )

    if old_commit is None:
        newly_installed = (
            _read_source_commit_log(repository, "-n", "1", new_commit)
            if new_commit_is_addressable
            else None
        )
        return delta(
            "no_baseline",
            forward_commits=newly_installed or [],
            reverse_commits=[],
            boundary_explanation=(
                "没有可用的上一次安装基线（official installation receipt 不存在或未记录 "
                f"local main commit），不做区间比较；已安装 plugin 现在反映到源 commit "
                f"{new_commit[:12] or '（无记录）'}。"
            ),
        )

    if not _looks_like_commit_sha(old_commit) or not new_commit_is_addressable:
        return unknown_revision("上次安装记录的源 commit 不是一个可寻址的 commit 名")
    # 存在性检查与「归一成同一个 commit object id」是同一件事：receipt 里的 OLD 可能是 NEW 的
    # 合法缩写（≥7 位十六进制）或大小写异体，字符串比较会把「同一个 commit」误判成两端不同，
    # 进而落到 `forward` 却带着空区间，说出「前进了 0 个源 commit」这种自相矛盾的话。先各自
    # 解析成完整 object id，再拿解析结果做后续比较与区间查询。解析失败（不在对象库里、缩写
    # 歧义）沿用既有约定降级成 `unknown_revision`，不抛错。
    resolved_commits: list[str] = []
    for revision in (old_commit, new_commit):
        resolved_commit = _git_optional(
            repository, "rev-parse", "--verify", f"{revision}^{{commit}}"
        )
        if not resolved_commit:
            return unknown_revision(f"源 commit {revision[:12]} 不在本仓对象库里")
        resolved_commits.append(resolved_commit)
    resolved_old_commit, resolved_new_commit = resolved_commits

    if resolved_old_commit == resolved_new_commit:
        return delta(
            "unchanged",
            forward_commits=[],
            reverse_commits=[],
            boundary_explanation=(
                f"已安装 plugin 反映的源 commit 没有变化（仍是 {resolved_new_commit[:12]}），"
                "本次部署没有让它反映任何新的源 commit。"
            ),
        )

    forward_commits = _read_source_commit_log(
        repository, f"{resolved_old_commit}..{resolved_new_commit}"
    )
    if forward_commits is None:
        return unknown_revision("读取源 commit 区间失败")

    if _git_optional(
        repository, "merge-base", "--is-ancestor", resolved_old_commit, resolved_new_commit
    ) is not None:
        return delta(
            "forward",
            forward_commits=forward_commits,
            reverse_commits=[],
            boundary_explanation=(
                f"已安装 plugin 现在反映到源 commit {resolved_new_commit[:12]}，"
                f"比上次安装反映的 {resolved_old_commit[:12]} 前进了 "
                f"{len(forward_commits)} 个源 commit。"
            ),
        )

    reverse_commits = _read_source_commit_log(
        repository, f"{resolved_new_commit}..{resolved_old_commit}"
    )
    if reverse_commits is None:
        return unknown_revision("读取反向源 commit 区间失败")
    return delta(
        "rollback_or_diverged",
        forward_commits=forward_commits,
        reverse_commits=reverse_commits,
        boundary_explanation=(
            f"已安装 plugin 现在反映的源 commit {resolved_new_commit[:12]} 并不在上次安装的 "
            f"{resolved_old_commit[:12]} 之后（回退或分叉）：{len(reverse_commits)} 个源 commit "
            f"不再被反映，{len(forward_commits)} 个源 commit 新被反映。"
        ),
    )


def _snapshot_structured_hook_configuration(
    hooks_path: Path,
) -> StructuredHookConfigurationSnapshot:
    hooks_path = hooks_path.expanduser().resolve()
    if not hooks_path.exists():
        return StructuredHookConfigurationSnapshot(
            path=hooks_path,
            existed=False,
            content=None,
        )
    try:
        content = hooks_path.read_bytes()
    except OSError as exc:
        raise DeployError(f"无法备份 Codex hooks 配置: {hooks_path}: {exc}") from exc
    return StructuredHookConfigurationSnapshot(
        path=hooks_path,
        existed=True,
        content=content,
    )


def _restore_structured_hook_configuration(
    snapshot: StructuredHookConfigurationSnapshot,
) -> None:
    try:
        if not snapshot.existed:
            if snapshot.path.exists():
                snapshot.path.unlink()
            return
        if snapshot.content is None:
            raise DeployError("Codex hooks backup 标记为存在但缺少内容")
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = snapshot.path.with_name(
            f".{snapshot.path.name}.{os.getpid()}.rollback.tmp"
        )
        temporary_path.write_bytes(snapshot.content)
        temporary_path.replace(snapshot.path)
    except OSError as exc:
        raise DeployError(f"无法恢复 Codex hooks 配置: {snapshot.path}: {exc}") from exc


@contextmanager
def _deployment_lock(home: Path) -> Iterator[Path]:
    lock_path = home / ".rvf/local-deploy.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeployError("已有 RVF local deploy 正在运行", exit_code=3) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        try:
            yield lock_path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def observe_deploy_host_concurrency_conditions() -> dict[str, Any]:
    """记录部署当刻的机器负载画像。

    契约检查里有一批测试对「机器有多忙」敏感（外部 reviewer 的空闲 watchdog、进程组超时等）。
    没有这份画像时，一次 `plugin_contract_check` 失败必须靠事后跑 `uptime` 倒推当时的负载，
    而那时负载早就变了 —— 2026-07-25 那次失败就是这么考古出来的。
    """
    try:
        load_average_1m, load_average_5m, load_average_15m = os.getloadavg()
    except (OSError, AttributeError):
        load_average_1m = load_average_5m = load_average_15m = None
    return {
        "host_cpu_count": os.cpu_count(),
        "host_load_average_1m": load_average_1m,
        "host_load_average_5m": load_average_5m,
        "host_load_average_15m": load_average_15m,
    }


def parse_contract_check_failure_observations(contract_failure_output: str) -> dict[str, Any]:
    """从契约检查的失败输出里抽出「哪一步失败」和「当时并发度多少」。

    两个标记的产出方都是 `scripts/check_skill_contracts.sh`：
    失败步骤来自 `printf '验证失败: %s\\n'`，并发画像来自 `run_parallel_test_steps` 开头的
    `RVF_CONTRACT_PARALLEL_EXECUTION` 行。receipt 里原本只记到 `plugin_contract_check`
    这一级，粒度不足以分辨是哪个 shard 翻的红。
    """
    failed_step_marker = "验证失败: "
    parallel_execution_marker = "RVF_CONTRACT_PARALLEL_EXECUTION "
    failed_step_labels: list[str] = []
    effective_parallel_jobs: int | None = None
    for line in contract_failure_output.splitlines():
        # 用子串而非行首匹配：`_run_phase` 会把 `<label> 失败: ` 粘在被捕获输出的第一行前面。
        if failed_step_marker in line:
            failed_step_labels.append(
                line.split(failed_step_marker, 1)[1].strip()
            )
        elif parallel_execution_marker in line:
            for field in line.split(parallel_execution_marker, 1)[1].split():
                key, _, value = field.partition("=")
                if key == "jobs" and value.isdigit():
                    effective_parallel_jobs = int(value)
    observations: dict[str, Any] = {}
    if failed_step_labels:
        observations["failed_contract_step_labels"] = failed_step_labels
    if effective_parallel_jobs is not None:
        observations["contract_effective_parallel_jobs"] = effective_parallel_jobs
    return observations


def _run_phase(
    label: str,
    argv: list[str],
    *,
    deployment_source_worktree: Path,
    environment: dict[str, str],
    child_exit_codes_preserved_on_failure: frozenset[int] = frozenset(),
) -> str:
    """跑一个部署阶段；非零退出一律抛 `DeployError`。

    ``child_exit_codes_preserved_on_failure`` 里的退出码会被原样带进
    ``DeployError.exit_code``，让调用方分辨**这一类**失败该走哪条处置路径（当前用途：verifier
    的注册唯一性失败不得回滚）。其余退出码一律折叠成 1，避免子进程的退出码约定悄悄变成 runner
    自己的对外契约。
    """
    completed = subprocess.run(
        argv,
        cwd=deployment_source_worktree,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DeployError(
            f"{label} 失败: {detail}",
            exit_code=(
                completed.returncode
                if completed.returncode in child_exit_codes_preserved_on_failure
                else 1
            ),
        )
    return completed.stdout.strip()


def run_local_deploy(
    *,
    repository_hint: Path,
    home: Path,
    expected_local_main_commit: str | None = None,
    runner_path: Path | None = None,
) -> dict[str, Any]:
    home = home.expanduser().resolve()
    resolved_runner_path = (runner_path or Path(__file__)).expanduser().resolve()
    source = validate_local_main_deployment_source(
        repository_hint=repository_hint,
        runner_path=resolved_runner_path,
        expected_local_main_commit=expected_local_main_commit,
    )
    deployment_source_worktree = deployment_source_worktree_path(home)
    environment = {**os.environ, "HOME": str(home)}
    user_local_data_root = _user_local_data_root(home, environment)
    environment["RVF_USER_LOCAL_DATA_ROOT"] = str(user_local_data_root)
    contract_environment = dict(environment)
    # Contract fixtures run nested synthetic deploys with disposable HOME
    # directories. Exposing the real root lets those fixtures mistake the
    # developer installation for their own and mutate its receipts.
    contract_environment.pop("RVF_USER_LOCAL_DATA_ROOT", None)
    installation_receipt_path = (
        user_local_data_root
        / "deployment-receipts/latest-official-cli-installation.json"
    )
    result_receipt_path = (
        user_local_data_root / "deployment-receipts/latest-local-deploy-result.json"
    )
    previous_installation_receipt_backup_path = (
        user_local_data_root
        / "deployment-receipts/previous-installation-before-latest-attempt.json"
    )
    hooks_path = home / ".codex/hooks.json"
    commands: list[list[str]] = []

    with _deployment_lock(home) as lock_path:
        installed_before = _installed_release_summary(installation_receipt_path)
        previous_installation_receipt = _read_json_object(
            installation_receipt_path
        )
        if previous_installation_receipt is not None:
            _write_result_receipt(
                previous_installation_receipt_backup_path,
                previous_installation_receipt,
            )
        hooks_snapshot = _snapshot_structured_hook_configuration(hooks_path)
        installation_mutation_phase_started = False
        deployment_source_worktree_state: dict[str, Any] = {
            "deployment_source_worktree": str(deployment_source_worktree),
            "deployment_source_worktree_created": None,
            "deployment_source_worktree_synced_to_commit": None,
        }

        def result_for(
            *,
            status: str,
            phase: str,
            error: str | None = None,
            **extra: Any,
        ) -> dict[str, Any]:
            result: dict[str, Any] = {
                "status": status,
                "phase": phase,
                "local_main_worktree": str(source.worktree_path),
                "local_main_commit": source.commit,
                "local_main_dirty_paths": list(source.dirty_paths),
                "runner_path": str(resolved_runner_path),
                "runner_matches_local_main_head": source.runner_matches_local_main_head,
                **deployment_source_worktree_state,
                "installed_before": installed_before,
                "installed_after": _installed_release_summary(
                    installation_receipt_path
                ),
                "stale_before": (
                    installed_before is None
                    or installed_before.get("local_main_commit") != source.commit
                ),
                "lock_path": str(lock_path),
                "receipt_path": str(result_receipt_path),
                "commands": commands,
                **observe_deploy_host_concurrency_conditions(),
            }
            if error is not None:
                result["error"] = error
                result["remediation"] = PARTIAL_REMEDIATION
                result.update(parse_contract_check_failure_observations(error))
            result.update(extra)
            return result

        try:
            deployment_source_worktree_state.update(
                _ensure_deployment_source_worktree(
                    local_main_worktree=source.worktree_path,
                    deployment_source_worktree=deployment_source_worktree,
                    commit=source.commit,
                )
            )
            paths = _required_deployment_entrypoint_paths(deployment_source_worktree)
        except DeployError as exc:
            failed = result_for(
                status="failed",
                phase="deployment_source_worktree_preparation",
                error=str(exc),
                rollback_status="not_required",
            )
            _write_result_receipt(result_receipt_path, failed)
            raise DeployError(
                str(exc),
                exit_code=exc.exit_code,
                result=failed,
            ) from exc

        # 失败 receipt 也要能带上「失败前已经拿到的现场」，所以先绑定，避免异常处置分支撞上未绑定名。
        contract_output: str | None = None
        installer_receipt: dict[str, Any] | None = None
        contract_command = [sys.executable, str(paths["contract"])]
        installer_command = [
            sys.executable,
            str(paths["installer"]),
            "--deployment-source-worktree",
            str(deployment_source_worktree),
            "--source-local-main-commit",
            source.commit,
            "--configure-stop-hook",
            "--configure-user-prompt-submit-hook",
            "--configure-kimi-plugin-registration",
            "--json",
        ]

        commands.append(contract_command)
        _write_result_receipt(
            result_receipt_path,
            result_for(status="running", phase="plugin_contract_check"),
        )
        try:
            contract_output = _run_phase(
                "plugin contract check",
                contract_command,
                deployment_source_worktree=deployment_source_worktree,
                environment=contract_environment,
            )
            commands.append(installer_command)
            _write_result_receipt(
                result_receipt_path,
                result_for(status="running", phase="official_cli_installer"),
            )
            installation_mutation_phase_started = True
            installer_output = _run_phase(
                "official CLI installer",
                installer_command,
                deployment_source_worktree=deployment_source_worktree,
                environment=environment,
            )
            try:
                installer_receipt = json.loads(installer_output)
            except json.JSONDecodeError as exc:
                raise DeployError("official CLI installer 未返回 JSON") from exc
            if not isinstance(installer_receipt, dict):
                raise DeployError("official CLI installer JSON 顶层不是 object")
            installer_receipt_path = installer_receipt.get("receipt_path")
            if isinstance(installer_receipt_path, str) and installer_receipt_path:
                installation_receipt_path = Path(installer_receipt_path).expanduser().resolve()
            verifier_command = [
                sys.executable,
                str(paths["verifier"]),
                "--installation-receipt",
                str(installation_receipt_path),
                "--hooks-path",
                str(hooks_path),
                "--home",
                str(home),
                "--json",
            ]
            commands.append(verifier_command)
            _write_result_receipt(
                result_receipt_path,
                result_for(status="running", phase="official_cli_verifier"),
            )
            verifier_output = _run_phase(
                "official CLI verifier",
                verifier_command,
                deployment_source_worktree=deployment_source_worktree,
                environment=environment,
                # verifier 的注册唯一性失败必须能与其余验证失败分开处置（不回滚）。
                child_exit_codes_preserved_on_failure=frozenset(
                    {OFFICIAL_CLI_VERIFIER_REGISTRATION_UNIQUENESS_EXIT_CODE}
                ),
            )
            try:
                verification = json.loads(verifier_output)
            except json.JSONDecodeError as exc:
                raise DeployError("official CLI verifier 未返回 JSON") from exc
            if not isinstance(verification, dict) or verification.get("status") != "ok":
                raise DeployError("official CLI verifier 未返回 status=ok")
            installed_after = _installed_release_summary(installation_receipt_path)
            if installed_after is None or installed_after.get("local_main_commit") != source.commit:
                raise DeployError(
                    "部署后 official installation receipt 未收敛到 local main commit"
                )
        except DeployError as exc:
            if exc.exit_code == OFFICIAL_CLI_VERIFIER_REGISTRATION_UNIQUENESS_EXIT_CODE:
                # verifier 判定宿主里存在第二份 RVF 注册 / payload 树。**刻意不回滚**：canonical
                # 刚装好，回滚会把它一并移除，只留下残留那份——正是这条门禁要消灭的局面。正确的
                # severity 是「部署做完但拒绝认证」。
                #
                # 这个判断必须写在这里（通用 except 的最前面）而不是下游的 config.toml 门禁里：
                # verifier 先抛，先被这个通用 except 接住，写在下游根本轮不到执行。
                registration_uniqueness_not_certified = result_for(
                    status="failed",
                    phase="official_cli_plugin_registration_uniqueness",
                    error=str(exc),
                    rollback_status="not_required",
                    remediation=REGISTRATION_UNIQUENESS_REMEDIATION,
                    contract_output=contract_output,
                    installer=installer_receipt,
                )
                _write_result_receipt(
                    result_receipt_path, registration_uniqueness_not_certified
                )
                raise DeployError(
                    f"{exc}；{REGISTRATION_UNIQUENESS_REMEDIATION}",
                    # 刻意**不**把 verifier 的退出码原样透传给 runner 自己的 CLI：runner 的 3
                    # 早已是「已有 RVF local deploy 正在运行」。这里与下游 config.toml 门禁
                    # 统一用 2（部署失败/拒绝认证）。
                    exit_code=2,
                    result=registration_uniqueness_not_certified,
                ) from exc
            rollback_result: dict[str, Any] | None = None
            rollback_error: str | None = None
            if installation_mutation_phase_started:
                rollback_command = [sys.executable, str(paths["installer"])]
                if previous_installation_receipt is None:
                    rollback_command.append(
                        "--rollback-remove-current-installation"
                    )
                else:
                    rollback_command.extend(
                        [
                            "--rollback-to-installation-receipt",
                            str(previous_installation_receipt_backup_path),
                        ]
                    )
                rollback_command.append("--json")
                commands.append(rollback_command)
                try:
                    rollback_output = _run_phase(
                        "official CLI verification rollback",
                        rollback_command,
                        deployment_source_worktree=deployment_source_worktree,
                        environment=environment,
                    )
                    parsed_rollback_result = json.loads(rollback_output)
                    if not isinstance(parsed_rollback_result, dict):
                        raise DeployError("rollback JSON 顶层不是 object")
                    rollback_result = parsed_rollback_result
                except (DeployError, json.JSONDecodeError) as rollback_exc:
                    rollback_error = str(rollback_exc)
                try:
                    _restore_structured_hook_configuration(hooks_snapshot)
                except DeployError as hooks_rollback_exc:
                    hooks_error = str(hooks_rollback_exc)
                    rollback_error = (
                        f"{rollback_error}; {hooks_error}"
                        if rollback_error
                        else hooks_error
                    )
            failed = result_for(
                status="failed",
                phase=(
                    "plugin_contract_check"
                    if not commands or commands[-1] == contract_command
                    else "official_cli_install_or_verify"
                ),
                error=str(exc),
                rollback_status=(
                    "rollback_failed"
                    if rollback_error
                    else (
                        rollback_result.get("rollback_status")
                        if rollback_result is not None
                        else "not_required"
                    )
                ),
                rollback=rollback_result,
                rollback_error=rollback_error,
            )
            _write_result_receipt(result_receipt_path, failed)
            failure_message = str(exc)
            if rollback_error:
                failure_message = (
                    f"{failure_message}; verification rollback 失败: {rollback_error}"
                )
            raise DeployError(
                failure_message,
                exit_code=exc.exit_code,
                result=failed,
            ) from exc

        codex_registration_uniqueness = (
            _sole_review_validate_fix_registration_in_codex_config(
                home, installer_receipt=installer_receipt
            )
        )
        codex_hook_trust = _observe_codex_hook_trust(
            home, installer_receipt=installer_receipt
        )
        if codex_registration_uniqueness.get("status") != CODEX_SOLE_REGISTRATION_STATUS:
            # 认证要的是**正向证明**：canonical 确实在册、且只有它一份。任何别的状态都不够格——
            # `multiple_registrations` 是已识别的多注册，`canonical_selector_not_registered`
            # 是 canonical 根本没注册，`unavailable` 是根本没核到（「核不到」不是「核过了」）。
            # 只排除已识别的坏状态，会把后两类当成好状态放行。
            #
            # 刻意不走上面的 rollback 分支：回滚会移除刚装好的 canonical，只留下残留那份，更差。
            registration_not_certified = result_for(
                status="failed",
                phase="codex_registration_uniqueness",
                error=codex_registration_uniqueness["note"],
                rollback_status="not_required",
                remediation=REGISTRATION_UNIQUENESS_REMEDIATION,
                contract_output=contract_output,
                installer=installer_receipt,
                verification=verification,
                codex_hook_trust=codex_hook_trust,
                codex_registration_uniqueness=codex_registration_uniqueness,
            )
            _write_result_receipt(result_receipt_path, registration_not_certified)
            cleanup_commands = codex_registration_uniqueness.get("remediation")
            raise DeployError(
                codex_registration_uniqueness["note"]
                + (f" 清理命令: {cleanup_commands}" if cleanup_commands else ""),
                exit_code=2,
                result=registration_not_certified,
            )
        # `rvf` 快捷入口 shim：best-effort。装不上时横幅 / 回执自动退到入口脚本绝对路径，
        # 功能不受影响，所以不因它回滚一次已经收敛的部署——只在结果里如实报出。
        deployed_plugin_payload_root, _selector, _locate_note = (
            _deployed_plugin_payload_root_from_installer_receipt(installer_receipt)
        )
        try:
            rvf_command_line_shortcut_shim = install_rvf_command_line_shortcut_shim(
                home=home,
                deployed_plugin_payload_root=deployed_plugin_payload_root,
                python_executable=sys.executable,
            )
        except OSError as exc:
            rvf_command_line_shortcut_shim = {
                "status": RVF_COMMAND_LINE_SHORTCUT_SHIM_INSTALL_FAILED,
                "error": f"{type(exc).__name__}: {exc}",
            }
        # `source_commit_delta` 只挂在**成功**结果上，且必须走 `result_for(**extra)`：
        # `result_for` 同时服务 status=running / failed，把它塞进函数本体等于给失败与回滚
        # 结果也发一份「已投入使用」的 forward 列表。位置也必须在上面那个 except 之外——
        # 部署已经收敛了，一次事后统计的异常不该把它拖回回滚分支。
        completed = result_for(
            status="ok",
            phase="completed",
            contract_output=contract_output,
            installer=installer_receipt,
            verification=verification,
            source_commit_delta=describe_source_commit_delta_since_last_installation(
                repository=source.worktree_path,
                previously_installed_commit=(installed_before or {}).get(
                    "local_main_commit"
                ),
                newly_installed_commit=source.commit,
            ),
            codex_hook_trust=codex_hook_trust,
            codex_registration_uniqueness=codex_registration_uniqueness,
            rvf_command_line_shortcut_shim=rvf_command_line_shortcut_shim,
        )
        _write_result_receipt(result_receipt_path, completed)
        return completed


RVF_COMMAND_LINE_SHORTCUT_SHIM_INSTALLED = "installed"
RVF_COMMAND_LINE_SHORTCUT_SHIM_SKIPPED_PAYLOAD_ROOT_UNRESOLVED = "skipped_deployed_payload_root_unresolved"
RVF_COMMAND_LINE_SHORTCUT_SHIM_SKIPPED_ENTRYPOINT_MISSING = "skipped_entrypoint_missing_in_deployed_payload"
RVF_COMMAND_LINE_SHORTCUT_SHIM_INSTALL_FAILED = "install_failed"
RVF_COMMAND_LINE_SHORTCUT_USER_PATH_LINK_CREATED_OR_REFRESHED = "created_or_refreshed"
RVF_COMMAND_LINE_SHORTCUT_USER_PATH_LINK_SKIPPED_FOREIGN_COMMAND = "skipped_foreign_command_already_present"
DEPLOYED_PLUGIN_PAYLOAD_RELATIVE_SCRIPTS_DIRECTORY = Path("skills/review-validate-fix/scripts")


def install_rvf_command_line_shortcut_shim(
    *,
    home: Path,
    deployed_plugin_payload_root: Path | None,
    python_executable: str,
) -> dict[str, Any]:
    """把 PATH 上的 ``rvf`` 指向**本次部署**的入口脚本。

    写两处：``~/.rvf/bin/rvf``（RVF 自有目录里的 shim 本体，每次部署整份覆盖）与
    ``~/.local/bin/rvf``（指向 shim 的符号链接，该目录在 Claude Code / Codex 的 shell PATH 上都
    常见）。后者已被**别人的**同名命令占用时不覆盖，只报出来——渲染侧此时自动退到 shim 绝对路径。
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from core.rvf_command_line_shortcut_shim_location import (  # noqa: PLC0415
        RVF_COMMAND_LINE_SHORTCUT_ENTRYPOINT_SCRIPT_FILENAME,
        is_rvf_owned_command_line_shortcut_shim,
        render_rvf_command_line_shortcut_shim_script,
        rvf_command_line_shortcut_shim_path,
        rvf_command_line_shortcut_user_path_link_path,
    )

    if deployed_plugin_payload_root is None:
        return {"status": RVF_COMMAND_LINE_SHORTCUT_SHIM_SKIPPED_PAYLOAD_ROOT_UNRESOLVED}
    entrypoint = (
        deployed_plugin_payload_root
        / DEPLOYED_PLUGIN_PAYLOAD_RELATIVE_SCRIPTS_DIRECTORY
        / RVF_COMMAND_LINE_SHORTCUT_ENTRYPOINT_SCRIPT_FILENAME
    )
    if not entrypoint.is_file():
        return {
            "status": RVF_COMMAND_LINE_SHORTCUT_SHIM_SKIPPED_ENTRYPOINT_MISSING,
            "entrypoint": str(entrypoint),
        }
    shim_path = rvf_command_line_shortcut_shim_path(home)
    shim_path.parent.mkdir(parents=True, exist_ok=True)
    staging = shim_path.with_name(f".{shim_path.name}.{os.getpid()}.tmp")
    staging.write_text(
        render_rvf_command_line_shortcut_shim_script(
            entrypoint_script_path=entrypoint, python_executable=python_executable
        ),
        encoding="utf-8",
    )
    staging.chmod(0o755)
    staging.replace(shim_path)

    link_path = rvf_command_line_shortcut_user_path_link_path(home)
    if os.path.lexists(link_path) and not (
        link_path.is_symlink() and Path(os.readlink(link_path)) == shim_path
    ) and not is_rvf_owned_command_line_shortcut_shim(link_path):
        link_status = RVF_COMMAND_LINE_SHORTCUT_USER_PATH_LINK_SKIPPED_FOREIGN_COMMAND
    else:
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(link_path):
            link_path.unlink()
        link_path.symlink_to(shim_path)
        link_status = RVF_COMMAND_LINE_SHORTCUT_USER_PATH_LINK_CREATED_OR_REFRESHED
    return {
        "status": RVF_COMMAND_LINE_SHORTCUT_SHIM_INSTALLED,
        "shim_path": str(shim_path),
        "entrypoint": str(entrypoint),
        "user_path_link": str(link_path),
        "user_path_link_status": link_status,
    }


def _deployed_plugin_payload_root_from_installer_receipt(
    installer_receipt: dict[str, Any] | None,
) -> tuple[Path | None, str | None, str | None]:
    """从安装器回执取**真实**的 plugin payload 根与 plugin selector。

    绝不硬拼 `$HOME/plugins/<plugin>`：本安装器把 payload 装进
    `$HOME/.rvf/developer-plugin-marketplaces/<marketplace>/plugins/<plugin>`。
    `$HOME/plugins/` 在本机上是一个早已停更的历史目录——照着它核对，核到的是一份陈旧快照
    （或者干脆什么都核不到），而真正被 Codex 加载的那份一次都没被核对，结果还报「全部一致」。

    返回 ``(payload_root, plugin_selector, 无法定位时的说明)``。定位不到就如实返回 ``None``
    并带上说明，**不**退回猜路径——猜错的路径正是这条缺陷本身。
    """
    if not isinstance(installer_receipt, dict):
        return (
            None,
            None,
            "安装器回执不可用 ⇒ 无法定位已部署 plugin payload 根，bundled hook 源未进核对。",
        )
    raw_selector = installer_receipt.get("plugin_selector")
    selector = raw_selector if isinstance(raw_selector, str) and raw_selector else None
    raw_marketplace_root = installer_receipt.get("active_marketplace_root")
    if not isinstance(raw_marketplace_root, str) or not raw_marketplace_root:
        return (
            None,
            selector,
            "安装器回执缺 active_marketplace_root ⇒ bundled hook 源未进核对。",
        )
    plugin_name = selector.split("@", 1)[0] if selector else ""
    if not plugin_name:
        return (
            None,
            selector,
            "安装器回执缺 plugin_selector ⇒ 无法确定 payload 目录名，bundled hook 源未进核对。",
        )
    payload_root = (
        Path(raw_marketplace_root).expanduser() / "plugins" / plugin_name
    )
    if not payload_root.is_dir():
        return (
            payload_root,
            selector,
            f"回执给出的 plugin payload 根不存在: {payload_root}",
        )
    return payload_root, selector, None


def _sole_review_validate_fix_registration_in_codex_config(
    home: Path, *, installer_receipt: dict[str, Any] | None = None
) -> dict[str, Any]:
    """核对 `~/.codex/config.toml` 里 **canonical selector 在册、且只有它一份**。**阻断 deploy。**

    与同文件的 `_observe_codex_hook_trust` 刻意分成两件事：那一项是观测（重新信任是人的决定，
    且本机未必用 Codex），这一项是硬约束。本机的双份 RVF 注册正是「只观测、只写文档、从不阻断」
    烂了三周的产物——两棵不同 commit 的插件树并行执行、各写各的 `review-highwater` 高水位。

    刻意**不触发部署回滚**：回滚会移除刚装好的 canonical、把机器留在「只剩旧那份」的更差状态。
    正确的 severity 是「部署做完但拒绝认证」，让人去清理残留。

    canonical selector 一律取自安装器回执的 ``plugin_selector``，不在这里硬拼 marketplace 名。

    返回的 ``status`` 只有 `CODEX_SOLE_REGISTRATION_STATUS` 一种够格放行，其余三类都必须阻断：

    - ``multiple_registrations``：已识别的多注册；
    - `CODEX_CANONICAL_SELECTOR_NOT_REGISTERED_STATUS`：config 可读但 canonical 不在
      ``[plugins.*]`` 里——「没有第二份」不等于「canonical 在册」，把它读成唯一等于谎报；
    - ``unavailable``：根本没核到（回执缺 selector / config 读不到 / 扫描抛异常）——「核不到」
      不是「核过了、没问题」，这正是 hook 信任观测那条同款失败模式。
    """
    canonical_selector = (
        installer_receipt.get("plugin_selector")
        if isinstance(installer_receipt, dict)
        else None
    )
    if not isinstance(canonical_selector, str) or not canonical_selector.strip():
        return {
            "status": "unavailable",
            "selectors": [],
            "note": (
                "安装器回执缺 plugin_selector ⇒ 无从判断哪个 selector 是 canonical，"
                "本次未核对 Codex 侧是否存在多份 RVF 注册。"
            ),
        }
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import codex_hook_trust_fingerprint as trust  # noqa: PLC0415

        codex_home = Path(home) / ".codex"
        document = trust.read_codex_config_document(codex_home)
        if document is None:
            return {
                "status": "unavailable",
                "selectors": [],
                "note": (
                    f"读不到 {codex_home / 'config.toml'}（文件缺失 / 无 tomllib / 解析失败）；"
                    "本次无法判断 Codex 侧是否存在多份 RVF 注册。"
                ),
            }
        registration_sources = (
            trust.rvf_plugin_selector_registration_sources_in_codex_config(document)
        )
        plugin_registration_table = trust.CODEX_CONFIG_PLUGIN_REGISTRATION_TABLE
        hook_trust_table = trust.CODEX_CONFIG_HOOK_TRUST_TABLE
    except Exception as exc:  # noqa: BLE001 - 核对本身失败不得伪装成「唯一」
        return {
            "status": "unavailable",
            "selectors": [],
            "note": f"Codex config 里的 RVF 注册核对失败：{type(exc).__name__}: {exc}",
        }
    selectors = list(registration_sources)
    extra_selectors = [
        selector for selector in selectors if selector != canonical_selector
    ]
    if extra_selectors:
        # 两类残留的清理手段不同，实测：`codex plugin remove` 删 cache 与 [plugins.*]，
        # 但**不**清 [hooks.state.*]。对只剩信任条目的 selector 建议跑 remove 等于给了
        # 一条证明无效的命令——那正是这条检查要终结的那种「看起来有指引」。
        remediation_steps: list[str] = []
        for selector in extra_selectors:
            tables = registration_sources.get(selector, ())
            if plugin_registration_table in tables:
                remediation_steps.append(f"codex plugin remove {selector} --json")
            if hook_trust_table in tables:
                remediation_steps.append(
                    f'手工删除 ~/.codex/config.toml 里所有 [hooks.state."{selector}:…"] 块'
                    "（codex plugin remove 不清这张表）"
                )
        return {
            "status": "multiple_registrations",
            "selectors": selectors,
            "extra_selectors": extra_selectors,
            "registration_sources": {
                selector: list(tables)
                for selector, tables in registration_sources.items()
            },
            "canonical_selector": canonical_selector,
            "note": (
                f"{Path(home) / '.codex/config.toml'} 里存在多份 RVF 注册: {selectors}；"
                f"canonical 只应是 {canonical_selector}。"
            ),
            "remediation": "；".join(remediation_steps),
        }
    if plugin_registration_table not in registration_sources.get(
        canonical_selector, ()
    ):
        # 「一个 RVF selector 都没有」和「canonical 只剩 [hooks.state] 残迹」在旧实现里都会落到
        # sole_registration，把「canonical 根本没注册」谎报成「唯一」。认证要的是正向证明。
        return {
            "status": CODEX_CANONICAL_SELECTOR_NOT_REGISTERED_STATUS,
            "selectors": selectors,
            "registration_sources": {
                selector: list(tables)
                for selector, tables in registration_sources.items()
            },
            "canonical_selector": canonical_selector,
            "note": (
                f"{Path(home) / '.codex/config.toml'} 的 [{plugin_registration_table}] 表里"
                f"没有 canonical RVF 注册 {canonical_selector}"
                f"（本次扫到的 RVF selector: {selectors or '无'}）；"
                "「没有第二份」不等于「canonical 在册」，本次部署不予认证。"
            ),
            "remediation": (
                f"确认 `codex plugin list` 里有 {canonical_selector}；"
                "若没有，说明官方 CLI 的安装没有落到 config.toml，"
                "查完安装器输出后从 local main 重新运行 runner。"
            ),
        }
    return {
        "status": CODEX_SOLE_REGISTRATION_STATUS,
        "selectors": selectors,
        "registration_sources": {
            selector: list(tables) for selector, tables in registration_sources.items()
        },
        "canonical_selector": canonical_selector,
    }


def _observe_codex_hook_trust(
    home: Path, *, installer_receipt: dict[str, Any] | None = None
) -> dict[str, Any]:
    """部署收尾核对 Codex 的 hook 信任指纹（**只观测、不改 live config**）。

    为什么必须有这一步：Codex 对每条 command hook 记信任指纹，改了命令而不重新信任 ⇒ 该 hook 被
    判 Modified 并**跳过**，而 `codex exec` 下这件事完全静默（实测 0.147.0：stdout+stderr 里
    `hook`/`trust`/`/hooks` 命中全为 0）。于是 deploy 会报告一切成功，而 RVF 在 Codex 上从此
    一次都不执行。

    刻意**不**自动改 `~/.codex/config.toml`：重新信任是一个信任决定，必须由人做。
    也刻意**不**让它使 deploy 失败：它是观测项，而这台机器上 Codex 未必是被使用的 host。

    ⚠️ 已知盲区：deploy verifier 的 synthetic smoke 是**直接执行脚本**，绕过 Codex 自己的 hook
    发现与信任层——所以只补普通 smoke 仍然会假绿，真正的端到端校验必须走 Codex 的发现路径。
    本观测是那条缝的**指纹侧**补丁，不是它的替代。

    bundled hook 源的位置一律从 ``installer_receipt`` 里取（``active_marketplace_root`` +
    ``plugin_selector``），**不**按 `$HOME/plugins/<plugin>` 硬拼——那个路径本安装器根本不写。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import codex_hook_trust_fingerprint as trust  # noqa: PLC0415

        codex_home = Path(home) / ".codex"
        table = trust.read_codex_hook_trust_table(codex_home)
        (
            payload_root,
            plugin_selector,
            payload_root_resolution_note,
        ) = _deployed_plugin_payload_root_from_installer_receipt(installer_receipt)
        sources = trust.rvf_codex_hook_sources(
            codex_home=codex_home,
            deployed_plugin_root=payload_root,
            trust_table=table,
            deployed_plugin_selector=plugin_selector,
        )
        observation = trust.verify_codex_hook_trust(
            codex_home=codex_home, hook_sources=sources
        )
        observation["deployed_plugin_payload_root"] = (
            str(payload_root) if payload_root is not None else None
        )
        observation["deployed_plugin_selector"] = plugin_selector
        if payload_root_resolution_note:
            # 定位不到 payload 根 = bundled hook 源一条都没进核对；这必须压到非 trusted，
            # 否则又回到「核到 0 条却报全部一致」。已经是非 trusted 的更严重结论不覆盖。
            if observation.get("status") == trust.TRUST_STATUS_TRUSTED:
                observation["status"] = trust.TRUST_STATUS_CHECK_COVERAGE_INCOMPLETE
            observation["coverage_incomplete"] = True
            observation["note"] = " ".join(
                part
                for part in (observation.get("note"), payload_root_resolution_note)
                if part
            )
            observation.setdefault(
                "remediation", trust.INCOMPLETE_COVERAGE_REMEDIATION
            )
        return observation
    except Exception as exc:  # noqa: BLE001 - 观测项永不阻断 deploy
        return {
            "status": "unavailable",
            "checked": 0,
            "untrusted": [],
            "missing_sources": [],
            "sources_without_any_trust_entry": [],
            "coverage_incomplete": True,
            "note": f"Codex hook 信任核对本身失败：{type(exc).__name__}: {exc}",
        }


def _print_text(result: dict[str, Any]) -> None:
    print("RVF local-main official CLI deploy OK")
    print("local main worktree:", result["local_main_worktree"])
    print("local main commit:", str(result["local_main_commit"])[:12])
    print("local main 未提交文件数:", len(result.get("local_main_dirty_paths") or []))
    print("deploy 源 worktree:", result["deployment_source_worktree"])
    print("deploy 源 worktree 本次新建:", result["deployment_source_worktree_created"])
    print("installed before:", result["installed_before"])
    print("installed after:", result["installed_after"])
    print("stale before:", result["stale_before"])
    _print_source_commit_delta(result.get("source_commit_delta") or {})
    if not result.get("runner_matches_local_main_head", True):
        print(
            "注意: runner 自身含未提交改动，与本次被部署的 HEAD 版本不是同一份"
            "（runner_matches_local_main_head=false）"
        )
    _print_codex_registration_uniqueness(
        result.get("codex_registration_uniqueness") or {}
    )
    _print_codex_hook_trust_observation(result.get("codex_hook_trust") or {})
    shim = result.get("rvf_command_line_shortcut_shim") or {}
    print(
        "rvf 快捷入口 shim:",
        shim.get("status"),
        shim.get("shim_path") or shim.get("entrypoint") or shim.get("error") or "",
        f"(PATH 链接 {shim.get('user_path_link')}: {shim.get('user_path_link_status')})"
        if shim.get("user_path_link")
        else "",
    )


# C0（含 ESC / BEL / 换行 / 制表）、DEL 与 C1：终端会把它们当命令而不是字符看待。
# U+FFFD（`errors="replace"` 容错解码留下的 `�`）刻意不在此列：它是一个普通可打印
# 字符，讲述坏字节这件事本身要如实呈现，不该被当成控制序列吃掉。
_TERMINAL_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _neutralize_terminal_control_sequences_for_human_readable_output(
    value: object,
) -> str:
    """把外部可控字符串里的终端控制字符转成可见转义，**只用于人读打印路径**。

    这些串的内容都不受本进程控制：commit `subject` 来自提交信息（任何能往本仓写提交的人
    都能构造），`old_commit` / `new_commit` 来自磁盘上的 official installation receipt。
    原样 `print` 会让 ESC/OSC 等序列直达终端，可以伪造输出行、清屏盖掉前面的真实结论，或
    触发终端功能——而人读输出恰恰是开发者用来判断「到底装了什么」的那一路，伪造的代价正好
    落在它上面。

    处理只发生在打印侧：`--json` 回执与 receipt 落盘必须继续携带原始值，转义不得回灌进
    结构化结果。换行 / 回车 / 制表符一并转义——它们同样能凭空造出一整行看着像真结论的输出。
    """
    return _TERMINAL_CONTROL_CHARACTER_PATTERN.sub(
        lambda match: f"\\x{ord(match.group()):02x}",
        "" if value is None else str(value),
    )


def _print_source_commit_delta(delta: dict[str, Any]) -> None:
    """把 commit 级 delta 印成人能读的结论。

    措辞不在这里另起炉灶：一律先印 runner 自己写的 `boundary_explanation`，再按 kind 决定
    要不要展开列表。只报版本串、不给 commit 列表，等于没有汇报。

    这一路上的每个外部可控串（commit `subject`、`old_commit` / `new_commit`，以及内插了
    它们的 `boundary_explanation`）在打印前都过
    `_neutralize_terminal_control_sequences_for_human_readable_output`：转义只发生在人读
    打印侧，`--json` 回执与 receipt 里的原始值保持原样。
    """
    kind = delta.get("kind")
    if not kind:
        return
    neutralize = _neutralize_terminal_control_sequences_for_human_readable_output
    print("已安装 plugin 现在反映的源 commit:", kind)
    print("  ", neutralize(delta.get("boundary_explanation", "")))

    def print_commits(caption: str, commits: list[dict[str, str]]) -> None:
        if not commits:
            return
        print(f"   {caption}（{len(commits)} 个）:")
        for commit in commits:
            sha = neutralize(commit.get("sha", ""))[:12]
            print(f"     - {sha} {neutralize(commit.get('subject', ''))}")

    forward_commits = delta.get("forward_commits") or []
    reverse_commits = delta.get("reverse_commits") or []
    if kind == "forward":
        print_commits("本次新投入使用的源 commit", forward_commits)
    elif kind == "no_baseline":
        print_commits("本次安装所在的源 commit", forward_commits)
    elif kind == "rollback_or_diverged":
        print("   ⚠️ 相对上次安装是回退或分叉，两个方向都必须一起看：")
        print_commits("不再被已安装 plugin 反映的源 commit", reverse_commits)
        print_commits("新被已安装 plugin 反映的源 commit", forward_commits)
    elif kind == "unknown_revision":
        print("   ⚠️ 上次: ", neutralize(delta.get("old_commit")))
        print("   ⚠️ 现在: ", neutralize(delta.get("new_commit")))

def _print_codex_registration_uniqueness(uniqueness: dict[str, Any]) -> None:
    """印 RVF 注册唯一性结论；只有 `sole_registration` 可以读成正常。

    `unavailable`（没核到）与 `canonical_selector_not_registered`（canonical 不在册）都必须
    长得像告警——它们在后果上与「存在多份」同级：都不构成「canonical 唯一」的证明。
    """
    status = uniqueness.get("status")
    if not status:
        return
    selectors = uniqueness.get("selectors") or []
    if status == CODEX_SOLE_REGISTRATION_STATUS:
        print(f"Codex RVF 注册: 唯一（{selectors[0] if selectors else '无条目'}）")
    elif status == "multiple_registrations":
        print(f"⚠️ Codex RVF 注册: 存在多份 {selectors}")
        print("   清理:", uniqueness.get("remediation"))
    elif status == CODEX_CANONICAL_SELECTOR_NOT_REGISTERED_STATUS:
        print(f"⚠️ Codex RVF 注册: canonical 未在册（{uniqueness.get('note')}）")
        print("   处置:", uniqueness.get("remediation"))
    else:
        print(f"⚠️ Codex RVF 注册唯一性: 未能核对（{uniqueness.get('note')}）")


def _print_codex_hook_trust_observation(trust: dict[str, Any]) -> None:
    """把 hook 信任观测印成人能读的结论。

    唯一的硬约束：**只有 `trusted` 一种状态可以读成「一切正常」**。核不到、核不全、源从没被
    信任过——每一种都要长得像告警，因为它们在后果上与「未受信」等价（Codex 静默不执行）。
    """
    status = trust.get("status")
    if not status:
        return
    checked = trust.get("checked")
    untrusted = trust.get("untrusted") or []
    missing_sources = trust.get("missing_sources") or []
    never_trusted_sources = trust.get("sources_without_any_trust_entry") or []
    if status == "trusted":
        print(f"Codex hook 信任指纹: 全部一致（核对 {checked} 条，所有 hook 源均已登记）")
    elif status in {"untrusted_entries", "trust_table_absent"}:
        print(
            f"⚠️ Codex hook 信任指纹: {len(untrusted)}/{checked} 条**未受信**"
            "——它们会被 Codex 静默跳过（exec 下无任何警告），RVF 在 Codex 侧等于停摆。"
        )
        for entry in untrusted:
            print(f"   - {entry['key']}  ({entry['reason']})")
            print(f"     current_fingerprint: {entry['current_fingerprint']}")
    elif status == "check_coverage_incomplete":
        print(
            f"⚠️ Codex hook 信任指纹: 核对**覆盖不完整**（只核到 {checked} 条）"
            "——本次结果**不能**当作「一切正常」读：没被核到的 hook 与「未受信」后果相同。"
        )
    else:
        print(f"⚠️ Codex hook 信任指纹: 未能核对（{status}）——这**不是**「一切正常」。")
    if status != "trusted":
        payload_root = trust.get("deployed_plugin_payload_root")
        if payload_root:
            print(f"   已部署 plugin payload 根: {payload_root}")
        for entry in never_trusted_sources:
            print(
                f"   - 从未被信任过的 hook 源（信任表里一个键都没有）: "
                f"{entry['source']} -> {entry['path']}"
            )
        for item in missing_sources:
            print(f"   - 缺失 / 不可读的 hook 源: {item}")
        if trust.get("note"):
            print(f"   注: {trust['note']}")
        if trust.get("remediation"):
            print(f"   处置: {trust['remediation']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-local-main-commit")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_local_deploy(
            repository_hint=ROOT,
            home=Path.home(),
            expected_local_main_commit=args.expected_local_main_commit,
            runner_path=Path(__file__).resolve(),
        )
    except DeployError as exc:
        print(f"RVF local-main official CLI deploy FAILED: {exc}", file=sys.stderr)
        if args.json:
            failure = exc.result or {
                "status": "failed",
                "phase": "preflight",
                "error": str(exc),
                "runner_path": str(Path(__file__).resolve()),
            }
            print(json.dumps(failure, ensure_ascii=False, indent=2))
        return exc.exit_code
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_text(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
