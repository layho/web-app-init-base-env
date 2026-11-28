import { useEffect, useState } from 'react'
import { Alert, Card, Descriptions, Empty, Typography } from 'antd'

type BoardStatus = {
  state?: string
  reason?: string
  owner?: string | null
  recorded_at?: string | null
  side_effect?: boolean
  production_enabled?: boolean
  items?: unknown[]
  history?: { event_id?: string; at?: string; reason?: string }[]
  failures?: { reason?: string; owner?: string; at?: string }[]
  macci?: { status?: string; missing?: string[]; system?: string }
}

export function ControlBoardPanel() {
  const [status, setStatus] = useState<BoardStatus | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetch('/api/v1/control/board', { headers: { Accept: 'application/json' } })
      .then(async response => {
        if (!response.ok) throw new Error('控制面状态不可用')
        return response.json() as Promise<BoardStatus>
      })
      .then(body => {
        if (!cancelled) setStatus(body)
      })
      .catch((exc: Error) => {
        if (!cancelled) setError(exc.message)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <Card title="联合控制面" size="small">
      {error && <Alert type="error" showIcon message={error} />}
      {!error && !status && <Typography.Text type="secondary">正在读取只读状态</Typography.Text>}
      {status && (
        <>
          <Descriptions size="small" column={1}>
            <Descriptions.Item label="状态">{status.state || '未记录'}</Descriptions.Item>
            <Descriptions.Item label="原因">{status.reason || '未记录'}</Descriptions.Item>
            <Descriptions.Item label="负责人">{status.owner || '未记录'}</Descriptions.Item>
            <Descriptions.Item label="时间">{status.recorded_at || '未记录'}</Descriptions.Item>
            <Descriptions.Item label="副作用">{status.side_effect ? '有' : '无'}</Descriptions.Item>
            <Descriptions.Item label="生产启用">{status.production_enabled ? '是' : '否'}</Descriptions.Item>
            <Descriptions.Item label="宿主能力">{status.macci?.status || '未记录'}{status.macci?.system ? ` · ${status.macci.system}` : ''}</Descriptions.Item>
          </Descriptions>
          {!!status.macci?.missing?.length && (
            <Alert style={{ marginTop: 12 }} type="warning" showIcon message="环境未具备" description={status.macci.missing.join('；')} />
          )}
          {!status.items?.length && !status.history?.length && !status.failures?.length && (
            <Empty style={{ marginTop: 12 }} description="没有权威任务行。此页不展示演示数据。" />
          )}
        </>
      )}
    </Card>
  )
}
