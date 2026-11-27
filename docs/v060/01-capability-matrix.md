# V0.6.0 现有能力与遗留事项矩阵

执行者：Grok。工作包 W00。Issue #204。

基线 `main` = `preview` = `db19bff19307fa763fd50e94b4cedfda7181a141`，tree `c126d37771fa66a832497e6b2814d782fafc1dff`。分类依据是该树的源码、测试和文档，加上 #199 七组审查评论与指定 PR 的对比。本文件不关闭任何 Issue 或 PR。

分类词只有四个：

- 已覆盖：该树已经包含这项要求的核心行为。宿主进程是否加载该 SHA 另行注明。
- 独有待做：该树还没有这项行为。已经存在的局部能力写在证据里，不算完成。
- 过时：旧补丁或旧前提不能再原样实施。
- 未证实：现有材料不能证明现场行为。缺证据就保持这一类，不用源码风险代替实机复现。

74 个原编号合计 P0 60、P1 14。按主分类计数：已覆盖 0，独有待做 66，过时 0，未证实 8。原编号的主分类没有“已覆盖”或“过时”，因为七组审查描述的缺口在这个基线上仍然能对上源码；局部能力都记在判断列。补充项和指定 PR 使用全部四类。

机器可读副本：`tests/fixtures/v060/w00/items.json`。契约夹具只校验分类集合和编号，不连接 GitHub、数据库或宿主。

## 原编号

