import { useState } from 'react'
import { Alert, Button, Card, Descriptions, Drawer, Empty, Flex, Modal, Select, Input, Space, Table, Tabs, Tag, Typography } from 'antd'

type LinkedPR = {number: number; title?: string; url?: string; owner?: string; test?: {state?: string; started_at?: string; finished_at?: string; phase?: string}; merge_state?: string; merged_at?: string | null; merge_sha?: string | null; deployment?: {state?: string; at?: string | null}; conflict?: {blocked?: boolean}}
type DevelopmentTask = {
  owner?: string
  issue_state?: string
  origin?: string
  github_actor?: string
  claimed_at?: string
  development_started_at?: string
  pr_numbers?: number[]
  prs?: LinkedPR[]
  key?: string
  source?: 'issue' | 'pr'
  pr_number?: number | null
  delivery_status?: string
  test_state?: string | null
  test_failures?: {number:number; head?:string; phase?:string; at?:string}[]
  historical_test_failures?: {number:number; head?:string; phase?:string; at?:string}[]
  merge_state?: string | null
  deployment_state?: string | null
  number: number
  title: string
  url: string
  status: string
  completed_steps: number
  total_steps: number
  updated_at: string | null
  note: string
}

type PRRecord = { head_branch?: string | null; base_branch?: string | null; number: number; title: string; url: string; head: string; base: string | null; test: {state: string; started_at?: string; finished_at?: string; duration_seconds?: number | null; phase?: string; tested_merge?: string | null; progress?: {phase:string;heartbeat_at:string;heartbeat_stale:boolean;elapsed_seconds:number}|null; failure_report?: {run_id:string; failures:{file:string;category:string;test?:string;line?:number;column?:number}[]}|null}; head_status?: {state: string; at: string | null}; log?: {pytest: Record<string, number> | null; phase: string} | null; merge_state: string; merge_blocked?: boolean; mergeability?: string; conflict?: {blocked: boolean; base_sha: string; candidate_files: string[]; file_scope: string; reason: string | null}; merged_at: string | null; merge_sha: string | null; deployment: {state: string; at: string | null} }
type PRTracking = { active_synced_at?: string; history_synced_at?: string|null; running_sha?: string | null; synced_at: string | null; stale: boolean; prs: PRRecord[]; repository?: string; branch?: string; can_release_draft?: boolean; manual_actions_available?: boolean }
export type TaskSnapshot = { refresh_status?: {state: string; phase?: string; requests?: number; elapsed_seconds?: number; reason?: string; token_health?: Record<string, {state:string; reason?:string; login?:string; permission?:string; remaining?:number; limit?:number; permissions?:Record<string,boolean>; elapsed_seconds?:number}>}; synced_at: string | null; stale: boolean; tasks: DevelopmentTask[]; pr_tracking?: PRTracking }

const statusColors: Record<string, string> = {
  开发中: 'processing',
  未记录开发状态: 'default',
  待验收: 'blue',
  受阻: 'error',
  已完成: 'success',
}

function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}

