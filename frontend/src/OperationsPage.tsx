import { useState } from 'react'
import { ScreenshotPicker } from './ScreenshotPicker'
import { Alert, Button, Card, Descriptions, Drawer, Empty, Flex, Input, Image, List, Modal, Select, Space, Table, Tag, Typography } from 'antd'

type Operation = { at: string; code: string; sha: string; detail?: string }
type ReleaseNote = { version: string; title: string; changes: string[]; validation?: string; date?: string; summary?: string; acceptance_required?: boolean; acceptance_status?: string; validation_steps?: string[]; validation_note?: string; reference_url?: string }
export type ReleaseHistory = {
  version: string
  running_sha: string
  releases: ReleaseNote[]
  deployments: Operation[]
  acceptance?: { enabled: boolean; state: 'unavailable' | 'not_requested' | 'queued' | 'published' | 'blocked' | 'unknown' | 'rejected'; attachments?: {id:string;url:string}[]; issue?: number | null; ready?: boolean; reason?: string; preview_sha?: string; main_sha?: string; is_published?: boolean }
}
type QueueJob = { id:string; state:string; head:string|null; base:string|null; created:number|null; expires:number|null }
type QueueEvent = { seq:number; job:string; event:string; at:number|null }
type QueueDiagnostics = { state:string; version:string|null; jobs:QueueJob[]; events:QueueEvent[]; truncated:boolean }
type PreviewAttempt = {seq:number;pr:number;head:string;base:string;candidate:string;state:'PENDING'|'INTEGRATED'|'REJECTED';created:number;finished:number|null}
type PreviewAttempts = {state:string;attempts:PreviewAttempt[];truncated:boolean}
export type SystemLogs = { events: Operation[]; integration?:QueueDiagnostics; preview_attempts?:PreviewAttempts }

const attemptLabels = {PENDING:'待对账',INTEGRATED:'已确认集成',REJECTED:'明确拒绝'}
function PreviewAttemptDiagnostics({data}:{data?:PreviewAttempts}) {
  const [state,setState]=useState<string>('all')
  const [selected,setSelected]=useState<PreviewAttempt|null>(null)
  const records=data?.attempts??[]
  const pending=records.filter(r=>r.state==='PENDING').length
  return <Card size="small" title="宿主合并执行回执" extra={<Select size="small" value={state} onChange={setState} options={[{value:'all',label:'全部状态'},...Object.entries(attemptLabels).map(([value,label])=>({value,label}))]} />}>
    {data?.state!=='available'?<Typography.Text type="secondary">{data?.state==='not_configured'?'尚未配置宿主回执来源':'宿主回执暂不可用'}</Typography.Text>:<Space direction="vertical" style={{width:'100%'}}>
      {pending>0&&<Alert type="warning" showIcon message="存在未确认合并结果，等待宿主对账；不得直接重复推送。" />}
      {data.truncated&&<Typography.Text type="secondary">仅展示最近100次执行</Typography.Text>}
      <Table<PreviewAttempt> size="small" rowKey="seq" dataSource={records.filter(r=>state==='all'||r.state===state)} pagination={{pageSize:5}} scroll={{x:650}} columns={[
        {title:'PR',dataIndex:'pr',render:(v:number)=>'#'+v},
        {title:'状态',dataIndex:'state',render:(v:PreviewAttempt['state'])=><Tag color={v==='PENDING'?'warning':v==='INTEGRATED'?'success':'error'}>{attemptLabels[v]}</Tag>},
        {title:'候选 SHA',dataIndex:'candidate',render:(v:string)=><code title={v}>{v.slice(0,8)}</code>},
        {title:'开始时间',dataIndex:'created',render:queueTime},
        {title:'结束时间',dataIndex:'finished',render:queueTime},
        {title:'操作',render:(_:unknown,row:PreviewAttempt)=><Button type="link" onClick={()=>setSelected(row)}>详情</Button>}
      ]}/>
      <Typography.Text type="secondary">宿主回执仅证明合并结果，不代表部署、验收或正式发布成功。</Typography.Text>
    </Space>}
    <Drawer title="宿主执行回执详情" open={selected!==null} onClose={()=>setSelected(null)}>
      {selected&&<Descriptions column={1} size="small" items={[
        {key:'pr',label:'PR',children:'#'+selected.pr},
        {key:'state',label:'状态',children:attemptLabels[selected.state]},
        ...(['head','base','candidate'] as const).map(k=>({key:k,label:k,children:<Typography.Text code copyable>{selected[k]}</Typography.Text>})),
        {key:'created',label:'开始',children:queueTime(selected.created)},
        {key:'finished',label:'结束',children:queueTime(selected.finished)}
      ]}/>}
    </Drawer>
  </Card>
}

