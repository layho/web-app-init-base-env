import { Alert, Button, Card, Descriptions, Flex, List, Skeleton, Table, Tabs, Tag, Typography, theme } from 'antd'
import { ArrowDownOutlined } from '@ant-design/icons'

type ArchitectureLayer = { layer: string; technology: string; role: string }
type WorkflowStage = { title: string; description: string }
type LoopStage = { id: string; title: string; actor: string; cadence: string; description: string; mode: 'input' | 'automatic' | 'manual'; failure?: string }
type TaskModule = { module: string; scope: string; acceptance: string; issue: number }

type VersionRef = { version: string; sha: string }
type FoundationVersion = {
  repository: string
  role: 'foundation' | 'application'
  current: VersionRef | null
  latest: VersionRef | null
  checked_at: string | null
  stale: boolean
  attempted_at?: string | null
  source?: 'github_api' | 'git_raw' | null
  error?: string | null
  status: 'unregistered' | 'unknown' | 'current' | 'different'
  auto_upgrade_enabled: boolean
}

export type ProjectInfo = {
  foundation_version?: FoundationVersion
  name: string
  summary: string
  architecture: ArchitectureLayer[]
  workflow: WorkflowStage[]
  development_loop?: LoopStage[]
  tasks: TaskModule[]
}

function DevelopmentLoop({ stages }: { stages: LoopStage[] }) {
  const { token } = theme.useToken()
  return <Flex vertical gap="middle">
    <Alert type="info" showIcon message="基座开发与正式发布闭环"
      description="本图说明配置的工作机制，不代表各阶段实时执行成功。实际状态以开发任务、MacCI 回执和运行 SHA 为准；不使用 GitHub Actions。" />
    <Flex vertical align="center" style={{ width: '100%', maxWidth: 720, margin: '0 auto' }} gap={6}>
      {stages.map((stage, index) => <Flex key={stage.id} vertical align="center" gap={6} style={{ width: '100%' }}>
        <Card size="small" style={{ width: '100%', borderColor: stage.mode === 'manual' ? token.colorWarningBorder : token.colorBorderSecondary, background: stage.mode === 'manual' ? token.colorWarningBg : token.colorBgContainer }}>
          <Flex align="center" gap={10} wrap="wrap">
            <Typography.Text strong style={{ color: token.colorPrimary, fontVariantNumeric: 'tabular-nums' }}>{String(index + 1).padStart(2, '0')}</Typography.Text>
            <Typography.Text strong style={{ flex: '1 1 180px' }}>{stage.title}</Typography.Text>
            <Tag color={stage.mode === 'manual' ? 'gold' : stage.mode === 'input' ? 'default' : 'blue'} style={{ marginInlineEnd: 0 }}>{stage.actor}</Tag>
          </Flex>
          <Typography.Paragraph type="secondary" style={{ margin: '6px 0' }}>{stage.cadence}</Typography.Paragraph>
          <Typography.Paragraph style={{ marginBottom: stage.failure ? 8 : 0 }}>{stage.description}</Typography.Paragraph>
          {stage.failure && <Typography.Text type="secondary" style={{ display: 'block', borderTop: '1px solid ' + token.colorBorderSecondary, paddingTop: 8 }}>{stage.failure}</Typography.Text>}
        </Card>
        {index < stages.length - 1 && <ArrowDownOutlined aria-hidden="true" style={{ fontSize: 18, color: token.colorTextTertiary }} />}
      </Flex>)}
    </Flex>
    <Alert type="success" showIcon message="正式发布后继续收集意见反馈"
      description="未经验收的 preview 变更不得进入 main；后续需求回到第一步，下游应用独立升级。" />
  </Flex>
}

