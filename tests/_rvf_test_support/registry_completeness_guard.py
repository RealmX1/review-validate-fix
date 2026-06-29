"""注册表完整性守卫：消灭「定义了 test_* 却漏登记进注册表 → 静默不跑、CI 仍绿」的假绿陷阱。

本仓库的两个 god test file 用**手写 runner（无 pytest）**，靠文件内一份注册表决定哪些测试会跑：
一个 ``def test_*`` 只有被登记进注册表才执行，漏登记就永远不跑而 CI 照绿（历史反复踩的「假绿」）。

本守卫在每个 god test file 的 ``main()`` 里、**分片过滤之前无条件调用一次**（不注册为普通用例，
否则会被分片掉），把「已定义但未注册」直接变成 ``AssertionError``（红），把沉默失败提到 CI 表面。
经 ``check_skill_contracts.sh`` 每个 shard 子进程各调用一次，因此每个 shard 都重跑、无法被分片绕过。

两个 god test file 用两种注册表形态，各自的 ``registered_names`` 由对应 helper 计算：
- 扁平 ``[func, ...]``（``test_codex_stop_review_validate_fix.py``）→ ``registered_names_from_callables``
- ``[(name, lambda|func), ...]``（``test_review_support_scripts.py``）→ ``registered_names_from_case_tuples``
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


def registered_names_from_callables(funcs: Iterable[Callable[..., Any]]) -> set[str]:
    """扁平注册表：每个条目就是测试函数本身，取其 ``__name__``。"""
    return {fn.__name__ for fn in funcs}


def registered_names_from_case_tuples(
    cases: Iterable[tuple[str, Callable[..., Any]]],
) -> set[str]:
    """``(name, callable)`` 注册表：callable 既可能是 ``lambda: test_x(root / ...)`` 包装，
    也可能是测试函数的**直接引用**。两种形态都要正确归集被注册的 ``test_*`` 名：

    - lambda 包装：lambda 自身 ``__name__`` 是 ``'<lambda>'``，真正被调用的测试名出现在其
      字节码 ``co_names`` 里（lambda 体只做一次具名调用），取 ``test_`` 前缀者即可。
    - 直接函数引用：``__name__`` 即测试名；**不**扫其函数体 ``co_names``——否则会把测试体内
      调用到的其它 ``test_`` 辅助误并入「已注册」，反而放过真正的孤儿（沉默失败方向）。
    """
    registered: set[str] = set()
    for _name, fn in cases:
        fn_name = getattr(fn, "__name__", "")
        if fn_name == "<lambda>":
            registered |= {n for n in fn.__code__.co_names if n.startswith("test_")}
        elif fn_name.startswith("test_"):
            registered.add(fn_name)
    return registered


def assert_every_defined_test_is_registered(
    module_globals: dict[str, Any],
    registered_names: set[str],
    *,
    source_path: str,
    intentionally_unregistered: frozenset[str] = frozenset(),
) -> None:
    """守卫主体：定义了但未注册（且不在豁免名单）的 ``test_*`` 即报错。

    只查 ``defined - registered``（沉默失败方向）；反向「注册了却没 def」会在 runner 取用时
    自然 ``NameError``，无需在此守卫。``intentionally_unregistered`` 是**唯一合法**的豁免出口，
    且额外校验它不含已不存在的名字（防豁免名单腐烂）。
    """
    defined = {
        name
        for name, value in module_globals.items()
        if name.startswith("test_") and callable(value)
    }
    orphans = defined - registered_names - intentionally_unregistered
    if orphans:
        raise AssertionError(
            f"{source_path}: 以下 test_* 已定义但未注册进注册表"
            f"（假绿陷阱：会静默不跑、CI 仍绿）：\n  "
            + "\n  ".join(sorted(orphans))
            + "\n→ 补进注册表；确属暂时隔离的，写入 INTENTIONALLY_UNREGISTERED"
            " 并附 # quarantined 原因。"
        )
    stale = intentionally_unregistered - defined
    if stale:
        raise AssertionError(
            f"{source_path}: INTENTIONALLY_UNREGISTERED 含已不存在的测试名"
            f"（豁免名单腐烂）：\n  "
            + "\n  ".join(sorted(stale))
            + "\n→ 从 INTENTIONALLY_UNREGISTERED 移除这些名字。"
        )
