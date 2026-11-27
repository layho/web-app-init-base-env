# V0.6.0 架构契约草案

执行者：Grok。状态：草案，schema_version = 1。机器可读副本是 `tests/fixtures/v060/w00/contract.json`。评审改语义时递增 schema_version，不静默改已发布的 v1 含义。

本草案不挂路由，不新建数据库，不替换现有 `integration_queue`、HostOps 任务或反馈状态。现有状态如何读旧、写新，见兼容方案。

## 根规则

七个对象分开建模：Task、Run、Candidate、Release、Deployment、Resource、Event。任何一个对象的 status 都不能代替另一个对象的证据。

每条写请求带调用身份、scope、expected_revision、idempotency_key，并返回 request_id 和真实执行状态。读请求无副作用。事件正文、PR 正文和候选文件都不能授予权限。

跨 GitHub 和数据库不承诺 exactly-once。投递是至少一次，加上幂等和可重建投影。远端结果未知时进入对账，不猜测成功。

涉及候选的记录必须同时有 head、base、merge、tree。涉及部署的记录必须有 artifact digest、deployment_id 和上一健康 SHA。

## 标识

| 字段 | 规则 |
| --- | --- |
| task_id / run_id / event_id / deployment_id / resource_id | 服务端生成，调用方不能挑选已存在的值来覆盖历史 |
| producer_id + producer_seq | 每个生产者单调递增。序号是整数，从 1 起 |
| schema_version | 正整数。接收方只接受自己声明支持的版本 |
| occurred_at | 生产者时钟，ISO-8601 UTC |
| received_at | 接收方时钟，由服务端填写，调用方填写无效 |
| idempotency_key | 调用方提供，作用域是 actor + scope + 操作名 |
| expected_revision | 调用方看到的对象修订号。不一致返回 CONFLICT_REVISION |

candidate 的身份是四元组的哈希，不是自增号。四元组任一变化都是新候选，旧候选进入 SUPERSEDED，证据不迁移。

## Task

阶段顺序：

`feedback_sync → claim → develop → review → test → integrate → preview_merge → deploy → accept → publish → close`

除下面这一条，禁止跳阶段：

- `accept → develop`，仅当独立策略已经记录 `repair_authorized`。这不是候选自己声明的修复。

`close` 没有出边。多 PR 任务在全部必要候选都已发布、且任务验收已记录之前，不能进入 `close`。

阶段上的执行词是当前 Run 的投影，不是第二套可随意覆盖的 status：

| 阶段执行词 | Run 状态 |
| --- | --- |
| READY | 没有未完成 Run，或 Run 为 PENDING |
| RUNNING | LEASED 或 RUNNING |
| SUCCEEDED / FAILED / RETRY_WAIT / BLOCKED / UNKNOWN / SUPERSEDED / CANCELLED | 同名 Run 状态 |

未知执行器保持 READY 或 BLOCKED，并返回 UNKNOWN_EXECUTOR。催促只追加一条可追踪请求，不把阶段改成 RUNNING。

## Run

状态：`PENDING`、`LEASED`、`RUNNING`、`SUCCEEDED`、`FAILED`、`RETRY_WAIT`、`BLOCKED`、`UNKNOWN`、`SUPERSEDED`、`CANCELLED`。

终态是 `SUCCEEDED`、`FAILED`、`SUPERSEDED`、`CANCELLED`。终态没有出边。新的尝试使用新 run_id。

| 从 | 到 | 条件 |
| --- | --- | --- |
| PENDING | LEASED | 颁发新 fencing 令牌 |
| LEASED | RUNNING | 同一令牌的开始心跳 |
| LEASED | PENDING | 尚未开始，持有者主动释放 |
| LEASED | CANCELLED / SUPERSEDED | 授权取消或新修订替换 |
| RUNNING | SUCCEEDED / FAILED / RETRY_WAIT / BLOCKED | 执行器在租约有效期内用同一令牌提交终态或等待 |
| RUNNING | UNKNOWN | 租约到期且没有终态回执 |
| RUNNING | CANCELLED / SUPERSEDED | 授权取消或替换 |
| RETRY_WAIT | RUNNING / FAILED / BLOCKED / CANCELLED | 新的有效租约或明确结束 |
| BLOCKED | PENDING | 重新授权之后才能再领取 |
| BLOCKED | CANCELLED | 授权取消 |
| UNKNOWN | RETRY_WAIT | fencing 证明旧执行器已停，且副作用未发生或已完整回滚 |
| UNKNOWN | FAILED | fencing 证明副作用已经失败且可核对 |
| UNKNOWN | CANCELLED | 对账之后的人工关闭 |
| PENDING | CANCELLED / SUPERSEDED | 尚未领取时取消或替换 |

UNKNOWN 不能因为超时回到 PENDING。没有证明时保持 UNKNOWN，错误码 SIDE_EFFECT_UNKNOWN。

