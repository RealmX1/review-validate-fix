"""问题身份、投递未知、关闭问题匹配、并发及断网恢复验收。"""
from __future__ import annotations

import copy
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.task_snapshot_review.github_issue_observations import collect_report, flush_pending_reports, problem_marker, observation_marker


class FakeGitHub:
    def __init__(self):
        self.issues = []
        self.comments = {}
        self.offline = False
        self.lose_issue_response = False
        self.lose_comment_response = False

    def request(self, method, endpoint, payload=None):
        if self.offline:
            raise OSError("网络不可用")
        parts = endpoint.split("?")[0].split("/")
        if method == "GET":
            if parts[-1] == "comments":
                if parts[-2] == "issues":
                    return [dict(comment,issue_url=f"https://api.github.com/repos/owner/repository/issues/{number}") for number,comments in self.comments.items() for comment in comments]
                return copy.deepcopy(self.comments.get(int(parts[-2]), []))
            if parts[-1].isdigit():
                return copy.deepcopy(next(issue for issue in self.issues if issue["number"] == int(parts[-1])))
            return copy.deepcopy(self.issues)
        if parts[-1] == "issues":
            number = len(self.issues) + 1
            issue = {"number": number, "state":"open", "html_url":f"https://github.com/owner/repository/issues/{number}", **payload}
            self.issues.append(issue)
            if self.lose_issue_response:
                self.lose_issue_response = False
                raise OSError("创建成功但响应丢失")
            return copy.deepcopy(issue)
        number = int(parts[-2])
        comments = self.comments.setdefault(number, [])
        comment = {"id":len(comments)+1,"html_url":f"https://github.com/owner/repository/issues/{number}#comment-{len(comments)+1}",**payload}
        comments.append(comment)
        if self.lose_comment_response:
            self.lose_comment_response = False
            raise OSError("评论成功但响应丢失")
        return copy.deepcopy(comment)


def report(observation_id="task-a:run-a:observation-1", **changes):
    return {"repository":"owner/repository","problem_key":"workflow:background-wip-overwritten",
            "title":"后台 WIP 被覆盖", "problem":"应用任务修复时覆盖了后台文件", "impact":"丢失未提交工作",
            "reproduction":"隔离仓库中保留后台文件再应用补丁", "evidence":"实际 Git diff 与命令结果",
            "observation_id":observation_id,"observation":{"task_id":"task-a","run_id":"run-a","validation":"已复现","repair":"已修复"}, **changes}


def test_same_problem_appends_observation_and_same_observation_retry_is_idempotent(tmp_path):
    api = FakeGitHub()
    collect_report(report(), storage_root=tmp_path, api=api)
    collect_report(report("task-b:run-b:observation-1", title="移动文件后的同根因表现"), storage_root=tmp_path, api=api)
    collect_report(report("task-b:run-b:observation-1", title="移动文件后的同根因表现"), storage_root=tmp_path, api=api)
    assert len(api.issues) == 1
    assert len(api.comments[1]) == 2
    assert problem_marker(report()["problem_key"]) in api.issues[0]["body"]


def test_closed_issue_is_matched_and_not_reopened(tmp_path):
    api = FakeGitHub()
    collect_report(report(), storage_root=tmp_path, api=api)
    api.issues[0]["state"] = "closed"
    collect_report(report("new-observation", title="行号和措辞均改变"), storage_root=tmp_path, api=api)
    assert len(api.issues) == 1
    assert api.issues[0]["state"] == "closed"
    assert len(api.comments[1]) == 2


@pytest.mark.parametrize("lose_response", ["lose_issue_response", "lose_comment_response"])
def test_unknown_write_result_is_reconciled_before_retry(tmp_path, lose_response):
    api = FakeGitHub()
    setattr(api, lose_response, True)
    result = collect_report(report(), storage_root=tmp_path, api=api)
    assert result["status"] == "pending"
    result = flush_pending_reports(storage_root=tmp_path, api=api)
    assert result["pending"] == 0
    assert len(api.issues) == 1
    assert len(api.comments[1]) == 1


def test_offline_report_survives_new_collector_instance(tmp_path):
    api = FakeGitHub()
    api.offline = True
    result = collect_report(report(), storage_root=tmp_path, api=api)
    assert result["status"] == "pending"
    assert list(tmp_path.glob("issue-observation-delivery-records/*.json"))
    api.offline = False
    assert flush_pending_reports(storage_root=tmp_path, api=api)["pending"] == 0
    assert len(api.issues) == len(api.comments[1]) == 1


