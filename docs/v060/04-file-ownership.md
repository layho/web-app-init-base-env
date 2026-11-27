# V0.6.0 文件所有权与并行开发

执行者：Grok。代码整合负责人是 Grok。接口冲突由 ChatGPT 裁决。高风险权限、生产数据库和宿主切换仍要用户单独授权。共享文件按窗口串行。W00 没有预约生产路径，也没有创建下表里标成新增的文件。

路径分两类。已存在：当前基线里有这个文件。新增：将来的工作包才能创建，本 PR 不创建。

## 工作包路径

| 包 | 路径 | 依赖 | 整合 |
| --- | --- | --- | --- |
| W01 | 新增 `app/control_events/`、新增 `app/control_routes_w01.py`。然后才改已存在的 `app/feedback_progress.py`、`app/operations.py`、`deploy/macci/runner.py` | W00 评审。最终验收要有持久 outbox、inbox 和 reducer | Grok |
| W02 | 新增 `app/control_identity/`、新增 `app/control_routes_w02.py`。已存在的 `app/auth.py`、`app/release_acceptance.py`、`app/request_limits.py` 只在生产启用被单独授权后改 | W00。覆盖 A01、A02、A03、A07、A08、A09、A10、A11、I07 的隔离实现 | Grok |
| W03 | W03 先交付隔离契约和构建产物：新增 `deploy/macci/isolated_build.py`。不在 W07 之后回头改 `deploy/macmini/manage.sh` | W02 的身份边界。禁止生产库迁移 | Grok |
| W04 | 已存在的 `app/integration_queue.py`、`deploy/macci/preview_journal.py`，以及 `deploy/macci/merge_preview.py` 的调度入口。新增 `app/control_routes_w04.py` | W01、W02 | Grok |
| W05 | 已存在的 `app/feedback_delivery.py`、`scripts/sync_feedback.py`、`scripts/sync_development_tasks.py`、`app/pr_tracking.py`、`deploy/macci/publish_main.py`、`app/manual_pr_actions.py`。新增 `app/control_routes_w05.py` | W01、W02、W04、W07 | Grok |
| W06 | 已存在的 `deploy/macci/runner.py` 失败摘要、`deploy/hostops/diagnostics.py`、`app/hostops.py`、`app/hostops_tasks.py`、`frontend/src/OperationsPage.tsx`、`frontend/src/HostOpsPage.tsx`。新增 `app/control_routes_w06.py` | W01、W02、W04，并消费 W07 的真实部署事件。解析器可以先用夹具，最终验收仍要接上 W07 | Grok |
| W07 | 在 W03 的隔离契约和构建产物交付之后，接管已存在的 `deploy/macmini/manage.sh`。已存在的 `scripts/apply_migrations.py` 只增加只读校验，不自动迁移。新增 `app/control_routes_w07.py` | W01、W02，以及 W03 已经交付的隔离契约。不是等 W03 来改 manage.sh | Grok |
| W08 | 已存在的 `deploy/app-init/app_init.py` 中的 `copy_foundation()`、`app/module_sdk.py`、`app/foundation_version.py`、`deploy/app-init/upgrade_plan.py`、`upgrade_pr.py`、`upgrade_scheduler.py`、`upgrade_auto_merge.py`、`upgrade_runtime_auto.py` | W03、W04、W05、W06、W07 | Grok |
| W09 | 已存在的 `deploy/app-init/bootstrap.sh` 和 `app_init.py` 的资源阶段。Linux 适配目录标为新增，本 PR 不创建 | W02、W03、W07、W08 | Grok |
| W10 | 只拥有已存在的界面文件：`frontend/src/DevelopmentTasksPage.tsx`、`frontend/src/OperationsPage.tsx`、`frontend/src/main.tsx`、`app/integration_diagnostics.py`。不拥有各包 API 路由 | W01、W04、W05、W06 | Grok |
| W11 | 已存在的 `README.md`、`docs/RELEASE_PLAN.md`，以及跨包验收记录 | W01–W10 | Grok 汇证据，用户验收 |

## 串行窗口

| 窗口 | 路径 | 顺序 | 释放 |
| --- | --- | --- | --- |
| S-contract | 已存在的 `tests/fixtures/v060/w00/contract.json` | 仍由单一 PR 修改 | 评审明确冻结某个 schema_version |
| S-auth | 已存在的 `app/auth.py`、`app/release_acceptance.py`、`app/request_limits.py` | 只在 W02 的生产启用获得单独授权后 | 该授权 PR 合并或放弃 |
| S-feedback | 已存在的 `app/feedback_progress.py`，然后 `app/feedback_delivery.py`、`scripts/sync_feedback.py` | W01 先改校验，W05 再改同步 | 前一包合并后再预约 |
| S-events | 已存在的 `app/operations.py`、`deploy/macci/runner.py` | W01 先改落盘、inbox 和兼容读取，W06 再改摘要 | 同上 |
| S-merge | 已存在的 `deploy/macci/merge_preview.py`、`deploy/macci/publish_main.py`、`app/manual_pr_actions.py` | W02 身份判断、W04 队列入口、W05 冻结与收尾 | 同上 |
| S-queue | 已存在的 `app/integration_queue.py`、`deploy/macci/preview_journal.py` | 只给 W04 | 同上 |
| S-build | 新增 `deploy/macci/isolated_build.py` | W03 先交付隔离契约和构建产物。W07 不阻塞这个新文件 | W03 的隔离夹具通过 |
| S-host | 已存在的 `deploy/macmini/manage.sh` | W03 先交付隔离契约之后，由 W07 接管这个窗口。W03 不在 W07 之后修改它 | W07 合并或放弃 |
| S-init | 已存在的 `deploy/app-init/app_init.py`、`deploy/app-init/bootstrap.sh` | W08 与 W09 串行，窗口内只有一个打开的 PR | 该 PR 合并或放弃 |
| S-main | 已存在的 `app/main.py` | 各包只把已经存在的 `app/control_routes_w01.py` 到 `app/control_routes_w07.py` include 进来。一次一个包 | 该 include 合并或放弃 |
| S-ui | 已存在的 `frontend/src/main.tsx`、`frontend/src/DevelopmentTasksPage.tsx`、`frontend/src/OperationsPage.tsx` | 只给 W10。不包括其他前端文件 | 同上 |
| S-release-docs | 已存在的 `README.md`、`docs/RELEASE_PLAN.md` | 只给 W11 | 版本交付检查完成 |

W01 和 W02 的新增目录可以同时做。任一侧要改 S-auth、S-merge、S-host 或 S-main 时，先预约。W10 不把 W01 到 W09 的路由注册留到最后。