function FoundationVersionPanel({ info, loading, onRefresh }: { info?: FoundationVersion; loading: boolean; onRefresh: () => void }) {
  const renderVersion = (ref: VersionRef | null | undefined) => ref
    ? <Flex vertical gap={4}><Typography.Text strong>v{ref.version}</Typography.Text><Typography.Text copyable style={{ wordBreak: 'break-all' }}>{ref.sha}</Typography.Text></Flex>
    : <Typography.Text type="secondary">未登记或未取得</Typography.Text>
  const label = info?.status === 'current' ? '基座已一致'
    : info?.status === 'different' ? '基座版本 / SHA 不同，待核对升级'
    : info?.status === 'unknown' ? '正式版本暂未核实' : '基座来源待登记'
  return <Flex vertical gap="middle">
    <Alert showIcon type={info?.status === 'current' ? 'success' : 'info'} message={label}
      description="应用自身版本与基座版本独立。正式基座来源固定为 main，preview 不作为下游升级目标。" />
    <Descriptions bordered column={1} size="small" items={[
      { key: 'source', label: '基座仓库', children: <Typography.Link href="https://github.com/layho/web-app-init-base-env" target="_blank" rel="noreferrer">layho/web-app-init-base-env</Typography.Link> },
      { key: 'current', label: info?.role === 'foundation' ? '当前运行基座' : '当前应用基座', children: renderVersion(info?.current) },
      { key: 'latest', label: '最新正式基座', children: renderVersion(info?.latest) },
      { key: 'checked', label: '最近成功检查', children: info?.checked_at ? new Date(info.checked_at).toLocaleString() : '尚未取得' },
      { key: 'attempted', label: '最近尝试检查', children: info?.attempted_at ? new Date(info.attempted_at).toLocaleString() : '尚未尝试' },
      { key: 'query-source', label: '最近成功查询方式', children: info?.source === 'git_raw' ? 'Git main + 固定提交版本记录' : info?.source === 'github_api' ? 'GitHub API' : '尚未取得' },
      { key: 'auto', label: '自动升级 PR', children: info?.auto_upgrade_enabled ? '已启用' : '待接入' },
    ]} />
    {info?.stale && <Alert type="warning" showIcon message="未取得实时正式版本；已有数据仅供参考。"
      description={info.error ? info.error.split('/').map(e => (({ api_access_or_rate_limit: 'API 权限或访问限额', http_error: 'HTTP 请求失败', timeout: '查询超时', network_error: '网络连接失败', git_error: 'Git 查询失败', invalid_response: '版本响应无效' } as Record<string, string>)[e] || '查询异常')).join('；备用通道：') : undefined} />}
    <div><Button loading={loading} onClick={onRefresh}>刷新版本状态</Button></div>
    <Typography.Text type="secondary">自动检查缓存五分钟；手动刷新重新查询（最短间隔10秒）。升级须经过独立 PR、应用回归测试及验收，不直接覆盖业务代码。</Typography.Text>
  </Flex>
}

export function ProjectInfoPage({ data, loading, issueBase, onOpenTasks, onRefresh }: {
  data: ProjectInfo | null
  loading: boolean
  issueBase: string
  onOpenTasks: () => void
  onRefresh: () => void
}) {
  if (!data) return loading ? <Card><Skeleton active /></Card> : null

  return <Card title={data.name}>
    <Typography.Paragraph type="secondary">{data.summary}</Typography.Paragraph>
    <Tabs items={[
      {
        key: 'architecture',
        label: '架构设计',
        children: <Table
          rowKey="layer"
          size="small"
          pagination={false}
          scroll={{ x: 640 }}
          dataSource={data.architecture}
          columns={[
            { title: '层级', dataIndex: 'layer', width: 120 },
            { title: '技术', dataIndex: 'technology', width: 205 },
            { title: '职责', dataIndex: 'role' },
          ]}
        />,
      },
      {
        key: 'workflow',
        label: '开发流程',
        children: <List dataSource={data.workflow} renderItem={stage => (
          <List.Item key={stage.title}>
            <List.Item.Meta title={stage.title} description={stage.description} />
          </List.Item>
        )} />,
      },
      ...(data.development_loop?.length ? [{
        key: 'development-loop',
        label: '开发闭环',
        children: <DevelopmentLoop stages={data.development_loop} />,
      }] : []),
      {
        key: 'foundation-version',
        label: '基座版本',
        children: <FoundationVersionPanel info={data.foundation_version} loading={loading} onRefresh={onRefresh} />,
      },
      {
        key: 'breakdown',
        label: '任务拆解',
        children: <Flex vertical gap="middle">
          <div><Button onClick={onOpenTasks}>查看开发进度</Button></div>
          <Table
            rowKey="issue"
            size="small"
            pagination={false}
            scroll={{ x: 680 }}
            dataSource={data.tasks}
            columns={[
              {
                title: '模块', dataIndex: 'module', width: 170,
                render: (name: string, task: TaskModule) => (
                  <Typography.Link href={`${issueBase}/${task.issue}`} target="_blank" rel="noreferrer">{name}</Typography.Link>
                ),
              },
              { title: '交付范围', dataIndex: 'scope', width: 235 },
              { title: '验收条件', dataIndex: 'acceptance' },
            ]}
          />
        </Flex>,
      },
    ]} />
  </Card>
}
