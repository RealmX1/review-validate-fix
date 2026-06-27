#!/usr/bin/env python3
"""RVF env-namespace 碰撞 gate（去-codex 重构 · S8.0）。

去-codex env 改名把中性的 ``CODEX_RVF_<BASE>`` 迁到 ``RVF_<BASE>``、真 codex
概念迁到 ``RVF_CODEX_<...>``。最大的隐患是**半改名**：某个 base 的部分读/写点
已改 ``RVF_<BASE>``、另一部分仍读 ``CODEX_RVF_<BASE>`` —— 运行时该变量分裂成
两个，行为静默损坏（既无语法错也无 import 错，测试若没覆盖该路径就假绿）。

本 gate 扫描全部 ``*.py`` / ``*.sh`` / ``*.json`` 源文件中**真正的 env-var 访问
点**（os.environ / getenv / env[...] / shell export / hooks.json 烘焙 / 契约字面
量），按 base 名归一，若同一 base 同时出现在 ``CODEX_RVF_`` 与裸 ``RVF_`` 两个
命名空间 → 判为碰撞并失败。

ALLOWLIST 收录「正在原子坍缩、过渡期允许双名共存」的 base（当前仅 RUN_DIR /
RUN_ID / CORRELATION_ID —— RunLedger 的裸名镜像，S8 坍缩后从本表删除）。

只扫描代码/配置（py/sh/json），不扫 markdown：env 在代码里访问，文档里只是叙述，
扫 md 会把 ``RVF_PARENT_CONVERSATION_REF`` 这类 prompt 字段误判为 env。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# 过渡期允许 CODEX_RVF_<base> 与 RVF_<base> 双名共存的 base（S8 坍缩后清空）。
ALLOWLIST: set[str] = {"RUN_DIR", "RUN_ID", "CORRELATION_ID"}

# env-var token：CODEX_RVF_<X> 或裸 RVF_<X>。捕获是否带 CODEX_ 前缀 + base 名。
_TOKEN_RE = re.compile(r"\b(CODEX_)?(RVF_[A-Z0-9_]+)\b")

# 仅当 token 出现在以下「env-访问形态」之一时才计入（排除 prompt 字段名等非 env 字符串）：
#  - Python: os.environ.get/[/setdefault、getenv、env/hook_env.get/[/setdefault、pop
#  - shell : NAME= 赋值/export、$NAME、${NAME}
#  - json/installer 烘焙: "NAME=..."（env_parts 条目）、"NAME": （键）
_ENV_ACCESS_RE = re.compile(
    r"""
    (?: environ \s* (?:\.\s*(?:get|setdefault|pop)\s*\(|\[) ) |   # os.environ.get( / [
    (?: getenv \s* \( ) |                                          # getenv(
    (?: \benv \s* (?:\.\s*(?:get|setdefault|pop)\s*\(|\[) ) |      # env.get( / env[
    (?: \bhook_env \s* (?:\.\s*get\s*\(|\[) ) |                    # hook_env.get( / [
    (?: ^\s*export\s+ ) |                                          # shell export NAME=
    (?: \$\{? ) |                                                  # $NAME / ${NAME}
    (?: ["'][A-Z0-9_]*=(?!=) ) |                                   # "NAME=value"（env_parts / shell 赋值字面量）
    (?: \b[A-Z_][A-Z0-9_]*= )                                      # 裸 NAME=value（shell）
    """,
    re.VERBOSE,
)


def base_names_by_namespace(text: str) -> tuple[set[str], set[str]]:
    """返回 (codex_prefixed_bases, bare_rvf_bases)。只统计 env-访问形态所在行。"""
    codex_bases: set[str] = set()
    bare_bases: set[str] = set()
    for line in text.splitlines():
        if not _ENV_ACCESS_RE.search(line):
            continue
        for prefix, name in _TOKEN_RE.findall(line):
            base = name[len("RVF_"):]  # name 形如 RVF_<BASE>
            if not base:
                continue
            if prefix:  # CODEX_RVF_<base>
                codex_bases.add(base)
            else:  # RVF_<base>
                bare_bases.add(base)
    return codex_bases, bare_bases


def scan_repo(repo_root: Path) -> dict[str, tuple[set[str], set[str]]]:
    """对每个被 git 跟踪的 py/sh/json 文件做 namespace 统计，返回 base -> (codex_files, bare_files)。"""
    tracked = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "*.py", "*.sh", "*.json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    # 跳过 gate 自身：它的 _self_test() 含 env-访问形态的样本字面量（CODEX_RVF_FOO /
    # RVF_FOO / export RVF_STATE_DIR 等），是检测器的测试夹具而非真实 env 访问；自扫会
    # 把这些样本误判成跨命名空间碰撞（gate 提交、被 git 跟踪后才发生）。
    self_path = Path(__file__).resolve()
    per_base: dict[str, tuple[set[str], set[str]]] = {}
    for rel in tracked:
        path = repo_root / rel
        if path.resolve() == self_path:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        codex_bases, bare_bases = base_names_by_namespace(text)
        for base in codex_bases:
            entry = per_base.setdefault(base, (set(), set()))
            entry[0].add(rel)
        for base in bare_bases:
            entry = per_base.setdefault(base, (set(), set()))
            entry[1].add(rel)
    return per_base


def find_collisions(per_base: dict[str, tuple[set[str], set[str]]]) -> dict[str, tuple[set[str], set[str]]]:
    return {
        base: files
        for base, (codex_files, bare_files) in per_base.items()
        if codex_files and bare_files and base not in ALLOWLIST
        for files in [(codex_files, bare_files)]
    }


def _self_test() -> None:
    # 半改名：同一 base FOO 一处 CODEX_RVF_FOO、一处裸 RVF_FOO，均在 env-访问形态 → 碰撞
    sample = 'a = os.environ.get("CODEX_RVF_FOO")\nb = os.environ.get("RVF_FOO")\n'
    codex, bare = base_names_by_namespace(sample)
    assert "FOO" in codex and "FOO" in bare, (codex, bare)
    # prompt 字段名（非 env 形态）不计入
    prose = 'lines.append("RVF_PARENT_CONVERSATION_REF: %s" % ref)\n'
    codex2, bare2 = base_names_by_namespace(prose)
    assert "PARENT_CONVERSATION_REF" not in bare2, bare2
    # 纯 codex 命名空间、无裸名 → 不算碰撞
    only_codex = 'x = os.environ.get("CODEX_RVF_MODE")\n'
    c3, b3 = base_names_by_namespace(only_codex)
    assert "MODE" in c3 and "MODE" not in b3
    # shell 赋值 + 引用
    sh = 'export RVF_STATE_DIR=/tmp\necho "$CODEX_RVF_STATE_DIR"\n'
    c4, b4 = base_names_by_namespace(sh)
    assert "STATE_DIR" in b4 and "STATE_DIR" in c4
    print("self-test OK")


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        _self_test()
        return 0
    repo_root = Path(__file__).resolve().parent.parent
    per_base = scan_repo(repo_root)
    collisions = find_collisions(per_base)
    if collisions:
        print("env-namespace 碰撞（同一 base 同时存在 CODEX_RVF_ 与裸 RVF_ env 访问）：", file=sys.stderr)
        for base in sorted(collisions):
            codex_files, bare_files = collisions[base]
            print(f"  {base}:", file=sys.stderr)
            print(f"    CODEX_RVF_{base} @ {sorted(codex_files)}", file=sys.stderr)
            print(f"    RVF_{base}       @ {sorted(bare_files)}", file=sys.stderr)
        print(
            "→ 半改名或命名碰撞。改名必须 base 原子（一个 base 只存活于一个命名空间）；"
            "若属过渡期坍缩，临时加入 ALLOWLIST 并在收尾切片删除。",
            file=sys.stderr,
        )
        return 1
    print(f"env-namespace collision gate OK（扫描 {len(per_base)} 个 RVF env base，0 碰撞，allowlist={sorted(ALLOWLIST)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
