# 反馈交付中心 · 数据与验收规范（V0.1.25）

## 目标与范围

一条反馈对应一条长期可追踪的交付链。后台以单条反馈 ID 聚合：本地提交数据库、GitHub Issue、可信开发里程碑、关联 PR、精确 MacCI HEAD/base/merge、合并提交、本地部署回执以及用户验收。**当前能力不等于可凭空获取 ChatGPT Automations 的执行计划或状态**。

主列表只展示反馈摘要、当前阶段、执行负责人、是否执行、下次触发、Issue 和“查看链路”；详情展示九段时间线和证据。避免在宽表上排列六七列长期为空的日期。

## 9 个关键阶段

| 阶段 | 成立证据 | 不应混淆 |
|---|---|---|
| 反馈提交 | 应用数据库 created_at | 仅提交不代表 GitHub 同步 |
| 同步 GitHub | Issue number + synced_at | 不代表领取 |
| 领取任务 | 可信 APP_PROGRESS claimed 评论时间 | 不代表已经开始开发 |
| 开始开发 | 可信 APP_PROGRESS in_progress | 已排期不代表执行中 |
| 提交修复 PR | 可信 submitted 的 PR + SHA 与实际 PR HEAD 相符 | GitHub 评论不代替 PR 存在 |
| MacCI 测试 | 最新精确 HEAD/base 回执成功 | 旧成功回执不可复用 |
| 合并代码 | 合并 PR 的真实 merged_at + merge SHA | 非 Draft 不代表已合并 |
| 部署验证 | 本机 updater 对精确合并 SHA 的成功回执 | GitHub main 更新不代表公网运行成功 |
| 用户验收 | 有可信 accepted 里程碑和准确时间 | Issue closed 不等于验收通过 |

阶段状态使用 complete/pending/failed，主状态可显示 blocked。没有可信来源时显示“尚无可信回执”，而非填入当前时间。

## 执行者、任务触发与心跳

原有 APP_PROGRESS 专门记录开发里程碑。GitHub OWNER/MEMBER/COLLABORATOR 创建的不可编辑评论可另行提供执行回执，示例：

```text
[APP_EXECUTION] {"state":"started","executor":"Panel Automation","task_id":"panel-feedback","next_run_at":"2026-11-24T02:00:00+00:00"}
```

状态仅允许 queued / started / finished / failed。由 GitHub 创建时间作为回执时间，额外的 next_run_at 必须来自执行器实际知道的调度设置，不得由“每小时一次”推算。15 分钟内有效的 started 回执显示“有执行回执”；超过 15 分钟，未收到完成或续报时显示未知，而非一直执行中。finished/failed/queued 不代表“正在执行”。可信评论作者身份仅表明谁写了进度，**不等于真正的执行进程身份**。

执行回执目前只能称为“GitHub Issue 执行回写”，并非 Automations 平台提供的独立心跳。没有回执时：负责人未关联、是否执行未知、最近一次实际执行未知、下次触发未知。以后若接通可核验的 Automations API，应增加 task_id、运行 ID、调度时区、计划与实际开始/结束时间、结果 SHA 和 source，保留字段来源。

## 失败、冻结和安全门禁

- MacCI failure/error 与部署失败必须独立呈现，不能把“已提交”当“测试成功”。
- PR 合并不得在未取得精确测试证据的情况下执行。
- 旧版/不同 SHA 的 PR 不能借用成功记录；反馈 Issue 关闭不代表用户验收。
- 跨应用缓存不得串仓库，任何错误来源都显示未知，不猜测。
- 现有 GitHub 访问凭据只留在服务端；前端不能配置 Token。
- 所有展示来自只读证据，不新增自动合并或自动部署权限。

## 验收清单

1. 一条待同步、一条已领取、一条开发中、一条测试失败、一条已合并未部署、一条已部署待验收，分别展示正确阶段与时间。
2. 关闭 Issue、人工评论“已发布”、旧版通过回执，均不能自动把阶段推进至验收。
3. 打开反馈详情可以依次看到全部九阶段、当前步骤与来源链接；没有数据明确显示未知。
4. 尚未接通执行器回执时，不显示实时运行或下一次计划时间的虚假值。
5. 存在可信 APP_EXECUTION 回执时，显示执行者、最近一次执行及回写的下次触发时间；过期 started 恢复未知。
6. MacCI 测试通过、合并 preview、部署、公网运行 SHA，必须分别验收后才能宣称功能已上线。
