from __future__ import annotations

import ast
import io
import json
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SKILL_SCRIPTS_DIR = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
)
CLAUDE_HOOKS_DIR = ROOT / "plugins" / "review-validate-fix" / "hooks"
sys.path.insert(0, str(SKILL_SCRIPTS_DIR))
sys.path.insert(0, str(CLAUDE_HOOKS_DIR))

from core import rvf_user_local_data_paths
from core.run_ledger import run_ledger

import _claude_hook_entry  # noqa: E402  — Claude host hook shim（stdlib-only）
import diagnose_stop_hook_scope  # noqa: E402  — 复用其 AST 赋值扫描器


def test_user_local_data_paths_default_to_home_scoped_rvf_root(tmp_path: Path) -> None:
    paths = rvf_user_local_data_paths.resolved_rvf_user_local_data_paths(
        environment={},
        home_directory=tmp_path,
    )

    assert paths.user_local_data_root == tmp_path / ".rvf/user-local-data"
    assert paths.reviewer_registry_path == (
        tmp_path
        / ".rvf/user-local-data/reviewer-configuration/reviewer-registry.json"
    )
    assert paths.runtime_state_root == tmp_path / ".rvf/user-local-data/runtime-state"
    assert paths.run_logs_root == tmp_path / ".rvf/user-local-data/run-logs"
    assert paths.deployment_receipts_root == (
        tmp_path / ".rvf/user-local-data/deployment-receipts"
    )


def test_user_local_data_root_environment_override_is_expanded(
    tmp_path: Path,
) -> None:
    override_root = tmp_path / "explicit-user-local-data"

    paths = rvf_user_local_data_paths.resolved_rvf_user_local_data_paths(
        environment={"RVF_USER_LOCAL_DATA_ROOT": str(override_root)},
        home_directory=tmp_path / "unused-home",
    )

    assert paths.user_local_data_root == override_root


def test_runtime_log_root_preserves_existing_explicit_environment_precedence(
    tmp_path: Path,
) -> None:
    explicit_log_root = tmp_path / "explicit-log-root"
    explicit_state_root = tmp_path / "explicit-state-root"

    resolved = rvf_user_local_data_paths.resolved_rvf_runtime_log_root(
        environment={
            "CODEX_RVF_LOG_ROOT": str(explicit_log_root),
            "CODEX_RVF_STATE_DIR": str(explicit_state_root),
        },
        home_directory=tmp_path,
    )

    assert resolved == explicit_log_root


def test_reviewer_registry_uses_bundled_default_until_user_copy_exists(
    tmp_path: Path,
) -> None:
    bundled_registry = tmp_path / "plugin/config/reviewer-registry.json"
    bundled_registry.parent.mkdir(parents=True)
    bundled_registry.write_text("{}\n", encoding="utf-8")

    without_user_copy = rvf_user_local_data_paths.resolved_rvf_reviewer_registry_path(
        bundled_registry,
        environment={},
        home_directory=tmp_path,
    )
    assert without_user_copy == bundled_registry

    user_registry = (
        tmp_path
        / ".rvf/user-local-data/reviewer-configuration/reviewer-registry.json"
    )
    user_registry.parent.mkdir(parents=True)
    user_registry.write_text("{\"user\": true}\n", encoding="utf-8")

    with_user_copy = rvf_user_local_data_paths.resolved_rvf_reviewer_registry_path(
        bundled_registry,
        environment={},
        home_directory=tmp_path,
    )
    assert with_user_copy == user_registry


def test_run_ledger_default_moves_out_of_replaceable_plugin_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("CODEX_RVF_LOG_ROOT", raising=False)
    monkeypatch.delenv("CODEX_RVF_STATE_DIR", raising=False)
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "rvf-user-data"))

    assert run_ledger.log_root() == tmp_path / "rvf-user-data/run-logs"


def test_run_ledger_explicit_log_root_still_wins(
    tmp_path: Path,
    monkeypatch,
) -> None:
    explicit_log_root = tmp_path / "explicit-log-root"
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "rvf-user-data"))
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(explicit_log_root))
    monkeypatch.setenv("CODEX_RVF_STATE_DIR", str(tmp_path / "explicit-state-root"))

    assert run_ledger.log_root() == explicit_log_root


