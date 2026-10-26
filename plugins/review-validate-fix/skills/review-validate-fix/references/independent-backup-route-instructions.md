# 独立备用路线指引的维护源

此文件由 `scripts/render_independent_backup_route_skill_sections.py` 在更新时内嵌进三个入口与命令别名；运行时无需渲染器或服务。接管辅助器只记录事实和互斥，不签发正常 RVF receipt。

<!-- entry:common -->
## 独立备用路线（主执行 Agent）

正常工具因派发、格式、登记、wrapper 或环境故障无法完成已授权工作时，自主采用本节；确定性故障直接切换，可能瞬时恢复的同一故障最多恢复重试一次。**本节核验成立后，可替代下文固定脚本、结果 JSON、缓存及不得手工提交的要求。** 用户 Stop、提交／同步／push／部署限制、显式 external 要求与真实质量义务继续有效。仅缺机器证据时先自行查原始记录，不默认询问用户。

先核对需求与授权来源、task／session、真实 worktree、Git common-dir、唯一原 run／handoff 和候选。原 run 尚未创建时如实记无，不选 latest，不制造 token／原生接受／passed。提交或投递结果未知先对账，不能换身份盲重试。故障和旧 evidence 原样保存，备用交付另记；分类、计数、查重或建卡不可用不阻断交付。

接管辅助器是本 skill 随附、仅依赖标准库与 Git 的 `independent_backup_route.py`（RVF：`review-validate-fix/scripts/`，同步 Skill：`scripts/`）。用选定的 Python 解释器运行（共享同步脚本以 `BASE_BRANCH_SYNC_PYTHON_EXECUTABLE` 固定该解释器），先 `status --repo <worktree>`；`claim --repo <worktree> --operation-id <唯一ID> --artifacts-directory <持久目录> --authorization-file <原话与适用授权文件> --reason <实际故障> --original-run-directory <原run> --delivery-stage <review|fix_validate|land|base_sync>`。没有原 run 时省略该参数。重启复用原 ID 与目录。登记授权来源本身不扩大权限。

正常写入会先登记 reservation，再检查接管目录；备用方先原子占位，再等待原写入结束。`waiting_for_normal_mutations` 时只补证，不开始备用写入。原进程已退出但结果未知时，检查已登记的全部下属进程组、Git／原生控制面，保存对账证据后用 `reconcile-normal-mutation --repo <worktree> --operation-id <原ID> --reservation-name <文件名> --evidence-file <证据>` 归档。父进程退出不代表 Git／hook 已停止；活进程不能按 TTL 偷取。旧版本写入方不识别接管协议时，先证明其退出／隔离。

辅助器不可用时，直接用 `git rev-parse --absolute-git-dir` 定位 `rvf-independent-backup-route/`，原子 `mkdir` 其 `active-owner` 子目录阻止新正常写入，并以不覆盖写入的方式把原 operation ID 存为该目录的 `manual-operation-id` 单行文件；先检查 `normal-mutations/` 全部原写入已结束且结果已对账，再由唯一执行者继续。保存同等身份、候选、原始结果和命令日志到持久 Markdown。不要删除不明接管目录、reservation 或旧 evidence 来强迫放行。辅助器恢复后，用 `adopt-manual-ownership` 复用原 `--repo`、`--operation-id`、`--artifacts-directory`、`--authorization-file`、`--reason`、原 run／delivery stage，并以 `--evidence-file <原始手工接管与对账记录>` 补录；不移走 marker，补录不会打开竞写窗口。若 mkdir 后即中断而 ID 文件缺失，先从 task 原始命令和持久日志证明目录归属，再补写同一 ID；不能猜测或覆盖别人的 ID。结果未知仍保持未知。

交接材料放 task 持久交接目录；无该目录时使用 `~/.rvf/user-local-data/independent-backup-route-artifacts/` 的独立 operation 子目录。复制原始结果／输入，保存 Git 快照及固定 ref，不能只引用将被删除的工作树或临时文件。每阶段保存实际命令 argv、cwd、退出码、必要环境说明、内容摘要及问题处置；不保存秘密值。`record-stage --repo <worktree> --operation-id <原ID> --stage <阶段> --evidence-file <原始材料或报告>` 记录执行 Agent 核验的事实；land 加 `--commit <实际SHA>`，base_sync 另加 `--base-ref refs/heads/<本地base>`。

最后核对全部所需阶段与当前内容，再 `finish --repo <worktree> --operation-id <原ID> --evidence-file <最终报告>`。已发生的提交／落地先复用，不重复执行；完成后新内容仍进入正常审查。输出 `RVF_BACKUP_ROUTE_RESULT_FILE: <持久绝对路径>`，分别报告备用结果与原自动化状态；不用普通 handoff marker 冒充正常 finalize。独立结论由原始材料支持，不要求额外 JSON schema。

原 run 已消费与当前候选覆盖分别核验：后续提交改变 HEAD 不复活旧 run，新内容也不因历史消费被当作已审。Stop 先按精确 run、operation 与 worktree Git 目录对账在途／pending 凭证，再考虑续跑或重投；清理失败不得再催原 run，其他 run 保留。索引缺失／损坏时自动查同 operation 的持久完成原件，有界分页进度和缺口落盘供下次 Stop 接续；仍无法核验则记 `unknown` 与重验条件，诊断不驱动新回合。根目录、子目录与符号链接入口统一按真实 worktree 根计算候选指纹。