| 编号 | 优先级 | 工作包 | 分类 | 证据 | 判断 |
| --- | --- | --- | --- | --- | --- |
| F01 | P0 | W04 | 独有待做 | scripts/sync_feedback.py；app/feedback_progress.py；app/hostops_tasks.py | 同步 worker 只推拉。APP_EXECUTION 只解析评论。HostOps 任务是只读诊断，不是 AI 执行器。本轮没有本机调度器消费证据。 |
| F02 | P0 | W05 | 独有待做 | scripts/sync_feedback.py main() | 已有按反馈 ID 查找，避免重复建 Issue。单条失败 return 1，后续条目被挡住。缺 token 时函数返回 None。没有行级认领。 |
| F03 | P1 | W05 | 独有待做 | scripts/sync_feedback.py refresh_progress() | 只取最近 100 条已关联反馈，每轮最多 15 条，缓存裁到这 100 条。单 Issue 评论有分页，但有 50 页上限。 |
| F04 | P0 | W01 | 独有待做 | app/feedback_progress.py | OWNER/MEMBER/COLLABORATOR 同一信任集合。agent 枚举没有 ChatGPT。APP_EXECUTION 没有 run_id 和合法迁移检查。非法 APP_PROGRESS 被跳过。 |
| F05 | P0 | W01 | 独有待做 | app/feedback_progress.py progress_from_comments() | 后写的 overall_status 覆盖先前状态，阶段时间保留首次值。没有按 run_id 或候选分离。 |
| F06 | P0 | W05 | 独有待做 | app/feedback_delivery.py timeline() | related 能收集多个关联 PR，但主里程碑仍由单一 developer_pr 和 delivered_sha 决定。失败列表没有排除已关闭的被替代 PR。 |
| F07 | P0 | W05 | 独有待做 | app/feedback_delivery.py timeline() | accepted 在当前验收决策缺失时可以退回旧 overall_status=accepted。较早缺口会被标成 unrecorded。旧 blocked 会盖过阶段相位。 |
| F08 | P0 | W05 | 独有待做 | deploy/macci/merge_preview.py replacement_allowed()；app/main.py | 网页有解除 Draft、测试和合并检查入口。替换仍要求 Owner 的 BASE_REPAIR_REVIEW 或部署修复标记。没有持久化的普通预授权策略。 |
| F09 | P0 | W05 | 独有待做 | app/pr_tracking.py active_snapshot()；scripts/sync_development_tasks.py | 活跃快照按文件时间覆盖同号 PR。#202 终态回退由评论 7214348117 登记。本轮没有读取本机 pr-active.json，现场缓存未证实。 |
| F10 | P0 | W05 | 独有待做 | deploy/macci/publish_main.py acknowledge() | 该函数关闭的是 BASE-ACCEPTANCE Issue，并写 BASE_PUBLISHED。没有在这里收尾关联业务反馈。 |
| F11 | P1 | W11 | 未证实 | 评论 7214477264 | 审查明确当前只有历史样本和源码审查。本轮没有新的同一 run_id 全链实机证据。 |
| U01 | P0 | W08 | 独有待做 | 评论 7214542835；deploy/app-init/upgrade_scheduler.py | 审查记录 Panel #13 滞留且 MacCI frontend-failed。本轮没有重读 Panel PR 或本机调度器注册表。源码不能单独证明该 PR 仍滞留。 |
| U02 | P0 | W06 | 独有待做 | deploy/macci/runner.py failure_summary() | 只提取 pytest FAILED 行和特定 TS 错误。其他阶段返回空列表。Panel #13 failures=[] 来自审查评论，本轮未重取。 |
| U03 | P0 | W08 | 独有待做 | deploy/app-init/app_init.py enroll_foundation_scheduler() | 只有预装调度器文件通过属主和权限检查时才 register --mode create。缺失或异常时打印提示并返回 False。 |
| U04 | P1 | W08 | 独有待做 | app/foundation_version.py | 审查确认已有 REST 到 git/raw 的回退，同时指出进程内锁包裹网络且缓存不持久。本轮未另测旧 Panel 显示。 |
| U05 | P0 | W08 | 独有待做 | deploy/app-init/app_init.py copy_foundation() | 固定后端清单加全部 frontend/src 复制和字符串替换。tests/test_auth.py 还有精确文本改写。这是所有权风险，不是已证实的业务覆盖事故。 |
| U06 | P0 | W08 | 独有待做 | app/module_sdk.py；app_modules/registry.py | 文件头写明同进程可信 SDK，不是沙箱。MODULES 为空元组。角色名不同不等于已实测 PostgreSQL ACL。 |
| U07 | P0 | W08 | 独有待做 | deploy/app-init/upgrade_plan.py；评论 7214542835 | 审查指出 #13 mergeable=true 但构建失败，不能把无文本冲突当成兼容。普通预授权自动合并尚未成为基座门禁。 |
| U08 | P0 | W08 | 独有待做 | deploy/hostops/diagnostics.py；deploy/macmini/manage.sh | 仓库版本和已安装 scheduler、MacCI、HostOps、launchd 运行版本是分开的。本轮没有读取本机安装回执或运行 SHA。 |
| U09 | P1 | W11 | 未证实 | 评论 7214542835 | 没有本次跨版本、同版本新 SHA、失败替换和回滚的实机记录。历史合并不能代替。 |
| S01 | P0 | W04 | 独有待做 | deploy/macci/merge_preview.py；docs/v050-integration-queue.md | MACCI_MANAGED_QUEUE 和 MACCI_ACTIVE_PR 是可选固定入口。文档写明生产协调器接入仍要可信宿主激活。未激活时不能说队列已被强制使用。 |
| S02 | P0 | W04 | 独有待做 | app/integration_queue.py；deploy/macci/preview_journal.py | activate 检查旧 job 是否 INTEGRATED 或 CANCELLED，不等于部署、验收和 Main 发布。版本状态机还没有机器实现。 |
| S03 | P0 | W04 | 独有待做 | app/integration_queue.py register() | 登记要求已有 head，并在规格变化时拒绝。没有开发前的 task/run/worktree 可信绑定。预约冲突直接失败，不能排队等待。 |
| S04 | P0 | W04 | 独有待做 | app/integration_queue.py；deploy/macci/preview_journal.py | 同 PR 新 head 不能改已有 spec。取消依赖会使下游无法满足。没有带证据的 revision 替换流程。 |
| S05 | P0 | W04 | 独有待做 | app/integration_queue.py claim()；deploy/macci/preview_journal.py | claim 按优先级选取，但 stage 可以把已有 QUEUED job 直接准备。固定 MACCI_ACTIVE_PR 可以和队列优先级不一致。 |
| S06 | P0 | W04 | 独有待做 | app/integration_queue.py claim()、reconcile_unknown() | 过期 CLAIMED 在下次 claim 时转 UNKNOWN，并阻塞该版本队列。恢复依赖可信 verify_stopped。没有生产自动 fencing 接入证据。 |
| S07 | P0 | W04 | 独有待做 | deploy/macci/preview_journal.py；docs/v050-integration-queue.md | 远端 SHA 与候选不一致时保持 PENDING。明确未合并且关闭的 PR 可以释放预约。丢失响应不能盲重试。受控恢复流程仍缺。 |
| S08 | P0 | W04 | 独有待做 | app/main.py；app/hostops_tasks.py | 集成准备重试和 push 恢复已有局部实现。没有通用的分阶段 run_id 重跑 API。HostOps 不能被当成 AI 执行器。 |
| S09 | P1 | W10 | 独有待做 | app/integration_diagnostics.py | 只读诊断存在，状态集合有限，文档写明不返回租约和路径。缺少依赖、优先级、责任人、下一步和分页所需的完整看板。 |
| S10 | P1 | W11 | 未证实 | tests/test_integration_queue.py；评论 7214573542 | 队列单测覆盖局部规则。多 Agent、推送丢失和版本切换的实机故障注入没有在本轮执行。 |
| D01 | P0 | W06 | 独有待做 | deploy/macci/runner.py failure_summary() | 结构化提取只覆盖 pytest 与指定 TS 格式。编译、依赖、容器、超时和启动失败没有同等摘要。 |
| D02 | P0 | W01 | 独有待做 | deploy/macci/runner.py | 存在本地 report-outbox、隔离区和锁。审查指出远端 status 与 finish 可能先于 save_report。本轮未重放进程中断。 |
| D03 | P0 | W06 | 独有待做 | app/operations.py recent_operations() | operations.tsv 只保留解析后的最近 100 条，不是逐次部署的持久 outbox。下游应用没有同一条路径。 |
| D04 | P0 | W06 | 独有待做 | deploy/hostops/diagnostics.py OPERATIONS | 只接受 host.health、service.inventory、agent.snapshot。非 Darwin 服务标 unsupported。dispatch 扩大不在本包，且本轮未读本机开关。 |
| D05 | P1 | W06 | 独有待做 | deploy/hostops/diagnostics.py services() | 五个固定 launchd 标签，只读当前 launchctl list 域。CPU 和内存固定为 None。 |
| D06 | P0 | W01 | 独有待做 | app/operations.py EVENT_CODES | 事件是时间、代码、SHA 和有限 detail。没有统一 run_id、producer_seq 或来源索引。 |
| D07 | P0 | W01 | 独有待做 | deploy/macci/runner.py | outbox 有批次、容量、隔离和去重限制。失败主要打印。没有统一的积压和最老事件健康投影。 |
| D08 | P0 | W06 | 独有待做 | 评论 7214599555 | 测试错误进 GitHub 评论，部分部署失败会建 Issue。没有证实执行器自动领取并修复。 |
| D09 | P1 | W06 | 独有待做 | app/main.py /api/v1/system-logs | 诊断页读取 operations、integration 和 preview attempts。没有按 run_id 的跨服务检索。前端是否完整渲染本轮未用浏览器复核。 |
| D10 | P0 | W11 | 未证实 | 评论 7214599555 | 没有 Panel #13 原始日志，也没有隔离故障注入记录。不能宣称 Agent 已能不复制日志完成修复。 |
| A01 | P0 | W02 | 独有待做 | deploy/macmini/manage.sh load_env；scripts/sync_feedback.py feedback_token() | 反馈 token 会回退到 APP_GITHUB_TOKEN 或 APP_GITHUB_PR_TOKEN。审查指出 MacCI 与合并读取共享路径。本轮不读取凭据值，也未核实 token 权限。 |
| A02 | P0 | W02 | 独有待做 | deploy/macci/merge_preview.py tested() | 成功门禁要求 macmini/isolated-tests-v1 的 state、description 和 creator.login==OWNER。共享 Owner 身份不能区分 MacCI、Agent 和人。 |
| A03 | P0 | W02 | 独有待做 | app/release_acceptance.py；deploy/macci/publish_main.py | 网页验收写 Owner Issue 和本地 receipt。发布器核对接纳 Issue 与同 UID 私有文件。独立验收签发还不存在。 |
| A04 | P0 | W03 | 独有待做 | deploy/app-init/app_init.py install_job() | 审查确认服务以安装者 UID 和 HOME 运行。目录权限不能隔离同一 UID。本轮没有切换任何生产服务。 |
| A05 | P0 | W03 | 独有待做 | deploy/app-init/app_init.py；app/module_sdk.py | 独立 cluster 和 Unix socket 已存在。trust 认证和同 UID 不能证明应用间 ACL。禁止在本任务改生产库。 |
| A06 | P0 | W03 | 独有待做 | deploy/macmini/manage.sh test_app() | 宿主 pytest 使用 env -i，但仍保留 HOME，并在宿主执行。审查指出 build 继承 load_env 且未禁生命周期脚本。泄露未被本轮实测。 |
| A07 | P0 | W02 | 独有待做 | app/auth.py authorize_request() | 单个 APP_API_TOKEN 比对成功即返回 service，可走通用 authorize 依赖。会话写操作另有 Origin 和 CSRF。没有分操作矩阵。 |
| A08 | P1 | W02 | 独有待做 | app/auth.py | 已有 PBKDF2、HttpOnly Cookie 和 CSRF。登录限流在进程内 Lock。审查要求不要把已实现项写成缺失。跨 worker 共享限流仍缺。 |
| A09 | P1 | W02 | 独有待做 | app/request_limits.py；app/main.py | 请求体限制和上传并发已有测试。CSP、HSTS 和代理信任按审查仍不完整。本轮未做利用性验证，也不把未验证的缺口写成已经可以攻击。 |
| A10 | P0 | W02 | 独有待做 | 评论 7214504062；deploy/macci/merge_preview.py | 普通预授权无人值守还不是现行门禁。高风险替换仍要 Owner 标记。策略不能由候选自改这一条尚未落成独立策略服务。 |
| A11 | P0 | W02 | 未证实 | 本轮只读 GitHub API | GET branch protection（main 与 preview）返回 403。GET /rulesets 返回空数组。组织级 ruleset 和 bypass 未读到。不能写成已无保护，也不能写成已有保护。 |
| A12 | P0 | W11 | 未证实 | 评论 7214629195 | 隔离环境的伪造验收、跨应用和重放矩阵没有在本轮执行。 |
| I01 | P0 | W09 | 独有待做 | deploy/app-init/bootstrap.sh | 入口要求 Darwin、非 root 和 TTY。帮助文本只描述 Mac。没有固定发布摘要校验，也没有把参数转发给后续安装。 |
| I02 | P0 | W09 | 独有待做 | deploy/app-init/app_init.py；deploy/app-init/bootstrap.sh | 依赖检查和网络预检是现有局部能力。审查要求的磁盘、端口、后台凭据和域名预检没有成为统一无副作用阶段。 |
| I03 | P0 | W09 | 独有待做 | deploy/app-init/app_init.py application_directory() | 默认目录是 ~/Projects/app-init/owner--repo。没有完整的宿主资源登记。本轮未制造目录冲突。 |
| I04 | P0 | W09 | 独有待做 | deploy/app-init/app_init.py find_port() | bind 成功后套接字关闭，没有持续预约。operations.EVENT_CODES 没有 port_conflict。 |
| I05 | P0 | W09 | 独有待做 | deploy/app-init/app_init.py | 初始化没有统一的阶段 journal 和 run_id。现有集成 journal 只覆盖 preview push，不覆盖初始化资源。 |
| I06 | P0 | W09 | 独有待做 | deploy/app-init/app_init.py map_domain() | 函数存在 GET 后更新 ingress 和 DNS 的路径。审查所述条件更新和局部失败恢复没有在本轮用真实 Cloudflare 验证。 |
| I07 | P0 | W02 | 独有待做 | deploy/app-init/app_init.py；scripts/sync_feedback.py | 重跑仍可能提示新 token。反馈 token 有多环境变量回退。本轮不读取或打印任何秘密。 |
| I08 | P0 | W09 | 独有待做 | deploy/app-init/app_init.py install_job() | 已有 plist 不一致会拒绝，一致且 active 会返回。没有统一资源清单把数据库 cluster 完整性算进恢复。 |
| I09 | P0 | W09 | 独有待做 | deploy/app-init/app_init.py | 命令参数没有专用重建或卸载计划。破坏性删除不能在没有单独授权时执行。 |
| I10 | P0 | W09 | 独有待做 | deploy/app-init/app_init.py seed()、enroll_foundation_scheduler() | 首次推送前的检查被审查标为静态检查。调度器缺失只创建不了 Draft 以外的自动推进。反馈 POST 与本地 receipt 不是同一个事务。 |
| I11 | P1 | W09 | 独有待做 | deploy/app-init/bootstrap.sh；Issue #197 | 当前入口拒绝非 Darwin。W00 只写平台适配契约，不实现 systemd，也不把 Linux 结果称作 MacCI。 |
| I12 | P1 | W11 | 未证实 | 评论 7214656091 | 没有空仓库、端口冲突、重复安装或 Linux 的实机记录。 |
| R01 | P0 | W07 | 独有待做 | deploy/macmini/manage.sh update() | 锁是 mkdir .webapp-deploy.lock，EXIT trap 里 rmdir。update_locked 只表示目录存在。没有进程 fencing。 |
| R02 | P0 | W07 | 独有待做 | deploy/macmini/manage.sh update() | HEAD 等于跟踪分支时先记录 update_no_change 并退出，发生在残留备份和健康检查之前。 |
| R03 | P0 | W07 | 独有待做 | deploy/macmini/manage.sh | fast-forward、venv、dist、重启和 commit_dependencies 分步发生。没有持久阶段和上一健康资源清单。 |
| R04 | P0 | W07 | 独有待做 | deploy/macmini/manage.sh rollback 路径 | 残留 frontend/dist.previous 或 .venv.previous 会使后续 update 在合并前退出。回滚失败没有独立协调器。 |
| R05 | P1 | W07 | 独有待做 | deploy/macmini/manage.sh fetch_with_retry | fetch 有分类和有限退避。依赖或构建失败会 hold SHA。临时故障和确定性缺陷还没有分开的自动复测策略。 |
| R06 | P0 | W07 | 独有待做 | deploy/macmini/manage.sh；app/operations.py | 本机健康和运行 SHA 事件已有。公网回源持续观察不是 update_ok 的同一证据。 |
| R07 | P0 | W07 | 独有待做 | scripts/apply_migrations.py | --check 按文件名比对 schema_migrations，不校验 SQL 摘要，也不自动执行。文件头写明 updater 不得自动迁移。 |
| R08 | P0 | W05 | 独有待做 | deploy/macci/merge_preview.py replacement_allowed() | 拒绝 Issue 和 Owner 修复标记是现有窄例外。普通预授权修复不能自动替换。不得伪造用户验收。 |
| R09 | P0 | W07 | 独有待做 | 评论 7214756309 | HostOps 7/7 是基础实机确认，审查明确不能扩大成冷启动通过。本轮没有重启全链证据。 |
| R10 | P1 | W11 | 未证实 | tests/test_deploy_script.py；评论 7214756309 | 部署脚本单测存在。各事务阶段 SIGKILL、磁盘满和回执中断没有在隔离环境执行。禁止生产故障注入。 |