const ciLabels: Record<string, string> = { success: '通过', pending: '测试中', failure: '失败', error: '失效 / 异常', unknown: '待测试 / 未记录' }
const mergeLabels: Record<string, string> = { merged: '已合并', draft: '草稿 · 等待开发审查', open: '待合并', closed: '已关闭' }
const deployLabels: Record<string, string> = { running: '当前运行', deployed: '曾成功部署', failed: '部署失败 / 回退', unverified: '待核验', not_merged: '尚未合并' }
const phaseLabels: Record<string, string> = { pending: '正在测试', passed: '后端测试、编译及前端构建通过', 'setup-failed': '环境准备失败', 'fetch-failed': '候选代码拉取失败', 'context-failed': '构建上下文失败', 'dependencies-failed': '依赖安装 / 镜像构建失败', 'pytest-failed': '后端测试失败', 'compile-failed': 'Python 编译失败', 'frontend-failed': '前端构建失败', 'test-failed': '容器测试失败', 'ref-changed': '分支已变更，回执无效' }
const progressLabels: Record<string,string>={setup:'准备环境',fetch:'拉取精确候选',context:'准备构建上下文',dependencies:'依赖安装 / 镜像构建',test:'隔离容器测试及构建',cleanup:'清理测试环境'}
function duration(seconds?: number | null) { return seconds == null ? '未记录' : seconds < 60 ? seconds + '秒' : Math.floor(seconds / 60) + '分' + seconds % 60 + '秒' }
function passRate(row: PRRecord) {
  const c = row.log?.pytest
  if (!c) return '未记录'
  const p = c.passed ?? 0
  const n = p + (c.failed ?? 0) + (c.error ?? 0) + (c.errors ?? 0) + (c.xpassed ?? 0)
  return n > 0 ? (100 * p / n).toFixed(1) + '%' : '未记录'
}
function ciTag(state: string) { return <Tag color={state === 'success' ? 'success' : state === 'pending' ? 'processing' : state === 'failure' || state === 'error' ? 'error' : 'default'}>{ciLabels[state] ?? '未记录'}</Tag> }
// Case-insensitive partial match across explicitly selected fields.
// Whitespace-separated terms may match different fields; #128 matches 128.
function matchesSearch(query:string, fields:unknown[]) {
  const normalize=(v:unknown)=>String(v??'').normalize('NFKC').toLowerCase().replace(/#/g,'')
  const haystack=fields.map(normalize).join(' ')
  return normalize(query).trim().split(/\s+/).filter(Boolean).every(term=>haystack.includes(term))
}
function PRTrackingPage({data, tasks, onReleaseDraft, onRequestRepair}: {data: PRTracking; tasks: DevelopmentTask[]; onReleaseDraft: (number: number, head: string) => Promise<void>; onRequestRepair: (number: number, head: string, base: string) => Promise<{url: string; created: boolean}>}) {
  const [selectedNumber, setSelectedNumber] = useState<number | null>(null)
  const [prSearch,setPrSearch]=useState('')
  const visiblePRs=[...data.prs].filter(p=>matchesSearch(prSearch,[p.number,p.title,p.head,p.base,p.merge_sha,p.head_branch,p.base_branch,ciLabels[p.test.state],phaseLabels[p.test.phase??''],mergeLabels[p.merge_state],deployLabels[p.deployment.state],...tasks.filter(t=>t.pr_numbers?.includes(p.number)).flatMap(t=>[t.number,t.title,t.owner])])).sort((a,b)=>Number(['open','draft'].includes(b.merge_state))-Number(['open','draft'].includes(a.merge_state))||b.number-a.number)
  const [repairNumber, setRepairNumber] = useState<number | null>(null)
  const [repairing, setRepairing] = useState(false)
  const [repairError, setRepairError] = useState<string | null>(null)
  const [repairIssueUrl, setRepairIssueUrl] = useState<string | null>(null)
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
  const repairPR = data.prs.find(row => row.number === repairNumber)
  async function createRepairTask() {
    if (!repairPR?.conflict?.blocked || !repairPR.conflict.base_sha || data.stale || repairing) return
    setRepairing(true)
    setRepairError(null)
    try {
      const result = await onRequestRepair(repairPR.number, repairPR.head, repairPR.conflict.base_sha)
      setRepairIssueUrl(result.url)
    } catch(error) {
      setRepairError(error instanceof Error ? error.message : '创建冲突修复任务失败')
    } finally { setRepairing(false) }
  }
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
    <Alert showIcon type={data.stale ? 'warning' : 'info'} message={data.stale ? '同步延迟或尚未同步，保留上次成功记录。' : 'GitHub 测试回执与本机部署记录'}
      description={`活跃 PR 独立同步，历史记录后台增量更新。当前仓库：${data.repository ?? '待识别'}；目标分支：${data.branch ?? '待识别'}。未取得本机测试或部署证据时显示“未记录”，不推算。`} />
    <Typography.Text type="secondary">活跃 PR 最近同步：{formatDate(data.active_synced_at ?? data.synced_at)} · 历史同步：{formatDate(data.history_synced_at ?? data.synced_at)}</Typography.Text>
    <Space wrap><Input.Search allowClear value={prSearch} onChange={e=>setPrSearch(e.target.value)} placeholder="搜索 PR、任务、负责人、版本、SHA、测试状态" style={{width:350}}/><Typography.Text type="secondary">{visiblePRs.length} / {data.prs.length} 条</Typography.Text></Space>
    <Table rowKey="number" size="small" dataSource={visiblePRs} scroll={{x: 1160}} locale={{emptyText: '暂无 PR 测试记录'}} columns={[
      {title: '关联任务', width: 120, render: (_,row) => tasks.filter(t => t.source !== 'pr' && t.pr_numbers?.includes(row.number)).map(t => '#' + t.number).join('、') || '待关联'},
      {title: 'PR', width: 280, render: (_,row) => <Typography.Link href={row.url} target="_blank" rel="noreferrer">#{row.number} {row.title}</Typography.Link>},
      {title: '测试状态', width: 155, render: (_,row) =>
        row.conflict?.blocked ? <Tag color="error">冲突阻止重新测试</Tag> :
        row.test.state === 'unknown' && row.head_status?.state === 'failure'
          ? <Tag color="error" title="GitHub 已记录本 HEAD 的 MacCI 失败，但精确 base/merge 回执尚未核实，不允许合并">失败 · 精确回执待核对</Tag>
          : row.test.state === 'unknown' && row.head_status?.state === 'error'
          ? <Tag color="error" title="GitHub 已记录本 HEAD 的 MacCI 异常，精确测试回执尚未核实">异常 · 精确回执待核对</Tag>
          : row.test.state === 'unknown' && row.head_status?.state === 'success'
          ? <Tag color="warning" title="GitHub 显示 success，但缺少与当前 base/merge 匹配的可信测试回执">成功信号 · 基线未核实</Tag>
          : <Flex vertical gap={2}>{ciTag(row.test.state)}{row.test.phase&&<Typography.Text type="secondary">{progressLabels[row.test.progress?.phase??'']??phaseLabels[row.test.phase]??row.test.phase}</Typography.Text>}{row.test.progress?.heartbeat_stale && <Tag color="warning">心跳超时 · 待核查</Tag>}</Flex>},
      {title: '测试开始', width: 165, render: (_,row) => formatDate(row.test.started_at ?? null)},
      {title: '测试结束', width: 165, render: (_,row) => formatDate(row.test.finished_at ?? null)},
      {title: '耗时', width: 95, render: (_,row) => duration(row.test.progress?.elapsed_seconds ?? row.test.duration_seconds)},
      {title: '通过率', width: 95, render: (_,row) => passRate(row)},
      {title: '合并', width: 122, render: (_,row) => row.conflict?.blocked ?
        <Tag color="error" title="Draft 已解除，但与当前 preview 存在冲突，MacCI/自动合并将暂停">存在冲突 · 待修复</Tag> :
        <Tag color={row.merge_state === 'merged' ? 'success' : 'default'}>{mergeLabels[row.merge_state]}</Tag>},
      {title: '操作', width: 134, render: (_,row) => row.conflict?.blocked ?
        <Button danger size="small" disabled={data.stale}
          onClick={() => { setRepairNumber(row.number); setRepairError(null); setRepairIssueUrl(null) }}>处理冲突</Button> :
        data.can_release_draft && row.merge_state === 'draft' ?
        releasedNumbers.includes(row.number) ? <Tag color="processing">已解除 · 待同步</Tag> :
        <Button size="small" disabled={data.stale || hasEarlierUnmergedRelease(row)}
          title={data.stale ? '同步已过期，请刷新' : hasEarlierUnmergedRelease(row) ? '存在更早的未合并版本，须先完成前置版本' : '确认后交由 MacCI 自动处理'}
          onClick={() => { setReleaseError(null); setReleaseNumber(row.number) }}>解除 Draft</Button> : '—'},
      {title: '部署', width: 125, render: (_,row) => <Tag color={row.deployment.state === 'running' || row.deployment.state === 'deployed' ? 'success' : row.deployment.state === 'failed' ? 'error' : 'default'}>{deployLabels[row.deployment.state]}</Tag>},
      {title: '详情', width: 70, render: (_,row) => <Button type="link" onClick={() => setSelectedNumber(row.number)}>查看</Button>},
    ]}/>
    <Modal title={repairPR ? 'PR #' + repairPR.number + ' · 合并冲突处理' : '合并冲突'}
      open={repairNumber !== null} onCancel={() => { if (!repairing) setRepairNumber(null) }}
      onOk={() => void createRepairTask()} okText="创建 / 查看修复任务"
      confirmLoading={repairing} okButtonProps={{disabled: data.stale || !repairPR?.conflict?.blocked || Boolean(repairIssueUrl)}}>
      <Alert showIcon type="error" message="合并冲突阻止当前基线测试"
        description="GitHub 已判定此 PR 与当前目标分支冲突。旧版测试通过并不等于当前基线通过；解除 Draft 也无法修复冲突。" />
      <Descriptions size="small" column={1} style={{marginTop:12}} items={[
        {key:'head', label:'PR HEAD',children:<Typography.Text code copyable>{repairPR?.head}</Typography.Text>},
        {key:'base', label:'当前目标基线',children:<Typography.Text code copyable>{repairPR?.conflict?.base_sha}</Typography.Text>},
        {key:'count', label:'PR 修改文件数',children:repairPR?.conflict?.candidate_files.length ?? 0},
      ]}/>
      <Typography.Paragraph strong style={{marginTop:12}}>需要检查的文件（不是确认存在冲突的文件）</Typography.Paragraph>
      <div style={{maxHeight:190,overflowY:'auto',background:'#f6f8fa',padding:12,borderRadius:8}}>
        {(repairPR?.conflict?.candidate_files ?? []).length ?
          repairPR?.conflict?.candidate_files.map((name) =>
            <div key={name}><Typography.Text code>{name}</Typography.Text></div>) :
          <Typography.Text type="secondary">GitHub 尚未返回文件列表，请打开 PR 查看变更。</Typography.Text>}
      </div>
      <Typography.Paragraph style={{marginTop:12}}>
        <Typography.Link href={repairPR?.url} target="_blank" rel="noreferrer">在 GitHub 打开 PR 并查看冲突</Typography.Link>
      </Typography.Paragraph>
      <Typography.Paragraph type="secondary">点击“创建 / 查看修复任务”会在当前仓库建立去重的 Issue，
        记录精确 SHA、阻塞原因和需检查的文件。开发任务可认领修复；完成后仍须重新通过 MacCI。此操作不会合并、覆盖代码或解除 Draft。</Typography.Paragraph>
      {repairIssueUrl && <Alert showIcon type="success" message="冲突修复任务已就绪"
        description={<Typography.Link href={repairIssueUrl} target="_blank" rel="noreferrer">打开 GitHub 修复任务</Typography.Link>} />}
      {repairError && <Alert showIcon type="error" message={repairError}/>}
    </Modal>
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
          {key:'status',label:'测试状态',children: selected.conflict?.blocked ? <Tag color="error">冲突阻止测试</Tag> : ciTag(selected.test.state)},
          {key:'conflict',label:'合并门禁',children:selected.conflict?.blocked ? <Button danger onClick={() => {setSelectedNumber(null);setRepairNumber(selected.number)}}>查看并处理冲突</Button> : '未确认冲突'},
          {key:'stage',label:'测试阶段',children:progressLabels[selected.test.progress?.phase??'']??phaseLabels[selected.test.phase ?? ''] ?? '未记录'},
          {key:'heartbeat',label:'最近心跳',children:selected.test.progress ? <Space>{formatDate(selected.test.progress.heartbeat_at)}{selected.test.progress.heartbeat_stale&&<Tag color="warning">超时 · 待核查</Tag>}</Space> : '未记录'},
          {key:'start',label:'开始时间',children:formatDate(selected.test.started_at ?? null)},
          {key:'end',label:'结束时间',children:formatDate(selected.test.finished_at ?? null)},
          {key:'duration',label:'测试耗时',children:duration(selected.test.progress?.elapsed_seconds ?? selected.test.duration_seconds)},
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
          {selected.test.failure_report && <Card size="small" title="MacCI 结构化失败报告">
            <Typography.Text type="secondary">运行 ID：{selected.test.failure_report.run_id}；诊断信息不作为合并授权。</Typography.Text>
            {selected.test.failure_report.failures.length ? selected.test.failure_report.failures.map((item,i)=><div key={i}><Typography.Text code>{item.file}{item.line ? ':'+item.line+':'+item.column : ''}{item.test ? '::'+item.test : ''} · {item.category}</Typography.Text></div>) : <Typography.Text>未提取到可安全展示的失败用例，请核对宿主日志。</Typography.Text>}
          </Card>}
          {selected.log?.pytest ? <Descriptions column={1} size="small" items={Object.entries(selected.log.pytest).map(([name,n])=>({key:name,label:name,children:n}))}/> : <Typography.Text type="secondary">未取得匹配本次测试的本机计数。可在 GitHub 核对回执；原始日志保留在 Mac mini。</Typography.Text>}
          <Typography.Paragraph type="secondary" style={{margin:'8px 0 0'}}>通过率 = passed / (passed + failed + errors + xpassed)，不计跳过、预期失败及未执行用例。前端构建结果单独以阶段回执记录。</Typography.Paragraph>
        </Card><Typography.Link href={selected.url} target="_blank" rel="noreferrer">查看 GitHub PR 原始状态</Typography.Link></Flex> : <Empty description="记录已更新或不可用"/>}
    </Drawer>
  </Flex>
}

export function DevelopmentTasksPage({ data, loading, onRefresh, onReleaseDraft, onRequestRepair }: {
  data: TaskSnapshot | null
  loading: boolean
  onRefresh: () => void
  onReleaseDraft: (number: number, head: string) => Promise<void>
  onRequestRepair: (number: number, head: string, base: string) => Promise<{url: string; created: boolean}>
}) {
  const [taskSelected, setTaskSelected] = useState<string | null>(null)
  const [filter, setFilter] = useState('current')
  const [search, setSearch] = useState('')
  const [order,setOrder]=useState('work')
  const category = (t: DevelopmentTask) => {
    const stage=t.delivery_status ?? ''
    if (t.status === '已关闭' || (t.source === 'pr' && t.merge_state === 'closed')) return 'closed'
    if (t.status === '已完成' || (t.source==='pr' && stage === '用户验收通过 · 已正式发布')) return 'done'
    if (t.test_failures?.length || t.status === '受阻' || /冲突|失败|不通过/.test(stage)) return 'blocked'
    if (/待验收确认|待正式发布/.test(stage) || (t.status==='待验收' && t.merge_state==='merged' && t.deployment_state==='running')) return 'review'
    if (stage === '关联代码已正式发布 · 需求待结项') return 'review'
    if (t.merge_state === 'merged') return 'review'
    if (t.merge_state === 'open' || t.merge_state === 'draft' || t.status === '开发中') return 'active'
    return 'todo'
  }
  const priority:Record<string,number>={blocked:0,review:1,active:2,todo:3,done:4,closed:5}
  const allTasks=data?.tasks ?? []
  const counts=allTasks.reduce<Record<string,number>>((acc,t)=>{const key=category(t);acc[key]=(acc[key]??0)+1;return acc},{})
  const timestamp=(t:DevelopmentTask)=>{const ts=Date.parse(t.updated_at ?? '');return Number.isFinite(ts)?ts:0}
  const visibleTasks = [...allTasks].filter(t => {
    const c=category(t)
    return (filter==='all' || (filter==='current' && c!=='done' && c!=='closed') || filter===c) &&
      matchesSearch(search,[t.number,t.title,t.owner,t.github_actor,t.note,t.status,t.delivery_status,...(t.pr_numbers??[]),...(t.prs??[]).flatMap(p=>[p.number,p.title,p.merge_sha,phaseLabels[p.test?.phase??'']]),...(data?.pr_tracking?.prs??[]).filter(p=>t.pr_numbers?.includes(p.number)).flatMap(p=>[p.head,p.base,p.head_branch,p.title,ciLabels[p.test.state],phaseLabels[p.test.phase??'']])])
  }).sort((a,b)=>{
    if(order==='number_desc')return b.number-a.number
    if(order==='number_asc')return a.number-b.number
    if(order==='work'){
      const diff=priority[category(a)]-priority[category(b)]
      if(diff)return diff
    }
    return timestamp(b)-timestamp(a)||b.number-a.number
  })
  const selectedTask = data?.tasks.find(task => (task.key ?? 'issue-' + task.number) === taskSelected)
  const sync = data?.refresh_status
  const syncing = sync?.state === 'queued' || sync?.state === 'running'
  const tasks = <Flex vertical gap="middle">
      {sync && sync.state !== 'unknown' && <Alert showIcon
        type={sync.state === 'failed' || sync.state === 'interrupted' ? 'warning' : sync.state === 'success' ? 'success' : 'info'}
        message={syncing ? '后台同步中 · 可继续操作' : sync.state === 'success' ? '最近同步成功' : '同步未完成 · 保留已有记录'}
        description={`阶段：${sync.phase ?? '等待领取'}；GitHub 请求：${sync.requests ?? 0}；耗时：${sync.elapsed_seconds ?? 0}秒${sync.reason ? '；原因：' + sync.reason : ''}`}/>}

      {sync?.token_health && Object.keys(sync.token_health).length > 0 && <Card size="small" title="GitHub Token 健康检测 · 最近手动同步">
        <Descriptions size="small" column={1} items={Object.entries(sync.token_health).map(([key, item])=>({
          key, label: ({identity:'REST 身份',rate_limit:'API 额度',repository:'仓库读取权限',graphql:'GraphQL 读取'} as Record<string,string>)[key] ?? key,
          children: <Space wrap><Tag color={item.state==='ok'?'success':'error'}>{item.state==='ok'?'通过':'失败'}</Tag>
            {item.login ?? item.permission ?? item.reason ?? ''}
            {item.remaining != null ? `${item.remaining}/${item.limit}` : ''}
            {item.permissions ? Object.entries(item.permissions).map(([k,v])=>`${k}:${v?'是':'否'}`).join('；') : ''}
            {item.elapsed_seconds != null ? `${item.elapsed_seconds}秒` : ''}</Space>
        }))}/>
        <Typography.Text type="secondary">只读检测，不代表已验证写入或合并授权；点击立即同步可重新检测。</Typography.Text>
      </Card>}
      {data?.stale && <Alert type="warning" showIcon message={
        data?.synced_at ? '同步已延迟，以下为上次成功获取的记录。' : '等待从 GitHub 同步开发任务。'
      } />}
      {data?.synced_at && <Typography.Text type="secondary">GitHub 最近同步 {formatDate(data.synced_at)}</Typography.Text>}
      <Space wrap>
        <Input.Search allowClear placeholder="搜索任务、PR、负责人、版本或状态" value={search} onChange={e=>setSearch(e.target.value)} style={{width:310}}/>
        <Select value={filter} onChange={setFilter} style={{width:155}} options={[
          {value:'current',label:'当前任务'},{value:'all',label:'全部任务'},{value:'todo',label:'待开发'},{value:'active',label:'开发中'},
          {value:'blocked',label:'受阻 / 失败'},{value:'review',label:'待验收 / 发布'},
          {value:'done',label:'已完成'},{value:'closed',label:'已关闭'}
        ]}/>
        <Select value={order} onChange={setOrder} style={{width:155}} options={[
          {value:'work',label:'当前工作优先'},{value:'updated',label:'最近更新优先'},
          {value:'number_desc',label:'编号从新到旧'},{value:'number_asc',label:'编号从旧到新'}
        ]}/>
        <Typography.Text type="secondary">{visibleTasks.length} / {allTasks.length} 条</Typography.Text>
      </Space>
      <Space wrap>{([{key:'blocked',label:'受阻'},{key:'review',label:'待验收 / 发布'},{key:'active',label:'开发中'},{key:'todo',label:'待开发'},{key:'done',label:'已完成'},{key:'closed',label:'已关闭'}]).map(x=><Button key={x.key} size="small" type={filter===x.key?'primary':'default'} onClick={()=>setFilter(filter===x.key?'current':x.key)}>{x.label} {counts[x.key]??0}</Button>)}
        {filter==='current'&&<Typography.Text type="secondary">历史任务已隐藏，可切换查看</Typography.Text>}
      </Space>
      <Table
        rowKey={task => task.key ?? 'issue-' + task.number}
        size="small"
        loading={loading}
        dataSource={visibleTasks}
        scroll={{ x: 1600 }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={
          data?.synced_at ? '暂无开发任务或 PR 记录' : '暂无同步记录'
        } /> }}
        columns={[
          { title: '任务', dataIndex: 'title', width: 310, render: (title: string, task: DevelopmentTask) =>
            <Typography.Link href={task.url} target="_blank" rel="noreferrer">#{task.number} {title}</Typography.Link> },
          { title: '负责人', width: 110, render: (_, task: DevelopmentTask) => task.owner || '未指定' },
          { title: '关联 PR', width: 110, render: (_, task: DevelopmentTask) => task.pr_numbers?.length ? task.pr_numbers.map(n => '#' + n).join('、') : '—' },
          { title: '操作', width: 84, render: (_, task: DevelopmentTask) => <Button type="link" size="small"
            onClick={() => setTaskSelected(task.key ?? 'issue-' + task.number)}>查看链路</Button> },
          { title: '来源', width: 85, render: (_, task: DevelopmentTask) =>
            <Tag>{task.source === 'pr' ? '代码任务' : task.origin === 'feedback' ? '意见反馈' : '需求任务'}</Tag> },
          { title: '当前阶段', width: 205, render: (_, task: DevelopmentTask) =>
            <Tag color={statusColors[task.status] ?? 'default'}>{task.delivery_status ?? task.status}</Tag> },
          { title: '测试', width: 220, render: (_, task: DevelopmentTask) =>
            task.test_failures?.length ? <Flex vertical gap={4}>{task.test_failures.map(f=><div key={f.number}>
              <Tag color="error">#{f.number} {phaseLabels[f.phase ?? ''] ?? '测试失败'}</Tag>
              <Typography.Text type="secondary">{formatDate(f.at ?? null)} · {f.head?.slice(0,7) ?? 'SHA 未记录'}</Typography.Text>
            </div>)}</Flex> : task.historical_test_failures?.length ? <Tag>历史失败 · 已关闭 PR</Tag> : task.test_state ? ciTag(task.test_state) : '—' },
          { title: '合并', width: 100, render: (_, task: DevelopmentTask) =>
            task.merge_state ? <Tag>{mergeLabels[task.merge_state] ?? task.merge_state}</Tag> : '—' },
          { title: '部署', width: 115, render: (_, task: DevelopmentTask) =>
            task.deployment_state ? <Tag>{deployLabels[task.deployment_state] ?? task.deployment_state}</Tag> : '—' },
          { title: '最新进展', dataIndex: 'note', render: (note: string) => note || '—' },
          { title: '更新时间', dataIndex: 'updated_at', width: 168, render: formatDate },
        ]}
      />
    </Flex>
  return <Card title="开发任务" extra={<Button onClick={onRefresh} loading={loading} disabled={syncing}>{syncing ? "同步中" : "立即同步"}</Button>}>
    {data?.pr_tracking ? <Tabs items={[{key:'tasks',label:'统一任务清单',children:tasks},{key:'tracking',label:'测试与发布追踪',children:<PRTrackingPage tasks={data.tasks} data={data.pr_tracking} onReleaseDraft={onReleaseDraft} onRequestRepair={onRequestRepair}/>}]} /> : tasks}
    <Drawer title={selectedTask ? '#' + selectedTask.number + ' · 完整交付链路' : '交付链路'}
      open={taskSelected !== null} onClose={() => setTaskSelected(null)} width="min(720px, 100vw)">
      {selectedTask ? <Flex vertical gap="middle">
        <Typography.Text strong>{selectedTask.title}</Typography.Text>
        <Descriptions size="small" bordered column={1} items={[
          {key:'owner',label:'负责人',children:selectedTask.owner || '未指定'},
          {key:'actor',label:'GitHub 作者',children:selectedTask.github_actor || '未记录'},
          {key:'claimed',label:'领取时间',children:formatDate(selectedTask.claimed_at ?? null)},
          {key:'started',label:'开发开始',children:formatDate(selectedTask.development_started_at ?? null)},
          {key:'stage',label:'当前阶段',children:selectedTask.delivery_status || selectedTask.status},
          {key:'source',label:'任务来源',children:selectedTask.source === 'pr' ? '独立代码任务' : '需求任务'},
          {key:'link',label:'关联 PR',children:selectedTask.pr_numbers?.length ? selectedTask.pr_numbers.map(n=>'#'+n).join('、') : '暂无关联'},
        ]}/>
        <Typography.Paragraph type="secondary">开发任务可对应多个 PR。测试成功不代表已发布，合并成功不代表完成验收；状态仅来自实际记录。</Typography.Paragraph>
        {(selectedTask.prs ?? []).map(pr => <Card key={pr.number} size="small"
          title={<Typography.Link href={pr.url} target="_blank" rel="noreferrer">PR #{pr.number} · {pr.title || '代码提交'}</Typography.Link>}>
          <Descriptions size="small" column={1} items={[
            {key:'dev',label:'开发负责人',children:pr.owner || '未指定'},
            {key:'ci',label:'MacCI 测试',children:ciLabels[pr.test?.state || 'unknown'] || '尚未核实'},
            {key:'phase',label:'测试阶段',children:phaseLabels[pr.test?.phase ?? ''] ?? '未记录'},
            {key:'start',label:'测试开始',children:formatDate(pr.test?.started_at ?? null)},
            {key:'end',label:'测试结束',children:formatDate(pr.test?.finished_at ?? null)},
            {key:'branch',label:'开发分支',children:data?.pr_tracking?.prs.find(r=>r.number===pr.number)?.head_branch ?? '未记录'},
            {key:'targetBranch',label:'目标分支',children:data?.pr_tracking?.prs.find(r=>r.number===pr.number)?.base_branch ?? '未记录'},
            {key:'head',label:'PR HEAD',children:data?.pr_tracking?.prs.find(r=>r.number===pr.number)?.head ?? '未记录'},
            {key:'running',label:'当前应用运行 SHA',children:data?.pr_tracking?.running_sha ?? '尚未核实'},
            {key:'mergeSha',label:'合并 SHA',children:pr.merge_sha ?? '未记录'},
            {key:'merge',label:'合并状态',children:mergeLabels[pr.merge_state || ''] || '未记录'},
            {key:'mergedAt',label:'合并时间',children:formatDate(pr.merged_at ?? null)},
            {key:'deploy',label:'部署状态',children:deployLabels[pr.deployment?.state || ''] || '尚未核实'},
            {key:'deployAt',label:'部署时间',children:formatDate(pr.deployment?.at ?? null)},
          ]}/>
        </Card>)}
        {!selectedTask.prs?.length && <Alert type="info" showIcon message="尚未找到明确关联的 PR"
          description="不会根据任务标题或编号猜测关联关系。代码提交需明确填写所对应的 Issue 编号。" />}
        <Typography.Link href={selectedTask.url} target="_blank" rel="noreferrer">查看原始任务记录</Typography.Link>
      </Flex> : <Empty description="没有可显示的任务" />}
    </Drawer>
  </Card>
}
