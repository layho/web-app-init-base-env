# V0.6.0 W00 契约与基线

执行者：Grok。Issue [#204](https://github.com/layho/web-app-init-base-env/issues/204)。父方案 [#199](https://github.com/layho/web-app-init-base-env/issues/199)。

基线：`main` = `preview` = `db19bff19307fa763fd50e94b4cedfda7181a141`。本文档集只描述草案和盘点。W01–W11 尚未实施。运行门禁、凭据、宿主配置和生产数据库都没有改。

契约在 ChatGPT 评审前不是冻结接口。`tests/fixtures/v060/w00/contract.json` 的 `normative` 为 false。

| 文档 | 内容 |
| --- | --- |
| [01-capability-matrix.md](01-capability-matrix.md) | 74 个原编号、#199 补充、#197，以及 #196/#200/#186/#161/#191/#155 的分类 |
| [02-architecture-contract.md](02-architecture-contract.md) | 状态机、合法转换、版本化 schema、幂等与 fencing、错误码、读写示例 |
| [03-identity-permission-matrix.md](03-identity-permission-matrix.md) | 身份边界、预授权、分支保护待核实项。不改变实际权限 |
| [04-file-ownership.md](04-file-ownership.md) | W01–W11 路径、整合负责人、依赖和串行窗口 |
| [05-compatibility-migration.md](05-compatibility-migration.md) | 旧事件、缓存、回执、应用和宿主的兼容，以及 Linux #197 适配契约 |
| [06-test-contract.md](06-test-contract.md) | 测试分层、故障注入、证据缺口和补证责任 |
| [07-w01-w02-plan.md](07-w01-w02-plan.md) | W01/W02 可并行子任务、验收、后续依赖和 SLO 建议 |

夹具目录 `tests/fixtures/v060/w00/` 与 `tests/test_v060_w00_contract.py` 只读本地 JSON，不访问网络、数据库或宿主。
