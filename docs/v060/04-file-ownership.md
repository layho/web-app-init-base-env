# V0.6.0 文件所有权与并行开发

执行者：Grok。代码整合负责人是 Grok。接口冲突和跨包顺序由 ChatGPT 裁决。高风险权限、生产数据库和宿主切换仍要用户单独授权。GitHub 评论不是原子锁；在 #153 的自动租约真正接入生产之前，共享文件按下面的窗口串行。

预约写在对应工作包 Issue，不写在 #199。内容包括路径、基线 SHA、预期改动和依赖的窗口。ChatGPT 确认后才改共享文件。PR 合并、或任务明确放弃并且工作树没有留下半成品时，窗口释放。范围扩大必须重新预约。

W00 没有预约下表里的生产路径。

## 工作包路径

| 包 | 主要路径 | 依赖 | 整合 |
| --- | --- | --- | --- |
| W01 | 新的事件校验与 outbox 投影；随后 `app/feedback_progress.py`、`app/operations.py`、`deploy/macci/runner.py` | W00 契约评审。与 W02 只共享夹具，不共享上述三个文件的第一轮编辑 | Grok |
| W02 | 新的策略与测试身份模块；`app/auth.py`、`app/release_acceptance.py`、`app/request_limits.py` 仅在单独授权后的窗口 | W00。不依赖 W01 的生产改动，依赖同一份契约夹具 | Grok |
| W03 | `deploy/app-init/app_init.py` 中的数据库与服务身份部分、`deploy/macmini/manage.sh` 的测试和构建环境 | W02 的身份边界。禁止生产库迁移 | Grok |
| W04 | `app/integration_queue.py`、`deploy/macci/preview_journal.py`、`deploy/macci/merge_preview.py` 的调度入口 | W01、W02 | Grok |
| W05 | `app/feedback_delivery.py`、`scripts/sync_feedback.py`、`scripts/sync_development_tasks.py`、`app/pr_tracking.py`、`deploy/macci/publish_main.py`、`app/manual_pr_actions.py` | W01、W02、W04、W07。部署事件字段已在 W00 冻结，投影夹具可以先写；并入版本线前要接上 W07 的真实回执形状 | Grok |
| W06 | `deploy/macci/runner.py` 的失败摘要、`deploy/hostops/diagnostics.py`、`app/hostops.py`、`frontend` 诊断页 | W01、W02。解析器可先用夹具，不启用新的 HostOps 操作 | Grok |
| W07 | `deploy/macmini/manage.sh` 的更新、锁、健康和回滚；`scripts/apply_migrations.py` 只加只读校验设计，不自动迁移 | W01、W02、W03 | Grok |
| W08 | `copy_foundation()`、`app/module_sdk.py`、`app/foundation_version.py`、`deploy/app-init/upgrade_*.py` | W03、W04、W05、W06、W07 的登记接口 | Grok |
| W09 | `deploy/app-init/bootstrap.sh`、`deploy/app-init/app_init.py` 的资源阶段；Linux 适配新目录，当前不创建 | W02、W03、W07、W08 | Grok |
| W10 | `frontend/src/DevelopmentTasksPage.tsx`、`frontend/src/main.tsx`、`app/integration_diagnostics.py` | W01、W04、W05、W06。UI 可以先按契约夹具做只读态 | Grok |
| W11 | 跨包验收记录、`README.md`、`docs/RELEASE_PLAN.md` | W01–W10 | Grok 汇证据，用户验收 |

## 串行窗口

同一窗口内的路径禁止两个任务同时修改。不同窗口可以并行。

| 窗口 | 路径 | 顺序 | 释放 |
| --- | --- | --- | --- |
| S-contract | `tests/fixtures/v060/w00/contract.json` | W00 拥有。评审后的修订仍由单一 PR 修改 | 契约评审通过并声明 schema 版本 |
| S-auth | `app/auth.py`、`app/release_acceptance.py`、`app/request_limits.py` | 只给已授权的 W02。W01、W05、W10 不改这些文件 | 该授权 PR 合并或放弃 |
| S-feedback | `app/feedback_progress.py` 然后 `app/feedback_delivery.py`、`scripts/sync_feedback.py` | W01 先改校验，W05 再改同步和多 PR 投影 | 前一包合并后再预约下一包 |
| S-events | `app/operations.py`、`deploy/macci/runner.py` | W01 先改落盘和兼容读取，W06 再改摘要 | 同上 |
| S-merge | `deploy/macci/merge_preview.py`、`deploy/macci/publish_main.py`、`app/manual_pr_actions.py` | W02 的身份判断、W04 的队列入口、W05 的冻结与收尾，按这个顺序 | 同上 |
| S-queue | `app/integration_queue.py`、`deploy/macci/preview_journal.py` | 只给 W04 | 同上 |
| S-host | `deploy/macmini/manage.sh` | W07 主写。W03 若要改测试环境，排在 W07 当前窗口之后，不并行 | 同上 |
| S-init | `deploy/app-init/app_init.py`、`deploy/app-init/bootstrap.sh` | W08 与 W09 都要改 `app_init.py`，因此合成一个窗口：先定所有权清单，再改复制和资源 journal | 窗口内只有一个打开的 PR |
| S-ui | `frontend/src/main.tsx`、`frontend/src/DevelopmentTasksPage.tsx`、`frontend/src/OperationsPage.tsx` | 只给 W10 | 同上 |
| S-release-docs | `README.md`、`docs/RELEASE_PLAN.md` | 只给 W11 发布前整体整理 | 版本交付检查完成 |

新文件默认不进窗口：W01 使用独立的新模块目录，W02 使用另一个新模块目录。谁先创建共享 schema 模块，谁就占用 S-contract 的实现侧；另一个包只导入已合并的模块，不在自己的分支里复制第二份。

## W01 与 W02 同时开工时的禁区

可以同时做：

- W01：契约校验器、runner 落盘顺序、feedback_progress 的轮次校验、operations 的兼容读取。
- W02：策略评估器、测试身份夹具、分支保护核对记录、凭据入口说明。

不能同时做：

- 任一方修改 `app/auth.py` 或 `deploy/macci/merge_preview.py`。
- 任一方把新路由注册进 `app/main.py`。
- 任一方修改 `deploy/macmini/manage.sh` 或生产配置样例。

`app/main.py` 的新路由留到 W10，并且要单独预约。W01 和 W02 的第一轮验收不依赖它。
