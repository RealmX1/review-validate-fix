"""``check_host_identity_and_reviewer_registry_harness_id_vocabulary_consistency.py`` 的行为闸。

这条 gate 有两条断言，本文件钉的是**第二条**（``HOST_IDENTITY_NOT_IN_VALID_MAIN_HARNESSES``）
必须同时满足的两个互相拉扯的性质：

1. **它是仓库契约检查，不是本机部署状态检查。** 它读到的 ``VALID_MAIN_HARNESSES`` 必须按
   仓库内 seed registry 求值。曾经的实现裸 import ``prepare_review_run``，而那个常量在
   import 期经 ``resolved_rvf_reviewer_registry_path`` **优先读运行时** user-local registry
   ⇒ 仓库完全正确、只是本机 registry 落后一个 host，提交前 gate 就退出 1，还造出
   「必须先部署才能让 gate 转绿」的循环依赖。
2. **它不能因此退化成结构性恒真。** 这条断言存在的全部理由是：``VALID_MAIN_HARNESSES``
   一旦被改回手写清单、漏掉某个 host，该 host 的显式 ``RVF_MAIN_HARNESS`` 会被**静默丢弃**。
   所以第二个测试用一个「假仓库根 + 手写清单」证明检测力还在——没有它，第一个测试就只是
   在给一块可能已经恒绿的表拍照。

⚠️ 全程用隔离的 ``RVF_USER_LOCAL_DATA_ROOT``，绝不碰真实 ``~/.rvf``（live infra）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
VOCABULARY_GATE = (
    REPOSITORY_ROOT
    / "scripts/check_host_identity_and_reviewer_registry_harness_id_vocabulary_consistency.py"
)
REVIEWER_REGISTRY_SEED = (
    REPOSITORY_ROOT
    / "plugins/review-validate-fix/skills/review-validate-fix/config/reviewer-registry.json"
)
PREPARE_REVIEW_RUN_RELATIVE_PATH = Path(
    "plugins/review-validate-fix/skills/review-validate-fix/scripts/prepare_review_run.py"
)


def _user_local_data_root_whose_registry_drops(tmp_path: Path, harness_id: str) -> Path:
    """造一份**运行时** registry，故意少一个 host —— 就是「本机部署落后仓库一个 host」的形状。"""
    user_local_data_root = tmp_path / "isolated-user-local-data"
    reviewer_configuration_root = user_local_data_root / "reviewer-configuration"
    reviewer_configuration_root.mkdir(parents=True)
    registry_document = json.loads(REVIEWER_REGISTRY_SEED.read_text(encoding="utf-8"))
    removed = registry_document["harnesses"].pop(harness_id, None)
    assert removed is not None, f"seed registry 里没有 {harness_id}，本测试的前提已失效"
    (reviewer_configuration_root / "reviewer-registry.json").write_text(
        json.dumps(registry_document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return user_local_data_root


def _fake_repository_root_with_hand_written_valid_main_harnesses(
    tmp_path: Path, hand_written_harness_ids: list[str]
) -> Path:
    """造一个只含 gate 所需三样东西的假仓库根，并把 ``prepare_review_run`` 换成手写清单桩。

    ``core/`` 用 symlink 复用真实实现（``KNOWN_HOST_IDENTITIES`` 必须是真的，否则测的就不是
    真判据）；registry seed 整份拷贝；只有 ``prepare_review_run`` 是桩——这正是要模拟的漂移点。
    gate 自身是**拷贝**而非 symlink，因为它用 ``Path(__file__).resolve().parent.parent`` 定位
    仓库根，symlink 会被 ``resolve()`` 追回真仓库。
    """
    fake_repository_root = tmp_path / "fake-repository-root"
    (fake_repository_root / "scripts").mkdir(parents=True)
    (fake_repository_root / "core").symlink_to(
        REPOSITORY_ROOT / "core", target_is_directory=True
    )
    (fake_repository_root / VOCABULARY_GATE.relative_to(REPOSITORY_ROOT)).write_text(
        VOCABULARY_GATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    seed_destination = fake_repository_root / REVIEWER_REGISTRY_SEED.relative_to(
        REPOSITORY_ROOT
    )
    seed_destination.parent.mkdir(parents=True)
    seed_destination.write_text(
        REVIEWER_REGISTRY_SEED.read_text(encoding="utf-8"), encoding="utf-8"
    )
    prepare_review_run_stub = fake_repository_root / PREPARE_REVIEW_RUN_RELATIVE_PATH
    prepare_review_run_stub.parent.mkdir(parents=True)
    prepare_review_run_stub.write_text(
        "# 桩：模拟「VALID_MAIN_HARNESSES 被改回手写清单」这一漂移。\n"
        f"VALID_MAIN_HARNESSES = frozenset({sorted(hand_written_harness_ids)!r})\n",
        encoding="utf-8",
    )
    return fake_repository_root


def _run_gate(gate_path: Path, *, user_local_data_root: Path | None = None):
    environment_overrides = {}
    if user_local_data_root is not None:
        environment_overrides["RVF_USER_LOCAL_DATA_ROOT"] = str(user_local_data_root)

    return subprocess.run(
        [sys.executable, str(gate_path)],
        capture_output=True,
        text=True,
        env={**os.environ, **environment_overrides},
    )


def test_gate_is_a_repository_contract_check_not_a_local_deployment_state_check(
    tmp_path: Path,
) -> None:
    """仓库完全正确时，运行时 registry 落后一个 host **不得**让提交前 gate 变红。"""
    user_local_data_root = _user_local_data_root_whose_registry_drops(
        tmp_path, "kimi_code"
    )

    completed = _run_gate(VOCABULARY_GATE, user_local_data_root=user_local_data_root)

    assert completed.returncode == 0, (
        "gate 读了运行时 user-local registry ⇒ 「必须先部署才能让提交前 gate 转绿」的循环依赖\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    assert "VOCABULARY_CONSISTENCY=OK" in completed.stdout, completed.stdout


def test_gate_still_catches_a_hand_written_valid_main_harnesses_list(
    tmp_path: Path,
) -> None:
    """检测力必须还在：手写清单漏掉一个 host ⇒ 当场红。

    没有这条，上一条就可能是在给一块已经恒绿的表拍照。
    """
    fake_repository_root = _fake_repository_root_with_hand_written_valid_main_harnesses(
        tmp_path, ["claude_code", "codex", "cursor"]
    )

    completed = _run_gate(
        fake_repository_root / VOCABULARY_GATE.relative_to(REPOSITORY_ROOT)
    )

    assert completed.returncode == 1, f"{completed.stdout}\n{completed.stderr}"
    assert "HOST_IDENTITY_NOT_IN_VALID_MAIN_HARNESSES" in completed.stderr, (
        completed.stderr
    )
    assert "kimi_code" in completed.stderr, completed.stderr


def test_fake_repository_root_scaffolding_is_green_when_the_list_is_complete(
    tmp_path: Path,
) -> None:
    """脚手架对照组：上一条的红必须来自漏掉的 host，而不是假仓库根本身跑不起来。"""
    fake_repository_root = _fake_repository_root_with_hand_written_valid_main_harnesses(
        tmp_path, ["claude_code", "codex", "cursor", "kimi_code", "oh_my_pi"]
    )

    completed = _run_gate(
        fake_repository_root / VOCABULARY_GATE.relative_to(REPOSITORY_ROOT)
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"


def test_loader_restores_the_caller_environment_and_derives_from_the_repository_seed(
    tmp_path: Path,
) -> None:
    """隔离是**临时**的：``load_valid_main_harnesses`` 不得把 env 改动泄漏给调用方。

    gate 靠改 ``RVF_USER_LOCAL_DATA_ROOT`` 逼 resolver 回落到 seed；若忘了还原，同进程里
    后续读该变量的代码就会被静默改道。用子进程跑，避免污染 pytest 进程自身。
    """
    user_local_data_root = _user_local_data_root_whose_registry_drops(
        tmp_path, "kimi_code"
    )
    driver = (
        "import importlib.util, json, os, sys\n"
        f"spec = importlib.util.spec_from_file_location('rvf_vocabulary_gate', {str(VOCABULARY_GATE)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "before = os.environ.get('RVF_USER_LOCAL_DATA_ROOT')\n"
        "harnesses = sorted(module.load_valid_main_harnesses())\n"
        "after = os.environ.get('RVF_USER_LOCAL_DATA_ROOT')\n"
        "print(json.dumps({'harnesses': harnesses, 'before': before, 'after': after}))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True,
        text=True,
        env={**os.environ, "RVF_USER_LOCAL_DATA_ROOT": str(user_local_data_root)},
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    observed = json.loads(completed.stdout)
    assert observed["before"] == str(user_local_data_root), observed
    assert observed["after"] == str(user_local_data_root), (
        f"隔离用的 env 改动泄漏给了调用方：{observed}"
    )
    assert "kimi_code" in observed["harnesses"], (
        f"没有按仓库内 seed registry 求值：{observed}"
    )


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
