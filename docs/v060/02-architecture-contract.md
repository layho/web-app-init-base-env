# V0.6.0 架构契约草案

执行者：Grok。状态：草案，schema_version = 2。对应评审评论 7216272486。`normative` 仍为 false。未经 REVIEW_APPROVED 不得冻结，不得据此启动 W01/W02。

机器可读副本是 `tests/fixtures/v060/w00/contract.json`。夹具测试会执行其中的转换、幂等、候选身份和流投影，而不是只检查字段是否存在。路径没有挂到 `app/main.py`。

## 根规则

Task、Run、Candidate、Release、Deployment、Resource、Event 分开建模。Event 的事实和投递回执也分开。一个对象的状态不能代替另一个对象的证据。

写请求带调用身份、scope、expected_revision、idempotency_key。读请求无副作用。事件正文和候选文件不能授予权限。

时钟只用服务端时间判断租约、证明过期和 received_at。调用方的 occurred_at 只作记录，不能延长租约，也不能冒充 received_at。调用方若自行填写 received_at，请求被拒绝。

跨 GitHub 和数据库不承诺 exactly-once。投递至少一次，加上幂等和对账。

## 标识

| 字段 | 规则 |
| --- | --- |
| run_id | 一次 attempt 一个新值。旧 run 不能继续执行 |
| attempt | 在同一个 task、阶段和 subject 上单调增加 |
| lease_generation | 每次成功领取或续租加一。只有当前代次的令牌有效 |
| candidate_id | 见下方规范编码。任务或仓库不同就是不同候选 |
| stream_id | `{producer_id}:{channel}`。channel 是 task、candidate、deployment、resource、diagnostic、authorization 之一 |
| stream_epoch | 从 1 开始。生产者确认序号不能连续时才能加一，不能把旧序号重新从 1 写回同一 epoch |
| producer_seq | 在同一个 stream_id 和 stream_epoch 内单调增加，并且先落盘再确认 |

候选规范编码是 UTF-8，没有结尾换行，六行依次为：

```text
v1
repo_id
task_id
head
base
merge
tree
```

`candidate_id` 等于 `cand_` 加上这段文本的 sha256 十六进制。SHA 必须是小写。只改仓库或只改 task_id 都会得到另一个 id。证据不能跨这个 id 复用。

## Task 与候选链

阶段词仍然是 feedback_sync、claim、develop、review、test、integrate、preview_merge、deploy、accept、publish、close。它们描述聚合，不表示只有一个 PR。

每个必要 PR 或修复线是一条候选链，状态为 OPEN、TESTING、INTEGRATING、DEPLOYING、ACCEPTING、SATISFIED、FAILED、REJECTED、SUPERSEDED。

| 从 | 到 | 条件 |
| --- | --- | --- |
| OPEN | TESTING | 开始这一条链的测试 |
| TESTING | INTEGRATING | 这一条链的测试成功 |
| TESTING | FAILED | 测试失败 |
| FAILED | SUPERSEDED | 新 attempt 使用新 run_id，旧链结束 |
| TESTING / INTEGRATING / OPEN | SUPERSEDED | 候选四元组或任务关联变了 |
| INTEGRATING | DEPLOYING | 精确远端观察 |
| DEPLOYING | ACCEPTING | 部署已 COMMIT |
| ACCEPTING | SATISFIED | 用户验收 |
| ACCEPTING | REJECTED | 用户拒绝 |
| REJECTED | SUPERSEDED | 已授权修复，并且新候选使用新链 |

同一条链没有从 REJECTED 回到 TESTING 的边。修复是新链。旧测试和旧验收不复用。

聚合边只有在对应的必要链都已经满足时才能走。部分交付保持任务打开，并同时列出已满足和未满足的链。close 没有出边，任务不能重开。关闭之后的新要求使用新 task_id，可以带 follows_task_id 指向旧任务。

## Run

状态：PENDING、LEASED、RUNNING、SUCCEEDED、FAILED、RETRY_WAIT、BLOCKED、UNKNOWN、SUPERSEDED、CANCELLED。

终态是 SUCCEEDED、FAILED、SUPERSEDED、CANCELLED。RETRY_WAIT 不是继续执行的状态。

