import { useState } from 'react'
import { Alert, Button, Card, Descriptions, Drawer, Empty, Flex, Modal, Table, Tabs, Tag, Typography } from 'antd'

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

type PRRecord = { number: number; title: string; url: string; head: string; base: string | null; test: {state: string; started_at?: string; finished_at?: string; duration_seconds?: number | null; phase?: string; tested_merge?: string | null}; log?: {pytest: Record<string, number> | null; phase: string} | null; merge_state: string; merge_blocked?: boolean; mergeability?: string; merged_at: string | null; merge_sha: string | null; deployment: {state: string; at: string | null} }
type PRTracking = { synced_at: string | null; stale: boolean; prs: PRRecord[] }
export type TaskSnapshot = { synced_at: string | null; stale: boolean; tasks: DevelopmentTask[]; pr_tracking?: PRTracking }

const statusColors: Record<string, string> = {
  开发中: 'processing',
  待验收: 'blue',
  受阻: 'error',
  已完成: 'success',
}

function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}

const ciLabels: Record<string, string> = { success: '通过', pending: '测试中', failure: '失败', error: '失效 / 异常', unknown: '待测试 / 未记录' }
const mergeLabels: Record<string, string> = { merged: '已合并', draft: '已暂停 · 等待解除', open: '待合并', closed: '已关闭' }
const deployLabels: Record<string, string> = { running: '当前运行', deployed: '曾成功部署', failed: '部署失败 / 回退', unverified: '待核验', not_merged: '尚未合并' }
const phaseLabels: Record<string, string> = { pending: '正在测试', passed: '后端测试、编译及前端构建通过', 'setup-failed': '环境准备失败', 'fetch-failed': '候选代码拉取失败', 'context-failed': '构建上下文失败', 'dependencies-failed': '依赖安装 / 镜像构建失败', 'pytest-failed': '后端测试失败', 'compile-failed': 'Python 编译失败', 'frontend-failed': '前端构建失败', 'test-failed': '容器测试失败', 'ref-changed': '分支已变更，回执无效' }
function duration(seconds?: number | null) { return seconds == null ? '未记录' : seconds < 60 ? seconds + '秒' : Math.floor(seconds / 60) + '分' + seconds % 60 + '秒' }
function passRate(row: PRRecord) {
  const c = row.log?.pytest
  if (!c) return '未记录'
  const p = c.passed ?? 0
  const n = p + (c.failed ?? 0) + (c.error ?? 0) + (c.errors ?? 0) + (c.xpassed ?? 0)
  return n > 0 ? (100 * p / n).toFixed(1) + '%' : '未记录'
}
function ciTag(state: string) { return <Tag color={state === 'success' ? 'success' : state === 'pending' ? 'processing' : state === 'failure' || state === 'error' ? 'error' : 'default'}>{ciLabels[state] ?? '未记录'}</Tag> }
function PRTrackingPage({data, onReleaseDraft}: {data: PRTracking; onReleaseDraft: (number: number, head: string) => Promise<void>}) {
  const [selectedNumber, setSelectedNumber] = useState<number | null>(null)
  const [releaseNumber, setReleaseNumber] = useState<number | null>(null)
  const [releasing, setReleasing] = useState(false)
  const [releaseError, setReleaseError] = useState<string | null>(null)
  const [releasedNumbers, setReleasedNumbers] = useState<number[]>([])
  const releaseOrdinal = (title: string) => {
    const match = /\bv(\d+)\.(\d+)\.(\d+)\b/i.exec(title)
    return match ? match.slice(1).map(Number) : null
  }
  const hasEarlierUnmergedRelease = (row: PRRecord) => {
    const target = releaseOrdinal(row.title)
    if (!target) return false
    return data.prs.some(other => {
      if (other.number === row.number || other.merge_state === 'merged' || other.merge_state === 'closed') return false
      const previous = releaseOrdinal(other.title)
      if (!previous) return false
      for (let i = 0; i < 3; i++) {
        if (previous[i] !== target[i]) return previous[i] < target[i]
      }
      return false
    })
  }
  const selected = data.prs.find(row => row.number === selectedNumber)
  const proposed = data.prs.find(row => row.number === releaseNumber)
  async function approveDraft() {
    if (!proposed || releasing || data.stale || proposed.merge_state !== 'draft' || hasEarlierUnmergedRelease(proposed)) return
    setReleasing(true)
    setReleaseError(null)
    try {
      await onReleaseDraft(proposed.number, proposed.head)
      setReleasedNumbers(items => [...items, proposed.number])
      setReleaseNumber(null)
    } catch (error) {
      setReleaseError(error instanceof Error ? error.message : '解除 Draft 失败')
    } finally {
      setReleasing(false)
    }
  }
  return <Flex vertical gap="middle">
    <Alert showIcon type={data.stale ? 'warning' : 'info'} message={data.stale ? '同步延迟或尚未同步，保留上次成功记录。' : 'GitHub 测试回执与本机部署记录'} description="每五分钟尝试同步。测试时间以 GitHub 回执为准；日志仅展示安全计数和阶段，未采集信息不推算。仅显示基座 preview PR。" />
    <Typography.Text type="secondary">最近同步：{formatDate(data.synced_at)}</Typography.Text>
    <Table rowKey="number" size="small" dataSource={data.prs} scroll={{x: 1160}} locale={{emptyText: '暂无 PR 测试记录'}} columns={[
      {title: 'PR', width: 280, render: (_,row) => <Typography.Link href={row.url} target="_blank" rel="noreferrer">#{row.number} {row.title}</Typography.Link>},
      {title: '测试状态', width: 125, render: (_,row) => ciTag(row.test.state)},
      {title: '测试开始', width: 165, render: (_,row) => formatDate(row.test.started_at ?? null)},
      {title: '测试结束', width: 165, render: (_,row) => formatDate(row.test.finished_at ?? null)},
      {title: '耗时', width: 95, render: (_,row) => duration(row.test.duration_seconds)},
      {title: '通过率', width: 95, render: (_,row) => passRate(row)},
      {title: '合并', width: 122, render: (_,row) => row.merge_blocked ?
        <Tag color="error" title="Draft 已解除，但与当前 preview 存在冲突，MacCI/自动合并将暂停">存在冲突 · 待修复</Tag> :
        <Tag color={row.merge_state === 'merged' ? 'success' : 'default'}>{mergeLabels[row.merge_state]}</Tag>},
      {title: '操作', width: 126, render: (_,row) => row.merge_state === 'draft' ?
        releasedNumbers.includes(row.number) ? <Tag color="processing">已解除 · 待同步</Tag> :
        <Button size="small" disabled={data.stale || hasEarlierUnmergedRelease(row)}
          title={data.stale ? '同步已过期，请刷新' : hasEarlierUnmergedRelease(row) ? '存在更早的未合并版本，须先完成前置版本' : '确认后交由 MacCI 自动处理'}
          onClick={() => { setReleaseError(null); setReleaseNumber(row.number) }}>解除 Draft</Button> : '—'},
      {title: '部署', width: 125, render: (_,row) => <Tag color={row.deployment.state === 'running' || row.deployment.state === 'deployed' ? 'success' : row.deployment.state === 'failed' ? 'error' : 'default'}>{deployLabels[row.deployment.state]}</Tag>},
      {title: '详情', width: 70, render: (_,row) => <Button type="link" onClick={() => setSelectedNumber(row.number)}>查看</Button>},
    ]}/>
    <Modal title="确认解除 Draft，进入自动合并流程" open={releaseNumber !== null}
      onCancel={() => { if (!releasing) setReleaseNumber(null) }}
      onOk={() => void approveDraft()} okText="解除 Draft"
      confirmLoading={releasing}
      okButtonProps={{ disabled: !proposed || data.stale || proposed.merge_state !== 'draft' || hasEarlierUnmergedRelease(proposed) }}>
      {proposed && hasEarlierUnmergedRelease(proposed) && <Alert type="warning" showIcon
        message="前置版本尚未完成，当前禁止解除 Draft"
        description="请先处理更早的版本 PR 和 MacCI 测试，避免候选版本乱序发布。" style={{marginBottom:12}}/>}
      <Typography.Paragraph>此操作仅解除 PR 草稿并记录当前 HEAD 的负责人审查授权，不会直接合并或部署。之后仍须 MacCI 针对最新 preview 基线重新测试，通过全部合并门禁才会自动合并。</Typography.Paragraph>
      <Typography.Paragraph strong>PR #{proposed?.number} · {proposed?.title}</Typography.Paragraph>
      <Typography.Text code copyable style={{overflowWrap:'anywhere'}}>{proposed?.head}</Typography.Text>
      {releaseError && <Alert type="error" showIcon message={releaseError} style={{marginTop:12}} />}
    </Modal>
    <Drawer title={selected ? 'PR #' + selected.number + ' · 测试与发布详情' : '测试详情'} open={selectedNumber !== null} onClose={() => setSelectedNumber(null)} width="min(680px, 100vw)">
      {selected ? <Flex vertical gap="middle"><Typography.Text strong>{selected.title}</Typography.Text>
        <Descriptions bordered size="small" column={1} items={[
          {key:'status',label:'测试状态',children: ciTag(selected.test.state)},
          {key:'stage',label:'测试阶段',children:phaseLabels[selected.test.phase ?? ''] ?? '未记录'},
          {key:'start',label:'开始时间',children:formatDate(selected.test.started_at ?? null)},
          {key:'end',label:'结束时间',children:formatDate(selected.test.finished_at ?? null)},
          {key:'duration',label:'测试耗时',children:duration(selected.test.duration_seconds)},
          {key:'rate',label:'Pytest 通过率',children:passRate(selected)},
          {key:'head',label:'PR HEAD',children:<Typography.Text copyable code style={{overflowWrap:'anywhere'}}>{selected.head}</Typography.Text>},
          {key:'base',label:'测试基线',children:selected.base ?? '未记录'},
          {key:'tested',label:'测试候选 SHA',children:selected.test.tested_merge ?? '未记录'},
          {key:'merge',label:'合并 SHA',children:selected.merge_sha ?? '未合并'},
          {key:'mergedAt',label:'合并时间',children:formatDate(selected.merged_at)},
          {key:'deploy',label:'部署状态',children:deployLabels[selected.deployment.state]},
          {key:'deployAt',label:'部署时间',children:formatDate(selected.deployment.at)},
        ]}/>
        <Card size="small" title="测试日志安全摘要">
          {selected.log?.pytest ? <Descriptions column={1} size="small" items={Object.entries(selected.log.pytest).map(([name,n])=>({key:name,label:name,children:n}))}/> : <Typography.Text type="secondary">未取得匹配本次测试的本机计数。可在 GitHub 核对回执；原始日志保留在 Mac mini。</Typography.Text>}
          <Typography.Paragraph type="secondary" style={{margin:'8px 0 0'}}>通过率 = passed / (passed + failed + errors + xpassed)，不计跳过、预期失败及未执行用例。前端构建结果单独以阶段回执记录。</Typography.Paragraph>
        </Card><Typography.Link href={selected.url} target="_blank" rel="noreferrer">查看 GitHub PR 原始状态</Typography.Link></Flex> : <Empty description="记录已更新或不可用"/>}
    </Drawer>
  </Flex>
}

export function DevelopmentTasksPage({ data, loading, onRefresh, onReleaseDraft }: {
  data: TaskSnapshot | null
  loading: boolean
  onRefresh: () => void
  onReleaseDraft: (number: number, head: string) => Promise<void>
}) {
  const tasks = <Flex vertical gap="middle">
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
  return <Card title="开发任务" extra={<Button onClick={onRefresh} loading={loading}>刷新列表</Button>}>
    {data?.pr_tracking ? <Tabs items={[{key:'tasks',label:'任务清单',children:tasks},{key:'tracking',label:'测试与发布追踪',children:<PRTrackingPage data={data.pr_tracking} onReleaseDraft={onReleaseDraft}/>}]} /> : tasks}
  </Card>
}
