# W11 共享文件改动

Grok 是这些路径在本候选上的唯一写入者。列表给协调者登记，不是再授权别的 Agent 同时修改。

## 已有共享窗口

| 路径 | 窗口 | 改动 |
| --- | --- | --- |
| `README.md` | S-release-docs | 重排版本说明，标明 V0.6.0 候选未合并 |
| `docs/RELEASE_PLAN.md` | S-release-docs | 增加未合并的 V0.6.0 记录，不改已发布 SHA |
| `app/main.py` | S-main | include 只读 `/api/v1/control/board` |
| `frontend/src/main.tsx` | S-ui | 开发任务页挂只读控制面卡片 |
| `frontend/src/workspace.css` | 外壳 | 顶栏高度 48px。配色为统一深色；`docs/ui-design/v2/README.md` 以深色为现行规范 |

## 包内文件

这些文件原来由对应包独占。联合整改直接改在包实现上，避免再做一个会晋升替身的平行权威。

- W02：`app/control_identity/service.py`，`app/control_lifecycle/grant.py` 增加 `server_principal`
- W04：`app/control_scheduler/scheduler.py`
- W05：`app/control_delivery/service.py`，`app/control_delivery/ports.py`
- W06：`app/control_diagnostics/safe_io.py`，`redaction.py`，`service.py`
- W07：`app/control_deploy/journal.py`
- W08：`app/control_upgrade/service.py`
- W09：`app/control_lifecycle/service.py`，`journal.py`
- W10：`app/control_board/projection.py`，`service.py`

## 明确没改

- `deploy/macmini/manage.sh` 的 `update` 会调用 `deploy/macmini/control_journal_adapter.sh`。未设置隔离临时根时适配器拒绝，原更新路径不因此中止。`V060_JOINT_EXECUTE=isolated` 时只写临时日志并拒绝切换服务。
- `deploy/app-init/app_init.py --control-adapter` 调用 `deploy/app-init/control_lifecycle_adapter.py`。默认拒绝。隔离临时根上的 `status`、`plan`、`record` 只读写该目录，不安装、不卸载、不提权。`bootstrap.sh` 没有改为执行安装。
- 没有新增 `.github/workflows`。
- 没有修改生产数据库、凭据或真实服务。