| 从 | 到 | 条件 |
| --- | --- | --- |
| PENDING | LEASED | 领取。颁发新代次，明文令牌只在这一次响应出现 |
| LEASED | RUNNING | 同一代次的开始心跳 |
| LEASED 或 RUNNING | 同一状态 | 续租。代次加一，旧令牌立即失效。这不是新 run |
| LEASED | PENDING | 尚未产生外部副作用，持有者释放 |
| LEASED | UNKNOWN | 服务端时钟上租约到期，或取消、替换时还不能证明没有副作用 |
| RUNNING | UNKNOWN | 同上 |
| LEASED | CANCELLED 或 SUPERSEDED | 已 fencing，并且证明没有外部副作用 |
| RUNNING | CANCELLED | 已 fencing，并且副作用不存在或已经补偿 |
| RUNNING | SUPERSEDED | 已 fencing，并且副作用已经分类且不是成功完成 |
| RUNNING | SUCCEEDED / FAILED / RETRY_WAIT / BLOCKED | 当前令牌在过期前提交，并且副作用已经分类 |
| UNKNOWN | SUCCEEDED | 可信对账证明副作用已经成功 |
| UNKNOWN | FAILED | 可信对账证明副作用已经失败 |
| UNKNOWN | RETRY_WAIT | 可信对账证明副作用没有发生 |
| RETRY_WAIT | SUPERSEDED | 新 attempt 已经创建了新 run_id |
| RETRY_WAIT | RUNNING | 不存在。这条边会绕过领取和开始 |
| BLOCKED | SUPERSEDED | 重新授权之后创建新 run |
| PENDING | CANCELLED 或 SUPERSEDED | 还没有领到租约 |

外部副作用必须带上 run_id 和 lease_generation。旧代次不能提交副作用。若旧代次可能已经发出外部动作，Run 进入 UNKNOWN，直到对账把它分成成功、失败或未发生。没有证明时不能取消成 CANCELLED。

丢失明文令牌时，同一幂等键的重放不返回明文租约。恢复方法是把该 run 对账到 UNKNOWN 或确认没有副作用，然后用新 run_id 和新的幂等键重新领取。同一幂等键不能签发第二个令牌。

## Candidate、Release、Deployment、Resource

Candidate：REGISTERED 只能到 SUPERSEDED、INVALID 或 INTEGRATED。INTEGRATED 需要精确远端观察。

Release 增加 PUBLISH_UNKNOWN。PUBLISHING 不能因为缺少回执直接回到 ACCEPTANCE。推送结果未知时进入 PUBLISH_UNKNOWN。只有对账证明推送没有发生，才能从 PUBLISH_UNKNOWN 回到 ACCEPTANCE。精确观察到 main 之后才到 PUBLISHED。

Deployment 在 HEALTH 成功后进入 COMMIT_PENDING。提交回执落盘才到 COMMIT。回执丢失进入 RECONCILE，不把未知写成 COMMIT。ROLLBACK 失败进入 SAFE_FROZEN。SAFE_FROZEN 没有出边。授权后的恢复是新的 deployment_id，并引用 recovers_deployment_id。

首次安装没有 previous healthy SHA。这时 previous_healthy_sha 为空，install_kind 必须是 first_install。首次安装不能走 no_change 的 PLAN → COMMIT。若不能证明部分资源已经移除，首次安装的回滚停在 SAFE_FROZEN，不能编造上一健康 SHA。

Resource 的 RELEASING 可以到 RELEASED、RELEASE_FAILED、PARTIAL 或 SAFE_FROZEN。PARTIAL 只有在再次授权后才能回到 RELEASING。部分释放不能标成全部 RELEASED。

## Event

事实被接受之后状态只有 ACCEPTED，没有出边，载荷不能改。

投递是另一张表，键是 event_id 加通道：DURABLE_LOCAL → OUTBOX → DELIVERED、DELIVERY_UNKNOWN 或 QUARANTINED。一个通道隔离或卡住，不推进、也不阻塞其他通道和其他 stream。

