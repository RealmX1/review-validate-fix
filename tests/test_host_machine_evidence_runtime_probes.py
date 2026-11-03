"""显式启用的真实宿主 boot、tsx IPC 和 Chromium 自动补证验收。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from core.task_snapshot_review.persistent_artifacts import read_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.reviewer_machine_evidence_recovery import collect_registered_reviewer_machine_evidence_request
from test_reviewer_machine_evidence_recovery import registered_request
from test_task_snapshot_review_workflow import repository, canary


@pytest.mark.skipif(not os.environ.get("RVF_HOST_EVIDENCE_NODE_EXECUTABLE"), reason="需要显式指定隔离宿主 Node、tsx、Chromium 入口")
def test_host_boot_tsx_ipc_and_chromium_commands_keep_candidate_and_original_logs(repository, tmp_path):
    node = os.environ["RVF_HOST_EVIDENCE_NODE_EXECUTABLE"]
    tsx = os.environ["RVF_HOST_EVIDENCE_TSX_ENTRYPOINT"]
    playwright = Path(os.environ["RVF_HOST_EVIDENCE_PLAYWRIGHT_MODULE"]).as_uri()
    chromium = os.environ["RVF_HOST_EVIDENCE_CHROMIUM_EXECUTABLE"]
    boot_command = ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"] if sys.platform == "darwin" else ["cat", "/proc/sys/kernel/random/boot_id"]
    commands = [boot_command,
        [node, tsx, "-e", "import {createServer} from 'node:net'; const proof: string = 'tsx IPC host proof'; "
            "const server=createServer(); server.listen(0,'127.0.0.1',()=>{console.log(proof,process.version);server.close()});"],
        [node, "--input-type=module", "-e", f"import {{chromium}} from {json.dumps(playwright)}; "
            f"const browser=await chromium.launch({{headless:true,executablePath:{json.dumps(chromium)}}}); "
            "try {const page=await browser.newPage(); await page.setContent('<h1>隔离宿主凭据</h1>'); "
            "if(await page.locator('h1').textContent()!=='隔离宿主凭据') throw Error('浏览器结果不匹配'); "
            "console.log(JSON.stringify({proof:'Chromium host proof',node:process.version,chromium:browser.version()}));} "
            "finally {await browser.close();}"],
    ]
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "host-runtime-probe-run"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"host runtime evidence probes",
        run_directory=run, execution_plan={"fix": False, "validation_commands": commands})
    before = canary(repository)
    request = registered_request(run)
    request["registered_command_references"] = [{"command_collection": "validation_commands", "command_index": index} for index in range(3)]
    original = collect_registered_reviewer_machine_evidence_request(run, read_json(run / "run.json"), read_json(run / "execution-plan.json"), request, [])
    assert original is not None
    proof = read_json(original)
    assert proof["status"] == "passed"
    assert len(proof["commands"]) == 3
    assert canary(repository) == before
    destination = os.environ.get("RVF_HOST_EVIDENCE_ARTIFACT_DIRECTORY")
    if destination:
        root = Path(destination).resolve()
        root.mkdir(parents=True, exist_ok=True)
        manifest = {}
        selected = [*run.glob("*.json"), *run.glob("*.md"), run / "task-changes.patch",
            *run.glob("reviewer-machine-evidence-requests/*/request.json"),
            *run.glob("reviewer-machine-evidence-requests/*/attempt-*/*.json"),
            *run.glob("reviewer-machine-evidence-requests/*/attempt-*/commands/**/*.json"),
            *run.glob("reviewer-machine-evidence-requests/*/attempt-*/commands/**/*.txt")]
        for source in selected:
            if not source.is_file():
                continue
            relative = source.relative_to(run)
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            manifest[str(relative)] = {"source_path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        (root / "archive-only-provenance.json").write_text(json.dumps({"purpose": "真实隔离验收原件归档，不是生产 run 或恢复入口",
            "tested_tree": proof["source_tree_before"], "candidate_canary_preserved": True, "originals": manifest}, ensure_ascii=False, indent=2))
