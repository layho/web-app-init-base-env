# 应用升级预检

这是 #156 C4 的最小预检单元，对应新增文件 `deploy/app-init/ownership_preflight.py` 与 `tests/test_ownership_preflight.py`。正式判定仍须由独立受控宿主运行时使用可信版本执行。本模块的 `authoritative` 固定为 false，语句固定为「预检结果不是安全批准、MacCI 回执或发布授权。」

## 输入

调用方提供：

- `baseline_sha` 与 `candidate_sha`：40 位小写十六进制。有变更时两者必须不同。
- `kind`：`app-change` 或 `foundation-upgrade`。
- `policy`：`schema` 为 1，`origin` 必须是 `trusted-caller`。`foundation`、`app`、`protected` 只接受显式文件或以前缀 `/` 结尾的目录。候选 diff 不能通过修改策略获得授权。
- `template_paths`：可信旧渲染模板中的路径清单。
- `changes`：实际 diff，每项含 `path`、`op`（`add`、`modify`、`delete`、`rename`）和 `symlink`。
- `observed_files`：调用方观察到、且本轮不应被 diff 触及的业务文件。

`frontend/src/`、`app/` 以及带通配符的规则不能登记为基座所有。更具体的文件可以登记，例如 `app/main.py`。同一路径被同等具体的 app 与 foundation 规则同时覆盖时拒绝。

## 判定

- 模板外且未声明为核心的业务文件，在应用变更中允许新增或修改；不在 diff 中的 `observed_files` 记为保留。
- 基座升级只能修改已出现在可信模板清单、且明确登记为 foundation 的文件。
- 应用变更修改 foundation 文件、基座升级触及 app 文件或模板外文件、模板内归属不明、删除、重命名、符号链接、超限文件、路径穿越、大小写冲突、重复路径，以及修改 `deploy/app-init/ownership_preflight.py` 或 `protected` 路径，均拒绝。
- `describe_tree_changes` 只比较两个目录的常规文件，不跟随符号链接，不读取链接目标。

测试夹具里的策略只服务于上述用例，不是已冻结的生产 manifest。

## 尚未接线

当前不修改 `app_init.py`、`upgrade_plan.py`、`upgrade_pr.py`、`upgrade_scheduler.py`、`upgrade_auto_merge.py`、`manage.sh`、前端、依赖锁或发布说明。预检通过也不表示可以合并、部署或发布。后续宿主接线需要单独预约，并与 #145、#153 的可信调用端对齐；生成应用中的副本不能替代该调用端。