const queueStateLabels:Record<string,string> = { QUEUED:'等待领取', CLAIMED:'已领取', PREPARED:'候选已准备', UNKNOWN:'结果未知 · 等待对账', INTEGRATED:'已集成', CANCELLED:'已取消' }
const queueTime = (value:number|null) => value===null ? '未记录' : new Date(value*1000).toLocaleString()
function IntegrationDiagnostics({ data }: {data?:QueueDiagnostics}) {
  if (!data || data.state !== 'available') return <Card size="small" title="集成队列诊断"><Typography.Text type="secondary">{data?.state==='not_configured'?'尚未配置队列证据源':data?.state==='not_initialized'?'队列尚未初始化':'队列证据暂不可用'}</Typography.Text></Card>
  return <Card size="small" title={'集成队列诊断 · '+data.version}>
    <Space direction="vertical" size="middle" style={{width:'100%'}}>
      {data.truncated && <Alert type="info" message="仅展示最近 100 条任务和事件" />}
      <Table<QueueJob> size="small" rowKey="id" dataSource={data.jobs} pagination={{pageSize:5}} scroll={{x:650}} columns={[
        {title:'任务',dataIndex:'id'},
        {title:'状态',dataIndex:'state',render:(v:string)=><Tag color={v==='UNKNOWN'?'warning':v==='INTEGRATED'?'success':'default'}>{queueStateLabels[v]??v}</Tag>},
        {title:'Head',dataIndex:'head',render:(v:string|null)=>v?.slice(0,8)??'未记录'},
        {title:'Base',dataIndex:'base',render:(v:string|null)=>v?.slice(0,8)??'未记录'},
        {title:'租约截止',dataIndex:'expires',render:queueTime}
      ]} />
      <Table<QueueEvent> size="small" rowKey="seq" dataSource={data.events} pagination={{pageSize:5}} scroll={{x:550}} columns={[
        {title:'事件时间',dataIndex:'at',render:queueTime},
        {title:'任务',dataIndex:'job'},
        {title:'执行事件',dataIndex:'event'}
      ]} />
      <Typography.Text type="secondary">只读执行证据；候选准备完成不代表测试、合并或部署成功。</Typography.Text>
    </Space>
  </Card>
}

const labels: Record<string, string> = {
  host_test_case_failed: '宿主测试用例失败',
  startup_ok: '启动自检通过',
  schema_check_failed: '数据库迁移检查失败',
  frontend_missing: '缺少前端构建产物',
  install_ok: '安装通过本机健康检查',
  install_failed: '安装健康检查失败',
  update_ok: '更新通过本机健康检查',
  update_blocked: '更新等待数据库迁移',
  update_started: '更新检查开始', update_candidate: '取得目标提交', update_dependencies: '依赖检查',
  update_test_build: '测试与构建', update_restarting: '重启与健康检查', update_locked: '更新锁已占用',
  update_no_change: '已检查，无新提交', update_held: '失败提交暂缓更新', update_preflight_failed: '更新前置检查失败',
  update_fetch_failed: '拉取失败', update_dependencies_failed: '依赖检查失败', update_schema_failed: '数据库检查失败',
  update_test_build_failed: '测试或构建失败', update_restart_failed: '重启或健康验证失败',
  rollback: '发布失败，已尝试回退',
  rollback_ok: '回滚后服务已恢复',
  rollback_failed: '回滚后服务未恢复',
  feedback_ok: '反馈已同步到 GitHub',
  feedback_failed: '反馈同步失败',
}

