import { Alert, Button, Card, Empty, Flex, Table, Tag, Typography } from 'antd'

type DevelopmentTask = {
  number: number
  title: string
  url: string
  status: string
  completed_steps: number
  total_steps: number
  updated_at: string | null
  note: string
}

export type TaskSnapshot = { synced_at: string | null; stale: boolean; tasks: DevelopmentTask[] }

const statusColors: Record<string, string> = {
  开发中: 'processing',
  待验收: 'blue',
  受阻: 'error',
  已完成: 'success',
}

function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}

export function DevelopmentTasksPage({ data, loading, onRefresh }: {
  data: TaskSnapshot | null
  loading: boolean
  onRefresh: () => void
}) {
  return <Card title="开发任务" extra={<Button onClick={onRefresh} loading={loading}>刷新列表</Button>}>
    <Flex vertical gap="middle">
      {data?.stale && <Alert type="warning" showIcon message={
        data?.synced_at ? '同步已延迟，以下为上次成功获取的记录。' : '等待从 GitHub 同步开发任务。'
      } />}
      {data?.synced_at && <Typography.Text type="secondary">GitHub 最近同步 {formatDate(data.synced_at)}</Typography.Text>}
      <Table
        rowKey="number"
        size="small"
        loading={loading}
        dataSource={data?.tasks ?? []}
        scroll={{ x: 920 }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={
          data?.synced_at ? '暂无登记的开发任务' : '暂无同步记录'
        } /> }}
        columns={[
          { title: '任务', dataIndex: 'title', width: 300, render: (title: string, task: DevelopmentTask) =>
            <Typography.Link href={task.url} target="_blank" rel="noreferrer">#{task.number} {title}</Typography.Link> },
          { title: '状态', dataIndex: 'status', width: 104, render: (status: string) =>
            <Tag color={statusColors[status] ?? 'default'}>{status}</Tag> },
          { title: '已完成步骤', key: 'progress', width: 106, render: (_, task: DevelopmentTask) =>
            task.total_steps ? `${task.completed_steps} / ${task.total_steps}` : '—' },
          { title: '最新进展', dataIndex: 'note', render: (note: string) => note || '—' },
          { title: 'Issue 更新', dataIndex: 'updated_at', width: 164, render: formatDate },
        ]}
      />
    </Flex>
  </Card>
}
