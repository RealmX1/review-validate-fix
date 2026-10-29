"""轨迹只读投影：历史事实、恢复状态与未知指标分别呈现。"""
from __future__ import annotations

from pathlib import Path

from .persistent_artifacts import read_json
from .role_failure_recovery import native_invocation_facts


def analyze_runs(run_directories: list[Path]) -> dict:
    runs = []
    for run in dict.fromkeys(path.expanduser().resolve() for path in run_directories):
        record = read_json(run / "run.json")
        attempts = []
        for request_file in sorted(run.glob("*/attempt-*/request.json")):
            directory = request_file.parent
            outcome_file = directory / "outcome.json"
            raw = read_json(outcome_file) if outcome_file.exists() else {}
            reconciliation_file = directory / "reconciled-native-completion.json"
            reconciliation = read_json(reconciliation_file) if reconciliation_file.exists() else {}
            request = read_json(request_file)
            invocation_file = directory / "model-invocation.json"
            invocation = read_json(invocation_file) if invocation_file.exists() else None
            facts = {key: raw.get(key) for key in ("native_usage", "actual_models", "native_session_ids")}
            facts_evidence = str(outcome_file) if raw else None
            stdout_file = directory / "stdout.txt"
            if all(value is None for value in facts.values()) and stdout_file.exists() and invocation and invocation.get("output_format", request.get("config", {}).get("output_format", "text")) != "text":
                facts = native_invocation_facts(stdout_file.read_text(errors="replace"))
                facts_evidence = str(stdout_file)
            attempts.append({"role": directory.parent.name, "attempt_directory": str(directory),
                "raw_status": raw.get("status"), "effective_status": reconciliation.get("status", raw.get("status")),
                "effective_status_evidence": str(reconciliation_file) if reconciliation else str(outcome_file) if raw else None,
                "raw_outcome": raw, "request": request, "model_invocation": invocation,
                **facts, "native_facts_evidence": facts_evidence})
        validations = [read_json(path) for path in sorted(run.glob("final-validation/attempt-*/validation.json"))]
        evidence = [read_json(path) for path in sorted(run.glob("machine-evidence/attempt-*/evidence.json"))]
        incomplete_fixer_machine_validations = []
        for directory in sorted(run.glob('incomplete-fixer-machine-validation/round-*/attempt-*')):
            path = directory / 'evidence.json'
            incomplete_fixer_machine_validations.append(dict(read_json(path), original=str(path)) if path.exists()
                else {'status': 'incomplete', 'attempt_directory': str(directory),
                      'commands': [read_json(original) for original in sorted(directory.glob('commands/command-*/execution.json'))]})
        runs.append({"run_directory": str(run), "status": record["status"], "base_commit": record.get("base_commit"),
            "candidate_commit": record.get("candidate_commit"), "source_version": record.get("source_version"),
            "runtime_environment": record.get("runtime_environment"), "attempts": attempts, "validations": validations,
            "shared_evidence": evidence, "incomplete_fixer_machine_validations": incomplete_fixer_machine_validations,
            "commit": record.get("commit"),
            "stage_events": read_json(run / "stage-events.json").get("events", []) if (run / "stage-events.json").exists() else None,
            "closeout": read_json(run / "business-result.json").get("closeout") if (run / "business-result.json").exists() else None})
    attempts = [attempt for run in runs for attempt in run["attempts"]]
    usages = [attempt["native_usage"] for attempt in attempts if attempt["native_usage"] is not None]
    return {"runs": runs, "analysis_required_for_completion": False,
        "metrics": {"main_agent_model_round_trips": None, "main_agent_tool_polls": None,
            "role_invocations": sum((Path(item["attempt_directory"]) / "model-invocation.json").exists() for item in attempts),
            "supplemental_context_responses": sum(len(list(Path(run["run_directory"]).glob("context-response-originals-by-sha256/*.md"))) for run in runs),
            "validation_attempts": sum(len(run["validations"]) + len(run["incomplete_fixer_machine_validations"]) for run in runs),
            "machine_evidence_collections": sum(len(run["shared_evidence"]) + len(run["incomplete_fixer_machine_validations"]) for run in runs),
            "role_elapsed_seconds": sum(item["raw_outcome"]["finished_at"] - item["raw_outcome"]["started_at"] for item in attempts
                if isinstance(item["raw_outcome"].get("finished_at"), (int, float)) and isinstance(item["raw_outcome"].get("started_at"), (int, float))),
            "native_usage": usages if len(usages) == len(attempts) and attempts else None,
            "known_native_usage_records": usages, "token_savings": None}}
