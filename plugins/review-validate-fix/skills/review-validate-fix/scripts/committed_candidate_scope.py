"""把 Kanban 证据适配成所有候选消费者共用的固定提交窗口。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import _rvf_pyroot  # noqa: F401
from adapters.cline_kanban.committed_candidate_provenance import resolve_kanban_committed_candidate_scope_evidence
from core.session_scope_allocation.committed_candidate_scope_evidence import validate_committed_candidate_scope_evidence
from core.session_scope_allocation.committed_round_detection import (
    _resolve_committed_round_base_ref, _resolve_committed_round_baseline,
    frozen_committed_round_base_exclusion_ref,
)


def resolve_committed_candidate_scope(
    repo: Path, task_id: str | None, session_id: str | None, *,
    event: Mapping[str, Any] | None = None, frozen_evidence: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    evidence = dict(frozen_evidence) if frozen_evidence is not None else (
        resolve_kanban_committed_candidate_scope_evidence(repo, task_id, session_id, event=event)
        if task_id else None
    )
    if evidence is not None:
        validate_committed_candidate_scope_evidence(repo, evidence, task_id=task_id)
    baseline = _resolve_committed_round_baseline(repo, task_id, session_id, scope_evidence=evidence)
    base_ref = evidence["base_ref"] if evidence and evidence.get("status") == "verified" else _resolve_committed_round_base_ref(repo)
    return baseline, base_ref, evidence


def committed_base_exclusion_for_scope(
    repo: Path, base_ref: str | None, evidence: Mapping[str, Any] | None,
    fixed_base_tip: str | None = None,
) -> str | None:
    tip = fixed_base_tip or (evidence.get("base_tip_commit") if evidence and evidence.get("status") == "verified" else None)
    return frozen_committed_round_base_exclusion_ref(repo, base_ref, tip) if tip else base_ref
