#!/usr/bin/env python3
"""断言「host 身份词表」与「reviewer-registry harness id 词表」是同一套词。

**为什么需要这条 gate（P-5，2026-08-23 由 Kimi host 切片的 RVF 评审实测暴露）**

RVF 里有两套独立演化、从无任何约束互相咬合的词表：

1. **host 身份**——``core/host_adapter/host_transcript_format_detection.py`` 的
   ``KNOWN_HOST_IDENTITIES``，由 transcript record 签名探测得出，回答「当前会话跑在哪个 host 上」。
2. **reviewer-registry harness id**——``config/reviewer-registry.json`` 的 ``harnesses`` 键，
   回答「哪些 harness 可以被派成评审腿」。

``prepare_review_run`` 把 ①的探测值直接当作 ``main_harness`` 去和 ②的池子比对，用来把
**主 harness 排除出 alternative reviewer 池**——双评审的全部价值建立在这条异构性上。

两套词表前两项（``codex`` / ``claude_code``）历史上恰好同名，**纯属巧合**，所以这条缺陷
至今从未暴露。Kimi 接入时 host 身份取了 ``kimi_code_cli`` 而 registry 早已是 ``kimi_code``，
巧合断裂：200-seed 实测 ⇒ Kimi 会话有 **88/200 = 44%** 概率被 Kimi 自己评审，
且**全程零报错**——比对不上就等于「主 harness 不在池里」，排除逻辑静默空转。

本 gate 把这种静默不一致变成部署前的响亮失败。每加一个 host 就多一次踩中的机会，
所以判据必须是结构性的（遍历 ``KNOWN_HOST_IDENTITIES``），而不是逐个 host 硬编码。

**方向是单向的**：每个 host 身份都必须在 registry 里有对应 harness；反向不要求——
``cursor`` 是 reviewer-only（没有 cursor host adapter，transcript 探测永不返回它），
``oh_my_pi`` 在其 host 适配落地前也处于同一状态。

**检的是仓库内那份 seed registry**，不是运行时 user-local 的那份
（``<user_local_data_root>/reviewer-configuration/reviewer-registry.json`` 才是运行时真相源，
见计划 P-3）。这是刻意的：gate 是**仓库契约**检查，管的是「源码里两套词表是否自洽」，
不是「本机部署状态是否正确」——后者归 ``verify_local_deploy.py``。

这条「只读仓库」纪律**对三份清单都成立**，包括经 ``prepare_review_run`` 间接读到的第三份：
它的 ``VALID_MAIN_HARNESSES`` 是 import 期从 ``resolved_rvf_reviewer_registry_path`` 求值的，
而该 resolver 在 user-local registry 存在时**优先用运行时那一份**。所以
``load_valid_main_harnesses`` 必须先把 ``RVF_USER_LOCAL_DATA_ROOT`` 指向一个空目录再 import，
否则这条仓库契约 gate 会去读本机部署状态——仓库完全正确、只是本机 registry 落后一个 host，
gate 就退出 1，并造出「必须先部署刷新 user-local registry，提交前 gate 才转绿」的循环依赖。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
REVIEWER_REGISTRY_SEED_RELATIVE_PATH = (
    "plugins/review-validate-fix/skills/review-validate-fix/config/reviewer-registry.json"
)
PREPARE_REVIEW_RUN_RELATIVE_PATH = (
    "plugins/review-validate-fix/skills/review-validate-fix/scripts/prepare_review_run.py"
)

# reviewer-only harness：存在于 registry、但**不是** host（没有对应 host adapter，
# transcript 探测永不返回它）。它们只出现在反方向的差集里，不构成违规。
# 这里不需要维护清单——本 gate 只做单向断言，该常量仅供报告可读性使用。
REVIEWER_ONLY_HARNESS_IDS_FOR_REPORTING = frozenset({"cursor"})


def load_known_host_identities() -> frozenset[str]:
    """从 core 的 host 身份真相源读取全部可被正向识别的 host 身份。"""
    if str(REPOSITORY_ROOT) not in sys.path:
        sys.path.insert(0, str(REPOSITORY_ROOT))
    from core.host_adapter.host_transcript_format_detection import (  # noqa: E402
        KNOWN_HOST_IDENTITIES,
    )

    return frozenset(KNOWN_HOST_IDENTITIES)


def load_reviewer_registry_harness_ids() -> frozenset[str]:
    """从仓库内 seed registry 读取全部 harness id。

    同时校验每条 harness 的 ``harness_id`` 字段与其在 ``harnesses`` 里的键一致——
    键与字段漂移本身就是同一类「两处写同一个名字」缺陷。
    """
    registry_path = REPOSITORY_ROOT / REVIEWER_REGISTRY_SEED_RELATIVE_PATH
    registry_document = json.loads(registry_path.read_text(encoding="utf-8"))
    harnesses = registry_document["harnesses"]
    harness_ids: set[str] = set()
    for harness_key, harness_entry in harnesses.items():
        declared_harness_id = harness_entry.get("harness_id")
        if declared_harness_id != harness_key:
            raise SystemExit(
                "REVIEWER_REGISTRY_HARNESS_ID_KEY_MISMATCH "
                f"key={harness_key!r} harness_id={declared_harness_id!r} "
                f"path={REVIEWER_REGISTRY_SEED_RELATIVE_PATH}"
            )
        harness_ids.add(harness_key)
    return frozenset(harness_ids)


def load_valid_main_harnesses() -> frozenset[str]:
    """读取 ``prepare_review_run`` 认可的 main harness 集合——**只按仓库内 seed registry 求值**。

    这是同一套词表的**第三份**清单（前两份是 host 身份与 registry harness id）。
    它决定显式 ``RVF_MAIN_HARNESS=<x>`` 会不会被静默丢弃，所以必须覆盖全部 host 身份。

    ``VALID_MAIN_HARNESSES`` 在 import 期求值，且经 ``resolved_rvf_reviewer_registry_path``
    优先读**运行时** user-local registry。裸 import 会让这条仓库契约 gate 变成本机部署状态
    检查（见模块 docstring）。所以这里先把 ``RVF_USER_LOCAL_DATA_ROOT`` 指到一个空临时目录，
    逼 resolver 回落到仓库内 seed，再 import。

    **仍然读模块常量、而不是重新调 ``_resolve_valid_main_harnesses()``**：``prepare_review_run``
    校验 ``RVF_MAIN_HARNESS`` 覆盖时消费的是那个**常量**，「把常量改回手写字面量」正是这条断言
    要抓的漂移；改成调函数会让 gate 对该漂移失明——那就是本仓反复栽过的假绿形状。
    """
    if str(REPOSITORY_ROOT) not in sys.path:
        sys.path.insert(0, str(REPOSITORY_ROOT))
    prepare_review_run_directory = str(
        (REPOSITORY_ROOT / PREPARE_REVIEW_RUN_RELATIVE_PATH).parent
    )
    if prepare_review_run_directory not in sys.path:
        sys.path.insert(0, prepare_review_run_directory)

    from core.rvf_user_local_data_paths import (  # noqa: E402
        RVF_USER_LOCAL_DATA_ROOT_ENVIRONMENT_VARIABLE,
        resolved_rvf_reviewer_registry_path,
    )

    bundled_reviewer_registry_path = REPOSITORY_ROOT / REVIEWER_REGISTRY_SEED_RELATIVE_PATH
    previous_user_local_data_root = os.environ.get(
        RVF_USER_LOCAL_DATA_ROOT_ENVIRONMENT_VARIABLE
    )
    # 本进程可能已经 import 过 prepare_review_run（那份常量按真实 env 求过值了）。弹掉重 import，
    # 保证读到的是隔离环境下的值；结束后把原模块对象放回去，不影响别的调用方。
    previously_imported_prepare_review_run = sys.modules.pop("prepare_review_run", None)
    try:
        with tempfile.TemporaryDirectory(
            prefix="rvf-repository-contract-gate-empty-user-local-data-root-"
        ) as empty_user_local_data_root:
            os.environ[RVF_USER_LOCAL_DATA_ROOT_ENVIRONMENT_VARIABLE] = (
                empty_user_local_data_root
            )
            # 隔离没生效 == 又在读运行时状态。让它当场炸，而不是静默退回旧行为。
            resolved_registry_path = resolved_rvf_reviewer_registry_path(
                bundled_reviewer_registry_path
            )
            if resolved_registry_path != bundled_reviewer_registry_path:
                raise SystemExit(
                    "REPOSITORY_SEED_REGISTRY_ISOLATION_FAILED "
                    f"env={RVF_USER_LOCAL_DATA_ROOT_ENVIRONMENT_VARIABLE} "
                    f"resolved={resolved_registry_path} "
                    f"expected_repository_seed={bundled_reviewer_registry_path}\n"
                    "    本 gate 是仓库契约检查，不得读运行时 user-local registry。"
                )
            import prepare_review_run  # noqa: E402

            return frozenset(prepare_review_run.VALID_MAIN_HARNESSES)
    finally:
        sys.modules.pop("prepare_review_run", None)
        if previously_imported_prepare_review_run is not None:
            sys.modules["prepare_review_run"] = previously_imported_prepare_review_run
        if previous_user_local_data_root is None:
            os.environ.pop(RVF_USER_LOCAL_DATA_ROOT_ENVIRONMENT_VARIABLE, None)
        else:
            os.environ[RVF_USER_LOCAL_DATA_ROOT_ENVIRONMENT_VARIABLE] = (
                previous_user_local_data_root
            )


def main() -> int:
    known_host_identities = load_known_host_identities()
    reviewer_registry_harness_ids = load_reviewer_registry_harness_ids()
    valid_main_harnesses = load_valid_main_harnesses()

    violations: list[str] = []

    host_identities_absent_from_registry = sorted(
        known_host_identities - reviewer_registry_harness_ids
    )
    if host_identities_absent_from_registry:
        violations.append(
            "HOST_IDENTITY_NOT_IN_REVIEWER_REGISTRY "
            f"missing={host_identities_absent_from_registry} "
            f"host_identities={sorted(known_host_identities)} "
            f"registry_harness_ids={sorted(reviewer_registry_harness_ids)}\n"
            "    后果：该 host 的会话其 main_harness 与池中 harness id 对不上 ⇒ 主 harness\n"
            "    不会被排除出 alternative reviewer 池 ⇒ 会话有相当概率被自己评审，且零报错。\n"
            "    修法：把 host 身份常量的**取值**改成与 registry harness id 一致\n"
            f"    （改 core/host_adapter/host_transcript_format_detection.py 里的常量值，\n"
            f"    不是改 {REVIEWER_REGISTRY_SEED_RELATIVE_PATH}——registry id 同时是\n"
            "    config 文件名、额度池与 gate 字面量的锚，迁移成本高得多）。"
        )

    host_identities_absent_from_valid_main_harnesses = sorted(
        known_host_identities - valid_main_harnesses
    )
    if host_identities_absent_from_valid_main_harnesses:
        violations.append(
            "HOST_IDENTITY_NOT_IN_VALID_MAIN_HARNESSES "
            f"missing={host_identities_absent_from_valid_main_harnesses} "
            f"valid_main_harnesses={sorted(valid_main_harnesses)}\n"
            "    后果：显式 RVF_MAIN_HARNESS=<该 host> 被静默丢弃、回落成探测值或默认 codex。\n"
            f"    修法：让 {PREPARE_REVIEW_RUN_RELATIVE_PATH} 的 VALID_MAIN_HARNESSES\n"
            "    从 registry 派生，而不是手写第三份清单。\n"
            "    注意：本条按**仓库内 seed registry** 求值，与本机 user-local registry 无关\n"
            "    ——不需要先部署就能让它转绿；反过来说，它红了就一定是源码里的问题。"
        )

    if violations:
        print(
            "HOST_IDENTITY_REVIEWER_REGISTRY_VOCABULARY_CONSISTENCY=FAILED", file=sys.stderr
        )
        for violation in violations:
            print(f"  ✗ {violation}", file=sys.stderr)
        return 1

    reviewer_only_harness_ids = sorted(
        reviewer_registry_harness_ids - known_host_identities
    )
    print("HOST_IDENTITY_REVIEWER_REGISTRY_VOCABULARY_CONSISTENCY=OK")
    print(f"  host_identities={sorted(known_host_identities)}")
    print(f"  registry_harness_ids={sorted(reviewer_registry_harness_ids)}")
    print(f"  reviewer_only_harness_ids={reviewer_only_harness_ids} (单向断言，不要求它们是 host)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