租约规则：

- 令牌只在领取响应里出现一次，存储哈希，比较使用恒定时间比较。
- 续租必须带当前令牌、当前 base 和未过期时间。过期令牌不能续、不能完成。
- 新令牌使旧令牌失效。旧令牌的完成请求返回 LEASE_FENCE_REJECTED，对象保持原状态；若到期点已经把 Run 写成 UNKNOWN，则保持 UNKNOWN。
- 一个 Run 同时只有一个有效租约。

## Candidate

状态：`REGISTERED → SUPERSEDED | INVALID | INTEGRATED`。后三个是终态。

`INTEGRATED` 只允许精确远端观察：仓库、head、base、merge、tree 和父提交关系都匹配。测试通过、审查通过或本地 journal 都不是 INTEGRATED。

新的 head 或 base 创建新候选。旧测试回执、旧验收和旧部署成功都不能复用。

## Release

状态：`PLANNED → ACTIVE → INTEGRATING → ACCEPTANCE → PUBLISHING → PUBLISHED → ARCHIVED`。

另外两条带条件的回边：

- `ACCEPTANCE → INTEGRATING`：只在 `repair_replacement` 已由策略记录时。
- `PUBLISHING → ACCEPTANCE`：发布没有形成可信回执，fail closed。

同一时刻只有一个版本可以处于 ACTIVE、INTEGRATING、ACCEPTANCE 或 PUBLISHING。其他版本可以停留在 PLANNED。PLANNED 不能直接进入 INTEGRATING。`ARCHIVED` 没有出边。

同版本的不同 SHA 仍是不同候选，必须各自验证。

## Deployment

`PLAN → STAGE → VERIFY → SWITCH → HEALTH → COMMIT`

失败边：

- `STAGE`、`VERIFY`、`SWITCH`、`HEALTH` 都可以进入 `RECONCILE`。
- `RECONCILE → ROLLBACK`，或在结果无法证明时 `RECONCILE → SAFE_FROZEN`。
- `ROLLBACK → VERIFY_OLD → ROLLED_BACK`。旧健康无法证明时 `VERIFY_OLD → SAFE_FROZEN`。

`PLAN → COMMIT` 只表示 no_change，并且同时满足：工作树与上一健康清单一致、服务健康、没有未决 journal。Git HEAD 相等本身不够。

`COMMIT`、`ROLLED_BACK`、`SAFE_FROZEN` 是终态。生产数据库迁移不在这些边上自动执行；请求若包含自动迁移，返回 PRODUCTION_DB_FORBIDDEN。

## Resource

`PLANNED → RESERVED → APPLYING → APPLIED`

`APPLYING → UNKNOWN → RECONCILE`。对账之后进入 `APPLIED`、`ROLLED_BACK` 或 `SAFE_FROZEN`。

预约可以 `RESERVED → RELEASED`。已应用资源进入 `RELEASING` 必须带 `explicit_authorization`。卸载默认先产生 dry-run 计划，dry-run 不是 `RELEASING`。

重建保留业务数据和凭据，除非授权范围明确包含删除。

## Event

`DURABLE_LOCAL → OUTBOX → DELIVERED | DELIVERY_UNKNOWN | QUARANTINED`

`DELIVERY_UNKNOWN` 可以用同一 event_id 回到 `OUTBOX`，或进入 `QUARANTINED`。`DELIVERED` 和 `QUARANTINED` 没有出边。新的业务事实使用新 event_id，不改旧事件。

必填：event_id、schema_version、event_type、producer_id、producer_seq、occurred_at、idempotency_key。候选类事件再加四元组。部署类事件再加 deployment_id、artifact digest、previous_healthy_sha。

投影水位等于已连续应用到的 producer_seq。缺口事件可以落盘，但 `applied` 为 false，水位不前进。旧序号不能把水位拉回去。

同一 producer_seq 再次到达：

- 载荷哈希相同：返回原 request_id，码 IDEMPOTENCY_REPLAY。
- 载荷哈希不同：IDEMPOTENCY_PAYLOAD_MISMATCH，原事件保持不变。

## 幂等、并发和重放

- 写操作的幂等范围是 actor、scope、操作名和 idempotency_key。
- 相同键和相同规范化载荷返回第一次的 request_id 与状态。
- 相同键和不同载荷返回 IDEMPOTENCY_PAYLOAD_MISMATCH。
- expected_revision 不匹配返回 CONFLICT_REVISION，不部分写入。
- 重放不创建第二个 Run、不推动第二次外部副作用。
- 乱序事件按上一节只落盘不应用。
- 读接口不写水位、不续租、不发外部请求。

## 错误码