def test_session_hook_state_dir_never_falls_back_to_the_run_log_root(
    tmp_path: Path,
) -> None:
    """reviewer 子进程 source 过 review-env.sh 之后不得把会话状态写进 run 目录。

    ``review-env.sh`` 里的 ``CODEX_RVF_LOG_ROOT`` 指向**那一个 run 的日志根**。
    历史上 ``stop_hook_router.state_dir()`` 在 ``CODEX_RVF_STATE_DIR`` 缺失时会
    回落到它，于是 session-hook 状态会被拖进某个 run 的目录里去。
    """

    run_scoped_log_root = tmp_path / "run-logs" / "rvf-20260809T080514Z-example"

    resolved = rvf_user_local_data_paths.resolved_rvf_session_hook_state_dir(
        environment={"CODEX_RVF_LOG_ROOT": str(run_scoped_log_root)},
        home_directory=tmp_path,
    )

    assert resolved == tmp_path / ".rvf/user-local-data/runtime-state/session-hook"
    assert run_scoped_log_root not in resolved.parents


def test_session_hook_state_dir_honours_explicit_then_runtime_state_dir(
    tmp_path: Path,
) -> None:
    explicit_session_hook_dir = tmp_path / "explicit-session-hook"
    runtime_state_root = tmp_path / "runtime-state"

    assert rvf_user_local_data_paths.resolved_rvf_session_hook_state_dir(
        environment={
            "CODEX_RVF_SESSION_HOOK_STATE_DIR": str(explicit_session_hook_dir),
            "CODEX_RVF_STATE_DIR": str(runtime_state_root),
        },
        home_directory=tmp_path,
    ) == explicit_session_hook_dir

    assert rvf_user_local_data_paths.resolved_rvf_session_hook_state_dir(
        environment={"CODEX_RVF_STATE_DIR": str(runtime_state_root)},
        home_directory=tmp_path,
    ) == runtime_state_root / "session-hook"


# ---------------------------------------------------------------------------
# 结构性守卫：不存在任何代码路径把 RVF 的状态根解析到 ``~/.claude`` 之下
#
# 双根分裂（``~/.claude/rvf`` 与 ``~/.rvf/user-local-data/run-logs``）的成因是
# Claude host 的 hook shim 用一行 ``env.setdefault`` 把状态根钉死在 ``~/.claude``
# 下：走 hook 的一切落一根、shell 里跑的 prepare/seal/reviewer/rvf-agent 落另一
# 根。seal 写进的根与铸 run 读的根不同 ⇒ 已封窗的轮次被反复重新派发，reviewer
# 又在另一个根里查不到 lease。下面三条守卫钉死「不许再有这样的赋值点」。
#
# 判据刻意**不是**「host 专用 shim 不得设置任何环境变量」——shim 合法地设置
# ``CODEX_RVF_CLINE_KANBAN_AGENT_ID`` 与 ``CODEX_RVF_DEV_SYNC``，且部署校验
# （``verify_local_deploy.py``）依赖后者存在。判据只针对**状态根**。
# ---------------------------------------------------------------------------

RVF_STATE_ROOT_ENVIRONMENT_VARIABLE_NAMES = (
    diagnose_stop_hook_scope.RVF_STATE_ROOT_ENVIRONMENT_VARIABLE_NAMES
)

SOURCE_SCAN_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        ".gitnexus",
        "node_modules",
        ".venv",
        # AGENTS.md 规定的兼容层暂存区，已 gitignore，不参与产品判据。
        "dev_backward_compatibility",
    }
)

# 扫描覆盖面下界。自扫型 checker 最典型的失效不是判据写错，而是它悄悄
# 扫了 0 个文件却报绿（本仓在 S8.0 踩过）。
MINIMUM_SCANNED_SOURCE_FILE_COUNT = 120

# 守卫本身必须排除在扫描面之外：本文件里的阳性/反向对照 fixture 逐字包含
# 当年那一行钉根赋值，否则守卫会稳定地把自己判红（自扫型 checker 的对偶
# 失效——不是假绿，是假红）。对照测试用的是**函数级**断言，覆盖不受影响。
GUARD_SELF_EXCLUDED_SOURCE_PATHS = frozenset({Path(__file__).resolve()})


def scanned_repository_source_paths() -> list[Path]:
    """全仓 Python / shell 源文件。

    刻意走文件系统遍历而非 ``git ls-files``：新写的、尚未 ``git add`` 的文件
    也必须被守卫看见，否则 pre-commit 一路绿灯、post-commit 才变红。
    """

    paths: list[Path] = []
    for pattern in ("*.py", "*.sh"):
        for path in ROOT.rglob(pattern):
            relative_parts = path.relative_to(ROOT).parts
            if any(part in SOURCE_SCAN_EXCLUDED_DIRECTORY_NAMES for part in relative_parts):
                continue
            if path.resolve() in GUARD_SELF_EXCLUDED_SOURCE_PATHS:
                continue
            paths.append(path)
    return sorted(paths)


