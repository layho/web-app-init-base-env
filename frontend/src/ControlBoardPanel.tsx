import { useEffect, useState } from 'react'
import { Alert, Button, Card, Descriptions, Empty, Space, Table, Typography } from 'antd'
import { boardRows, emptyBecauseNoRows, enabledActions, failureRows, historyRows, testRows } from './controlBoardView.ts'

type BoardStatus = {
  state?: string
  reason?: string
  code?: string
  detail?: string
  side_effect?: boolean
  production_enabled?: boolean
  items?: unknown[]
  history?: unknown[]
  failures?: unknown[]
  tests?: unknown[]
  actions?: { id?: string; label?: string; task_id?: string; enabled?: boolean; code?: string | null; calls_authority?: boolean }[]
  macci?: { status?: string; missing?: string[]; system?: string }
}

async function readJson(response: Response): Promise<BoardStatus> {
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    const message = typeof body?.detail === 'string' ? body.detail : typeof body?.reason === 'string' ? body.reason : `控制面请求失败（${response.status}）`
    const error = new Error(message) as Error & { status?: number; body?: BoardStatus }
    error.status = response.status
    error.body = body || undefined
    throw error
  }
  return body as BoardStatus
}

export function ControlBoardPanel({ csrf }: { csrf: string }) {
  const [status, setStatus] = useState<BoardStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  function load() {
    return fetch('/api/v1/control/board', { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' })
      .then(readJson)
      .then(body => {
        setStatus(body)
        setError(null)
      })
      .catch((exc: Error) => {
        setStatus(null)
        setError(exc.message)
      })
  }

  useEffect(() => {
    let cancelled = false
    load().finally(() => {
      if (cancelled) return undefined
    })
    return () => {
      cancelled = true
    }
  }, [])

  async function act(action: { id?: string; task_id?: string }) {
    if (!action.id || !action.task_id) return
    setBusy(true)
    setNotice(null)
    try {
      const response = await fetch('/api/v1/control/board/actions', {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrf,
        },
        body: JSON.stringify({
          action: action.id,
          task_id: action.task_id,
          idempotency_key: `${action.id}:${action.task_id}:${Date.now()}`,
          expected_revision: 0,
        }),
      })
      const body = await readJson(response)
      setNotice(body.detail || body.reason || body.code || '请求已记录')
      await load()
    } catch (exc) {
      setNotice(exc instanceof Error ? exc.message : '人工干预被拒绝')
    } finally {
      setBusy(false)
    }
  }

  const tasks = boardRows(status)
  const history = historyRows(status)
  const failures = failureRows(status)
  const tests = testRows(status)
  const actions = enabledActions(status)

  return (
    <Card title="联合控制面" size="small">
      {error && <Alert type="error" showIcon message={error} />}
      {!error && !status && <Typography.Text type="secondary">正在读取授权范围内的记录</Typography.Text>}
      {status && (
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Descriptions size="small" column={1}>
            <Descriptions.Item label="状态">{status.state || '未记录'}</Descriptions.Item>
            <Descriptions.Item label="原因">{status.reason || '未记录'}</Descriptions.Item>
            <Descriptions.Item label="副作用">{status.side_effect ? '有' : '无'}</Descriptions.Item>
            <Descriptions.Item label="生产启用">{status.production_enabled ? '是' : '否'}</Descriptions.Item>
            <Descriptions.Item label="宿主能力">{status.macci?.status || '未记录'}{status.macci?.system ? ` · ${status.macci.system}` : ''}</Descriptions.Item>
          </Descriptions>
          {!!status.macci?.missing?.length && (
            <Alert type="warning" showIcon message="环境未具备" description={status.macci.missing.join('；')} />
          )}
          {notice && <Alert type="info" showIcon message={notice} />}
          <Table
            size="small"
            pagination={false}
            rowKey="task_id"
            dataSource={tasks}
            locale={{ emptyText: emptyBecauseNoRows(status) ? '当前授权范围内没有记录' : '没有可显示的任务' }}
            columns={[
              { title: '任务', dataIndex: 'task_id' },
              { title: '负责人', dataIndex: 'owner' },
              { title: '时间', dataIndex: 'at', render: value => value || '未记录' },
              { title: '测试', dataIndex: 'tests' },
              { title: '错误', dataIndex: 'failures' },
            ]}
          />
          <Table
            size="small"
            pagination={false}
            rowKey={row => `${row.task_id}:${row.state}`}
            dataSource={tests}
            locale={{ emptyText: '没有测试记录' }}
            columns={[
              { title: '任务', dataIndex: 'task_id' },
              { title: '测试状态', dataIndex: 'state' },
              { title: '权威', dataIndex: 'authoritative', render: value => value ? '是' : '否' },
            ]}
          />
          <Table
            size="small"
            pagination={false}
            rowKey={row => `${row.event_id}:${row.at}`}
            dataSource={history}
            locale={{ emptyText: '没有历史记录' }}
            columns={[
              { title: '事件', dataIndex: 'event_id' },
              { title: '时间', dataIndex: 'at' },
              { title: '状态', dataIndex: 'state' },
              { title: '权威', dataIndex: 'authoritative', render: value => value ? '是' : '否' },
            ]}
          />
          {failures.length > 0 && (
            <Table
              size="small"
              pagination={false}
              rowKey={row => `${row.text}:${row.at}`}
              dataSource={failures}
              columns={[
                { title: '错误', dataIndex: 'text' },
                { title: '时间', dataIndex: 'at' },
                { title: '权威', dataIndex: 'authoritative', render: value => value ? '是' : '否' },
              ]}
            />
          )}
          <Space wrap>
            {actions.map(action => (
              <Button key={`${action.id}:${action.task_id}`} loading={busy} onClick={() => void act(action)}>
                {action.label || action.id} · {action.task_id}
              </Button>
            ))}
            {!actions.length && <Typography.Text type="secondary">当前授权下没有可执行的人工干预。</Typography.Text>}
          </Space>
          {emptyBecauseNoRows(status) && <Empty description={status.reason || '当前授权范围内没有记录'} />}
        </Space>
      )}
    </Card>
  )
}
