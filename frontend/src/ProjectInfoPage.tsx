import { Alert, Button, Card, Flex, List, Skeleton, Table, Tabs, Tag, Typography, theme } from 'antd'
import { ArrowDownOutlined } from '@ant-design/icons'

type ArchitectureLayer = { layer: string; technology: string; role: string }
type WorkflowStage = { title: string; description: string }
type LoopStage = { id: string; title: string; actor: string; cadence: string; description: string; mode: 'input' | 'automatic' | 'manual'; failure?: string }
type TaskModule = { module: string; scope: string; acceptance: string; issue: number }

export type ProjectInfo = {
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

export function ProjectInfoPage({ data, loading, issueBase, onOpenTasks }: {
  data: ProjectInfo | null
  loading: boolean
  issueBase: string
  onOpenTasks: () => void
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