function OperationTable({ records, loading, onDetails }: { records: Operation[]; loading: boolean; onDetails?: (row: Operation) => void }) {
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
        diagnosticSeverity(code) === 'error' ? 'error' : diagnosticSeverity(code) === 'warning' ? 'warning' : diagnosticSeverity(code) === 'success' ? 'success' : 'default'
      }>{labels[code] ?? code}</Tag> },
      { title: '错误详情', render: (_,row) => row.detail ? <Typography.Text code copyable>{row.detail}</Typography.Text> : '—' },
      { title: '关联提交', dataIndex: 'sha', width: 120, render: (sha: string) => <code title={sha}>{sha.slice(0, 10)}</code> },
      ...(onDetails ? [{ title: '操作', key: 'action', width: 80, render: (_: unknown, row: Operation) => <Button type="link" onClick={() => onDetails(row)}>详情</Button> }] : []),
    ]}
  />
}

type RefreshProps = { loading: boolean; onRefresh: () => void }

function acceptanceTag(release: ReleaseNote) {
  if (release.acceptance_required) return <Tag color="warning">需要验收</Tag>
  if (release.acceptance_status === 'observed') return <Tag color="success">已有使用反馈</Tag>
  return <Tag>{release.acceptance_required === false ? '常规更新' : '验收状态未记录'}</Tag>
}

function safeReference(value?: string) {
  if (!value) return undefined
  try {
    const parsed = new URL(value)
    return parsed.protocol === 'https:' && parsed.hostname === 'github.com' && !parsed.username && !parsed.password ? parsed.href : undefined
  } catch { return undefined }
}

export function releaseEvidence(data: ReleaseHistory | null) {
  const valid = (value?: string) => typeof value === 'string' && /^[0-9a-f]{40}$/.test(value) ? value : '未记录'
  const running = valid(data?.running_sha)
  const preview = valid(data?.acceptance?.preview_sha)
  // A published acceptance proves the accepted running SHA, not an independently refreshed main head.
  const main = valid(data?.acceptance?.main_sha)
  const success = (data?.deployments ?? []).filter(row =>
    ['update_ok','install_ok'].includes(row.code) && valid(row.sha) !== '未记录' && Number.isFinite(Date.parse(row.at))
  ).sort((a,b)=>Date.parse(b.at)-Date.parse(a.at))[0]
  return {running,preview,main,deployedAt:success ? new Date(success.at).toLocaleString('zh-CN',{hour12:false}) : '未记录',deployedSha:valid(success?.sha)}
}

