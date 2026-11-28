# V0.6.0 W11 联合集成候选

这是集成记录，不是 preview 合并、main 发布或生产启用授权。

建议 PR 基线是 `preview`。隔离祖先 `b2011968a9c14845b89fe6d15dda4a595699e9ec` 只用于核对 W00–W10 的分叉点，不作为 PR base。

## 串行并入

| 步骤 | 候选 | 并入后 HEAD | 并入后 tree | 方式 |
| --- | --- | --- | --- | --- |
| W00 | `2aa0c3ecd0a1ddf42594df1218691a0e6ed48411` | `2aa0c3ecd0a1ddf42594df1218691a0e6ed48411` | `86fab7e54c20f6e1614ab215a3e00d3398857461` | 从 `db19bff` 快进，历史经过 `b201196` |
| W01 | `33e65dc0925f03e60518a5c91a0e2254fd0fd12f` | `7a0559d47a431373d7b39d966d8734a3dc269324` | `b3e0a916afbd9ef731408c5f32e7f2e1386a3e54` | merge，父提交是 W00 与 W01 |
| W02 | `5dd0eb38e175a8a905836fbdbac5633ff9eb19ae` | `7ba5c31f051d56445185b54637440c126b2a7384` | `5d1a63f546913b0ac2a9b74d0cc94f3d133f7e8d` | merge |
| W03 | `1bc3f83c67cee103365a3dcfaed9e31db4793422` | `bb60e3aeda21d9e253a7139763854e57cc1e110d` | `b4cc6ea14e4adf0d308b71d9e6692ec7af507352` | merge，架构文档自动合并 |
| W04 | `33c9d88595cf2d18e32ab78c22d5d8499131547e` | `09ccccc02e6ead5d54da637fa1fab09623cdb86b` | `1cf96e4a626afe8212334fa59428eb07f60010aa` | merge |
| W05 | `29b3dbf227c561f530d17d21cc8f3751e29bd0af` | `264754848d659afacb996619c8f418ed157eeff1` | `b98d23c7f1c4fc89e3853fa3c92b20387af85ea6` | merge |
| W06 | `2bc963e735072ec75751b106e4ed6fe929519c51` | `e636a0e0a624f2c85b83a5dd35f05838a7fd47b2` | `c98ebdeb355ec308ed7f9d23dab806602329c9b3` | merge |
| W07 | `8f1ad70fd331ae18d8ea1a82d3c1b3be84f1feda` | `43facac3389b072e546283d665521a66835fd264` | `a49768c698102c644d8bf7b0a68d64a0780e5a74` | merge |
| W08 | `468d58615c1dc573f122e1eb052852138f7a7f5c` | `557ded9db0ec24c0f0fba8a4b93b4bd584351b43` | `0a8294b3c0718496b39ef84449475ef22cbd07b9` | merge |
| W09 | `8e6a42f23bfff06c65b5ccfe56babd33e2dfc642` | `410c87a6c608158d3446605b473d86aa4d7d516f` | `066360ba489397dd88f0eac500f57a9f77c4c41a` | merge |
| W10 | `cae7a668079f975554df2798966098691d16eec2` | `7f5a579c1c6537157836e85d568e44b45df2362a` | `cb08826c98afe3dd8f0617c0f27cc3a63769f453` | merge |

最终交付 HEAD 和 tree 以该提交之后的 `git rev-parse HEAD` 与 `git rev-parse HEAD^{tree}` 为准。上表只记录串行并入，不含其后的整改提交。

## 评审遗留

技术通过不免除这些条件。下表只记录本候选处理到哪一步。

| 包 | 评审 | 遗留 | 本候选 |
| --- | --- | --- | --- |
| W00 | 7217469025，收口 HEAD `2aa0c3ec` | 签名与游标由 W01/W02 实现；不是合并授权 | 已快进该历史 |
| W01 | 7226697522；此前 7225909658 等 | 与 W02 最新 HEAD 重建 tree，核对报告与数据库摘要，损坏拒绝 | 同一 tree 上保留 W01 引擎和 W02 `AuthzService`。本机测试不是独立回执 |
| W02 | 7223110361；此前 7222505164 等 | 拒绝记录不能当接受；A11 未证实 | `_block_publish` 在 `outcome != accepted` 时返回 `ACCEPTANCE_REJECTED`。生产发布仍关闭。A11 仍是 BLOCKED |
| W03 | 7227920019 | 真实 MacCI 容器、磁盘配额、进程树、产物与 W02 绑定；缺能力不得降级到宿主 | `host_capability()` 返回 BLOCKED，`host_downgrade` 为 false |
| W04 | 7228604869 | 对账要 payload digest 和预期权威；发布绑定要成功测试和完整候选 | `_reconcile_event` 要求字段一致且 `trusted_source` 等于 `event_authority`。同序号不同事件不采纳任一方。发布要求测试 outcome 和完整四元组，并把链的 tree 绑到选定版本树 |
| W05 | 7233342568 | 多对象 CAS、absent 证明、对账绑定 author/repo/digest | outbox 保存 `change_plan` 并围栏每个对象。absent 需要证明。对账缺 author、repo、scope 或 digest 时为 unknown，不把缺记录当成已接受 |
| W06 | 7233311341 | Darwin/Linux fd、FIFO、未知键、幂等键不明文 | `proc_fd_path` 区分平台。读文件使用 `O_NONBLOCK`。未知键只记 `[REDACTED]`。幂等 scope 保存哈希 |
| W07 | 7231884875 | 持锁后复验 generation/run/inode；manage.sh 独立适配 | `Fence.reclaim` / `takeover` 在持锁后复验。`manage.sh update` 调用 `control_journal_adapter.sh`。默认拒绝与隔离写入分开；隔离模式不切换服务 |
| W08 | 7234123859 | fresh 来源也按 `observed_at` 判断，不能一律 `stale=False` | `_mark_freshness` 覆盖可用的 fresh 来源。真实升级仍关闭 |
| W09 | 7234812462 | grant 必须来自服务端主体；inspect 过滤；清理竞态；端口代次不复用 | `server_principal()` 不一致则拒绝。inspect 按 repo/app/host 过滤。unlink 在 fd 仍打开时执行，并确认 nlink 降为 0。已释放代次不能再预约。真实安装仍拒绝 |
| W10 | 7234124286 | 服务端 principal、完整 read receipt、同序号不先采纳、React 真实路由 | `_gate` 使用服务端 principal。receipt 缺 digest、stream、epoch 或 seq 时不是权威。同序号冲突不进入 applied。页面读取已登录的 `/api/v1/control/board`，按授权范围投影真实记录；无配置或无记录时没有演示行 |

## 74 项

机器可读副本：`tests/fixtures/v060/w11/tracker.json`。F11、U09、S10、D10、A12、I12、R10 的 `integration_status` 是 `BLOCKED`。其余项是 `ISOLATED_CODE`：联合树里有对应包代码，没有 MacCI 回执，也没有公网运行 SHA。没有任何项是 `PRODUCTION_ACCEPTED`。

## BLOCKED 环境

本进程看不到独立 MacCI、macOS 宿主，也没有公网运行 SHA。容器运行时缺失时同样 BLOCKED。探测不启动容器，也不把命令降级到宿主执行。
