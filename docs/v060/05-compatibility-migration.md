# V0.6.0 兼容与迁移

执行者：Grok。本方案不迁移生产数据库，不删除历史事件、缓存、回执或 Issue，不重写已发布 SHA。升级先双读，确认投影可以重建之后才允许新写入。回滚代码不等于回滚数据库。

## 旧事件

`logs/operations.tsv` 继续按 `app/operations.py` 的三列或四列解析。已知 `EVENT_CODES` 保持可显示。未知代码保留在原始文件里，投影标成“未识别”，不能丢行，也不能因此中断整个文件。

新事件使用 schema_version 1 的字段。旧行没有 run_id、producer_id 和 producer_seq。读模型把它们标成 `legacy_unscoped`，不编造序号。新投影的水位不把旧行算进生产者序号。

`recent_operations()` 今天只返回最后 100 条解析结果。兼容期读接口必须能说明这是截断视图，而不是“只有 100 条历史”。完整保留期限等有基线测量后再定，本文件不写一个没有样本的天数。

## 旧缓存

| 缓存 | 当前行为 | 兼容要求 |
| --- | --- | --- |
| `logs/feedback-progress.json` | `refresh_progress()` 只保留最近 100 条关联反馈 | 同步失败时保留上一份可信快照，并标 stale。不把缺失写成新的 overall_status |
| `logs/pr-active.json` | `active_snapshot()` 用较新的文件时间覆盖同号 PR | 终态只能被更新的可信证据替换。合并、部署和发布分字段保存。本机文件本轮未读，现场是否回退仍未证实 |
| 集成队列 SQLite | 私有账本，状态为 QUEUED、CLAIMED、PREPARED、UNKNOWN、INTEGRATED、CANCELLED | 不就地改成新的 Release 状态。新模型是旁边的投影，旧账本继续由现有协调器读写 |

队列状态到新模型的只读映射：

| 现有 job | 新投影 |
| --- | --- |
| QUEUED | Run PENDING，阶段 integrate |
| CLAIMED | Run LEASED 或 RUNNING |
| PREPARED | 准备阶段 SUCCEEDED，不是发布，也不是部署 |
| UNKNOWN | Run UNKNOWN |
| INTEGRATED | Candidate INTEGRATED，仅当远端观察已经核对 |
| CANCELLED | Run CANCELLED |

PREPARED 不能显示成 preview 已部署。

## 旧回执与协议

下列正文继续可解析，解析失败时忽略该条并计格式错误，不授权：

- `[APP_PROGRESS]`、`[APP-CLAIM]`、`[APP_EXECUTION]`
- `[MACCI_REPORT]`
- `[BASE_PUBLISHED]`、`[BASE_REJECTION]`、`[BASE_REPAIR_REVIEW]`、`[BASE_DEPLOY_REPAIR_REVIEW]`
- commit status `macmini/isolated-tests-v1`、`macmini/progress-v1`、`macmini/deployment-v1`

旧 APP_PROGRESS 没有 run_id。兼容读取仍展示它，但新的阶段机不让一条旧 accepted 覆盖新候选。agent 枚举以外的值继续删除显示，直到 W02 有独立证明；不能因为 W00 草案提到 ChatGPT 就把这个字符串加入信任集合。

成功测试门禁继续读 `/commits/{sha}/statuses`，不改回 combined status。创建者今天仍是 Owner 登录名。在独立身份落地前，这个检查保持原样，并且文档明确它不能区分人、Agent 和 MacCI。

## 旧应用与宿主

`copy_foundation()` 的固定清单和字符串替换继续服务已安装应用。W08 之前不改复制规则，避免一次契约文档造成下游文件漂移。

已安装的 launchd 标签保持现名：`com.layho.macci`、`com.layho.macci-preview-merge`、`com.layho.macci-main-publish`、`com.layho.macci-receipt-delivery`，以及应用自己的标签。仓库里出现新代码，不等于这些服务已经重启到 `db19bff`。升级宿主组件要有单独的版本、摘要和健康回执。

HostOps 保持现有三个只读操作。非 Darwin 返回 unsupported，不执行 launchctl。

配置名 `MACCI_MANAGED_QUEUE`、`MACCI_ACTIVE_VERSION`、`MACCI_ACTIVE_PR`、`MACCI_INTEGRATION_JOURNAL` 保持可选。未设置时，现有协调器按今天的路径工作，不能因为契约写了自动调度就认为生产已经启用。

## 升级与回滚

1. 先部署只读投影和夹具校验，默认不写新存储。
2. 双读旧 tsv、旧缓存、旧 status 和新投影。不一致时显示两份来源和检查时间，不自动选一个当作发布证据。
3. 新写入只在对应工作包被评审并打开开关后发生。开关不放在候选可以自行修改的默认值里。
4. 代码回滚恢复上一健康产物和服务。数据库结构、业务行和历史 Issue 不动。
5. 回滚本身失败时进入 SAFE_FROZEN，保留备份，不删除 journal。

`scripts/apply_migrations.py --check` 继续只按文件名比对。它不证明 SQL 内容、ACL 或数据无损。生产库的任何迁移仍要人工批准。

## Linux 平台适配契约（#197）

这是契约，不是实现。`deploy/app-init/bootstrap.sh` 今天在非 Darwin 时直接失败，这个行为保持到 W09。

适配器标识：

| 标识 | 含义 | 本轮状态 |
| --- | --- | --- |
| `darwin-launchd` | 当前 macOS 路径 | 已有实现，是兼容基线 |
| `linux-systemd` | systemd 的 Linux 试点 | 未实现，未实机 |

探测结果至少包含：操作系统、版本、架构、发行版、包管理器、服务管理器。探测失败或组合不在支持矩阵内时返回 `PLATFORM_UNSUPPORTED`，并且不执行安装、包管理或服务命令。

建议但未验证的试点矩阵：Ubuntu 24.04 LTS 的 x86_64 与 arm64，Debian 13 的 systemd。未跑过的格子不能写成支持。其他发行版、非 systemd、以及没有写明的架构，适配器拒绝。

服务操作与 launchd 对齐的名称是 start、stop、restart、enable、disable、health。必须区分系统级和用户级，权限不足时返回明确错误，不提权。

Linux 测试回执使用单独 context，建议名 `linux-host/isolated-tests-v1`。它不能写入或冒充 `macmini/isolated-tests-v1`。

初始化仍使用 `~/Projects/app-init/owner--repo`，也允许显式路径。探测到新旧目录并存时停止，不自动搬迁或删除。端口冲突只报告占用者，不杀进程，不静默换端口。

HostOps 在 Linux 上如果没有对应只读实现，返回 unsupported，不新增命令通道。

macOS 回归是 Linux 工作的同时条件。W00 不创建 Linux 分支，也不跑 Linux 安装。
