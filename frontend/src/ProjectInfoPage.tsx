import { Alert, Button, Card, Descriptions, Flex, List, Skeleton, Table, Tabs, Tag, Typography, theme } from 'antd'
import { ArrowDownOutlined } from '@ant-design/icons'

type ArchitectureLayer = { layer: string; technology: string; role: string }
type WorkflowStage = { title: string; description: string }
type LoopStage = { id: string; title: string; actor: string; cadence: string; description: string; mode: 'input' | 'automatic' | 'manual'; failure?: string }
type TaskModule = { module: string; scope: string; acceptance: string; issue: number }

type GuideStep = { title: string; detail: string; check?: string; caution?: string }
type GuideSection = { id: string; title: string; summary: string; steps: GuideStep[] }
type OperatingGuide = { scope: 'foundation' | 'application'; title: string; summary: string; cautions: string[]; sections: GuideSection[] }

function OperatingGuidePage({ guide }: { guide: OperatingGuide }) {
  return <Flex vertical gap="middle">
    <Alert type="info" showIcon message={guide.title} description={guide.summary} />
    {guide.cautions.map((warning, index) => <Alert key={index} type="warning" showIcon message={warning} />)}
    {guide.sections.map(section => <Card key={section.id} size="small" title={section.title}>
      <Typography.Paragraph type="secondary">{section.summary}</Typography.Paragraph>
      <List dataSource={section.steps} renderItem={(step, index) => <List.Item key={step.title}>
        <Flex vertical gap={6} style={{ width: '100%' }}>
          <Typography.Text strong>{index + 1}. {step.title}</Typography.Text>
          <Typography.Paragraph style={{ marginBottom: 0 }}>{step.detail}</Typography.Paragraph>
          {step.check && <Typography.Text type="secondary">验收依据：{step.check}</Typography.Text>}
          {step.caution && <Alert type="warning" showIcon message={step.caution} />}
        </Flex>
      </List.Item>} />
    </Card>)}
  </Flex>
}

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
  scheduler?: { registered: boolean; mode: 'create' | 'observe' | 'unregistered'; state: string; checked_at: string | null; stale: boolean; pr_url: string | null; target_sha: string | null; app_sha: string | null; review_count: number | null } | null
}

export type ProjectInfo = {
  foundation_version?: FoundationVersion
  name: string
  summary: string
  architecture: ArchitectureLayer[]
  workflow: WorkflowStage[]
  development_loop?: LoopStage[]
  operating_guide?: OperatingGuide
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

const schedulerLabels: Record<string, string> = {
  current: '已检查：基座一致', upgrade_pending: '升级 PR 待处理', draft_created: '升级 PR 已创建',
  upgrade_merged: '升级 PR 已合并，下一轮检查最新版本',
  upgrade_waiting_verification: '等待 MacCI 或安全校验，禁止合并',
  upgrade_review_required: '升级异常，需自动修复任务审查',
  waiting_adoption: '观察模式，待兼容迁移', blocked: '升级受阻，需审查冲突',
  closed_pr: '关联 PR 已关闭', no_main: '应用 main 不可访问', error: '本轮调度检查失败',
  awaiting_first_check: '已登记，等待首次检查', unavailable: '调度器未登记',
}
const schedulerMode: Record<string, string> = { create: '自动创建升级 PR', observe: '仅观察（不会创建 PR）', unregistered: '未登记' }

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
      { key: 'auto', label: '自动升级模式', children: info?.scheduler ? schedulerMode[info.scheduler.mode] : info?.role === 'foundation' ? '不适用（基座本身）' : '调度器状态不可用' },
      ...(info?.scheduler ? [
        { key: 'scheduler-state', label: '调度器最近结果', children: schedulerLabels[info.scheduler.state] || '未知状态' },
        { key: 'blocked-files', label: '升级冲突文件',
          children: info.scheduler.blocked_files?.length
            ? info.scheduler.blocked_files.map(item => item.path + '（' + (item.action === 'conflict' ? '内容冲突' : '需要审查') + '）').join('；')
            : '无已确认冲突文件清单' },
        { key: 'scheduler-checked', label: '调度器最近检查', children: info.scheduler.checked_at ? new Date(info.scheduler.checked_at).toLocaleString() : '尚未取得' },
        { key: 'scheduler-target', label: '调度器目标 SHA', children: info.scheduler.target_sha ? <Typography.Text copyable>{info.scheduler.target_sha}</Typography.Text> : '未取得' },
        { key: 'scheduler-pr', label: '升级 PR', children: info.scheduler.pr_url ? <Typography.Link href={info.scheduler.pr_url} target="_blank" rel="noopener noreferrer">{info.scheduler.pr_url.split('/').pop()} 号升级 PR（打开）</Typography.Link> : '当前无关联 PR' },
        ...(info.scheduler.review_count !== null ? [{ key: 'review-count', label: '需人工审查文件', children: info.scheduler.review_count }] : []),
      ] : []),
    ]} />
    {info?.scheduler?.registered && info.scheduler.stale && <Alert type="warning" showIcon message="调度器检查过期或尚未收到运行回执；不能确认自动升级正常运行。" />}
    {info?.stale && <Alert type="warning" showIcon message="未取得实时正式版本；已有数据仅供参考。"
      description={info.error ? info.error.split('/').map(e => (({ api_access_or_rate_limit: 'API 权限或访问限额', http_error: 'HTTP 请求失败', timeout: '查询超时', network_error: '网络连接失败', git_error: 'Git 查询失败', invalid_response: '版本响应无效' } as Record<string, string>)[e] || '查询异常')).join('；备用通道：') : undefined} />}
    <div><Button loading={loading} onClick={onRefresh}>刷新版本状态</Button></div>
    <Typography.Text type="secondary">自动检查缓存五分钟；手动刷新重新查询（最短间隔10秒）。调度器状态来自本机独立服务的只读回执，不等于 MacCI 测试或公网部署成功。升级仍须通过独立 PR、回归测试及验收。</Typography.Text>
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
      ...(data.operating_guide?.sections?.length ? [{
        key: 'operating-guide',
        label: '操作指南',
        children: <OperatingGuidePage guide={data.operating_guide} />,
      }] : []),
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
