import React from 'react'
import { Alert, Card, Descriptions, Space, Tag, Timeline, Typography } from 'antd'

export type DeliveryStep = { key: string; label: string; state: 'complete' | 'pending' | 'failed' | 'unrecorded'; at: string | null }
export type Delivery = {
  steps: DeliveryStep[]
  current_stage: string
  executor: { name: string | null; reported_actor?: string | null; mode: string; running: boolean | null; last_run_at: string | null
    next_run_at: string | null; schedule_source: string | null }
  evidence: { issue: number | null; pr: number | null; head_sha: string | null; merge_sha: string | null }
  last_verified_at: string | null
  stale: boolean
}
const date = (input: string | null | undefined) => input
  ? new Date(input).toLocaleString('zh-CN', {hour12: false})
  : '尚无可信回执'

export function FeedbackDelivery({delivery, issueBase}: {delivery?: Delivery; issueBase: string}) {
  if (!delivery) return <Alert type="info" showIcon message="旧版反馈尚未生成交付进度快照，请刷新同步" />
  const current = delivery.steps.find(step => step.key === delivery.current_stage)
  const owner = delivery.executor.name ?? '未取得执行负责人信息'
  const missingHistory = delivery.steps.filter(step => step.state === 'unrecorded').length
  const currentState = delivery.current_stage === 'blocked' ? '当前受阻'
    : delivery.current_stage === 'failed' ? '测试或部署失败'
    : current ? '等待：' + current.label : '交付链路已完成'
  const base = issueBase.replace(/\/issues$/, '')
  return <Space direction="vertical" size="middle" style={{width: '100%'}}>
    <Card size="small" title="反馈交付进度" extra={<Tag color={delivery.current_stage === 'blocked' || delivery.current_stage === 'failed' ? 'error' : 'processing'}>{currentState}</Tag>}>
      <Typography.Paragraph type="secondary" style={{marginBottom: 12}}>
        只显示有真实证据的进度。MacCI 测试、合并、部署和验收分别核实，不能互相代替。
      </Typography.Paragraph>
      <Timeline items={delivery.steps.map(step => ({
        color: step.state === 'complete' ? 'green' : step.state === 'failed' ? 'red' : step.state === 'unrecorded' ? 'orange' : 'gray',
        children: <Space direction="vertical" size={0}>
          <Typography.Text strong={step.key === delivery.current_stage}>{step.label}
            {step.state === 'complete' ? ' · 已完成' : step.state === 'failed' ? ' · 失败' : step.state === 'unrecorded' ? ' · 历史回执缺失' : ' · 等待证据'}
          </Typography.Text>
          <Typography.Text type="secondary" style={{fontSize: 12}}>{date(step.at)}</Typography.Text>
        </Space>
      }))}/>
      {missingHistory > 0 && <Alert showIcon type="warning" message={`已有后续进展，但 ${missingHistory} 项历史里程碑缺少独立回执`} description="这些历史阶段没有可信时间记录，不会补造完成时间；当前交付阶段按后续真实 PR、MacCI 等证据推进。" />}
      {delivery.stale && <Alert showIcon type="warning" message="进度同步已过期；当前状态可能落后于 GitHub" />}
    </Card>
    <Card size="small" title="任务执行与触发">
      <Descriptions size="small" bordered column={1} items={[
        {key:'owner',label:'当前执行负责人',children:owner},
        {key:'actor',label:'最近可信开发回复账号',children:delivery.executor.reported_actor ?? '尚无记录（不等于实际任务执行人）'},
        {key:'mode',label:'执行方式',children:delivery.executor.mode === 'unverified'
          ? '尚未关联定时任务或实时执行回执' : 'GitHub Issue 执行回写（非独立平台心跳）'},
        {key:'running',label:'是否正在执行',children:delivery.executor.running === null ? '未知（没有执行器心跳）'
          : delivery.executor.running ? '有执行回执' : '当前未执行'},
        {key:'previous',label:'最近一次实际执行',children:date(delivery.executor.last_run_at)},
        {key:'next',label:'下一次计划触发',children:date(delivery.executor.next_run_at)},
        {key:'sync',label:'最近同步 GitHub',children:date(delivery.last_verified_at)}
      ]}/>
      <Alert style={{marginTop:12}} type="info" showIcon
        message="定时任务的计划时间不等于实际开始时间"
        description="未收到可信回执时不会推算触发时间；显示的执行时间若来自 GitHub Issue 回写，也不等于独立的 Automations 平台心跳。" />
    </Card>
    <Card size="small" title="交付证据">
      <Space direction="vertical" size="small">
        {delivery.evidence.issue
          ? <a href={issueBase + '/' + delivery.evidence.issue} target="_blank" rel="noopener noreferrer">GitHub Issue #{delivery.evidence.issue}</a>
          : <Typography.Text type="secondary">Issue：尚未同步</Typography.Text>}
        {delivery.evidence.pr
          ? <a href={base + '/pull/' + delivery.evidence.pr} target="_blank" rel="noopener noreferrer">修复 PR #{delivery.evidence.pr}</a>
          : <Typography.Text type="secondary">修复 PR：尚未核实</Typography.Text>}
        {delivery.evidence.head_sha && <Typography.Text copyable>开发 HEAD：{delivery.evidence.head_sha}</Typography.Text>}
        {delivery.evidence.merge_sha && <Typography.Text copyable>合并 SHA：{delivery.evidence.merge_sha}</Typography.Text>}
      </Space>
    </Card>
  </Space>
}