## #199 补充与 #197

这些项不重复计入 74。

| 编号 | 优先级 | 工作包 | 分类 | 证据 | 判断 |
| --- | --- | --- | --- | --- | --- |
| SUP-P0-1 | P0 | W05 | 已覆盖 | deploy/macci/merge_preview.py published_preview_equivalent()；提交 e5623cf 与 c9202ad | #200 的冻结判断已进入 #196，并在基线中。相等 tree 本身不解冻。#200 补丁相对当前文件已过时，禁止 cherry-pick。本轮没有读取 MacCI 进程是否已加载该 SHA。 |
| SUP-P0-2 | P0 | W02 | 独有待做 | app/pr_tracking.py；deploy/macci/merge_preview.py；app/manual_pr_actions.py | 门禁和跟踪读取的是 /commits/{sha}/statuses，不是 combined /status。tested() 仍以共享 OWNER login 作为创建者。跟踪里的 evidence() 不检查终端状态创建者。诊断原因仍缺。 |
| SUP-P0-3 | P0 | W10 | 独有待做 | 评论 7211876147；frontend/src/DevelopmentTasksPage.tsx | 阶段责任人、催促、立即执行和真实回执没有统一模型。现有手动按钮不能被写成已接通执行器。 |
| SUP-DATA | P0 | W10 | 独有待做 | 评论 7212519006 | 默认列表仍可能混入历史和被替代项。要求归档而不是删除。本轮不关闭 #186、#161、#191。 |
| SUP-SYNC | P0 | W05 | 未证实 | app/pr_tracking.py active_snapshot()；评论 7214348117 | 源码存在活跃文件覆盖同号 PR 的路径。本机缓存在 #202 之后是否回退，本轮没有读到缓存文件。 |
| SUP-F08 | P0 | W02 | 独有待做 | 评论 7214504062 | 普通同仓 PR 无人值守是目标，不是当前门禁。与 F08、A10 同一缺口，不单独实现第二套授权。 |
| SUP-197 | P1 | W09 | 独有待做 | Issue #197；docs/v060/05-compatibility-migration.md | Linux 适配只在 W00 写成契约。bootstrap 仍拒绝非 Darwin。不提前实现。 |

