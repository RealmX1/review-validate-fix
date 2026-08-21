"""让 RVF 的 hook 命令永不以「退出码 2 + 非空 stderr」结束，而不是把 agent 驱动起来。

## 为什么必须活在命令串里

宿主在**会话启动时**把 hook 注册快照进内存；此后磁盘上的插件 payload 被删除或换版本
目录，正在运行的会话仍会去 spawn 那条已经失效的绝对路径。此时 ``python3`` 打不开脚本，
**以退出码 2 结束并把 ``[Errno 2] No such file or directory`` 写到 stderr**。

而两个宿主都把「Stop hook 退出码 2 且 stderr 非空」硬编码成**驱动 agent 再跑一回合**：

- Codex v0.147.0：stderr 逐字包进 ``<hook_prompt>``，作为一条 ``role:"user"`` 消息注入。
  实测 286 次、同一个 ``turn_id``、持续 2h37m，**没有任何自然上界**，最后由人工 kill。
- Claude Code 2.1.235 内嵌 hook 契约原文：
  ``Stop → Exit code 2 - show stderr to model and continue conversation``。

RVF 自己的全部保护（``is_foreign_invocation``、受众契约翻译层、续跑预算）都写在
``run_claude_hook`` 内部，也就是**这个失败的下游**——文件打不开，它们一行都跑不到。
所以守卫只能放在**命令串本身**里：命令串是宿主快照的一部分，磁盘被掏空也照样存在。

## 危险条件精确到一种

实测（Codex 二进制内嵌模板 + 全量 rollout 样本 + Claude 二进制内嵌 schema）：只有
**「退出码 2 且 stderr 非空」**会驱动 agent。退出码 2 但 stderr 为空、非法 JSON、
超时、其它任何非零退出码（含 ``python3`` 不存在的 127）、以及退出码 0 带 stderr，
**都不会**。而 ``run_claude_hook`` 的每一条 return 都是 ``return 0``——RVF 的 hook 入口
**从不**故意使用非零退出码，所以「强制这条命令永不以 2 退出」是零信号损失的改动。

## 为什么「存在性检查」不够：两个残留窗口

``[ -f "$rvf_hook_target_script" ]`` 只覆盖**检查那一瞬**的存在性。检查通过之后
``python3`` 仍可能以退出码 2 + 非空 stderr 结束：

1. **目标存在但不可读**（权限位被改坏、payload 被 ``chmod`` 锁死）：``[ -f ]`` 为真，
   解释器 open 时拿到 ``[Errno 13] Permission denied``，照样 exit 2。
2. **检查与 open 之间目标被删/被换**（部署恰好在这几毫秒里替换版本目录）：经典 TOCTOU，
   任何「先检查再执行」的形态都不可能靠加检查消灭。

所以守卫**不依赖检查与执行之间的原子性**：目标脚本跑完后，把退出码 2 逐一改写成 0
（``AGENT_DRIVING_HOOK_EXIT_STATUS_NORMALIZED_TO_ZERO``），其余退出码原样透传。
这正是上面那条「零信号损失」结论的直接兑现：RVF 的 hook 入口从不故意 exit 2，被改写的
只可能是 spawn 失败本身。stderr 刻意**不**清空——退出码 0 + stderr 不驱动 agent，
而那行 ``[Errno ...]`` 是宿主 hook 日志里这条失败路径的 developer_only 诊断。

## 不掩盖真实的部署损坏

目标缺失时往 ``<breadcrumb 根>/rvf-hook-target-missing.log`` 追加一行面包屑，再退出 0。
这正是 ``docs/architecture/stop-hook-output-audience-contract.md`` 铁律要求的形态：
**失败一律 developer_only，永不驱动 agent 再跑一回合**。面包屑写入的一切输出都被重定向掉，
任何写入失败都不会反过来污染 hook 的 stdout/stderr。

面包屑落在哪个根，必须由**调用方显式传入**
（``hook_target_missing_breadcrumb_directory_shell_literal``）：installer 烘焙进
``environment_assignment_prefix`` 的自定义 ``RVF_USER_LOCAL_DATA_ROOT`` 只作用于
``python3`` 这一条命令，面包屑分支读到的是宿主进程的 ambient 环境变量。不显式传入时，
用自定义 user-local-data 根的部署会把这条**唯一**诊断写进默认的
``$HOME/.rvf/user-local-data``——落错位置等于没有。只有 bundled ``hooks.json``
（生成时还不知道最终根）才回退到 ambient / ``$HOME``。
"""

from __future__ import annotations


HOOK_TARGET_MISSING_BREADCRUMB_LOG_FILE_NAME = "rvf-hook-target-missing.log"
"""面包屑日志文件名，落在 breadcrumb 根下。"""

DEFAULT_RVF_USER_LOCAL_DATA_ROOT_SHELL_FALLBACK = "$HOME/.rvf/user-local-data"
"""调用方未传 breadcrumb 根、且 ambient ``RVF_USER_LOCAL_DATA_ROOT`` 也为空时的回退目录。

刻意写成 ``$HOME/...`` 而不是 ``${RVF_USER_LOCAL_DATA_ROOT:-...}`` 形态：bundled
``hooks.json`` 会经过 Claude Code 的 ``${CLAUDE_PLUGIN_ROOT}`` 变量替换，命令串里多余的
``${...}`` 花括号形态是不必要的替换风险面。
"""

