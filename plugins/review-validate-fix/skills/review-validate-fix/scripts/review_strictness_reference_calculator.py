#!/usr/bin/env python3
"""审查严格度「参考档位」计算器（R13）。

给定一组待审文件的增/删行文本，按 R13 规则算出一个**加权有效改动量**，据此给主
agent 建议一个严格度参考档（``light`` / ``standard`` / ``strict``）。这是 S2「候选摘要」
随注入送达的一个字段：agent 最终仍自行选档（R16 三项有界决策之一），本模块只给**参考**，
不做裁决、不派发、不落任何状态。

R13 规则（逐条，均在 ``compute_strictness_reference`` 内实现，为唯一权威）：

1. **非空行计数**（``count_nonblank_lines``）：仅 ``line.strip()`` 非空的增/删行计入；纯空白
   行（空行、只有空格/制表符的行）一律计 0。避免「大段空行 reformat」把改动量灌水。
2. **删除行降权**（``DELETION_LINE_WEIGHT_FACTOR``）：删除一行的审查成本低于新增一行（删除
   通常是移除既有已审逻辑，风险面小于引入新逻辑），故删除的非空行数乘以 < 1 的降权系数后
   才计入加权有效改动量。
3. **代码 1.0 / 文档 0.3**（``CATEGORY_EFFECTIVE_WEIGHT``）：文档类文件（``.md`` 等）的每行
   审查权重远低于代码；文档的加权有效改动量 = 非空行数 × 0.3。
4. **generated 文件归零**：生成物（lock 文件、``*.min.js``、``dist/`` 等，见
   ``GENERATED_PATH_SUBSTRING_MARKERS`` / ``GENERATED_PATH_SUFFIX_MARKERS``）的改动量一律计 0
   —— 它们不是人写的逻辑，审查它们是浪费预算。
5. **不拆 comment**：本模块**刻意不**识别、不单独降权注释行。注释行与代码行一视同仁按非空行
   计入（``comment_lines_are_not_split_out=True``）。理由：可靠区分「注释 vs 代码 vs 字符串
   里的 ``#``/``//``」需要按语言解析，成本高且易错；而注释本身也是需要审阅的语义（错误的注释
   比没有注释更坏）。R13 明确「不拆 comment」。

另有 R24「超大改动应 elevate」信号：``oversized_change_elevate_recommended`` —— 当**非测试、
非生成物**的原始非空改动行（增+删，不加权、不降权）超过
``OVERSIZED_CHANGE_ELEVATE_NONBLANK_LINE_THRESHOLD``（默认 1000，R24「不含测试 >1000 行」）
时置真，提示 agent：这种体量本应在开工前 elevate 给用户，而非直接 RVF。注意该 elevate 信号
用**原始非空行**（不加权），与档位选择用的**加权有效改动量**是两个不同度量，刻意分开。

档位阈值与降权系数均为**启发式默认**，后续由 R5 遥测数据细化（``rule_parameters`` 里全部
显式化，可被调用方覆盖，便于将来外部调参而不改码）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1

# 规则 2：删除一非空行的加权成本 = 新增一非空行的 1/4。
DELETION_LINE_WEIGHT_FACTOR = 0.25

# 规则 3/4：各类别每非空行的加权系数。generated 归零、文档 0.3、其余（含 test / 配置 / 数据）
# 按代码 1.0 计（test 虽按 1.0 计入加权档位，但在 R24 elevate 信号里被单独排除，见下）。
CATEGORY_CODE = "code"
CATEGORY_TEST = "test"
CATEGORY_DOCUMENTATION = "documentation"
CATEGORY_GENERATED = "generated"

CATEGORY_EFFECTIVE_WEIGHT: dict[str, float] = {
    CATEGORY_CODE: 1.0,
    CATEGORY_TEST: 1.0,
    CATEGORY_DOCUMENTATION: 0.3,
    CATEGORY_GENERATED: 0.0,
}

# 档位阈值（作用于**加权有效改动量**）：< light 上限 → light；< standard 上限 → standard；
# 否则 strict。启发式默认，R5 数据后细化。
LIGHT_TIER_MAX_WEIGHTED_EFFECTIVE_LINES = 40.0
STANDARD_TIER_MAX_WEIGHTED_EFFECTIVE_LINES = 400.0

# R24：非测试非生成物的原始非空改动行超过此阈值 → 建议 elevate（本应开工前上报用户）。
OVERSIZED_CHANGE_ELEVATE_NONBLANK_LINE_THRESHOLD = 1000

STRICTNESS_TIER_LIGHT = "light"
STRICTNESS_TIER_STANDARD = "standard"
STRICTNESS_TIER_STRICT = "strict"

# 规则 4：generated 判定标记。子串命中（路径任意位置）或后缀命中即判 generated。刻意用
# 显式白名单而非「猜」，宁可漏判（当代码审，浪费一点预算）也不误判（当 generated 归零，漏审
# 真实逻辑）。可由调用方在 rule_parameters 里覆盖扩展。
GENERATED_PATH_SUBSTRING_MARKERS: tuple[str, ...] = (
    "/node_modules/",
    "/dist/",
    "/build/",
    "/__pycache__/",
    "/.gitnexus/",
    "/vendor/",
    "/.next/",
    "/coverage/",
)
GENERATED_PATH_SUFFIX_MARKERS: tuple[str, ...] = (
    ".min.js",
    ".min.css",
    ".map",
    ".lock",
    ".gen.go",
    ".pb.go",
    "_pb2.py",
    "_pb2_grpc.py",
    ".snap",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "cargo.lock",
    "poetry.lock",
    "go.sum",
)

# 文档判定：后缀命中即判 documentation（先于 test 判定，见 classify_review_path 的优先级说明）。
DOCUMENTATION_PATH_SUFFIX_MARKERS: tuple[str, ...] = (
    ".md",
    ".mdx",
    ".rst",
    ".txt",
    ".adoc",
)

# test 判定：目录段或文件名模式命中即判 test。
TEST_PATH_SEGMENT_MARKERS: tuple[str, ...] = (
    "tests",
    "test",
    "__tests__",
    "spec",
)


@dataclass(frozen=True)
class FileLineChange:
    """单文件的增/删行文本（去掉了 diff 的 ``+``/``-`` 前缀的裸行内容）。

    ``added_lines`` / ``deleted_lines`` 是原始行文本；非空行计数、加权、分类全部由
    ``compute_strictness_reference`` 承担（R13 单一权威），本结构只搬运数据、不做判断。
    """

    path: str
    added_lines: tuple[str, ...] = ()
    deleted_lines: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, entry: Mapping[str, Any]) -> "FileLineChange":
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("FileLineChange requires a non-empty 'path'")
        added = entry.get("added_lines") or ()
        deleted = entry.get("deleted_lines") or ()
        return cls(
            path=path,
            added_lines=tuple(str(line) for line in added),
            deleted_lines=tuple(str(line) for line in deleted),
        )


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/")


def _path_segments(path: str) -> list[str]:
    return [segment for segment in PurePosixPath(_normalize_path(path)).parts if segment not in ("/", "")]


def _is_generated_path(normalized: str, extra_substrings: tuple[str, ...], extra_suffixes: tuple[str, ...]) -> bool:
    lowered = normalized.lower()
    # 头部也可能命中（如 ``dist/app.js`` 无前导斜杠）——补一个前导斜杠再比子串标记。
    padded = "/" + lowered
    for marker in (*GENERATED_PATH_SUBSTRING_MARKERS, *extra_substrings):
        if marker in padded:
            return True
    for marker in (*GENERATED_PATH_SUFFIX_MARKERS, *extra_suffixes):
        if lowered.endswith(marker):
            return True
    return False


def _is_documentation_path(normalized: str) -> bool:
    lowered = normalized.lower()
    return lowered.endswith(DOCUMENTATION_PATH_SUFFIX_MARKERS)


def _is_test_path(normalized: str) -> bool:
    """test 文件判定。目录段命中（``tests/`` / ``spec/`` / ``__tests__/`` 等），或文件名匹配
    常见测试命名模式：``test_*`` / ``*_test.<ext>`` / ``*Test.java`` / ``*_spec.rb`` /
    ``*.test.ts`` / ``*.spec.js``。用**原始大小写**判 camelCase 的 ``FooTest``，其余按小写比。
    """
    normalized_original_case_name = PurePosixPath(normalized).name
    lowered = normalized.lower()
    lowered_name = PurePosixPath(lowered).name
    segments = set(_path_segments(lowered))
    if segments & set(TEST_PATH_SEGMENT_MARKERS):
        return True
    if lowered_name.startswith(("test_", "test-")):
        return True
    if any(marker in lowered_name for marker in (".test.", ".spec.")):
        return True
    stem = lowered_name.rsplit(".", 1)[0] if "." in lowered_name else lowered_name
    if stem.endswith(("_test", "-test", "_spec", "-spec")):
        return True
    # camelCase ``FooTest`` / ``FooSpec``：需原始大小写，避免把 ``latest`` / ``manifest`` 误判。
    original_stem = (
        normalized_original_case_name.rsplit(".", 1)[0]
        if "." in normalized_original_case_name
        else normalized_original_case_name
    )
    if (original_stem.endswith("Test") or original_stem.endswith("Spec")) and len(original_stem) > 4:
        return True
    return False


def classify_review_path(
    path: str,
    *,
    extra_generated_substrings: tuple[str, ...] = (),
    extra_generated_suffixes: tuple[str, ...] = (),
) -> str:
    """把路径归入 ``generated`` / ``documentation`` / ``test`` / ``code`` 之一。

    **优先级顺序**（先命中先归类，顺序 load-bearing）：
      1. generated —— 生成物即便是 ``.md`` 也应归零，故最高优先。
      2. documentation —— 按后缀判定，无歧义；置于 test 之前，使 ``docs/`` 下或任意 ``.md``
         一律按文档（0.3）计，即便它落在名字含 ``test`` 的目录里。
      3. test —— 目录段 / 文件名模式判定；影响 R24 elevate 排除（不影响加权系数，test 仍按
         代码 1.0）。
      4. code —— 兜底默认（含配置、数据、脚本、真实源码）。
    """
    normalized = _normalize_path(path)
    if _is_generated_path(normalized, extra_generated_substrings, extra_generated_suffixes):
        return CATEGORY_GENERATED
    if _is_documentation_path(normalized):
        return CATEGORY_DOCUMENTATION
    if _is_test_path(normalized):
        return CATEGORY_TEST
    return CATEGORY_CODE


def count_nonblank_lines(lines: Iterable[str]) -> tuple[int, int]:
    """返回 ``(非空行数, 空白行数)``。空白 = ``str.strip()`` 为空（规则 1）。"""
    nonblank = 0
    blank = 0
    for line in lines:
        if str(line).strip():
            nonblank += 1
        else:
            blank += 1
    return nonblank, blank


def _default_rule_parameters() -> dict[str, Any]:
    return {
        "deletion_line_weight_factor": DELETION_LINE_WEIGHT_FACTOR,
        "category_effective_weight": dict(CATEGORY_EFFECTIVE_WEIGHT),
        "light_tier_max_weighted_effective_lines": LIGHT_TIER_MAX_WEIGHTED_EFFECTIVE_LINES,
        "standard_tier_max_weighted_effective_lines": STANDARD_TIER_MAX_WEIGHTED_EFFECTIVE_LINES,
        "oversized_change_elevate_nonblank_line_threshold": OVERSIZED_CHANGE_ELEVATE_NONBLANK_LINE_THRESHOLD,
        "comment_lines_are_not_split_out": True,
        "extra_generated_path_substring_markers": [],
        "extra_generated_path_suffix_markers": [],
    }


def _merge_rule_parameters(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    params = _default_rule_parameters()
    if not overrides:
        return params
    for key, value in overrides.items():
        if key == "category_effective_weight" and isinstance(value, Mapping):
            merged = dict(params["category_effective_weight"])
            merged.update(value)
            params[key] = merged
        else:
            params[key] = value
    return params


def _suggest_tier(weighted_effective_lines: float, params: Mapping[str, Any]) -> str:
    light_max = float(params["light_tier_max_weighted_effective_lines"])
    standard_max = float(params["standard_tier_max_weighted_effective_lines"])
    if weighted_effective_lines < light_max:
        return STRICTNESS_TIER_LIGHT
    if weighted_effective_lines < standard_max:
        return STRICTNESS_TIER_STANDARD
    return STRICTNESS_TIER_STRICT


def compute_strictness_reference(
    file_changes: Iterable[FileLineChange | Mapping[str, Any]],
    *,
    rule_parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按 R13 规则算加权有效改动量，返回结构化参考档建议（见模块 docstring 的字段说明）。

    输入：每文件的增/删行文本（``FileLineChange`` 或等价 mapping）。空输入 → 加权量 0 →
    档位 ``light``、不建议 elevate（稳定的空态，供「候选非空但全 generated」等边界复用）。
    """
    params = _merge_rule_parameters(rule_parameters)
    deletion_factor = float(params["deletion_line_weight_factor"])
    category_weight: Mapping[str, float] = params["category_effective_weight"]
    extra_gen_sub = tuple(params.get("extra_generated_path_substring_markers") or ())
    extra_gen_suf = tuple(params.get("extra_generated_path_suffix_markers") or ())

    per_file: list[dict[str, Any]] = []
    total_nonblank_added = 0
    total_nonblank_deleted = 0
    total_blank_ignored = 0
    total_weighted_effective = 0.0
    weighted_by_category: dict[str, float] = {}
    elevate_nonblank_excluding_tests_and_generated = 0

    for raw in file_changes:
        change = raw if isinstance(raw, FileLineChange) else FileLineChange.from_mapping(raw)
        category = classify_review_path(
            change.path,
            extra_generated_substrings=extra_gen_sub,
            extra_generated_suffixes=extra_gen_suf,
        )
        nonblank_added, blank_added = count_nonblank_lines(change.added_lines)
        nonblank_deleted, blank_deleted = count_nonblank_lines(change.deleted_lines)
        weight = float(category_weight.get(category, 1.0))
        generated_zeroed = category == CATEGORY_GENERATED
        weighted_effective = weight * (nonblank_added + deletion_factor * nonblank_deleted)

        per_file.append(
            {
                "path": change.path,
                "category": category,
                "nonblank_added_lines": nonblank_added,
                "nonblank_deleted_lines": nonblank_deleted,
                "blank_lines_ignored": blank_added + blank_deleted,
                "category_effective_weight": weight,
                "weighted_effective_lines": round(weighted_effective, 4),
                "generated_zeroed": generated_zeroed,
            }
        )

        total_nonblank_added += nonblank_added
        total_nonblank_deleted += nonblank_deleted
        total_blank_ignored += blank_added + blank_deleted
        total_weighted_effective += weighted_effective
        weighted_by_category[category] = round(
            weighted_by_category.get(category, 0.0) + weighted_effective, 4
        )
        if category not in (CATEGORY_TEST, CATEGORY_GENERATED):
            elevate_nonblank_excluding_tests_and_generated += nonblank_added + nonblank_deleted

    total_weighted_effective = round(total_weighted_effective, 4)
    suggested_tier = _suggest_tier(total_weighted_effective, params)
    elevate_threshold = int(params["oversized_change_elevate_nonblank_line_threshold"])
    elevate_recommended = elevate_nonblank_excluding_tests_and_generated > elevate_threshold

    return {
        "schema_version": SCHEMA_VERSION,
        "rule_parameters": params,
        "per_file": per_file,
        "totals": {
            "file_count": len(per_file),
            "nonblank_added_lines": total_nonblank_added,
            "nonblank_deleted_lines": total_nonblank_deleted,
            "blank_lines_ignored": total_blank_ignored,
            "weighted_effective_lines": total_weighted_effective,
            "weighted_effective_lines_by_category": weighted_by_category,
            "nonblank_change_lines_excluding_tests_and_generated": elevate_nonblank_excluding_tests_and_generated,
        },
        "suggested_strictness_tier": suggested_tier,
        "oversized_change_elevate_recommended": elevate_recommended,
    }


__all__ = [
    "SCHEMA_VERSION",
    "DELETION_LINE_WEIGHT_FACTOR",
    "CATEGORY_EFFECTIVE_WEIGHT",
    "CATEGORY_CODE",
    "CATEGORY_TEST",
    "CATEGORY_DOCUMENTATION",
    "CATEGORY_GENERATED",
    "STRICTNESS_TIER_LIGHT",
    "STRICTNESS_TIER_STANDARD",
    "STRICTNESS_TIER_STRICT",
    "OVERSIZED_CHANGE_ELEVATE_NONBLANK_LINE_THRESHOLD",
    "FileLineChange",
    "classify_review_path",
    "count_nonblank_lines",
    "compute_strictness_reference",
]
