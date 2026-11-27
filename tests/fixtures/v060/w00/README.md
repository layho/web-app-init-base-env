# V0.6.0 W00 契约夹具

这些 JSON 是设计草案的机器可读副本，不是生产配置。

- `contract.json`：实体、合法转换、错误码、请求响应示例。`implemented` 全部为 false，路径没有挂到 `app/main.py`。
- `items.json`：74 个原编号、补充项和指定 PR 的分类。

测试 `tests/test_v060_w00_contract.py` 只读取这两个文件和 `docs/v060/01-capability-matrix.md`。它不导入应用、不打开数据库、不读取环境变量里的凭据。

示例里的 SHA、proof 和 idempotency key 都是固定占位符。
