"""工作树根的符号链接别名不得改变显式目录范围的忽略语义。"""
from core.task_snapshot_review.persistent_artifacts import git, git_text
from core.task_snapshot_review.task_snapshot import capture_worktree_tree
from test_task_snapshot_review_workflow import repository


def test_symlinked_worktree_root_preserves_directory_scope_ignore_semantics(repository, tmp_path):
    (repository / ".gitignore").write_text("ignored-review-runtime/\n")
    ignored_runtime = repository / "ignored-review-runtime"
    ignored_runtime.mkdir()
    (ignored_runtime / "execution-state.txt").write_text("不可进入评审候选的运行材料\n")
    (repository / "task.py").write_text("value = 2\n")
    workspace_alias = tmp_path / "review-workspace-symbolic-alias"
    workspace_alias.symlink_to(repository, target_is_directory=True)
    index_before = git(repository, "diff", "--cached", "--binary", "--full-index")

    physical_root_tree = capture_worktree_tree(repository, ["."], tmp_path)
    symbolic_root_tree = capture_worktree_tree(workspace_alias, ["."], tmp_path)

    assert symbolic_root_tree == physical_root_tree
    assert "ignored-review-runtime" not in git_text(repository, "ls-tree", "-r", "--name-only", symbolic_root_tree)
    assert git(repository, "diff", "--cached", "--binary", "--full-index") == index_before