def docstring_line_ranges(source_text: str) -> list[tuple[int, int]]:
    """docstring / 裸字符串语句占据的行区间（闭区间）。"""

    try:
        parsed_module = ast.parse(source_text)
    except SyntaxError:
        return []
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(parsed_module):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            ranges.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
    return ranges


def executable_source_lines(path: Path) -> dict[int, str]:
    """按行返回**去掉注释与 docstring** 之后的源码内容。

    注释与 docstring 必须剔除：解释「历史上这里曾经把状态根钉在 ``~/.claude``
    下」的说明文字本身会让任何朴素的子串判据变红。
    """

    source_text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix != ".py":
        return {
            line_number: line.split("#", 1)[0]
            for line_number, line in enumerate(source_text.splitlines(), 1)
        }

    excluded_ranges = docstring_line_ranges(source_text)
    try:
        tokens_by_line: dict[int, list[str]] = {}
        for token in tokenize.generate_tokens(io.StringIO(source_text).readline):
            if token.type in (
                tokenize.COMMENT,
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
            ):
                continue
            line_number = token.start[0]
            if any(low <= line_number <= high for low, high in excluded_ranges):
                continue
            tokens_by_line.setdefault(line_number, []).append(token.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return {
            line_number: line
            for line_number, line in enumerate(source_text.splitlines(), 1)
        }
    return {
        line_number: " ".join(token_texts)
        for line_number, token_texts in tokens_by_line.items()
    }


def source_lines_pairing_state_root_variable_with_claude_home(
    path: Path,
) -> list[tuple[int, str]]:
    """同一行里既提到状态根环境变量、又提到 ``.claude`` 路径的可执行代码行。

    覆盖 AST 赋值扫描看不到的写法，例如 ``install_to_codex.py`` 里
    ``f"CODEX_RVF_LOG_ROOT={...}"`` 这种把赋值编码进字符串的 env 前缀。
    """

    offending: list[tuple[int, str]] = []
    for line_number, line in executable_source_lines(path).items():
        if ".claude" not in line:
            continue
        if any(name in line for name in RVF_STATE_ROOT_ENVIRONMENT_VARIABLE_NAMES):
            offending.append((line_number, line.strip()[:200]))
    return offending


def test_no_structured_environment_assignment_pins_rvf_state_root_under_claude_home() -> None:
    scanned_paths = [path for path in scanned_repository_source_paths() if path.suffix == ".py"]
    assert len(scanned_paths) >= MINIMUM_SCANNED_SOURCE_FILE_COUNT, (
        f"扫描面塌陷到 {len(scanned_paths)} 个文件，守卫已失去意义"
    )

    offending: list[str] = []
    total_assignment_site_count = 0
    for path in scanned_paths:
        source_text = path.read_text(encoding="utf-8", errors="replace")
        assignments = diagnose_stop_hook_scope.environment_variable_assignments_in_python_source(
            source_text,
            environment_variable_names=RVF_STATE_ROOT_ENVIRONMENT_VARIABLE_NAMES,
        )
        total_assignment_site_count += len(assignments)
        for assignment in assignments:
            value_source = assignment.get("value_source") or ""
            if ".claude" in value_source:
                offending.append(
                    f"{path.relative_to(ROOT)}:{assignment['line']} "
                    f"{assignment['environment_variable_name']} = {value_source}"
                )

    assert total_assignment_site_count > 0, "一个赋值点都没扫到，扫描器已失效"
    assert not offending, (
        "RVF 的状态根被钉死在 ~/.claude 之下（双根分裂的成因）：\n"
        + "\n".join(offending)
    )


def test_no_source_line_pairs_an_rvf_state_root_variable_with_a_claude_home_path() -> None:
    scanned_paths = scanned_repository_source_paths()
    assert len(scanned_paths) >= MINIMUM_SCANNED_SOURCE_FILE_COUNT, (
        f"扫描面塌陷到 {len(scanned_paths)} 个文件，守卫已失去意义"
    )

    offending: list[str] = []
    for path in scanned_paths:
        for line_number, line in source_lines_pairing_state_root_variable_with_claude_home(path):
            offending.append(f"{path.relative_to(ROOT)}:{line_number}  {line}")

    assert not offending, (
        "有可执行代码行把 RVF 状态根环境变量与 ~/.claude 路径写在一起：\n"
        + "\n".join(offending)
    )


def test_rvf_state_root_guard_detects_a_reintroduced_claude_root(tmp_path: Path) -> None:
    """阳性对照：把当年那一行原样放回去，两条守卫都必须变红。

    没有这条，上面两条测试在判据退化成恒真时会静默保持绿色。
    """

    reintroduced_source = tmp_path / "reintroduced_claude_root.py"
    reintroduced_source.write_text(
        'import os\n'
        'from pathlib import Path\n'
        'env = os.environ.copy()\n'
        'env.setdefault("CODEX_RVF_LOG_ROOT", str(Path.home() / ".claude" / "rvf"))\n',
        encoding="utf-8",
    )

    assignments = diagnose_stop_hook_scope.environment_variable_assignments_in_python_source(
        reintroduced_source.read_text(encoding="utf-8"),
        environment_variable_names=RVF_STATE_ROOT_ENVIRONMENT_VARIABLE_NAMES,
    )
    assert [
        assignment
        for assignment in assignments
        if ".claude" in (assignment.get("value_source") or "")
    ], "AST 守卫漏掉了被放回去的钉根赋值"

    assert source_lines_pairing_state_root_variable_with_claude_home(
        reintroduced_source
    ), "行级守卫漏掉了被放回去的钉根赋值"


def test_rvf_state_root_guard_ignores_explanatory_comments_and_docstrings(
    tmp_path: Path,
) -> None:
    """反向对照：解释这段历史的注释/docstring 不得把守卫点红。

    ``_claude_hook_entry.py`` 里那段「历史上这里 setdefault 成 ~/.claude/rvf」的
    注释是刻意留下的，守卫必须容得下它，否则维护者会去删注释而不是删代码。
    """

    explanatory_source = tmp_path / "explanatory_comment_only.py"
    explanatory_source.write_text(
        '"""历史上这里把 CODEX_RVF_LOG_ROOT setdefault 成 ~/.claude/rvf。"""\n'
        'import os\n'
        '# 刻意不设 CODEX_RVF_LOG_ROOT，历史上它被钉在 ~/.claude/rvf 下。\n'
        'env = os.environ.copy()\n',
        encoding="utf-8",
    )

    assert not source_lines_pairing_state_root_variable_with_claude_home(explanatory_source)


def test_claude_host_hook_shim_hands_core_an_unpinned_rvf_state_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """行为守卫：Claude host shim 转发给核心的环境不得预设任何状态根。

    静态扫描只能证明「没有 ``.claude`` 字面量」；这条证明的是更强的性质——
    shim 交给核心子进程的那份 env 里根本没有状态根变量，于是核心解析出的根
    与不走 hook 的 shell 子进程完全一致。
    """

    for variable_name in RVF_STATE_ROOT_ENVIRONMENT_VARIABLE_NAMES:
        monkeypatch.delenv(variable_name, raising=False)

    captured_hook_environments: list[dict[str, str]] = []

    class CompletedProcessStub:
        stdout = '{"continue": true}'
        stderr = ""
        returncode = 0

    def fake_subprocess_run(command, **keyword_arguments):
        captured_hook_environments.append(dict(keyword_arguments["env"]))
        return CompletedProcessStub()

    monkeypatch.setattr(_claude_hook_entry.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"hook_event_name": "Stop", "cwd": str(tmp_path)})),
    )

    exit_code = _claude_hook_entry.run_claude_hook(
        event_name="Stop",
        core_script=("skills", "review-validate-fix", "scripts", "stop_review_validate_fix.py"),
        timeout_env="CLAUDE_RVF_STOP_HOOK_TIMEOUT",
        default_timeout="115",
        silent_success=False,
    )

    assert exit_code == 0
    assert captured_hook_environments, "shim 没有把事件转发给核心，本测试失去意义"
    hook_environment = captured_hook_environments[0]

    pinned_state_root_variables = {
        variable_name: hook_environment[variable_name]
        for variable_name in RVF_STATE_ROOT_ENVIRONMENT_VARIABLE_NAMES
        if variable_name in hook_environment
    }
    assert not pinned_state_root_variables, (
        f"Claude host shim 预设了 RVF 状态根：{pinned_state_root_variables}"
    )

    synthetic_home = tmp_path / "synthetic-home"
    assert rvf_user_local_data_paths.resolved_rvf_runtime_log_root(
        environment=hook_environment,
        home_directory=synthetic_home,
    ) == synthetic_home / ".rvf/user-local-data/run-logs"
    assert rvf_user_local_data_paths.resolved_rvf_session_hook_state_dir(
        environment=hook_environment,
        home_directory=synthetic_home,
    ) == synthetic_home / ".rvf/user-local-data/runtime-state/session-hook"