HOOK_TARGET_SCRIPT_SHELL_VARIABLE_NAME = "rvf_hook_target_script"
"""守卫命令串里承载目标脚本路径的 shell 变量名。

``verify_local_deploy.py`` 解析 ``~/.codex/hooks.json`` 的命令串时必须认得这个赋值
形态，否则「Stop hook 是否精确指向 active marketplace router」会假红。那份脚本刻意
stdlib-only、不 import ``core``，故自带一份同名常量副本，**两处必须同步**——
``tests/test_hook_command_spawn_guard.py`` 有结构性 gate 钉住这一点。
"""

AGENT_DRIVING_HOOK_EXIT_STATUS_NORMALIZED_TO_ZERO = 2
"""唯一会驱动 agent 再跑一回合的退出码；守卫把它改写成 0。

这是**刻意的契约**，不是疏漏：RVF 的 hook 入口全部 ``return 0``，所以命令串里出现
退出码 2 只可能来自 spawn 失败（目标缺失 / 不可读 / 检查后被删）。改写发生在目标脚本
真正跑完之后，因此它同时覆盖了「存在性检查」结构上够不到的两个残留窗口。
其余任何非零退出码都原样透传，不被吞。
"""

_BREADCRUMB_DIRECTORY_SHELL_VARIABLE = "rvf_hook_breadcrumb_directory"
_TARGET_SCRIPT_EXIT_STATUS_SHELL_VARIABLE = "rvf_hook_target_script_exit_status"


def hook_command_guarded_against_missing_target_script(
    *,
    target_script_shell_literal: str,
    hook_event_name: str,
    environment_assignment_prefix: str = "",
    hook_target_missing_breadcrumb_directory_shell_literal: str | None = None,
) -> str:
    """把 ``python3 <script>`` 包成「永不以退出码 2 结束」的 POSIX sh 命令串。

    ``target_script_shell_literal`` 必须是**已经 shell 安全**的字面量：绝对路径用
    ``shlex.quote(...)``；bundled ``hooks.json`` 用带双引号的
    ``"${CLAUDE_PLUGIN_ROOT}/hooks/stop.py"``（``${CLAUDE_PLUGIN_ROOT}`` 由宿主替换）。

    ``environment_assignment_prefix`` 是原样保留的 ``KEY=value `` 前缀串（含尾随空格）。
    它只作用于 ``python3`` 这一条命令，与改造前的语义逐字一致——刻意不改成 ``export``，
    以免把变量泄进面包屑分支。**也正因如此**，面包屑根必须单独传
    ``hook_target_missing_breadcrumb_directory_shell_literal``（同样要求 shell 安全的
    字面量）；不传时才回退到 ambient ``RVF_USER_LOCAL_DATA_ROOT`` / ``$HOME``。
    """
    if not target_script_shell_literal:
        raise ValueError("target_script_shell_literal 不得为空")
    if not hook_event_name:
        raise ValueError("hook_event_name 不得为空")
    if environment_assignment_prefix and not environment_assignment_prefix.endswith(" "):
        environment_assignment_prefix = f"{environment_assignment_prefix} "

    target = HOOK_TARGET_SCRIPT_SHELL_VARIABLE_NAME
    exit_status = _TARGET_SCRIPT_EXIT_STATUS_SHELL_VARIABLE
    breadcrumb_directory = _BREADCRUMB_DIRECTORY_SHELL_VARIABLE
    breadcrumb_log = (
        f'"${breadcrumb_directory}/{HOOK_TARGET_MISSING_BREADCRUMB_LOG_FILE_NAME}"'
    )
    if hook_target_missing_breadcrumb_directory_shell_literal:
        breadcrumb_directory_resolution = (
            f"{breadcrumb_directory}="
            f"{hook_target_missing_breadcrumb_directory_shell_literal}; "
        )
    else:
        breadcrumb_directory_resolution = (
            f'{breadcrumb_directory}="$RVF_USER_LOCAL_DATA_ROOT"; '
            f'[ -n "${breadcrumb_directory}" ] || '
            f'{breadcrumb_directory}="{DEFAULT_RVF_USER_LOCAL_DATA_ROOT_SHELL_FALLBACK}"; '
        )
    breadcrumb = (
        f"{{ {breadcrumb_directory_resolution}"
        f'mkdir -p "${breadcrumb_directory}" && '
        f"printf '%s\\t%s\\t%s\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" "
        f"'{hook_event_name}' \"${target}\" >>{breadcrumb_log}; }} >/dev/null 2>&1"
    )
    spawn_with_normalized_exit_status = (
        f'{environment_assignment_prefix}python3 "${target}"; '
        f"{exit_status}=$?; "
        f'if [ "${exit_status}" -eq {AGENT_DRIVING_HOOK_EXIT_STATUS_NORMALIZED_TO_ZERO} ]; '
        f"then {exit_status}=0; fi; "
        f'exit "${exit_status}"; '
    )
    return (
        f"{target}={target_script_shell_literal}; "
        f'if [ -f "${target}" ]; then '
        f"{spawn_with_normalized_exit_status}"
        f"else {breadcrumb}; exit 0; fi"
    )