def test_concurrent_collection_uses_one_issue_and_one_comment_per_observation(tmp_path):
    api = FakeGitHub()
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(lambda i: collect_report(report(f"observation-{i // 2}"), storage_root=tmp_path, api=api), range(12)))
    assert len(api.issues) == 1
    assert len(api.comments[1]) == 6


def test_explicit_existing_issue_matches_preexisting_unmarked_problem(tmp_path):
    api = FakeGitHub()
    api.issues.append({"number":1,"title":"已有人工问题","body":"旧描述", "state":"closed"})
    collect_report(report(issue_number=1), storage_root=tmp_path, api=api)
    assert len(api.issues) == 1
    assert len(api.comments[1]) == 1
    collect_report(report('follow-up-without-explicit-number'),storage_root=tmp_path,api=api)
    assert len(api.issues) == 1
    assert len(api.comments[1]) == 2


def test_same_observation_identity_cannot_silently_replace_original_report(tmp_path):
    api = FakeGitHub()
    collect_report(report(), storage_root=tmp_path, api=api)
    with pytest.raises(ValueError):
        collect_report(report(problem="不同的原始报告"), storage_root=tmp_path, api=api)


def test_tls_fallback_is_selected_by_a_read_and_never_blindly_replays_a_post(monkeypatch):
    import io,subprocess,ssl
    from core.task_snapshot_review import github_issue_observations as collection
    calls=[]
    monkeypatch.setenv('GH_TOKEN','isolated-test-token')
    monkeypatch.setattr(collection.subprocess,'run',lambda argv,**kwargs: calls.append(argv) or subprocess.CompletedProcess(argv,1,'','x509 certificate error'))
    def request(request,context,timeout):
        assert context.check_hostname
        assert context.verify_mode==ssl.CERT_REQUIRED
        calls.append(request.method)
        return io.BytesIO(b'[]' if request.method=='GET' else b'{"number":1}')
    monkeypatch.setattr(collection.urllib.request,'urlopen',request)
    api=collection.GitHubIssueApi()
    assert api.request('GET','repos/owner/repository/issues')==[]
    assert api.request('POST','repos/owner/repository/issues',{'title':'test'})=={'number':1}
    assert len(calls)==3
    calls.clear()
    with pytest.raises(OSError):
        collection.GitHubIssueApi().request('POST','repos/owner/repository/issues',{'title':'test'})
    assert len(calls)==1


def test_pending_delivery_is_scheduled_in_background_at_most_once_per_minute(tmp_path,monkeypatch):
    from core.task_snapshot_review import github_issue_observations as collection
    from core.task_snapshot_review.persistent_artifacts import read_json,write_json
    api=FakeGitHub();api.offline=True
    receipt=collect_report(report(),storage_root=tmp_path,api=api)
    path=Path(receipt['report_file']);entry=read_json(path);entry['next_automatic_check_at']=0;write_json(path,entry)
    launched=[]
    monkeypatch.setattr(collection.subprocess,'Popen',lambda argv,**kwargs: launched.append(argv))
    assert collection.schedule_pending_issue_delivery(tmp_path,tmp_path/'rvf.py')
    assert not collection.schedule_pending_issue_delivery(tmp_path,tmp_path/'rvf.py')
    assert len(launched)==1
    assert launched[0][-2:]==['collect-issues','--flush']


@pytest.mark.parametrize('stage',['issues','comments'])
def test_unknown_unaccepted_write_waits_for_two_complete_absence_checks(tmp_path,monkeypatch,stage):
    from types import SimpleNamespace
    from core.task_snapshot_review import github_issue_observations as collection
    api=FakeGitHub();original=api.request;lost=[False]
    def request(method,endpoint,payload=None):
        if method=='POST' and endpoint.endswith(stage) and not lost[0]:
            lost[0]=True
            raise OSError('请求未被接收，调用方不知道结果')
        return original(method,endpoint,payload)
    api.request=request
    assert collect_report(report(),storage_root=tmp_path,api=api)['status']=='pending'
    assert flush_pending_reports(storage_root=tmp_path,api=api)['pending']==1
    now=collection.time.time()
    monkeypatch.setattr(collection,'time',SimpleNamespace(time=lambda:now+61))
    assert flush_pending_reports(storage_root=tmp_path,api=api)['pending']==0
    assert len(api.issues)==len(api.comments[1])==1