export function ReleasePage({ data, loading, onRefresh, onAccept, onReject }: RefreshProps & { data: ReleaseHistory | null; onAccept: (sha: string, version: string) => Promise<void>; onReject:(sha:string,version:string,reason:string,screenshots:string[])=>Promise<void> }) {
  const [releaseQuery,setReleaseQuery] = useState('')
  const [selectedVersion, setSelectedVersion] = useState<string | null>(null)
  const [deployment, setDeployment] = useState<Operation | null>(null)
  const [accepting, setAccepting] = useState(false)
  const [confirm, setConfirm] = useState<{sha: string; version: string} | null>(null)
  const [acceptError, setAcceptError] = useState<string | null>(null)
  const [rejecting,setRejecting]=useState(false)
  const [rejectTarget,setRejectTarget]=useState<{sha:string;version:string}|null>(null)
  const [reason,setReason]=useState('')
  const [screenshots,setScreenshots]=useState<string[]>([])
  const [rejectError,setRejectError]=useState('')
  const acceptance = data?.acceptance
  const canReject=Boolean(acceptance?.enabled&&acceptance.state==='not_requested'&&acceptance.ready===true&&acceptance.is_published===false&&data&&/^[0-9a-f]{40}$/.test(data.running_sha)&&data.releases[0]?.acceptance_required)
  const canAccept = Boolean(acceptance?.enabled && acceptance.state === 'not_requested' && acceptance.ready === true && data && /^[0-9a-f]{40}$/.test(data.running_sha) && data.releases[0]?.acceptance_required && data.releases[0]?.version === data.version)
  const gateWarning = acceptance?.enabled && (Boolean(acceptance.reason) || (acceptance.state === 'not_requested' && !canAccept))
  function requestAcceptance() {
    if (!data || !canAccept) return
    setAcceptError(null)
    setConfirm({ sha: data.running_sha, version: data.version })
  }
  async function confirmAcceptance() {
    if (!confirm || accepting || !canAccept || data?.running_sha !== confirm.sha || data?.version !== confirm.version) {
      setConfirm(null)
      setAcceptError('运行版本或候选状态已变化，请刷新后重新验收')
      return
    }
    setAccepting(true)
    setAcceptError(null)
    try {
      await onAccept(confirm.sha, confirm.version)
      setConfirm(null)
    } catch (error) {
      setAcceptError(error instanceof Error ? error.message : '验收提交失败，请重试')
    } finally {
      setAccepting(false)
    }
  }
  function acceptanceAction() {
    if (!acceptance?.enabled) return null
    if (acceptance.state === 'rejected') return <Tag color="error">验收不通过 · 待修复</Tag>
    if (acceptance.state === 'published') return <Tag color="success">验收通过 · 已发布 main</Tag>
    if (acceptance.state === 'queued') return <Tag color="processing">验收已提交 · 等待发布 main</Tag>
    if (acceptance.state === 'blocked') return <Tag color="error">发布受阻，请查看验收任务</Tag>
    if (acceptance.state === 'unknown') return <Tag color="warning">发布状态待刷新</Tag>
    if (!canAccept) return <Button disabled title={acceptance.reason || '候选未就绪'}>验收通过</Button>
    return <Button type="primary" onClick={requestAcceptance} disabled={accepting || loading}>验收通过</Button>
  }
  const selected = data?.releases.find(release => release.version === selectedVersion)
  const awaitingAcceptance = Boolean(data?.releases[0]?.acceptance_required && acceptance?.state === 'not_requested' && acceptance.ready)
  const evidence = releaseEvidence(data)
  const visibleReleases = (data?.releases ?? []).filter(row=>[row.version,row.title,row.summary,...(row.changes ?? [])].some(value=>String(value ?? '').toLowerCase().includes(releaseQuery.trim().toLowerCase())))
  return <Flex vertical gap="middle">
    <div><Typography.Title level={3} style={{margin:'0 0 4px'}}>更新记录</Typography.Title><Typography.Text type="secondary">版本、部署证据与用户验收记录</Typography.Text></div>
    <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(min(220px,100%),1fr))',gap:12}}>
      {[
        ['运行版本',data?.version || '未记录',evidence.running],
        ['Preview 候选','候选 SHA',evidence.preview],
        ['Main 正式发布','正式 SHA',evidence.main],
        ['最近成功部署',evidence.deployedAt,evidence.deployedSha],
      ].map(([label,value,sha])=><Card key={label} size="small"><Typography.Text type="secondary">{label}</Typography.Text><Typography.Title level={4} style={{margin:'8px 0',overflowWrap:'anywhere'}}>{value}</Typography.Title><Typography.Text code style={{overflowWrap:'anywhere'}}>{sha}</Typography.Text></Card>)}
    </div>
    <Card title="当前运行版本" extra={<Space wrap>{acceptance?.is_published && acceptance.state==='not_requested' && <Tag color="blue">已发布 · 待补验收</Tag>}{awaitingAcceptance && <Tag color="warning">待验收 v{data?.version}</Tag>}{acceptanceAction()}{canReject&&<Button danger onClick={()=>{setRejectError('');setRejectTarget(data?{sha:data.running_sha,version:data.version}:null)}}>验收不通过</Button>}<Button onClick={onRefresh} loading={loading}>刷新</Button></Space>}>
      {data ? <Flex wrap gap="small" align="center">
        <Tag color="blue">{data.version}</Tag>
        <Typography.Text copyable code style={{ overflowWrap: 'anywhere' }}>{data.running_sha === 'unknown' ? '未上报 Git SHA' : data.running_sha}</Typography.Text>
      </Flex> : <Typography.Text type="secondary">暂无版本信息</Typography.Text>}
    </Card>
    {gateWarning ?
      <Alert type="warning" showIcon message="当前页面暂不可验收" description={acceptance.reason || '正在核对 preview 和本机部署，请刷新后重试。'} /> :
      <Alert type="info" showIcon message="代码已提交不等于线上已验收" description={acceptance?.enabled ? '仅在最新 preview 与运行 SHA 和部署记录一致时允许验收。' : '请核对当前运行版本和 Git SHA，以实际验证结果为准。'} />}
    {acceptance?.is_published && acceptance.state==='not_requested' && <Alert type="info" showIcon message="正式发布与功能验收独立记录" description="当前版本已经发布；如发现问题，请通过开发任务提交需求或问题反馈，不使用未发布候选的拒绝流程。" />}
    {acceptance?.issue && <Typography.Link href={`https://github.com/layho/web-app-init-base-env/issues/${acceptance.issue}`} target="_blank" rel="noopener noreferrer">{acceptance.state==='rejected'?'查看修复任务':'查看正式发布任务'} #{acceptance.issue}</Typography.Link>}
    {acceptance?.state==='rejected'&&<Alert type="error" showIcon message="当前候选验收不通过" description={<Space direction="vertical"><span>{acceptance.reason}</span><Space wrap>{acceptance.attachments?.map(x=><Image key={x.id} width={100} src={x.url}/>)}</Space></Space>}/>}
    <Modal title="提交验收不通过" open={rejectTarget!==null} onCancel={()=>{if(!rejecting)setRejectTarget(null)}} confirmLoading={rejecting} okText="提交不通过" cancelText="取消"
      okButtonProps={{danger:true,disabled:!canReject||reason.trim().length<5}} onOk={async()=>{
        if(!data||!canReject||rejecting||!rejectTarget||data.running_sha!==rejectTarget.sha||data.version!==rejectTarget.version){setRejectError('候选已变化，请重新打开验收窗口');return}
        setRejecting(true);setRejectError('')
        try{await onReject(rejectTarget.sha,rejectTarget.version,reason,screenshots);setRejectTarget(null);setReason('');setScreenshots([])}
        catch(e){setRejectError(e instanceof Error?e.message:'提交失败')}
        finally{setRejecting(false)}
      }}>
      <Space direction="vertical" style={{width:'100%'}}>
        <Typography.Text>v{rejectTarget?.version} · {rejectTarget?.sha}</Typography.Text>
        <Input.TextArea rows={5} maxLength={2000} showCount value={reason} onChange={e=>setReason(e.target.value)} placeholder="必填：描述验收失败原因"/>
        <ScreenshotPicker value={screenshots} onChange={setScreenshots} disabled={rejecting}/>
        {rejectError&&<Alert type="error" message={rejectError}/>}
      </Space>
    </Modal>
    <Modal title={acceptance?.is_published ? "补录功能验收" : "确认验收并正式发布"} open={confirm !== null} onCancel={() => { if (!accepting) setConfirm(null) }} onOk={() => void confirmAcceptance()} okText={acceptance?.is_published ? "确认验收通过" : "验收通过，提交发布"} cancelText="取消" confirmLoading={accepting} okButtonProps={{ disabled: !canAccept || loading }} closable={!accepting} maskClosable={!accepting} cancelButtonProps={{ disabled: accepting }}>
      <Typography.Paragraph>{acceptance?.is_published ? "确认已体验当前正式运行版本，仅补录功能验收，不重复发布。" : "确认已体验以下候选版本，授权将该版本发布至正式 main 分支。系统会再次核验测试结果和分支 SHA。"}</Typography.Paragraph>
      <Typography.Paragraph strong>v{confirm?.version}</Typography.Paragraph>
      <Typography.Paragraph code style={{ overflowWrap: 'anywhere' }}>{confirm?.sha}</Typography.Paragraph>
      {acceptError && <Alert type="error" showIcon message={acceptError} />}
    </Modal>
    <Card size="small"><Space wrap><Typography.Text strong>版本时间线</Typography.Text><Input.Search allowClear value={releaseQuery} onChange={event=>setReleaseQuery(event.target.value)} placeholder="搜索版本、标题、更新内容" style={{maxWidth:'100%',width:280}}/><Typography.Text type="secondary">{visibleReleases.length} / {data?.releases.length ?? 0} 个版本</Typography.Text></Space></Card>
    {visibleReleases.length ? visibleReleases.map(release =>
      <Card key={release.version}
        title={<Space wrap><Typography.Text strong>v{release.version}</Typography.Text><span>{release.title}</span></Space>}
        extra={<Space wrap><Typography.Text type="secondary">{release.date || '日期未记录'}</Typography.Text>{release.version === data?.version && acceptance?.state === 'published' ? <Tag color="success">已验收</Tag> : acceptanceTag(release)}{release.version === data?.version ? acceptanceAction() : null}<Button type="link" onClick={() => setSelectedVersion(release.version)}>详情</Button></Space>}>
        {release.summary && <Typography.Paragraph type="secondary">{release.summary}</Typography.Paragraph>}
        <List size="small" dataSource={release.changes ?? []} renderItem={change =>
          <List.Item><Typography.Text style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{change}</Typography.Text></List.Item>} />
        {release.validation_steps?.length ? <div style={{ marginTop: 12, padding: 12, border: '1px solid #f5d591', background: '#fffdf5', borderRadius: 8 }}>
          <Typography.Text strong>验收步骤</Typography.Text>
          <ol>{release.validation_steps.map((step, i) => <li key={i} style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{step}</li>)}</ol>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>{release.validation_note || release.validation}</Typography.Paragraph>
        </div> : <Typography.Paragraph type="secondary" style={{ margin: '12px 0 0' }}>{release.validation_note || release.validation || '暂无验收说明'}</Typography.Paragraph>}
        {safeReference(release.reference_url) && <Typography.Link href={safeReference(release.reference_url)} target="_blank" rel="noopener noreferrer">查看 GitHub 记录</Typography.Link>}
      </Card>
    ) : <Card><Empty description={loading ? '正在读取更新记录…' : releaseQuery ? '未找到匹配版本' : '暂无更新记录'} /></Card>}
    <Card title="本机发布记录">
      <OperationTable records={data?.deployments ?? []} loading={loading} onDetails={setDeployment} />
    </Card>
    <Drawer title={selected ? '版本 ' + selected.version + ' · ' + selected.title : '版本详情'} width={680}
      open={selectedVersion !== null} onClose={() => setSelectedVersion(null)}>
      {selected ? <Flex vertical gap="middle">
        <Descriptions bordered size="small" column={1} items={[
          { key: 'version', label: '版本号', children: selected.version },
          { key: 'date', label: '发布日期', children: selected.date || '未记录' },
          { key: 'acceptance', label: '验收状态', children: selected.version === data?.version && acceptance?.state === 'published' ? <Tag color="success">已验收</Tag> : acceptanceTag(selected) },
        ]} />
        {selected.version === data?.version && acceptanceAction()}
        {selected.summary && <Card size="small" title="版本摘要"><Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{selected.summary}</Typography.Paragraph></Card>}
        <Card size="small" title="更新内容"><List size="small" dataSource={selected.changes ?? []} renderItem={change => <List.Item><Typography.Text style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{change}</Typography.Text></List.Item>} /></Card>
        <Card size="small" title="测试与验收">
          {selected.validation_steps?.length ? <ol>{selected.validation_steps.map((step, i) => <li key={i} style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{step}</li>)}</ol> : null}
          <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{selected.validation_note || selected.validation || '暂无验收说明'}</Typography.Paragraph>
          {safeReference(selected.reference_url) && <a href={safeReference(selected.reference_url)} target="_blank" rel="noopener noreferrer">查看 GitHub 记录</a>}
        </Card>
      </Flex> : <Empty description="该版本暂不在当前记录中" />}
    </Drawer>
    <Drawer title="发布详情" width={580} open={deployment !== null} onClose={() => setDeployment(null)}>
      {deployment && <Descriptions bordered column={1} size="small" items={[
        { key: 'at', label: '运行时间', children: new Date(deployment.at).toLocaleString('zh-CN', { hour12: false }) },
        { key: 'result', label: '发布事件', children: <Tag color={deployment.code.endsWith('_ok') ? 'success' : 'warning'}>{labels[deployment.code] ?? deployment.code}</Tag> },
        { key: 'sha', label: '完整提交 SHA', children: <Typography.Text copyable style={{ overflowWrap: 'anywhere' }}>{deployment.sha}</Typography.Text> },
        { key: 'running', label: '是否当前运行', children: data?.running_sha && data.running_sha !== 'unknown' ? (data.running_sha === deployment.sha ? 'SHA 一致' : 'SHA 不一致') : '未确认' },
      ]} />}
    </Drawer>
  </Flex>
}

