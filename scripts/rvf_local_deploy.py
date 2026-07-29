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
) -> str:
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
        raise DeployError(f"{label} 失败: {detail}")
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

        completed = result_for(
            status="ok",
            phase="completed",
            contract_output=contract_output,
            installer=installer_receipt,
            verification=verification,
        )
        _write_result_receipt(result_receipt_path, completed)
        return completed


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
    if not result.get("runner_matches_local_main_head", True):
        print(
            "注意: runner 自身含未提交改动，与本次被部署的 HEAD 版本不是同一份"
            "（runner_matches_local_main_head=false）"
        )


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
