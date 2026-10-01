#!/usr/bin/env python3
"""契约套件结束后，真实 RVF 状态根下不得出现本次套件的测试产物（结构性守卫）。

## 为什么需要它

测试隔离全靠每个用例自己记得把落盘根指向 tmp（``RVF_HOME`` / 显式 ``root=`` / ``lock_root=``
/ ``CODEX_RVF_LOG_ROOT`` …）。漏掉一处不会让任何断言失败——产物只是安静地写进开发者的真实
``~/.rvf``。2026-09-30 的实测：``~/.rvf/kanban-followup-in-progress/`` 872 个标记里 854 个来自测试
临时路径（当天仍在写入）；``run-logs/runs/`` 里 1.4 万个 run 目录的 ``repo`` 指向测试临时仓库。
这些不是无害垃圾：in-progress 锁目录正是 Stop 读侧判断「有没有 RVF 在途」的地方。

## 判据：只认「本次套件私有临时目录」的名字

真实状态根同时被活会话写入（本机长期有多个 agent 在跑 RVF），所以「套件期间有新增条目」
本身不能当判据——那会在任何一台有活会话的机器上恒红。本守卫改用一个**只有本次套件知道**
的标记：

1. 契约 runner 在最开始建一个唯一目录 ``rvf-contract-suite-private-tmp.XXXXXX`` 并把
   ``TMPDIR`` 指向它。Python ``tempfile``、pytest ``tmp_path``、BSD/GNU ``mktemp`` 都遵循
   ``TMPDIR``，于是本次套件里每个测试的临时仓库 / 临时 run / 临时 HOME 的路径里都带着这个
   唯一目录名。
2. 套件结束后，扫真实状态根里「套件开始之后被创建或修改」的文件，**内容或路径里出现这个唯一
   目录名**的即判为测试泄漏。

活会话写的东西不可能引用本次套件私有目录，并发跑的另一轮套件用的是它自己的唯一名——两类都
天然不会误报。只匹配目录的 basename：它同时覆盖 ``/var/…`` 与 ``/private/var/…`` 两种写法。

## 为什么不全量遍历

真实 ``~/.rvf`` 实测约 100 万个文件（32G），全量 ``lstat`` 一遍要 61 秒。本守卫按目录剪枝：
只有 ``max(mtime, ctime)`` 不早于套件开始的目录才下钻（新增 / 删除 / 改名子项都会刷新目录
mtime），但前两层目录（状态根本身与它的直接子目录）无条件列举，用来接住「对已有文件原地追加」
这种不刷新目录 mtime 的写法。代价是：更深层、原地追加进**旧文件**的泄漏会漏检——那需要测试
先知道某个真实旧文件的路径，现存用例里没有这种形态。

## 已知盲区（必须知情）

内容里只有字面量假路径（如 ``"/tmp/task-worktree"``）、不引用任何真实临时目录的泄漏，本守卫
看不见。它守的是「用了临时仓库却忘了隔离落盘根」这一主导形态。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

# 状态根口径直接复用生产的唯一裁决点，不在这里再抄一份优先级链：一旦 core 改了覆盖顺序，
# 抄本不会跟着变，守卫就会静默扫错根（假绿）。
from core.rvf_user_local_data_paths import (  # noqa: E402
    resolved_rvf_runtime_log_root,
    resolved_rvf_session_hook_state_dir,
    resolved_rvf_user_local_data_paths,
)

RVF_HOME_ENVIRONMENT_VARIABLE = "RVF_HOME"
DEFAULT_RVF_HOME_DIRECTORY_NAME = ".rvf"

CONTRACT_SUITE_PRIVATE_TEMPORARY_DIRECTORY_NAME_PREFIX = "rvf-contract-suite-private-tmp."
"""runner 用 ``mktemp -d "$TMPDIR/<本前缀>XXXXXX"`` 建目录；verify 拒绝不带本前缀的目录名，
防止有人把普通的 ``/tmp`` 传进来——那样判据会退化成「任何提到 tmp 的新文件都算泄漏」。"""

DIRECTORY_DEPTH_ALWAYS_LISTED_REGARDLESS_OF_MTIME = 2
"""状态根记为深度 0；深度 ≤ 本值的目录无条件列举（见模块 docstring「为什么不全量遍历」）。"""

MODIFICATION_TIME_CLOCK_SKEW_TOLERANCE_SECONDS = 2.0
"""文件系统时间戳与 ``time.time()`` 之间的粒度 / 偏差余量；宁可多检不可漏检。"""

MAXIMUM_BYTES_READ_PER_CANDIDATE_FILE = 8 * 1024 * 1024
"""单个候选文件最多读这么多字节做匹配（tracker.sqlite3 这类二进制也按字节匹配）。"""

MAXIMUM_LEAKED_FILES_LISTED_PER_STATE_DIRECTORY = 5


@dataclass(frozen=True)
class LeakedContractTestArtifactInRealRvfStateRoot:
    path: Path
    top_level_state_directory: str


def resolve_real_rvf_state_roots_that_contract_tests_must_not_write(
    *,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> list[Path]:
    """与生产代码同一口径解析「真实」状态根——即同一份环境下生产代码实际会写的每一个根：

    - ``$RVF_HOME``（缺省 ``~/.rvf``）；
    - ``$RVF_USER_LOCAL_DATA_ROOT``（缺省 ``~/.rvf/user-local-data``）；
    - run-log 根：``CODEX_RVF_LOG_ROOT`` → ``CODEX_RVF_STATE_DIR`` → ``<user-local-data>/run-logs``
      （RunLedger / ``latest.json`` 写在这里）；
    - session-hook 状态目录：``CODEX_RVF_SESSION_HOOK_STATE_DIR`` → ``CODEX_RVF_STATE_DIR/session-hook``
      → ``<user-local-data>/runtime-state/session-hook``。

    后两者的优先级链由 ``core.rvf_user_local_data_paths`` 裁决，这里只调用不复刻。覆盖根可能在
    ``RVF_HOME`` 之内也可能在之外：嵌在另一个根之内的根只留外层那个（与出现顺序无关），免得
    同一文件被扫两遍、重复计数。"""
    effective_environment = os.environ if environment is None else environment
    effective_home_directory = Path.home() if home_directory is None else home_directory
    configured_rvf_home = effective_environment.get(RVF_HOME_ENVIRONMENT_VARIABLE, "").strip()
    rvf_home = (
        Path(configured_rvf_home).expanduser()
        if configured_rvf_home
        else effective_home_directory / DEFAULT_RVF_HOME_DIRECTORY_NAME
    )
    production_resolver_arguments = {
        "environment": effective_environment,
        "home_directory": effective_home_directory,
    }
    candidate_state_roots = (
        rvf_home,
        resolved_rvf_user_local_data_paths(**production_resolver_arguments).user_local_data_root,
        resolved_rvf_runtime_log_root(**production_resolver_arguments),
        resolved_rvf_session_hook_state_dir(**production_resolver_arguments),
    )
    distinct_resolved_state_roots: list[Path] = []
    for candidate in candidate_state_roots:
        resolved = candidate.resolve() if candidate.exists() else candidate
        if resolved not in distinct_resolved_state_roots:
            distinct_resolved_state_roots.append(resolved)
    return [
        state_root
        for state_root in distinct_resolved_state_roots
        if not any(
            other_state_root in state_root.parents
            for other_state_root in distinct_resolved_state_roots
        )
    ]


def _latest_change_time(stat_result: os.stat_result) -> float:
    # ctime 接住 ``shutil.copy2`` 这类保留源 mtime 的新建文件。
    return max(stat_result.st_mtime, stat_result.st_ctime)


def iterate_files_created_or_modified_since(
    state_root: Path,
    *,
    suite_started_at_epoch_seconds: float,
) -> Iterable[Path]:
    threshold = suite_started_at_epoch_seconds - MODIFICATION_TIME_CLOCK_SKEW_TOLERANCE_SECONDS
    pending_directories: list[tuple[Path, int]] = [(state_root, 0)]
    while pending_directories:
        directory, depth = pending_directories.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                stat_result = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            changed_since_suite_start = _latest_change_time(stat_result) >= threshold
            if entry.is_dir(follow_symlinks=False):
                if (
                    depth + 1 <= DIRECTORY_DEPTH_ALWAYS_LISTED_REGARDLESS_OF_MTIME
                    or changed_since_suite_start
                ):
                    pending_directories.append((Path(entry.path), depth + 1))
                continue
            if entry.is_file(follow_symlinks=False) and changed_since_suite_start:
                yield Path(entry.path)


def _file_references_marker(path: Path, marker_bytes: bytes) -> bool:
    if marker_bytes in str(path).encode("utf-8", errors="surrogateescape"):
        return True
    try:
        with path.open("rb") as handle:
            return marker_bytes in handle.read(MAXIMUM_BYTES_READ_PER_CANDIDATE_FILE)
    except OSError:
        return False


def find_leaked_contract_test_artifacts_in_real_rvf_state_roots(
    *,
    state_roots: Iterable[Path],
    suite_private_temporary_directory_name: str,
    suite_started_at_epoch_seconds: float,
) -> list[LeakedContractTestArtifactInRealRvfStateRoot]:
    marker_bytes = suite_private_temporary_directory_name.encode("utf-8")
    leaked: list[LeakedContractTestArtifactInRealRvfStateRoot] = []
    for state_root in state_roots:
        if not state_root.is_dir():
            continue
        for path in iterate_files_created_or_modified_since(
            state_root, suite_started_at_epoch_seconds=suite_started_at_epoch_seconds
        ):
            if not _file_references_marker(path, marker_bytes):
                continue
            relative_parts = path.relative_to(state_root).parts
            leaked.append(
                LeakedContractTestArtifactInRealRvfStateRoot(
                    path=path,
                    top_level_state_directory=str(state_root / relative_parts[0]),
                )
            )
    return sorted(leaked, key=lambda item: str(item.path))


def render_leak_report(leaked: list[LeakedContractTestArtifactInRealRvfStateRoot]) -> str:
    by_directory: dict[str, list[Path]] = {}
    for item in leaked:
        by_directory.setdefault(item.top_level_state_directory, []).append(item.path)
    lines = [
        f"契约测试把 {len(leaked)} 个产物写进了真实 RVF 状态根（内容或路径引用了本次套件的私有 TMPDIR）。",
        "修法：让对应测试把落盘根指向 tmp（RVF_HOME / 显式 root= / lock_root= / CODEX_RVF_LOG_ROOT …），",
        "不要删本守卫。按状态目录分组（每组最多列 "
        f"{MAXIMUM_LEAKED_FILES_LISTED_PER_STATE_DIRECTORY} 个，文件内容里的临时路径能指认是哪个测试）：",
    ]
    for directory in sorted(by_directory):
        paths = by_directory[directory]
        lines.append(f"- {directory}: {len(paths)} 个")
        for path in paths[:MAXIMUM_LEAKED_FILES_LISTED_PER_STATE_DIRECTORY]:
            lines.append(f"    {path}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite-private-temporary-directory", required=True)
    parser.add_argument("--suite-started-at-epoch-seconds", required=True, type=float)
    args = parser.parse_args(argv)

    suite_private_temporary_directory_name = Path(
        args.suite_private_temporary_directory
    ).name
    if not suite_private_temporary_directory_name.startswith(
        CONTRACT_SUITE_PRIVATE_TEMPORARY_DIRECTORY_NAME_PREFIX
    ):
        print(
            "拒绝执行：--suite-private-temporary-directory 必须是 runner 建的 "
            f"{CONTRACT_SUITE_PRIVATE_TEMPORARY_DIRECTORY_NAME_PREFIX}XXXXXX 目录，"
            f"实际是 {args.suite_private_temporary_directory!r}。",
            file=sys.stderr,
        )
        return 2

    started = time.monotonic()
    leaked = find_leaked_contract_test_artifacts_in_real_rvf_state_roots(
        state_roots=resolve_real_rvf_state_roots_that_contract_tests_must_not_write(),
        suite_private_temporary_directory_name=suite_private_temporary_directory_name,
        suite_started_at_epoch_seconds=args.suite_started_at_epoch_seconds,
    )
    elapsed_seconds = time.monotonic() - started
    if leaked:
        print(render_leak_report(leaked), file=sys.stderr)
        return 1
    print(
        "真实 RVF 状态根未发现本次契约套件的测试产物"
        f"（扫描 {elapsed_seconds:.1f}s，标记 {suite_private_temporary_directory_name}）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
