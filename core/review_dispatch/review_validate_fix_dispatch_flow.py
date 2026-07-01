#!/usr/bin/env python3
from __future__ import annotations

from typing import Any


DEFAULT_RVF_MODE = "fork"
DEFAULT_FORK_LAUNCH_MODE = "auto"
AUTO_FORK_LAUNCH_MODES = {"auto", "detect", "fallback"}


def rvf_mode_from_value(mode: str | None) -> str:
    value = (mode or DEFAULT_RVF_MODE).strip().lower()
    if value in {"continuation", "continue", "block"}:
        return "report"
    if value in {"off", "skip", "disabled", "disable"}:
        return "off"
    return "fork"


def backend_from_values(
    *,
    mode: str | None,
    fork_mode: str | None,
    in_kanban_task: bool,
) -> str:
    rvf_mode = (mode or DEFAULT_RVF_MODE).strip().lower()
    if rvf_mode in {"off", "skip", "disabled", "disable"}:
        return "off"
    if rvf_mode in {"continuation", "continue", "block"}:
        return "report-only"

    value = (DEFAULT_FORK_LAUNCH_MODE if fork_mode is None else fork_mode).strip().lower()
    if value in AUTO_FORK_LAUNCH_MODES:
        return "kanban-followup" if in_kanban_task else "kanban"
    if value in {"gui", "app-server", "appserver"}:
        return "gui"
    if value in {"cline-kanban", "cline", "kanban", "ck"}:
        return "kanban"
    if value in {"kanban-followup", "kanban-message", "kanban-inject"}:
        return "kanban-followup"
    if value in {"manual", "prepare", "prepared", "log-only"}:
        return "manual"
    if value == "dry-run":
        return "dry-run"
    return value


def backend_selection_mode_from_fork_mode(fork_mode: str | None) -> str:
    value = (DEFAULT_FORK_LAUNCH_MODE if fork_mode is None else fork_mode).strip().lower()
    return "auto" if value in AUTO_FORK_LAUNCH_MODES else "explicit"


def launch_mode_for_backend(backend: str) -> str:
    if backend == "kanban":
        return "cline-kanban"
    if backend == "gui":
        return "gui"
    return backend


def cline_kanban_failure_allows_legacy_gui_fallback(result: dict[str, Any]) -> bool:
    if result.get("status") not in {"cline-kanban-unavailable", "cline-kanban-unconfigured"}:
        return False
    error = str(result.get("error") or "")
    blocking_fragments = (
        "no listener pane belongs to tmux session `cline-kanban`",
        "Stop the foreign listener",
    )
    return not any(fragment in error for fragment in blocking_fragments)


def should_attempt_legacy_gui_fallback(
    *,
    primary_result: dict[str, Any],
    backend_selection_mode: str | None,
    fallback_enabled: bool,
) -> bool:
    return (
        backend_selection_mode == "auto"
        and fallback_enabled
        and cline_kanban_failure_allows_legacy_gui_fallback(primary_result)
    )