完整表在 `contract.json` 的 `error_codes`。草案包含：CONFLICT_REVISION、LEASE_FENCE_REJECTED、IDEMPOTENCY_REPLAY、IDEMPOTENCY_PAYLOAD_MISMATCH、CANDIDATE_STALE、EVIDENCE_MISSING、POLICY_DENIED、NOT_AUTHORIZED、UNKNOWN_EXECUTOR、SIDE_EFFECT_UNKNOWN、SCHEMA_UNSUPPORTED、RATE_LIMITED、DEPENDENCY_UNSATISFIED、RESERVATION_CONFLICT、FREEZE_ACTIVE、PRODUCTION_DB_FORBIDDEN、PLATFORM_UNSUPPORTED。

IDEMPOTENCY_REPLAY 的 HTTP 状态沿用第一次成功响应。其余冲突使用 409，拒绝使用 403，调用方载荷不合法使用 422。

## 提议路径

下列路径是逻辑草案，当前进程没有这些路由。现有 `/api/v1/feedback`、`/api/v1/development-tasks`、`/api/v1/system-logs`、`/api/v1/deploy/*` 和 `/api/v1/releases/*` 保持原样。

| 操作 | 方法与路径 | 副作用 |
| --- | --- | --- |
| Task.claim | POST `/api/v1/control/tasks/{task_id}:claim` | 有，颁发租约 |
| Task.renew | POST `/api/v1/control/tasks/{task_id}:renew` | 有，只延长当前租约 |
| Task.complete | POST `/api/v1/control/runs/{run_id}:complete` | 有，提交本轮结果 |
| Task.request_action | POST `/api/v1/control/tasks/{task_id}:request-action` | 有，只记录催促或重试请求 |
| Event.append | POST `/api/v1/control/events` | 有，先本地落盘 |
| Event.query | GET `/api/v1/control/events` | 无 |
| Policy.evaluate | POST `/api/v1/control/policy:evaluate` | 无 |
| Policy.authorize / revoke | POST `/api/v1/control/policy:authorize` 与 `:revoke` | 有，且不能由候选身份调用 |
| Candidate.register / supersede | POST `/api/v1/control/candidates` 与 `/{id}:supersede` | 有 |
| Candidate.next | GET `/api/v1/control/candidates:next` | 无 |
| CI.test / build / attest | POST `/api/v1/control/ci:test`、`:build`、`:attest` | 有，只接受可信 CI 身份 |
| Deploy.plan / apply / reconcile / rollback | POST `/api/v1/control/deployments:plan` 与 `/{id}:apply|:reconcile|:rollback` | 有 |
| Diagnostic.query | GET `/api/v1/control/diagnostics` | 无 |
| Resource.plan / apply / inspect | POST `:plan`、`/{id}:apply`，GET `/{id}` | plan 与 apply 有副作用，inspect 无 |
| Release.accept / reject / publish | POST `/api/v1/control/releases/{version}:accept|:reject|:publish` | 有 |
| Upgrade.plan / reconcile | POST `/api/v1/control/upgrades:plan|:reconcile` | 有 |

## 请求与响应示例

占位 SHA 不是真实提交。`proof_ref` 不是凭据。

领取：

```json
{
  "schema_version": 1,
  "actor": {"kind": "agent", "id": "grok", "proof_ref": "test-proof://agent/grok/1"},
  "scope": {"repo": "layho/web-app-init-base-env", "task_id": "tsk_example_w00"},
  "expected_revision": 3,
  "idempotency_key": "idem-w00-claim-example",
  "phase": "develop",
  "candidate": {
    "head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "base": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "merge": "cccccccccccccccccccccccccccccccccccccccc",
    "tree": "dddddddddddddddddddddddddddddddddddddddd"
  }
}
```

首次响应 `state=LEASED`、`replayed=false`。同一请求再送一次，响应同一 `request_id` 与 `run_id`，`replayed=true`，码 `IDEMPOTENCY_REPLAY`。只改 `expected_revision` 再复用同一幂等键，响应 `IDEMPOTENCY_PAYLOAD_MISMATCH`。

过期租约完成：

请求 `idempotency_key` 为 `idem-w00-stale-lease`，`lease_token` 为 `expired-or-replaced-token`。响应码 `LEASE_FENCE_REJECTED`，`state` 保持 `UNKNOWN`。

乱序事件：

`producer_seq` 为 9 且 `gap_after_seq` 为 7 时，响应 `state=DURABLE_LOCAL`、`applied=false`、`watermark_seq=7`。随后的 GET `/api/v1/control/events?producer_id=macci-test&after_seq=7` 返回 `side_effect=false` 和 `gap=true`，不补写远端。

策略拒绝：

候选身份请求 `release.publish` 时，`policy:evaluate` 返回 `allow=false`、`code=POLICY_DENIED`、`side_effect=false`。评估本身不发布。

部署计划请求必须带 `previous_healthy_sha` 和 `artifact_digest`。响应停在 `PLAN`，不表示已经切换流量。

以上示例与夹具中的同名样例一致。夹具测试检查写请求含四个必填字段，并检查终态没有出边。