始终必填：event_id、schema_version、event_type、event_class、repo_id、producer_id、stream_id、stream_epoch、producer_seq、occurred_at、idempotency_key。received_at 只由服务端填写。

按 event_class 再必填：

| 类 | 附加字段 |
| --- | --- |
| task | app_id、host_id、task_id、run_id、attempt、revision、stage、state。host_id 可以空 |
| candidate | task_id、candidate_id、head、base、merge、tree、revision |
| deployment | deployment_id、app_id、host_id、artifact_digest、previous_healthy_sha、install_kind、stage、state。previous_healthy_sha 只有首次安装可以为空 |
| resource | resource_id、app_id、host_id、stage、state |
| evidence | evidence_ref、run_id |
| authorization | task_id、revision、stage、state、evidence_ref |

每个 stream 有自己的水位。序号正好等于水位加一才应用。缺口只挡住这条 stream。其他生产者的事件照常应用。authorization 流不允许用 never_existed 补洞来跳过未证实的授权事实。诊断流可以用对账事件声明某个序号从未存在，而且只能补本流的洞。

进程重启必须读回已落盘的 epoch 和序号。计数丢失时增加 epoch，旧 epoch 的缺口继续保持，不用新 epoch 的 1 去填旧洞。

## 幂等与身份顺序

处理顺序固定为：先验证身份，再判断授权，最后才查幂等键。身份或授权失败时不创建、不读取、不改变幂等记录。

幂等键的范围是已认证主体、scope 和操作名。重放返回 original_result 和 current_state 两个字段。前者是当时的公开结果，后者是对象现在的状态。重放不返回明文租约。

证明至少包含 signer_id、scope、issued_at、expires_at、nonce、payload_hash、alg。草案把 alg 记为 ed25519。服务端检查过期、nonce 不重复、以及撤销名单。撤销按 signer_id 立即生效。

用户验收使用独立的 signer kind `user_acceptance`。agent、MacCI、候选和网页会话都不能充当它。在身份验证实现并被单独授权之前，`production_verification_enabled` 为 false，只接受 kind 为 test、proof_ref 以 `test-proof://` 开头的隔离身份。这时连 `user_acceptance` 也不能把生产发布放行。

## 提议路径

这些路径仍未实现。现有 `/api/v1/feedback`、`/api/v1/development-tasks`、`/api/v1/system-logs` 和 `/api/v1/deploy/*` 保持原样。

每个包将来使用自己的 APIRouter。`app/main.py` 只在串行窗口里增加 include。W10 不代替各包注册路由。

写接口都在夹具 examples 里有最小请求和响应，并带 http 和 acceptance_owner：

Task.claim、Task.renew、Run.complete、Task.request_action、Event.append、Policy.authorize、Policy.revoke、Candidate.register、Candidate.supersede、CI.test、CI.build、CI.attest、Deploy.plan、Deploy.apply、Deploy.reconcile、Deploy.rollback、Resource.plan、Resource.apply、Release.accept、Release.reject、Release.publish、Upgrade.plan、Upgrade.reconcile。

读接口：Event.query、Candidate.next、Resource.inspect、Policy.evaluate。Policy.evaluate 没有副作用。候选身份请求发布时返回 POLICY_DENIED。

Diagnostic.subscribe 不在本草案提供。替代是游标轮询：GET `/api/v1/control/diagnostics?subject=&after_seq=`。响应含 watermark_seq、gap、items、side_effect=false。没有推送通道，客户端用 after_seq 接着读。

## 示例

领取的第一次响应可以包含 lease_token。同一幂等键的重放示例 `task_claim_replay` 把 lease_token 设为 null，original_result 仍是 LEASED，current_state 可以已经是 RUNNING。

过期或旧代次完成的示例 `run_finish_stale_lease` 返回 LEASE_FENCE_REJECTED，状态 UNKNOWN。

正例和反例都在 contract.json 的 cases 里，包括 LEASED 到期进入 UNKNOWN、没有 fencing 的取消被拒绝、UNKNOWN 在对账证明成功后进入 SUCCEEDED、RETRY_WAIT 不能进入 RUNNING、缺少回执不能从 PUBLISHING 回到 ACCEPTANCE。
