import { Button, Card, Flex, List, Skeleton, Table, Tabs, Typography } from 'antd'

type ArchitectureLayer = { layer: string; technology: string; role: string }
type WorkflowStage = { title: string; description: string }
type TaskModule = { module: string; scope: string; acceptance: string; issue: number }

export type ProjectInfo = {
  name: string
  summary: string
  architecture: ArchitectureLayer[]
  workflow: WorkflowStage[]
  tasks: TaskModule[]
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
