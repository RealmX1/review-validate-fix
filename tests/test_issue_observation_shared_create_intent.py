"""从真实收集与补写入口验证同问题观察共享未知创建结果。"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.task_snapshot_review import github_issue_observations as collection


class DelayedIssueApi:
    def __init__(self, lost_results=(True,)):
        self.issues = {}
        self.comments = {}
        self.issue_posts = []
        self.hidden = True
        # True 表示已接收但响应丢失；False 表示未接收且响应丢失。
        self.lost_results = list(lost_results)

    def request(self, method, endpoint, payload=None):
        parts = endpoint.split("?", 1)[0].split("/")
        repository = "/".join(parts[1:3]).lower()
        issues = self.issues.setdefault(repository, [])
        if method == "GET":
            if parts[-1] == "issues":
                return [] if self.hidden else copy.deepcopy(issues)
            if parts[-2:] == ["issues", "comments"]:
                return [copy.deepcopy(comment) for (repo, _), comments in self.comments.items()
                        if repo == repository for comment in comments]
            return copy.deepcopy(self.comments.get((repository, int(parts[-2])), []))
        if parts[-1] == "issues":
            self.issue_posts.append((repository, copy.deepcopy(payload)))
            lost = self.lost_results.pop(0) if self.lost_results else None
            number = len(issues) + 1
            issue = {"number": number, "html_url": f"https://example.invalid/{repository}/issues/{number}", **payload}
            if lost is not False:
                issues.append(issue)
            if lost is not None:
                raise OSError("创建响应丢失，列表暂不可见")
            return copy.deepcopy(issue)
        number = int(parts[-2])
        comments = self.comments.setdefault((repository, number), [])
        comment = {"html_url": f"https://example.invalid/{repository}/issues/{number}#comment-{len(comments) + 1}",
                   "issue_url": f"https://api.example.invalid/repos/{repository}/issues/{number}", **payload}
        comments.append(comment)
        return copy.deepcopy(comment)


def report(observation_id, **changes):
    return {"repository": "example/rvf", "problem_key": "original-root-cause", "title": "原始问题标题",
            "problem": "原始问题说明", "observation_id": observation_id,
            "observation": {"task_id": observation_id}, **changes}


@pytest.mark.parametrize("original_first", [True, False])
@pytest.mark.parametrize("alias_direction", ["same", "original", "new"])
def test_shared_unknown_creation_recovers_both_observations_in_either_flush_order(tmp_path, original_first, alias_direction):
    api = DelayedIssueApi()
    ids = sorted(["observation-a", "observation-b"],
                 key=lambda value: hashlib.sha256(("example/rvf\0" + value).encode()).hexdigest())
    if not original_first:
        ids.reverse()
    first_report, second_report = report(ids[0]), report(ids[1], title="另一任务的观察")
    if alias_direction == "original":
        first_report["problem_key_aliases"] = ["renamed-root-cause"]
        second_report["problem_key"] = "renamed-root-cause"
    elif alias_direction == "new":
        second_report.update(problem_key="renamed-root-cause", problem_key_aliases=["original-root-cause"])
    first = collection.collect_report(first_report, storage_root=tmp_path, api=api)
    second = collection.collect_report(second_report, storage_root=tmp_path, api=api)
    assert first["status"] == second["status"] == "pending"
    assert len(api.issue_posts) == 1

    api.hidden = False
    recovered = collection.flush_pending_reports(storage_root=tmp_path, api=api)
    assert recovered["delivered"] == 2
    assert recovered["pending"] == 0
    assert len(api.issue_posts) == len(api.issues["example/rvf"]) == 1
    comments = api.comments[("example/rvf", 1)]
    assert len(comments) == 2
    for receipt, original in ((first, first_report), (second, second_report)):
        assert json.loads(Path(receipt["report_file"]).read_text())["report"] == original
        assert sum(collection.observation_marker(original["observation_id"]) in comment["body"]
                   for comment in comments) == 1


def test_transitive_aliases_share_the_original_creation(tmp_path):
    api = DelayedIssueApi()
    reports = [report("a", problem_key="alpha", problem_key_aliases=["legacy"]),
               report("b", problem_key="beta", problem_key_aliases=["legacy"]),
               report("c", problem_key="gamma", problem_key_aliases=["beta"])]
    for original in reports:
        assert collection.collect_report(original, storage_root=tmp_path, api=api)["status"] == "pending"
    assert len(api.issue_posts) == 1
    api.hidden = False
    recovered = collection.flush_pending_reports(storage_root=tmp_path, api=api)
    assert (recovered["delivered"], recovered["pending"]) == (3, 0)
    assert len(api.issue_posts) == 1
    assert len(api.comments[("example/rvf", 1)]) == 3


def test_known_issue_binding_prevents_new_creation_even_after_retry_window(tmp_path, monkeypatch):
    api = DelayedIssueApi()
    clock = [100.0]
    monkeypatch.setattr(collection, "time", SimpleNamespace(time=lambda: clock[0]))
    assert collection.collect_report(report("a"), storage_root=tmp_path, api=api)["status"] == "pending"
    assert collection.collect_report(report("b", issue_number=1), storage_root=tmp_path, api=api)["status"] == "pending"
    clock[0] += 120
    assert collection.collect_report(report("c", problem_key="renamed", problem_key_aliases=["original-root-cause"]),
                                     storage_root=tmp_path, api=api)["status"] == "pending"
    assert len(api.issue_posts) == 1
    api.hidden = False
    recovered = collection.flush_pending_reports(storage_root=tmp_path, api=api)
    assert (recovered["delivered"], recovered["pending"]) == (3, 0)
    assert len(api.issue_posts) == 1
    assert len(api.comments[("example/rvf", 1)]) == 3


def test_unaccepted_creation_retries_from_original_report_after_two_shared_absence_checks(tmp_path, monkeypatch):
    api = DelayedIssueApi(lost_results=(False,))
    clock = [100.0]
    monkeypatch.setattr(collection, "time", SimpleNamespace(time=lambda: clock[0]))
    original = report("a", problem_key_aliases=["renamed"])
    newer = report("b", problem_key="renamed", title="不应替代原始标题", problem="不应替代原始说明")
    assert collection.collect_report(original, storage_root=tmp_path, api=api)["status"] == "pending"
    assert collection.collect_report(newer, storage_root=tmp_path, api=api)["status"] == "pending"
    clock[0] = 159
    assert collection.collect_report(newer, storage_root=tmp_path, api=api)["status"] == "pending"
    assert len(api.issue_posts) == 1
    clock[0] = 160
    api.hidden = False
    assert collection.collect_report(newer, storage_root=tmp_path, api=api)["status"] == "delivered"
    assert len(api.issue_posts) == 2
    assert api.issue_posts[0] == api.issue_posts[1]
    assert api.issue_posts[1][1]["title"] == original["title"]
    assert collection.problem_marker(original["problem_key"]) in api.issues["example/rvf"][0]["body"]
    recovered = collection.flush_pending_reports(storage_root=tmp_path, api=api)
    assert (recovered["delivered"], recovered["pending"]) == (1, 0)
    assert len(api.issues["example/rvf"]) == 1
    assert len(api.comments[("example/rvf", 1)]) == 2


def test_another_unknown_retry_starts_a_new_shared_confirmation_window(tmp_path, monkeypatch):
    api = DelayedIssueApi(lost_results=(False, False))
    clock = [100.0]
    monkeypatch.setattr(collection, "time", SimpleNamespace(time=lambda: clock[0]))
    for observation in ("a", "b"):
        assert collection.collect_report(report(observation), storage_root=tmp_path, api=api)["status"] == "pending"
    clock[0] = 160
    assert collection.collect_report(report("b"), storage_root=tmp_path, api=api)["status"] == "pending"
    assert len(api.issue_posts) == 2
    assert collection.collect_report(report("c"), storage_root=tmp_path, api=api)["status"] == "pending"
    assert len(api.issue_posts) == 2
    clock[0] = 220
    api.hidden = False
    recovered = collection.flush_pending_reports(storage_root=tmp_path, api=api)
    assert (recovered["delivered"], recovered["pending"]) == (3, 0)
    assert len(api.issue_posts) == 3
    assert len(api.issues["example/rvf"]) == 1
    assert len(api.comments[("example/rvf", 1)]) == 3


def test_repository_identity_is_case_insensitive_but_does_not_cross_repositories(tmp_path):
    api = DelayedIssueApi()
    assert collection.collect_report(report("a"), storage_root=tmp_path, api=api)["status"] == "pending"
    assert collection.collect_report(report("b", repository="Example/RVF"), storage_root=tmp_path, api=api)["status"] == "pending"
    assert len(api.issue_posts) == 1
    assert collection.collect_report(report("c", repository="example/other"), storage_root=tmp_path, api=api)["status"] == "delivered"
    assert len(api.issue_posts) == 2
    api.hidden = False
    recovered = collection.flush_pending_reports(storage_root=tmp_path, api=api)
    assert (recovered["delivered"], recovered["pending"]) == (2, 0)
    assert len(api.issues["example/rvf"]) == len(api.issues["example/other"]) == 1
    assert len(api.comments[("example/rvf", 1)]) == 2
