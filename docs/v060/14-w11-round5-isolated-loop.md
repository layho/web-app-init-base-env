# W11 第五轮：隔离闭环与未执行的 Preview 核对

这是记录，不是合并、部署、发布或宿主操作授权。本轮没有执行下面的 Preview 步骤。

## 隔离闭环实际调用

在临时目录里，`app.control_joint.isolated_loop.run_isolated_loop` 按这个顺序调用，生产开关保持关闭：

1. `DeliveryService.submit_feedback`，然后 `claim_task`、`bind_test_evidence`、两次 `apply_observation`。
2. `Scheduler.open_version` 和 `Scheduler.open_task`。
3. `DeployEngine` 经 `IsolatedDeploy.recover_unknown_switch` 走到 `SAFE_FROZEN`。它不切换宿主服务。
4. `DeliveryService.project` 和 `app.control_board.projection.project`。
5. `LifecycleService.call("cleanup.synthetic")`，只在已经是 `0700` 的清单目录里删除合成文件。`REAL_UNINSTALL`、`REAL_INSTALL`、`HOST_MUTATION` 仍为 false。
6. `issue_w05_receipt` 把回执写进交付日志的同一事务，再用 `query_operation_receipt` 核对。冲突不会改写已提交的 W05 回执，也不会改写 W04 查询结果。

W05 查询在签发头还没有这条事务时返回 `unknown`。收据文件里自算的 SHA、`issued` 和 `persisted` 不是签发。

人工动作在调用执行器之前把围栏留在 `RESERVED`。这个阶段的崩溃可以恢复。围栏变成 `ENTERED` 之后，只有隔离调度器的一致快照确实没有回执行才可以恢复。快照超时、超预算、目录不独占或快照撕裂都是 `blocked`/`unknown`，不能当成没执行，也不能据此重试。HTTP 状态本身不能证明没有副作用；低于 400 且没有精确可信回执仍然不是成功。

历史授权以已应用的 `authorization.granted` / `authorization.revoked` 序列为准。可写的 `authorized` 标志和跨修订重复使用的签名不能代替这个序列。

## 74 项

矩阵由 `app.control_joint.evidence.evidence_matrix` 按这次闭环真正返回的入口生成。`macci_receipt` 为 false，`public_running_sha` 为 null。A11 保持 blocked：本轮没有读取 main 或 preview 的分支保护。F11、U09、S10、D10、A12、I12、R10 保持 blocked。没有 `PRODUCTION_ACCEPTED`。

未接入的入口包括：W01–W10 的生产路由挂载、`manage.sh update`、`app_init.py` 安装、GitHub 远程客户端、MacCI runner、分支保护读取，以及 `execute_isolated` 的 merge、deploy、request_publish。后三项仍是 `PRODUCTION_CLOSED`。

## Preview 实机核对（只记录，不执行）

这些步骤需要 Owner 另一次明确授权后才能做。本轮没有执行任何一条。

1. 在 Preview 宿主上检出本 PR 的 HEAD，确认工作树干净，并且 `origin/preview` 仍是 `db19bff19307fa763fd50e94b4cedfda7181a141`。
2. 用只读的隔离配置启动服务，确认 `REAL_INSTALL`、`REAL_UNINSTALL`、`HOST_MUTATION` 为 false，merge、deploy、request_publish 仍返回 `PRODUCTION_CLOSED`。
3. 对一个临时任务走一遍反馈、调度、测试记录、交付记录、部署事务和验收投影，核对 W05 回执来自日志事务而不是手写 JSON。
4. 核对看板没有演示行，人工动作的未知结果不能换成新的幂等键重放。
5. 停止服务，确认没有安装单元、没有切换宿主服务、没有写入生产数据库。

需要 Owner 授权、本轮没有做的动作：

- 把本 PR 合并进 `preview` 或 `main`。
- 在 Preview 或生产宿主上安装、卸载、切换服务，或打开生产数据库。
- 把 `REAL_INSTALL`、`REAL_UNINSTALL` 或 `HOST_MUTATION` 设为 true。
- 放开 `execute_isolated` 的 merge、deploy 或 request_publish。
- 发布、打公开运行 SHA，或把本机测试写成 MacCI 回执。
- 读取或修改 GitHub 分支保护来闭合 A11。
- 新增或修改 GitHub Actions。
