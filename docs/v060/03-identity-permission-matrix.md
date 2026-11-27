# V0.6.0 身份与权限矩阵

执行者：Grok。本文只记录边界和待核实项。W00 不修改 `app/auth.py`、token 文件、GitHub 规则、宿主用户或数据库角色。

共享 GitHub 登录名 `layho` 和评论里的 `[GROK_EXECUTOR]` 只用于区分协作发言。它们不是密码学身份，不能作为 MacCI、发布器或用户验收的证明。

## 当前实现与目标身份

| 主体 | 当前基线里实际靠什么识别 | 目标 | 本阶段是否改代码 |
| --- | --- | --- | --- |
| Core 网页会话 | `app/auth.py` 的会话 Cookie、PBKDF2 和 CSRF | 保持人机会话，不持有高权限 GitHub token | 否 |
| APP 服务 | 环境变量 `APP_API_TOKEN`。比对成功即 `authorize_request()` 返回 `service` | 按仓库、应用和操作分开的服务身份，默认拒绝 | 否 |
| Agent | 评论中的 agent 字段，枚举为 Codex、Grok、Claude、DeepSeek、Qwen | 受权开发身份。证明与人、MacCI、发布器分离 | 否 |
| MacCI | commit status `macmini/isolated-tests-v1`，且 `creator.login` 为 Owner | 独立测试身份或签名，绑定仓库、head、base、merge、run_id、过期时间 | 否 |
| 发布器 | `publish_main.py` 使用同一 Owner 身份写 `BASE_PUBLISHED` 并推 main | 只消费已验收候选的发布身份 | 否 |
| 宿主部署 | `manage.sh` 的 `load_env()` 把共享 token 导入更新进程 | 部署身份只接收已验证产物，不继承测试和发布的写权限 | 否 |
| HostOps | 本地只读操作 `host.health`、`service.inventory`、`agent.snapshot` | 可选诊断身份。不是 Agent，不获得 dispatch 或 MCP 扩大 | 否 |
| 用户验收 | 网页创建 Owner Issue，发布器再看本地 receipt | 独立验收证明，绑定精确候选。网页和候选都不能自签发布权 | 否 |

统一凭据入口的目标是：人配置一次，由可信宿主按操作发给最小权限代理。当前代码还不是这个形态。`scripts/sync_feedback.py` 的 `feedback_token()` 会依次尝试 `APP_GITHUB_TOKEN`、`APP_GITHUB_PR_TOKEN`、`APP_GITHUB_FEEDBACK_TOKEN`。本文件不记录这些变量的值，也不要求任何人粘贴 token。

## 操作边界

行是操作，列是目标身份。`允许` 表示契约目标，不是当前已授权。`拒绝` 包括当前和目标。空白的当前行为见上表，不把空白写成已经允许。

| 操作 | Core 会话 | APP 服务 | Agent | MacCI | 发布器 | 宿主 | HostOps | 用户验收身份 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 读取本应用诊断摘要 | 允许 | 仅本应用 | 仅被授权任务 | 仅自己的回执 | 仅发布回执 | 仅本机部署记录 | 允许只读 | 仅验收状态 |
| 写入反馈或任务评论 | 允许本应用范围 | 拒绝跨应用 | 仅任务回执 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| 领取开发 Run | 拒绝 | 拒绝 | 允许，需有效策略 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| 写入测试 status | 拒绝 | 拒绝 | 拒绝 | 允许 | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| 解除 Draft、合并 preview | 现有手动入口保留 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 仅当预授权策略和精确门禁都满足 | 拒绝 | 拒绝 |
| 部署切换 | 拒绝直接执行 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 允许，消费已验证产物 | 拒绝 | 拒绝 |
| 验收或拒绝候选 | 现有网页入口保留 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 允许，绑定四元组 |
| 发布 main | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 仅在验收证明有效时 | 拒绝 | 拒绝 | 拒绝 |
| 修改策略、门禁或权限范围 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝。必须另一次用户授权 |
| 生产数据库迁移或回滚 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝自动执行 | 拒绝 | 拒绝 |
| HostOps 非只读或任意命令 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 |

候选身份请求发布时，策略评估返回 `POLICY_DENIED`，且评估没有副作用。这在夹具样例 `policy_evaluate_deny` 中。

## 预授权

普通预授权的目标范围：同一仓库、已批准的开发范围内、精确 MacCI 通过、策略检查通过、无冲突、集成队列允许、验收冻结允许。满足后才自动解除 Draft、合并 preview 和部署。

下列事项不在普通预授权里，必须单独授权：

- 改变安全门禁、身份或分支规则
- 宿主提权或扩大文件、网络、数据库权限
- 生产数据读取、迁移、回滚或故障注入
- 超出原任务路径的改动
- 把 HostOps 从只读扩到 dispatch 或 MCP 写操作

预授权策略存放在可信宿主，不存放在候选分支可改的文件里。候选 PR 修改策略文件本身就超出普通预授权。

Main 仍只在真实用户验收之后发布。验收证明过期、候选四元组变化或证明被撤销之后，旧证明无效。

当前基线还没有这份策略的持久化和执行器。`replacement_allowed()` 仍要求 Owner 的修复标记。所以 F08、A10 和 SUP-F08 分类为独有待做，而不是已生效。

## 分支保护待核实

2026-11-27 本轮只读结果：

| 请求 | 结果 | 可以得出的结论 |
| --- | --- | --- |
| GET `/repos/layho/web-app-init-base-env/branches/main/protection` | HTTP 403，`Resource not accessible by integration` | 这个凭据不能读 classic protection。不能写成启用，也不能写成关闭 |
| GET 同路径的 preview | HTTP 403，同一错误 | 同上 |
| GET `/repos/layho/web-app-init-base-env/rulesets` | HTTP 200，正文 `[]` | 该凭据可见的仓库级 ruleset 列表为空 |
| 组织级 ruleset、bypass 名单、管理员例外 | 未读到 | 仍未证实 |

先前评论里“protection.enabled=false”不是本轮重新读到的结果，不能拿来弥补 403。A11 保持未证实。

W02 的只读核对清单：

1. 用有 `administration:read` 的只读身份分别读 main 与 preview 的 classic protection。
2. 读仓库 rulesets，以及会作用于该仓库的组织 rulesets。
3. 记录强制状态检查的 context、是否禁止强推、谁可以 bypass。
4. 把原始规则编号和查询时间写进证据，不写 token。
5. 403、空列表和未查询组织规则，都保持“未证实”，产品门禁继续 fail closed。

应用内的冻结、精确 status 和 Owner 审查是现有门禁，不是 GitHub 服务端强制规则的替代证明。

## 证明的最小契约

生产身份验证尚未实现。在 `production_verification_enabled` 变为 true 之前，契约只接受隔离测试身份：kind 为 test，并且 proof_ref 以 `test-proof://` 开头。agent、共享 Owner 登录名和评论标记都不能通过。

证明字段是 signer_id、scope、issued_at、expires_at、nonce、payload_hash、alg。草案中的 alg 名称是 ed25519。服务端用自己的时钟拒绝过期证明，拒绝重复 nonce，并拒绝已撤销的 signer_id。撤销立即生效。

处理顺序是先认证、再授权、最后才查幂等记录。前两步失败时不创建幂等记录。重放返回当时的公开结果和对象的当前状态，不返回明文租约。

用户验收的 signer kind 是 `user_acceptance`。MacCI、Agent、候选和网页会话都不是这条通道。验证实现之前，即使带上 `user_acceptance` 也不能放行生产发布。本文件不改 `app/auth.py`，也不启用这套证明。