export function diagnosticCategory(code: string) {
  if (/^feedback_/.test(code)) return 'GitHub / 反馈'
  if (/test|build/.test(code)) return '测试与构建'
  if (/rollback|restart|update_|install_/.test(code)) return '部署与恢复'
  if (/schema|startup|frontend/.test(code)) return '运行环境'
  return '其他'
}

export function diagnosticSeverity(code: string) {
  if (/failed|failure|error|missing|interrupted|mismatch/.test(code)) return 'error'
  if (/blocked|held|locked|rollback$|retry|unacknowledged/.test(code)) return 'warning'
  return /_ok$/.test(code) ? 'success' : 'info'
}

export function SystemLogPage({ data, loading, onRefresh }: RefreshProps & { data: SystemLogs | null }) {
  const [query,setQuery] = useState('')
  const [category,setCategory] = useState('all')
  const [severity,setSeverity] = useState('all')
  const [selected,setSelected] = useState<Operation | null>(null)
  const events = data?.events ?? []
  const filtered = events.filter(row =>
    (category === 'all' || diagnosticCategory(row.code) === category) &&
    (severity === 'all' || diagnosticSeverity(row.code) === severity) &&
    [row.code, labels[row.code], row.sha, row.at, row.detail].some(value => String(value ?? '').toLowerCase().includes(query.trim().toLowerCase()))
  )
  const counts = [
    ['已读取事件', events.length],
    ['失败事件', events.filter(row => diagnosticSeverity(row.code) === 'error').length],
    ['警告事件', events.filter(row => diagnosticSeverity(row.code) === 'warning').length],
    ['成功事件', events.filter(row => diagnosticSeverity(row.code) === 'success').length],
  ] as const
  return <Flex vertical gap="middle">
    <div><Typography.Title level={3} style={{margin:'0 0 4px'}}>运行诊断</Typography.Title><Typography.Text type="secondary">查看真实系统事件、错误原因及关联提交。</Typography.Text></div>
    <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(min(150px,100%),1fr))',gap:12}}>
      {counts.map(([title,count]) => <Card key={title} size="small"><Typography.Text type="secondary">{title}</Typography.Text><Typography.Title level={3} style={{margin:'8px 0 0'}}>{data ? count : '未记录'}</Typography.Title></Card>)}
    </div>
    <Alert showIcon type="info" message="统计范围仅为接口返回的最近系统事件，不代表全量故障、实时服务健康或未解决阻塞数量。" />
    <IntegrationDiagnostics data={data?.integration}/>
    <PreviewAttemptDiagnostics data={data?.preview_attempts}/>
    <Card title="系统事件" extra={<Button onClick={onRefresh} loading={loading}>刷新</Button>}>
      <Space wrap style={{marginBottom:12}}>
        <Input.Search allowClear value={query} onChange={event=>setQuery(event.target.value)} placeholder="搜索事件、原因、SHA" style={{width:'min(260px,100%)'}} />
        <Select value={category} onChange={setCategory} style={{minWidth:140}} options={['all','GitHub / 反馈','测试与构建','部署与恢复','运行环境','其他'].map(value=>({value,label:value==='all'?'全部类别':value}))}/>
        <Select value={severity} onChange={setSeverity} style={{minWidth:120}} options={[{value:'all',label:'全部级别'},{value:'error',label:'失败'},{value:'warning',label:'警告'},{value:'success',label:'成功'},{value:'info',label:'信息'}]}/>
        <Typography.Text type="secondary">{filtered.length} / {events.length} 条</Typography.Text>
      </Space>
      <OperationTable records={filtered} loading={loading} onDetails={setSelected}/>
    </Card>
    <Drawer title="系统事件详情" width={580} open={selected!==null} onClose={()=>setSelected(null)}>
      {selected && <Descriptions bordered size="small" column={1} items={[
        {key:'at',label:'事件时间',children:selected.at},
        {key:'category',label:'分类',children:diagnosticCategory(selected.code)},
        {key:'code',label:'事件代码',children:<Typography.Text code copyable>{selected.code}</Typography.Text>},
        {key:'label',label:'事件说明',children:labels[selected.code]??'未分类事件'},
        {key:'sha',label:'关联 SHA',children:<Typography.Text copyable style={{overflowWrap:'anywhere'}}>{selected.sha || '未记录'}</Typography.Text>},
        {key:'detail',label:'接口返回详情',children:<Typography.Text style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{selected.detail || '未记录'}</Typography.Text>},
      ]}/>}
    </Drawer>
  </Flex>
}