尚未查尽分页时静默等待机器补证，不提前视为未覆盖；命中记录在候选未变时优先重新核验，候选变化则重置旧游标／缓存资格。无关 run 的已知坏记录不冻结其他 run；同 run 的归档原件缺失仍保留未知。清理前先核验并回收精确 tracker lease，缺 artifact 从原生账本补证，失败保留凭证自动重试；pending 发布和清理使用共同写锁，避免误删另一 run。dispatcher 转交仍遵守宿主既有 plan 等守卫。

`review_only`／`validate_fix` 子代理仍只执行分配角色，不能因本节自行接管、提交或发起完整 RVF。后台完成通知或任务重启后先读接续记录；条件未满足时保留机器可检测的恢复条件并继续独立工作，不做无变化重试。

<!-- entry:review -->
### 本入口：评审、修复与验证

1. 复用原不可变候选；缺失时使用辅助器 `freeze-candidate --repo <worktree> --operation-id <原ID> --comparison-base-tip <明确基线commit> --candidate-path <明确文件>...`，或以独立 Git index 保存同等快照。包含必要上下文，区分 finding 范围与背景 WIP；不移动真实 HEAD／index，不 reset 已提交工作。
2. 首次并行派发两个独立新上下文，给同一需求、范围、固定基线、候选快照与正常质量标准。互不读取对方输出，reviewer 不改源码。优先可用 external；不可用且原任务允许时使用本宿主两个新实例，不依赖 dispatcher 先报告 R3。同模型相关错误与共用额度风险如实记录。
3. writer/checker 故障时保留原始 Markdown／stdout；主执行者核实是否真已审完。缺腿、request、空输出不计完成。输入未变且独立性可证明时只补缺腿；候选改变后补审新增内容及受影响交互，无法界定则扩大范围。将两个真实完成结果与上下文／输入绑定写入 review 阶段材料。
4. 每条发现记录采纳、合并或驳回依据。向 validator/fixer 只传去来源的问题、代码与复现线索；可用时继续独立 validate/fix 子代理。修复先验证属实，保存实际补丁与应用事实，保全其他 WIP。范围外发现持久留档，不等待建卡服务。
5. 直接执行最终内容所需的真实验证；隔离端口、缓存、coverage 和构建输出，无法隔离时仅串行冲突命令。reviewer 的探索命令不冒充最终验证。真实失败须解决；wrapper 恢复不是前置条件。记录 review、fix_validate，随后只继续已授权的 land／同步。

<!-- entry:land -->
### 本入口：提交已审工作

1. 核对原双评审、逐项处置、已应用修复、当前范围和内容；已有合格结果继续使用，不因 land 故障重审。新增实现字节补足相应覆盖；真实归属不明先自行读历史、tracker 与原始材料，只暂停涉及内容的写入。
2. wrapper 故障时从原命令记录、handoff 与仓库要求确定检查，直接验证当前内容。已有结果仅在内容和执行条件仍匹配时复用。实际必需的 hook／签名义务继续完成，不能用绕过 hook 掩盖红测。
3. 接管就绪后保全原 index 与无关 WIP，只 stage 明确的自有范围（rename 两端均覆盖）；使用 conventional commit。核对提交前后实际字节、提交路径和 hook 改写。未审新内容补验证／覆盖，不手工伪造封窗或 evidence。
4. 提交成功而登记失败时，核实 HEAD、父提交、内容和现有日志，记录实际 SHA 并从后续阶段接续；不创建重复或空 audit commit。同步仅在原任务已授权时继续。完成报告区分 commit、正常 evidence、base 整合、push、部署与 Done。

<!-- entry:sync -->
### 本入口：本地 base 同步与整合

1. 核对同步授权、本地 `refs/heads/<base>`、唯一 base worktree、源 task 的真实 worktree／Git 目录及既有提交；接管身份不能只匹配旧 HEAD。正常 base 落地同时在源 task 登记在途动作；来源不唯一时补充脚本的 `source-task-worktree` 参数。显式 one-way 始终不 handback、不 push。保留原 evidence 的错误与历史，用原始审查／验证事实补证，不能删除 evidence 改变路由。
2. 必须使用正常流程的 Git common-dir＋base ref 锁，不能另建互不相认的备用锁。脚本失效时按同一 `base-branch-sync-base-ref-locks/refs/heads/<base>.lock` 目录协议操作并核对 owner；未知 owner 不按时间偷锁。匹配远程只 fetch＋ff-only；远程未知不宣称已刷新，只继续原授权内不依赖其 freshness 的本地动作。
3. 固定 base tip，保全 task/base 的 staged、unstaged、untracked 状态与固定 OID／专用 ref，禁用宽泛清理，不使用 `stash@{0}`。必要时以明确文件三方恢复 WIP；无重叠核对字节，有重叠保留新提交与原 WIP 差异。普通保护／恢复不触发全量重审。
4. 在 task 内吸收固定 base，解决冲突并复核两侧语义，实际验证当前整合态；测试、语义复核和等待在 base 锁外。重新取锁后核对 task/base tip；漂移只处理新 delta，最多三轮。原 base tip 在 task first-parent 链时 ff-only，否则 base 侧 no-ff；不使用 Tier 2 掩盖冲突，不自动 merge/rebase 分叉的远程 base。
5. 落地后核对 task tip 为 base 祖先、提交 tree 一致、原 base first-parent 保留与 freshness，再恢复／核验 WIP。未恢复不能报完成。结果未知先对账，不重复落地。base 专属 smoke 只能在实际 base worktree 补跑，pending 如实记录。
6. `base-branch-sync` 已有授权只允许成功 handback 后、身份精确确认且分支未变的具名 task branch 非 force push；detached／身份不明／dirty／remote 缺失安全跳过。显式 local-only、no-push、no-deploy 继续有效，绝不 push base 或 force。