## 指定 PR 与 #155

| 对象 | 状态 | 分类 | 证据与处理 |
| --- | --- | --- | --- |
| #196 | 已合并，merge `4cd400ddc47039c0c08e331e7901e880d03acbf2`，是 HEAD 祖先 | 已覆盖 | V0.5.0 队列、预约、租约和诊断接入已在基线。其内部提交 e5623cf / c9202ad 吸收了已发布 preview 的冻结修正。不把 #196 重新实现一遍。RELEASE_PLAN 仍记录实机补验，那是发布证据，不是这个 PR 的源码缺失。 |
| #200 | 关闭，未合并，head `78aa70a271a10d4b5047236e019ae5837256d83e` | 过时 | 冻结修正的意图已经在基线 `published_preview_equivalent()`。该函数要求 main 的第二父提交是 preview、tree 相同、发布 PR 已合并且仓库匹配。仅 tree 相同不会解冻。当前文件比 #200 补丁更长，禁止 cherry-pick。 |
| #186 | 打开的 Draft，head `e63f31254aa222b26804cd413592d8a29f4c37c4` | 独有待做 | 相对基线只多 58 个路径：`tests/fixtures/legacy-generated-baseline/**` 和 `tests/test_ownership_preflight_legacy_baseline.py`。没有生产代码。基线是旧 preview `fab59818`，夹具对当前生成器是否有效未证实。不合并、不关闭。 |
| #161 | 打开的 Draft，head `78a7ca772a8b84de0aeec9fb0108e2e3d60be2c1` | 过时 | `frontend/src/guide/*` 不在基线。`frontend/src/main.tsx` 已把 `UsageGuidePage` 放在 `ProjectInfoPage` 上方，发布顺序文案在 `frontend/src/v2/usage-guide/content.ts`。不要把旧指南组件再嵌进项目页。逐段 FAQ 是否一字不差，本轮没有做文本等价证明。不关闭。 |
| #191 | 打开的 Draft，head `a35886fa4aadcb6eb0cc8147a46c5ac3a304381c` | 已覆盖 | 不在 HEAD 历史上的提交 `a35886f` 只改 `frontend/src/HostOpsTasks.tsx`。该 blob 与 HEAD 同为 `b963e41c390f3ea19de036ad5a46e26254b3a34e`，创建或取消后会 `refreshAfterMutation()`。PR 本身基于旧 main，不能再合并。不关闭。 |
| #155 | 关闭，未合并 | 过时 | 数据治理评论点名与 #200 一起关闭。协作正文已在 `docs/AGENT_COLLABORATION.md`。本任务不再改它。 |

## 本轮没有取得的证据

- 没有登录用户本机，没有读 HostOps 原始日志、调度器注册表、preview 运行 SHA 或 `logs/pr-active.json`。
- 没有读取任何 token、密码或生产数据库。
- classic branch protection 的 GET 返回 403。仓库 rulesets 列表返回空数组。组织级规则和 bypass 主体没有读到。详见身份矩阵。
- 既有 pytest 全套本轮没有执行。只执行了无生产副作用的契约夹具测试。
