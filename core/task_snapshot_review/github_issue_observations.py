"""主 Agent 的 GitHub 问题收集；网络投递与评审/提交资格互不依赖。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import subprocess
import time
import urllib.request
from pathlib import Path

from .persistent_artifacts import exclusive_lock, git_text, read_json, write_json


def problem_marker(problem_key: str) -> str:
    return f"<!-- rvf-problem:{hashlib.sha256(problem_key.encode()).hexdigest()} -->"


def observation_marker(observation_id: str) -> str:
    return f"<!-- rvf-observation:{hashlib.sha256(observation_id.encode()).hexdigest()} -->"


class GitHubIssueApi:
    def __init__(self):
        self.use_python_tls_transport = False

    def request(self, method: str, endpoint: str, payload: dict | None = None):
        if self.use_python_tls_transport:
            return self._python_request(method,endpoint,payload)
        command = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint,
                   "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2026-03-10"]
        if payload is not None:
            command.extend(["--input", "-"])
        result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                text=True, capture_output=True, timeout=40)
        if result.returncode:
            if method == 'GET' and ('x509' in result.stderr or 'certificate' in result.stderr):
                # 只在安全的读请求上选择另一套可信 TLS 实现；未知 POST 不在这里重发。
                self.use_python_tls_transport = True
                return self._python_request(method,endpoint,payload)
            error = result.stderr.strip()
            for name in ('GH_TOKEN','GITHUB_TOKEN'):
                if os.environ.get(name):
                    error=error.replace(os.environ[name],'[REDACTED]')
            raise OSError(f"GitHub API {method} 失败，退出码 {result.returncode}：{error[:800]}")
        return json.loads(result.stdout)

    def _python_request(self,method,endpoint,payload):
        token=os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
        if not token:
            auth=subprocess.run(['gh','auth','token','--hostname','github.com'],capture_output=True,text=True,timeout=10)
            if auth.returncode==0:
                token=auth.stdout.strip()
        headers={'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2026-03-10',
                 'User-Agent':'review-validate-fix-issue-observation-collector'}
        if token:
            headers['Authorization']='Bearer '+token
        data=json.dumps(payload).encode() if payload is not None else None
        if data is not None:
            headers['Content-Type']='application/json'
        request=urllib.request.Request('https://api.github.com/'+endpoint,method=method,data=data,headers=headers)
        context=ssl.create_default_context()
        with urllib.request.urlopen(request,context=context,timeout=40) as response:
            return json.load(response)


def github_repository_from_origin(repo: Path) -> str:
    remote = git_text(repo, "remote", "get-url", "origin")
    match = re.fullmatch(r"(?:git@github\.com:|ssh://git@github\.com/|https://(?:[^/@]+@)?github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", remote)
    if not match:
        raise ValueError("origin 不是明确的 GitHub 仓库；请指定问题所属 repository")
    return match[1]


def _all_pages(api, endpoint: str) -> list[dict]:
    values = []
    separator = "&" if "?" in endpoint else "?"
    for page in range(1, 10001):
        batch = api.request("GET", f"{endpoint}{separator}per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("GitHub 列表响应格式异常")
        values.extend(batch)
        if len(batch) < 100:
            return values
    raise OSError("GitHub 分页未查尽；保持待补写，不据此新建 Issue")


def list_repository_issues(repository: str, *, api=None) -> list[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository 必须是 owner/repository")
    return [issue for issue in _all_pages(api or GitHubIssueApi(), f"repos/{repository}/issues?state=all") if "pull_request" not in issue]


def _issue_body(report: dict) -> str:
    return (f"{problem_marker(report['problem_key'])}\n\n"
            f"根因标识：`{report['problem_key']}`\n\n"
            + "\n\n".join(f"## {label}\n\n{report.get(key) or '本次未提供'}" for key, label in
                             (("problem", "问题"), ("impact", "影响"), ("reproduction", "复现"), ("evidence", "依据"))))


def _observation_body(report: dict) -> str:
    observation = report["observation"]
    content = observation if isinstance(observation, str) else json.dumps(observation, ensure_ascii=False, indent=2)
    if not isinstance(observation, str):
        content = f"```json\n{content}\n```"
    return f"{observation_marker(report['observation_id'])}\n{problem_marker(report['problem_key'])}\n\n本次观察：`{report['observation_id']}`\n\n{content}"


def _may_retry_unknown_write(entry: dict) -> bool:
    now = time.time()
    observations = entry.setdefault("absent_remote_result_checks", [])
    if not observations or now - observations[-1] >= 60:
        observations.append(now)
    return len(observations) >= 2


def _deliver(entry_path: Path, entry: dict, api) -> dict:
    report = entry["report"]
    repository = report["repository"]
    endpoint = f"repos/{repository}/issues"
    try:
        # 所有写入（包括未知结果恢复）之前查询实际远端状态。
        issues = list_repository_issues(repository, api=api)
        markers = [problem_marker(key) for key in [report["problem_key"], *report.get("problem_key_aliases", [])]]
        issue_number = entry.get("issue_number") or report.get("issue_number")
        matches = [issue for issue in issues if issue.get("number") == issue_number] if issue_number else [
            issue for issue in issues if any(marker in (issue.get("body") or "") for marker in markers)]
        if not matches and not issue_number and issues:
            comments = _all_pages(api, f"repos/{repository}/issues/comments")
            matched_numbers = {int(comment["issue_url"].rsplit("/",1)[1]) for comment in comments
                               if comment.get("issue_url") and any(marker in (comment.get("body") or "") for marker in markers)}
            matches = [issue for issue in issues if issue["number"] in matched_numbers]
        if len(matches) > 1:
            entry["recovery_condition"] = "发现多个同身份 Issue；主 Agent 合并问题归属并指定 issue_number 后接续"
            return entry
        if issue_number and not matches:
            raise ValueError("指定的 Issue 未在完整仓库列表中找到，不自动新建替代问题")
        if not matches:
            # 旧人工 Issue 的语义匹配由统一收集的主 Agent 完成，可显式绑定 issue_number。
            if entry.get("write_intent") == "create_issue" and not _may_retry_unknown_write(entry):
                entry["recovery_condition"] = "创建结果未知且完整查询未命中；60 秒后再次可达查询确认，随后自动接续"
                return entry
            entry["write_intent"] = "create_issue"
            write_json(entry_path, entry)
            issue = api.request("POST", endpoint, {"title": report["title"], "body": _issue_body(report)})
            issue_number = issue["number"]
        else:
            issue = matches[0]
            issue_number = issue["number"]
        entry["issue_number"] = issue_number
        entry["issue_url"] = issue.get("html_url")
        if entry.get("write_intent") == "create_issue":
            entry.pop("absent_remote_result_checks", None)
            entry.pop("write_intent", None)
        write_json(entry_path, entry)
        comments_endpoint = f"{endpoint}/{issue_number}/comments"
        comments = _all_pages(api, comments_endpoint)
        marker = observation_marker(report["observation_id"])
        matches = [comment for comment in comments if marker in (comment.get("body") or "")]
        if not matches:
            if entry.get("write_intent") == "create_comment" and not _may_retry_unknown_write(entry):
                entry["recovery_condition"] = "评论结果未知；60 秒后完整查询实际评论再自动接续"
                return entry
            entry["write_intent"] = "create_comment"
            write_json(entry_path, entry)
            comment = api.request("POST", comments_endpoint, {"body": _observation_body(report)})
        else:
            comment = matches[0]
        entry.update({"status": "delivered", "observation_url": comment.get("html_url")})
        for key in ("write_intent", "recovery_condition", "error", "absent_remote_result_checks"):
            entry.pop(key, None)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        entry.update({"status": "pending", "error": str(error),
                      "recovery_condition": "GitHub/凭据恢复后执行 collect-issues --flush；先核对远端实际结果"})
    finally:
        if entry.get('status') != 'delivered':
            entry['next_automatic_check_at'] = time.time()+60
        write_json(entry_path, entry)
    return entry


def collect_report(report: dict, *, storage_root: Path, api=None, repo: Path | None = None) -> dict:
    report = dict(report)
    if not report.get("repository"):
        if repo is None:
            raise ValueError("需要问题所属 repository 或用于解析 origin 的 repo")
        report["repository"] = github_repository_from_origin(repo)
    for key in ("repository", "problem_key", "title", "problem", "observation_id"):
        if not isinstance(report.get(key), str) or not report[key].strip():
            raise ValueError(f"报告缺少 {key}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", report["repository"]):
        raise ValueError("repository 必须是 owner/repository")
    if not isinstance(report.get("observation"), (str, dict)):
        raise ValueError("报告需要本次 observation")
    if "issue_number" in report and (type(report["issue_number"]) is not int or report["issue_number"] <= 0):
        raise ValueError("issue_number 必须是正整数")
    if not isinstance(report.get("problem_key_aliases", []), list) or not all(isinstance(key,str) and key for key in report.get("problem_key_aliases", [])):
        raise ValueError("problem_key_aliases 必须是非空字符串列表")
    repository_key = hashlib.sha256(report["repository"].lower().encode()).hexdigest()
    identity = hashlib.sha256((report["repository"].lower() + "\0" + report["observation_id"]).encode()).hexdigest()
    entry_path = storage_root / "issue-observation-delivery-records" / f"{identity}.json"
    with exclusive_lock(storage_root / "issue-collection-locks" / f"{repository_key}.lock"):
        if entry_path.exists():
            entry = read_json(entry_path)
            if entry["report"] != report:
                raise ValueError("同一 observation_id 已绑定不同原始报告；保留原件，请为新观察使用新 ID")
            if entry["status"] == "delivered":
                return _receipt(entry, entry_path)
        else:
            entry = {"status": "pending", "report": report, "created_at": time.time()}
            write_json(entry_path, entry)
        _deliver(entry_path, entry, api or GitHubIssueApi())
        return _receipt(entry, entry_path)


def _receipt(entry: dict, path: Path) -> dict:
    return {key: value for key, value in {"status": entry["status"], "report_file": str(path),
            "issue_url": entry.get("issue_url"), "observation_url": entry.get("observation_url"),
            "recovery_condition": entry.get("recovery_condition")}.items() if value is not None}


def flush_pending_reports(*, storage_root: Path, api=None) -> dict:
    receipts = []
    for path in sorted((storage_root / "issue-observation-delivery-records").glob("*.json")):
        try:
            entry = read_json(path)
            if entry["status"] != "delivered":
                receipts.append(collect_report(entry["report"], storage_root=storage_root, api=api))
        except (OSError, ValueError, KeyError) as error:
            receipts.append({"status":"pending","report_file":str(path),"recovery_condition":str(error)})
    return {"delivered": sum(item["status"] == "delivered" for item in receipts),
            "pending": sum(item["status"] != "delivered" for item in receipts), "reports": receipts}


def schedule_pending_issue_delivery(storage_root: Path, cli_entrypoint: Path) -> bool:
    """随正常 CLI 活动异步补写；网络等待不占用主流程，也不唤醒 Agent。"""
    for path in (storage_root/'issue-observation-delivery-records').glob('*.json'):
        try:
            entry=read_json(path)
            if entry.get('status')=='pending' and entry.get('next_automatic_check_at',0)<=time.time():
                break
        except (OSError,ValueError):
            continue
    else:
        return False
    progress=storage_root/'issue-delivery-background-recovery.json'
    with exclusive_lock(progress.with_suffix('.lock')):
        if progress.exists() and read_json(progress).get('last_started_at',0)>time.time()-60:
            return False
        write_json(progress,{'last_started_at':time.time()})
        with (storage_root/'issue-delivery-background-recovery.log').open('ab') as log:
            import sys
            subprocess.Popen([sys.executable,str(cli_entrypoint),'collect-issues','--flush'],
                             stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    return True
