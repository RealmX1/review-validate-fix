"""host-中性的跨簇共享常量内核（去-codex 重构 S10d，R-NEW-1）。

承载「一次 RVF run / Stop-hook 会话」协调用、被引擎 ≥2 个功能簇共同引用的模块级
常量：fork / cline-kanban-task / kanban-followup / fork-experiment 触发 marker、
cline-kanban worktree 模式、handoff 最终回复结构指令、父对话 context 的 env / artifact /
prompt 键、session-hook 控制键、suppress marker 与 env 名、manual-RVF marker 键族、
session / transcript path 键。

引擎簇 S10e–S10j 物理拆分为独立 core 模块后，这些常量必须由一个中性叶子统一供给，
否则「簇 A 引用簇 B 定义的常量」会在 core 内部形成 import 环（见计划 R-NEW-1）。本模块
是纯数据叶子：不含任何按宿主（host）类型分派的逻辑、不 import 任何 host SDK / adapters /
兄弟模块、不 spawn 子进程、从不作为 ``__main__`` 运行——满足 core/ host-free 不变量，
且可被任意簇无环 import。

仅承载被 ≥2 簇引用的常量；单簇私有常量（如 kanban-followup 调参、plan-doc 复审标记、
RVF mode 默认、analyze-thread 守卫键）留在引擎，随其所属簇在 S10e–S10j 一并迁出。
"""

# fork / cline-kanban / kanban-followup 触发 marker（env-var 名形态，既作 env 注入键、
# 也作 transcript / prompt 内的文本探针）。
FORK_EXPERIMENT_MARKER = "RVF_FORK_EXPERIMENT"
RVF_FORK_MARKER = "RVF_FORKED_REVIEW_VALIDATE_FIX"
CLINE_KANBAN_TASK_MARKER = "RVF_CLINE_KANBAN_TASK"
KANBAN_FOLLOWUP_MARKER = "RVF_KANBAN_FOLLOWUP_TRIGGER"

# cline-kanban task worktree 模式枚举与默认值。
CLINE_KANBAN_WORKTREE_MODES = {"branch", "inplace"}
DEFAULT_CLINE_KANBAN_WORKTREE_MODE = "branch"

# 主 agent 最终回复里 `RVF_HANDOFF_FILE:` marker 之后那段摘要的固定结构指令。
# 单一来源，供 fork / kanban-followup / kanban-dispatch 三处 prompt builder 复用，
# 避免再次像历史那样三份拷贝各自漂移成「1-3 句」自由散文、导致输出成无结构 paragraph。
# 与 references/handoff-template.md 的 `Reviewers：`/`Validate/fixers：` 两行结构、
# 以及 check_review_output.py 已保留的同名标签一致。
HANDOFF_FINAL_REPLY_STRUCTURE_INSTRUCTION = (
    "空一行后按固定结构分两行追加极短中文摘要："
    "`Reviewers：<reviewers 检查了什么、发现几项或没问题>` 一行、"
    "`Validate/fixers：<validate/fixers 验证/修复/驳回/升级了什么>` 一行，"
    "每行各自一句、不要挤成一段"
)

# 父会话对话 context 注入（dispatch 期把父 transcript 抽成可读 blob 写进 run
# artifacts，供 cline-kanban child agent 在 review 前阅读作背景；不重定义 scope）。
PARENT_CONTEXT_ENV = "RVF_PARENT_CONTEXT"
"""开关：默认开启；设 ``0`` / ``false`` / ``no`` / ``off`` 关闭父对话 context 生成。"""
PARENT_CONTEXT_MAX_BYTES_ENV = "RVF_PARENT_CONTEXT_MAX_BYTES"
"""总字节上限覆盖；缺省用 rvf_parent_context.DEFAULT_MAX_BYTES (64KB)，超限保留最近内容。"""
PARENT_CONTEXT_ARTIFACT_NAME = "parent-conversation-context.md"
"""run artifacts 中父对话 context 的文件名，与 task prompt / review-env 引用一致。"""
PARENT_CONTEXT_PROMPT_KEY = "RVF_PARENT_CONVERSATION_CONTEXT"
"""task prompt / review-env 中标记父对话 context 路径的键名。"""

# session-hook 开关控制键 + suppress marker / env 名。
SESSION_HOOK_CONTROL_KEY = "RVF_STOP_HOOK"
SUPPRESS_STOP_HOOK_MARKER = "RVF_SUPPRESS_STOP_HOOK=1"
SUPPRESS_ENV_NAMES = (
    "RVF_SUPPRESS",
    "RVF_SUPPRESS_STOP_HOOK",
)

# manual-RVF marker 键族（写进 session-hook state marker JSON，标记某仓某 HEAD/dirty 的
# 手动 RVF 已完成，供 committed-round 检测 / 重复触发抑制读取）+ marker TTL。
MANUAL_RVF_COMPLETED_AT_KEY = "manual_rvf_completed_at"
MANUAL_RVF_RUN_ID_KEY = "manual_rvf_run_id"
MANUAL_RVF_MARKER_KEYS = (
    MANUAL_RVF_COMPLETED_AT_KEY,
    MANUAL_RVF_RUN_ID_KEY,
    "manual_rvf_updated_at",
    "manual_rvf_expires_at",
    "manual_rvf_repo",
    "manual_rvf_head",
    "manual_rvf_dirty_hash",
)
MANUAL_RVF_MARKER_TTL_SECONDS = 12 * 60 * 60

# host transcript / session 路径在 Stop event 里可能出现的键名（按优先序探测）；
# SESSION_SCOPE_PATH_KEYS 去掉 log_path（日志路径不参与 session 作用域归属）。
SESSION_PATH_KEYS = (
    "transcript_path",
    "session_path",
    "conversation_path",
    "log_path",
    "session_file",
)
SESSION_SCOPE_PATH_KEYS = tuple(key for key in SESSION_PATH_KEYS if key != "log_path")
