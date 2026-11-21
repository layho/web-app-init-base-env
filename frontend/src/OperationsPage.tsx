import { Button, Card, Empty, Flex, List, Table, Tag, Typography } from 'antd'

type Operation = { at: string; code: string; sha: string }
type ReleaseNote = { version: string; title: string; changes: string[]; validation: string }
export type ReleaseHistory = {
  version: string
  running_sha: string
  releases: ReleaseNote[]
  deployments: Operation[]
}
export type SystemLogs = { events: Operation[] }

const labels: Record<string, string> = {
  startup_ok: '启动自检通过',
  schema_check_failed: '数据库迁移检查失败',
  frontend_missing: '缺少前端构建产物',
  install_ok: '安装通过本机健康检查',
  install_failed: '安装健康检查失败',
  update_ok: '更新通过本机健康检查',
  update_blocked: '更新等待数据库迁移',
  rollback: '发布失败，已尝试回退',
  feedback_ok: '反馈已同步到 GitHub',
  feedback_failed: '反馈同步失败',
}

function OperationTable({ records, loading }: { records: Operation[]; loading: boolean }) {
  return <Table
    rowKey={(_, index) => String(index)}
    size="small"
    loading={loading}
    dataSource={records}
    locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无记录" /> }}
    scroll={{ x: 480 }}
    columns={[
      { title: '时间', dataIndex: 'at', width: 180, render: (at: string) => new Date(at).toLocaleString('zh-CN', { hour12: false }) },
      { title: '事件', dataIndex: 'code', render: (code: string) => <Tag color={
        code.endsWith('_ok') ? 'success' : 'warning'
      }>{labels[code] ?? code}</Tag> },
      { title: '关联提交', dataIndex: 'sha', width: 120, render: (sha: string) => <code title={sha}>{sha.slice(0, 10)}</code> },
    ]}
  />
}

type RefreshProps = { loading: boolean; onRefresh: () => void }

export function ReleasePage({ data, loading, onRefresh }: RefreshProps & { data: ReleaseHistory | null }) {
  return <Flex vertical gap="middle">
    <Card title="当前运行版本" extra={<Button onClick={onRefresh} loading={loading}>刷新</Button>}>
      {data ? <Flex wrap gap="small" align="center">
        <Tag color="blue">{data.version}</Tag>
        <Typography.Text code style={{ overflowWrap: 'anywhere' }}>{data.running_sha === 'unknown' ? '未上报 Git SHA' : data.running_sha}</Typography.Text>
      </Flex> : <Typography.Text type="secondary">暂无版本信息</Typography.Text>}
    </Card>
    {data?.releases.map(release => <Card key={release.version} title={`${release.version} · ${release.title}`}>
      <List size="small" dataSource={release.changes} renderItem={change => <List.Item>{change}</List.Item>} />
      <Typography.Paragraph type="secondary" style={{ margin: '12px 0 0' }}>{release.validation}</Typography.Paragraph>
    </Card>)}
    <Card title="本机发布记录">
      <OperationTable records={data?.deployments ?? []} loading={loading} />
    </Card>
  </Flex>
}

export function SystemLogPage({ data, loading, onRefresh }: RefreshProps & { data: SystemLogs | null }) {
  return <Card title="最近 100 条系统事件" extra={<Button onClick={onRefresh} loading={loading}>刷新</Button>}>
    <OperationTable records={data?.events ?? []} loading={loading} />
  </Card>
}
